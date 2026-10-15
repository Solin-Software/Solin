"""Bounded, Qt-free access to the private FFmpeg media extraction process."""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .info_queue import MediaInfoFailure, MediaInfoFailureKind

PROTOCOL_VERSION = 1
MAX_IMAGE_WIDTH = 640
MAX_IMAGE_HEIGHT = 360
_HEADER_LIMIT = 64 * 1024
_OUTPUT_LIMIT = _HEADER_LIMIT + MAX_IMAGE_WIDTH * MAX_IMAGE_HEIGHT * 4
_DIAGNOSTIC_LIMIT = 64 * 1024
_POLL_INTERVAL = 0.05
_REQUEST_TIMEOUT = 35.0


class ImagePolicy(StrEnum):
    NONE = "none"
    COVER_ONLY = "cover"
    COVER_OR_FRAME = "frame"


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class MediaExtractRequest:
    source: str
    image_policy: ImagePolicy = ImagePolicy.COVER_OR_FRAME
    require_title: bool = True
    require_duration: bool = True
    width: int = MAX_IMAGE_WIDTH
    height: int = MAX_IMAGE_HEIGHT

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source or "\0" in self.source:
            raise ValueError("A media source without NUL characters is required")
        if not isinstance(self.image_policy, ImagePolicy):
            raise ValueError("Invalid media image policy")
        if type(self.require_title) is not bool or type(self.require_duration) is not bool:
            raise ValueError("Media field selectors must be booleans")
        if type(self.width) is not int or not 1 <= self.width <= MAX_IMAGE_WIDTH:
            raise ValueError("Invalid media extraction width")
        if type(self.height) is not int or not 1 <= self.height <= MAX_IMAGE_HEIGHT:
            raise ValueError("Invalid media extraction height")


@dataclass(frozen=True, slots=True)
class MediaExtractImage:
    data: bytes
    width: int
    height: int
    stride: int
    kind: str

    def __post_init__(self) -> None:
        if type(self.width) is not int or not 1 <= self.width <= MAX_IMAGE_WIDTH:
            raise ValueError("Invalid extracted image width")
        if type(self.height) is not int or not 1 <= self.height <= MAX_IMAGE_HEIGHT:
            raise ValueError("Invalid extracted image height")
        if type(self.stride) is not int or self.stride != self.width * 4:
            raise ValueError("The extracted image must contain packed BGRA pixels")
        if not isinstance(self.data, bytes) or len(self.data) != self.stride * self.height:
            raise ValueError("Invalid extracted image payload")
        if self.kind not in {"cover", "frame"}:
            raise ValueError("Invalid extracted image kind")


@dataclass(frozen=True, slots=True)
class MediaExtractResult:
    title: str = ""
    duration_ms: int = 0
    has_video: bool = False
    image: MediaExtractImage | None = None


class MediaExtractError(RuntimeError):
    def __init__(self, failure: MediaInfoFailure) -> None:
        super().__init__(failure.message or failure.code)
        self.failure = failure


class MediaExtractCancelled(MediaExtractError):
    def __init__(self) -> None:
        super().__init__(
            MediaInfoFailure(
                MediaInfoFailureKind.PERMANENT,
                "Media extraction was cancelled",
                "cancelled",
            )
        )


def _error(code: str, message: str, *, transient: bool = False) -> MediaExtractError:
    return MediaExtractError(
        MediaInfoFailure(
            MediaInfoFailureKind.TRANSIENT if transient else MediaInfoFailureKind.PERMANENT,
            message,
            code,
        )
    )


def media_extract_executable() -> Path:
    """Locate the helper beside the installed runtime, never through PATH."""
    specification = importlib.util.find_spec("pylibobs")
    if specification is None or specification.origin is None:
        raise _error("extractor-unavailable", "The bundled media runtime is unavailable")
    operating_system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(
        sys.platform,
    )
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().casefold())
    if operating_system is None or architecture is None:
        raise _error("extractor-unavailable", "Unsupported media extraction platform")
    name = "solin-media-extract.exe" if sys.platform == "win32" else "solin-media-extract"
    path = (
        Path(specification.origin).resolve().parent
        / "_libs"
        / operating_system
        / architecture
        / name
    )
    if not path.is_file():
        raise _error("extractor-unavailable", "The bundled media extraction helper is missing")
    return path


def decode_response(payload: bytes, request: MediaExtractRequest) -> MediaExtractResult:
    """Validate the protocol envelope and owned, bounded pixels before Qt sees them."""
    if len(payload) > _OUTPUT_LIMIT:
        raise _error("extractor-protocol", "Media extraction output exceeded its limit")
    header, separator, pixels = payload.partition(b"\n")
    if not separator or len(header) > _HEADER_LIMIT:
        raise _error("extractor-protocol", "Invalid media extraction header")
    try:
        document = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _error("extractor-protocol", "Invalid media extraction JSON") from error
    if not isinstance(document, dict) or type(document.get("protocol")) is not int:
        raise _error("extractor-protocol", "Invalid media extraction envelope")
    if document["protocol"] != PROTOCOL_VERSION:
        raise _error("extractor-protocol", "Incompatible media extraction protocol")
    if document.get("status") == "error":
        failure = document.get("error")
        if not isinstance(failure, dict) or pixels:
            raise _error("extractor-protocol", "Invalid media extraction error")
        try:
            kind = MediaInfoFailureKind(failure["kind"])
            code, message = failure["code"], failure["message"]
            if not isinstance(code, str) or not isinstance(message, str):
                raise ValueError("Invalid failure fields")
        except (KeyError, TypeError, ValueError) as error:
            raise _error("extractor-protocol", "Invalid media extraction failure") from error
        raise MediaExtractError(MediaInfoFailure(kind, message, code))
    if document.get("status") != "ok":
        raise _error("extractor-protocol", "Unknown media extraction response")
    title, duration, video = (
        document.get("title"),
        document.get("duration_ms"),
        document.get("has_video"),
    )
    if not isinstance(title, str) or type(duration) is not int or not 0 <= duration < 2**63:
        raise _error("extractor-protocol", "Invalid extracted media metadata")
    if type(video) is not bool:
        raise _error("extractor-protocol", "Invalid media video flag")
    if "image" not in document:
        raise _error("extractor-protocol", "Missing extracted image description")
    description = document["image"]
    image = None
    if description is not None:
        if not isinstance(description, dict) or request.image_policy is ImagePolicy.NONE:
            raise _error("extractor-protocol", "Unexpected extracted image")
        try:
            size = description["bytes"]
            if type(size) is not int or size != len(pixels):
                raise ValueError("Invalid image length")
            image = MediaExtractImage(
                pixels,
                description["width"],
                description["height"],
                description["stride"],
                description["kind"],
            )
            if image.width > request.width or image.height > request.height:
                raise ValueError("Image exceeds requested dimensions")
            if request.image_policy is ImagePolicy.COVER_ONLY and image.kind != "cover":
                raise ValueError("A cover-only request received a video frame")
        except (KeyError, TypeError, ValueError) as error:
            raise _error("extractor-protocol", "Invalid extracted image") from error
    elif pixels:
        raise _error("extractor-protocol", "Unexpected trailing media extraction bytes")
    return MediaExtractResult(
        title if request.require_title else "",
        duration if request.require_duration else 0,
        video,
        image,
    )


def _reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass  # Another cancellation or shutdown can win this race.
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass  # The child exited between the timeout and kill.
        process.wait()


def _child_environment() -> dict[str, str]:
    """Keep transitive libraries in the qualified private runtime graph."""
    environment = os.environ.copy()
    for name in (
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LD_AUDIT",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "DYLD_VERSIONED_LIBRARY_PATH",
    ):
        environment.pop(name, None)
    environment["PATH"] = (
        str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32")
        if sys.platform == "win32"
        else ""
    )
    return environment


class MediaExtractService:
    """Own at most two native children, including cancellation and shutdown."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        max_concurrent: int = 2,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        if max_concurrent < 1 or timeout <= 0:
            raise ValueError("Media extraction limits must be positive")
        self._command = tuple(command) if command is not None else None
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._timeout = timeout

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._processes)

    def _check(self, cancel: CancellationToken | None, deadline: float) -> None:
        if self._stop.is_set() or (cancel is not None and cancel.is_set()):
            raise MediaExtractCancelled()
        if time.monotonic() >= deadline:
            raise _error("extractor-timeout", "Media extraction timed out", transient=True)

    def extract(
        self,
        request: MediaExtractRequest,
        *,
        cancel: CancellationToken | None = None,
    ) -> MediaExtractResult:
        deadline = time.monotonic() + self._timeout
        while True:
            self._check(cancel, deadline)
            if self._slots.acquire(timeout=_POLL_INTERVAL):
                break
        process = None
        try:
            self._check(cancel, deadline)
            command = self._command or (str(media_extract_executable()),)
            arguments = [
                *command,
                "--source",
                request.source,
                "--image-policy",
                request.image_policy.value,
                "--width",
                str(request.width),
                "--height",
                str(request.height),
                "--title",
                str(int(request.require_title)),
                "--duration",
                str(int(request.require_duration)),
            ]
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
                with self._lock:
                    self._check(cancel, deadline)
                    try:
                        process = subprocess.Popen(
                            arguments,
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=diagnostics,
                            shell=False,
                            env=_child_environment(),
                            creationflags=(
                                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                            ),
                        )
                    except OSError as error:
                        raise _error(
                            "extractor-unavailable", "Could not start the media extractor"
                        ) from error
                    self._processes.add(process)
                while process.poll() is None:
                    self._check(cancel, deadline)
                    if os.fstat(output.fileno()).st_size > _OUTPUT_LIMIT or (
                        os.fstat(diagnostics.fileno()).st_size > _DIAGNOSTIC_LIMIT
                    ):
                        raise _error(
                            "extractor-protocol", "Media extractor output exceeded its limit"
                        )
                    try:
                        process.wait(timeout=_POLL_INTERVAL)
                    except subprocess.TimeoutExpired:
                        continue
                self._check(cancel, deadline)
                if os.fstat(diagnostics.fileno()).st_size > _DIAGNOSTIC_LIMIT:
                    raise _error(
                        "extractor-protocol", "Media extractor diagnostics exceeded its limit"
                    )
                output.seek(0)
                payload = output.read(_OUTPUT_LIMIT + 1)
                if not payload and process.returncode != 0:
                    raise _error(
                        "extractor-exit", "The media extraction process failed", transient=True
                    )
                result = decode_response(payload, request)
                if process.returncode != 0:
                    raise _error(
                        "extractor-exit", "The media extraction process failed", transient=True
                    )
                return result
        finally:
            if process is not None:
                _reap(process)
                with self._lock:
                    self._processes.discard(process)
            self._slots.release()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            _reap(process)
            with self._lock:
                self._processes.discard(process)


_default_lock = threading.Lock()
_default_service: MediaExtractService | None = None


def get_media_extract_service() -> MediaExtractService:
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = MediaExtractService()
        return _default_service


def shutdown_media_extract_service() -> None:
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = MediaExtractService()
        # Keep shutdown and reopening serialized until the old children have
        # drained; no second application lifetime can exceed the global budget.
        _default_service.close()


def initialize_media_extract_service() -> None:
    """Begin an application lifetime; only bootstrap can reopen a stopped service."""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = MediaExtractService()
        elif _default_service._stop.is_set():
            _default_service.close()
            _default_service = MediaExtractService()


atexit.register(shutdown_media_extract_service)
