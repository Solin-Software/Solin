"""Optional Qt-to-native GPU frame transport.

The public Python surface is intentionally backend-neutral. The current native
extension is a pinned Windows/D3D11 implementation; future platform adapters
can implement the same contract without changing content playback policy.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import qVersion
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoSink

from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
    normalize_image_transform,
)
from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.model import VideoColorRange, VideoColorSpace, VideoPixelFormat


log = logging.getLogger(__name__)
QT_MEDIA_BRIDGE_QT_VERSION = "6.11.1"


class QtMediaBridgeUnavailableError(RuntimeError):
    """The accelerated Qt frame adapter is absent or incompatible."""


class QtMediaBridgeSubmitResult(StrEnum):
    ACCEPTED = "accepted"
    DROPPED = "dropped"
    UNAVAILABLE = "unavailable"
    NO_DEMAND = "no_demand"
    SESSION_REJECTED = "session_rejected"


class _NativeBridgeModule(Protocol):
    def create(self, width: int, height: int, generation: int, /) -> object: ...

    def descriptor(self, bridge: object, /) -> dict[str, object]: ...

    def status(self, bridge: object, /) -> dict[str, object]: ...

    def set_route_state(
        self,
        bridge: object,
        session_id: int,
        accepting_frames: bool,
        demanded: bool,
        /,
    ) -> None: ...

    def set_decoder_frame_gate(
        self,
        bridge: object,
        playback_session_id: int,
        accepting_frames: bool,
        /,
    ) -> None: ...

    def set_direct_submission(
        self,
        bridge: object,
        enabled: bool,
        maximum_fps: int,
        /,
    ) -> None: ...

    def stage_media_epoch(
        self,
        bridge: object,
        media_epoch: int,
        /,
    ) -> None: ...

    def set_image_transform(
        self,
        bridge: object,
        enabled: bool,
        animate: bool,
        media_epoch: int,
        canvas_width: int,
        canvas_height: int,
        duration_ms: int,
        zoom: float,
        norm_x: float,
        norm_y: float,
        /,
    ) -> None: ...

    def submit(
        self,
        bridge: object,
        frame: QVideoFrame,
        session_id: int,
        /,
    ) -> str: ...

    def submit_image(
        self,
        bridge: object,
        image: QImage,
        session_id: int,
        /,
    ) -> str: ...

    def bind_video_sink(
        self,
        bridge: object,
        sink: QVideoSink,
        /,
    ) -> None: ...

    def unbind_video_sink(self, bridge: object, /) -> None: ...

    def close(self, bridge: object, /) -> None: ...


class QtMediaBridgePublisher:
    """Own one bounded accelerated frame channel and its native worker."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        generation: int = 1,
        native_module: _NativeBridgeModule | None = None,
    ) -> None:
        module = native_module or load_qt_media_bridge()
        try:
            bridge = module.create(width, height, generation)
            native_descriptor = module.descriptor(bridge)
            descriptor = FrameChannelDescriptor(
                channel_id=_required_text(native_descriptor, "channel_id"),
                generation=_required_int(native_descriptor, "generation"),
                producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
                transport=FrameChannelTransport.D3D11_SHARED_TEXTURE,
                handle_token=_required_text(native_descriptor, "handle_token"),
                width=_required_int(native_descriptor, "width"),
                height=_required_int(native_descriptor, "height"),
                pixel_format=VideoPixelFormat.DYNAMIC,
                color_space=VideoColorSpace.BT709,
                color_range=VideoColorRange.LIMITED,
            )
        except Exception as error:  # noqa: BLE001 - normalize native ABI failures at the adapter boundary
            raise QtMediaBridgeUnavailableError(
                "The native Qt media bridge could not be initialized"
            ) from error
        self._module = module
        self._bridge = bridge
        self._descriptor = descriptor
        self._session_id = 0
        self._enabled = True
        self._closed = False
        self._video_sink: QVideoSink | None = None
        self._module.set_route_state(self._bridge, 0, True, True)
        self._module.set_decoder_frame_gate(self._bridge, 0, False)
        self._module.set_direct_submission(self._bridge, False, 30)

    @property
    def descriptor(self) -> FrameChannelDescriptor:
        return self._descriptor

    def begin_session(self, session_id: int, *, enabled: bool) -> None:
        normalized = _non_negative_u64(session_id, "Qt media bridge session")
        if normalized < self._session_id:
            return
        self._session_id = normalized
        self._enabled = bool(enabled)
        self._module.set_route_state(
            self._bridge,
            normalized,
            self._enabled,
            self._enabled,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.begin_session(self._session_id, enabled=enabled)

    def set_decoder_frame_gate(
        self,
        playback_session_id: int,
        *,
        accepting_frames: bool,
    ) -> None:
        self._module.set_decoder_frame_gate(
            self._bridge,
            _non_negative_u64(
                playback_session_id,
                "Qt media bridge playback session",
            ),
            bool(accepting_frames),
        )

    def set_direct_submission(self, enabled: bool, *, maximum_fps: int) -> None:
        if isinstance(maximum_fps, bool) or not isinstance(maximum_fps, int):
            raise ValueError("Qt media bridge FPS must be an integer")
        if not 1 <= maximum_fps <= 60:
            raise ValueError("Qt media bridge FPS must be between 1 and 60")
        self._module.set_direct_submission(
            self._bridge,
            bool(enabled),
            maximum_fps,
        )

    def stage_media_epoch(self, media_epoch: int) -> None:
        normalized = _non_negative_u64(media_epoch, "Qt media bridge epoch")
        self._module.stage_media_epoch(self._bridge, normalized)

    def set_image_transform(
        self,
        transform: ImageTransform | None,
        *,
        media_epoch: int,
        canvas_width: int,
        canvas_height: int,
        animate: bool,
        duration_ms: int,
    ) -> None:
        normalized = normalize_image_transform(transform or IDENTITY_IMAGE_TRANSFORM)
        self._module.set_image_transform(
            self._bridge,
            transform is not None,
            bool(animate),
            _non_negative_u64(media_epoch, "Qt media bridge transform epoch"),
            canvas_width,
            canvas_height,
            duration_ms,
            normalized.zoom,
            normalized.norm_x,
            normalized.norm_y,
        )

    def submit(
        self,
        frame: QVideoFrame,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult:
        if self._closed:
            return QtMediaBridgeSubmitResult.UNAVAILABLE
        value = self._module.submit(
            self._bridge,
            frame,
            _non_negative_u64(session_id, "Qt media bridge frame session"),
        )
        try:
            return QtMediaBridgeSubmitResult(value)
        except ValueError as error:
            raise RuntimeError("The native Qt media bridge returned an invalid result") from error

    def submit_image(
        self,
        image: QImage,
        *,
        session_id: int,
    ) -> QtMediaBridgeSubmitResult:
        if self._closed:
            return QtMediaBridgeSubmitResult.UNAVAILABLE
        value = self._module.submit_image(
            self._bridge,
            image,
            _non_negative_u64(session_id, "Qt media bridge image session"),
        )
        try:
            return QtMediaBridgeSubmitResult(value)
        except ValueError as error:
            raise RuntimeError("The native Qt media bridge returned an invalid result") from error

    def status(self) -> dict[str, object]:
        return self._module.status(self._bridge)

    def bind_video_sink(self, sink: QVideoSink) -> None:
        if self._closed:
            raise RuntimeError("The Qt media bridge is closed")
        if not isinstance(sink, QVideoSink):
            raise TypeError("Qt media bridge sink must be a QVideoSink")
        if self._video_sink is sink:
            return
        if self._video_sink is not None:
            raise RuntimeError("The Qt media bridge already owns a video sink")
        self._module.bind_video_sink(self._bridge, sink)
        self._video_sink = sink

    def unbind_video_sink(self) -> None:
        if self._video_sink is None:
            return
        try:
            self._module.unbind_video_sink(self._bridge)
        finally:
            self._video_sink = None

    def close(self) -> None:
        if self._closed:
            return
        self.unbind_video_sink()
        self._closed = True
        self._module.close(self._bridge)


def create_qt_media_bridge_publisher(
    width: int,
    height: int,
) -> QtMediaBridgePublisher | None:
    try:
        return QtMediaBridgePublisher(width, height)
    except QtMediaBridgeUnavailableError:
        log.info("Accelerated Qt media ingress is unavailable", exc_info=True)
        return None


def load_qt_media_bridge(
    *,
    application_dir: str | Path | None = None,
    repository_root: str | Path | None = None,
    platform: str | None = None,
) -> _NativeBridgeModule:
    current_platform = platform or sys.platform
    if current_platform != "win32":
        raise QtMediaBridgeUnavailableError("The Qt media bridge currently requires Windows")
    if PYSIDE_VERSION != QT_MEDIA_BRIDGE_QT_VERSION or qVersion() != QT_MEDIA_BRIDGE_QT_VERSION:
        raise QtMediaBridgeUnavailableError(
            "The Qt media bridge requires its pinned PySide and Qt runtime version"
        )
    try:
        return cast(_NativeBridgeModule, importlib.import_module("solin_qt_media_bridge"))
    except ImportError:
        pass

    app_root = Path(application_dir) if application_dir else Path(sys.executable).resolve().parent
    repo_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[4]
    for candidate in _bridge_candidates(app_root, repo_root):
        spec = importlib.util.spec_from_file_location("solin_qt_media_bridge", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError:
            continue
        sys.modules.setdefault("solin_qt_media_bridge", module)
        return cast(_NativeBridgeModule, module)
    raise QtMediaBridgeUnavailableError("The native Qt media bridge was not found")


def _bridge_candidates(app_root: Path, repo_root: Path) -> Sequence[Path]:
    roots = (
        app_root / "native" / "media-engine" / "qt-media-bridge",
        repo_root / "build" / "native" / "qt-media-bridge" / "Release",
        repo_root / "build" / "native" / "qt-media-bridge" / "RelWithDebInfo",
        repo_root / "build" / "native" / "qt-media-bridge" / "Debug",
    )
    return tuple(
        candidate
        for root in roots
        if root.is_dir()
        for candidate in sorted(root.glob("solin_qt_media_bridge*.pyd"))
        if candidate.is_file()
    )


def _required_text(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Native Qt media bridge descriptor has invalid {key}")
    return value


def _required_int(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Native Qt media bridge descriptor has invalid {key}")
    return value


def _non_negative_u64(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**64 - 1:
        raise ValueError(f"{label} must be a non-negative 64-bit integer")
    return value


__all__ = [
    "QT_MEDIA_BRIDGE_QT_VERSION",
    "QtMediaBridgePublisher",
    "QtMediaBridgeSubmitResult",
    "QtMediaBridgeUnavailableError",
    "create_qt_media_bridge_publisher",
    "load_qt_media_bridge",
]
