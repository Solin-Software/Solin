from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
from collections.abc import Buffer
from dataclasses import dataclass
from multiprocessing import shared_memory
from types import TracebackType
from uuid import uuid4

from solin.core.scenes.engine import (
    FrameChannelDescriptor,
    FrameChannelTransport,
    FrameProducerKind,
)
from solin.core.scenes.model import VideoColorRange, VideoColorSpace, VideoPixelFormat


FRAME_CHANNEL_MAGIC = b"SLNFRM01"
FRAME_CHANNEL_VERSION = 3
FRAME_CHANNEL_HEADER_SIZE = 128
FRAME_CHANNEL_SLOT_HEADER_SIZE = 64
FRAME_CHANNEL_SLOT_COUNT = 3
FRAME_CHANNEL_PIXEL_FORMAT_BGRA = 1
FRAME_CHANNEL_PIXEL_FORMAT_NV12 = 2
FRAME_CHANNEL_MUTEX_PREFIX = "Local\\SolinFrameMutex."
MAXIMUM_SHARED_MEMORY_FRAME_BYTES = 3840 * 2160 * 4

_PUBLISHED_SEQUENCE_OFFSET = 56
_SLOT_MARKER_OFFSET = 0
_SLOT_METADATA_OFFSET = 8
_SLOT_PAYLOAD_SIZE_OFFSET = 40
_MAXIMUM_SEQUENCE = (2**63 - 1) // 2


class FrameChannelUnavailableError(RuntimeError):
    """The requested frame transport is unavailable on this platform."""


@dataclass(frozen=True, slots=True)
class VideoFrame:
    sequence: int
    presentation_timestamp_ns: int
    duration_ns: int
    produced_monotonic_ns: int
    width: int
    height: int
    pixel_format: VideoPixelFormat
    pixels: bytes


class SharedMemoryVideoFramePublisher:
    """Own a bounded latest-frame channel for packed BGRA or NV12 video."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        channel_id: str | None = None,
        generation: int = 1,
        color_space: VideoColorSpace = VideoColorSpace.BT709,
        color_range: VideoColorRange = VideoColorRange.LIMITED,
        _dynamic_pixel_format: bool = True,
    ) -> None:
        if sys.platform != "win32":
            raise FrameChannelUnavailableError(
                "Shared-memory BGRA publication is not implemented on this platform"
            )
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValueError("Frame width must be a positive integer")
        if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
            raise ValueError("Frame height must be a positive integer")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("Frame channel generation must be a non-negative integer")
        if not isinstance(color_space, VideoColorSpace):
            raise ValueError("Invalid frame channel color space")
        if not isinstance(color_range, VideoColorRange):
            raise ValueError("Invalid frame channel color range")
        frame_bytes = width * height * 4
        if frame_bytes > MAXIMUM_SHARED_MEMORY_FRAME_BYTES:
            raise ValueError("Shared-memory frame exceeds the native media budget")

        self._width = width
        self._height = height
        self._dynamic_pixel_format = _dynamic_pixel_format
        self._stride = 0 if _dynamic_pixel_format else width * 4
        self._frame_bytes = frame_bytes
        self._slot_size = FRAME_CHANNEL_SLOT_HEADER_SIZE + frame_bytes
        self._mapping_size = (
            FRAME_CHANNEL_HEADER_SIZE + FRAME_CHANNEL_SLOT_COUNT * self._slot_size
        )
        self._memory = shared_memory.SharedMemory(create=True, size=self._mapping_size)
        buffer = self._memory.buf
        if buffer is None:
            self._memory.close()
            self._memory.unlink()
            raise FrameChannelUnavailableError("Shared-memory mapping is unavailable")
        self._buffer: memoryview = buffer
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        process_mutex: _WindowsNamedMutex | None = None
        try:
            process_mutex = _WindowsNamedMutex(
                f"{FRAME_CHANNEL_MUTEX_PREFIX}{self._memory.name}"
            )
            descriptor = FrameChannelDescriptor(
                channel_id=channel_id or f"solin-content-{uuid4().hex}",
                generation=generation,
                producer_kind=FrameProducerKind.SOLIN_OFFSCREEN,
                transport=(
                    FrameChannelTransport.SHARED_MEMORY_VIDEO
                    if _dynamic_pixel_format
                    else FrameChannelTransport.SHARED_MEMORY_BGRA
                ),
                handle_token=self._memory.name,
                width=width,
                height=height,
                pixel_format=(
                    VideoPixelFormat.DYNAMIC
                    if _dynamic_pixel_format
                    else VideoPixelFormat.BGRA
                ),
                color_space=color_space,
                color_range=color_range,
            )
            self._initialize_mapping(generation)
        except BaseException:  # noqa: BLE001 - partially initialized IPC cleanup boundary
            if process_mutex is not None:
                try:
                    process_mutex.close()
                except OSError:
                    pass
            self._buffer.release()
            self._memory.close()
            self._memory.unlink()
            raise
        self._process_mutex = process_mutex
        self._descriptor = descriptor

    @property
    def descriptor(self) -> FrameChannelDescriptor:
        return self._descriptor

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    def publish(
        self,
        pixels: Buffer,
        *,
        frame_width: int | None = None,
        frame_height: int | None = None,
        pixel_format: VideoPixelFormat = VideoPixelFormat.BGRA,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
    ) -> int:
        width = self._width if frame_width is None else frame_width
        height = self._height if frame_height is None else frame_height
        stride = width * (4 if pixel_format is VideoPixelFormat.BGRA else 1)
        return self.publish_planes(
            (pixels,),
            plane_strides=(stride,),
            frame_width=width,
            frame_height=height,
            pixel_format=pixel_format,
            presentation_timestamp_ns=presentation_timestamp_ns,
            duration_ns=duration_ns,
        )

    def publish_planes(
        self,
        planes: tuple[Buffer, ...],
        *,
        plane_strides: tuple[int, ...],
        frame_width: int,
        frame_height: int,
        pixel_format: VideoPixelFormat,
        presentation_timestamp_ns: int = 0,
        duration_ns: int = 0,
    ) -> int:
        if not isinstance(pixel_format, VideoPixelFormat) or pixel_format not in {
            VideoPixelFormat.BGRA,
            VideoPixelFormat.NV12,
        }:
            raise ValueError("Unsupported shared-memory video pixel format")
        if not self._dynamic_pixel_format and pixel_format is not VideoPixelFormat.BGRA:
            raise ValueError("BGRA frame channels do not accept other pixel formats")
        if (
            isinstance(frame_width, bool)
            or not isinstance(frame_width, int)
            or isinstance(frame_height, bool)
            or not isinstance(frame_height, int)
            or not 1 <= frame_width <= self._width
            or not 1 <= frame_height <= self._height
        ):
            raise ValueError("Video frame dimensions exceed the channel capacity")
        if pixel_format is VideoPixelFormat.NV12 and (
            frame_width % 2 != 0 or frame_height % 2 != 0
        ):
            raise ValueError("NV12 frame dimensions must be even")
        row_bytes = frame_width * (4 if pixel_format is VideoPixelFormat.BGRA else 1)
        plane_rows = (
            (frame_height,)
            if pixel_format is VideoPixelFormat.BGRA
            else (frame_height, frame_height // 2)
        )
        if len(planes) != len(plane_rows) or len(plane_strides) != len(plane_rows):
            raise ValueError("Video frame plane layout does not match its pixel format")
        frame_planes = tuple(memoryview(plane) for plane in planes)
        normalized_planes: list[memoryview] = []
        try:
            for plane, stride, rows in zip(
                frame_planes,
                plane_strides,
                plane_rows,
                strict=True,
            ):
                if isinstance(stride, bool) or not isinstance(stride, int) or stride < row_bytes:
                    raise ValueError("Video frame stride is smaller than its visible row")
                if not plane.contiguous:
                    raise ValueError("Video frame planes must be contiguous")
                normalized = plane if plane.format == "B" and plane.ndim == 1 else plane.cast("B")
                required = stride * (rows - 1) + row_bytes if rows else 0
                if normalized.nbytes < required:
                    raise ValueError("Video frame size is smaller than its declared layout")
                normalized_planes.append(normalized)
            payload_size = row_bytes * sum(plane_rows)
            timestamp = _non_negative_u64(
                presentation_timestamp_ns,
                "frame presentation timestamp",
            )
            duration = _non_negative_u64(duration_ns, "frame duration")
            with self._lock:
                if self._closed:
                    raise RuntimeError("Frame channel publisher is closed")
                if self._sequence >= _MAXIMUM_SEQUENCE:
                    raise OverflowError("Frame channel sequence is exhausted")
                with self._process_mutex:
                    sequence = self._sequence + 1
                    slot_index = (sequence - 1) % FRAME_CHANNEL_SLOT_COUNT
                    slot_offset = FRAME_CHANNEL_HEADER_SIZE + slot_index * self._slot_size
                    marker_offset = slot_offset + _SLOT_MARKER_OFFSET
                    struct.pack_into("<Q", self._buffer, marker_offset, sequence * 2 - 1)
                    struct.pack_into(
                        "<QQQQ",
                        self._buffer,
                        slot_offset + _SLOT_METADATA_OFFSET,
                        sequence,
                        timestamp,
                        duration,
                        time.monotonic_ns(),
                    )
                    struct.pack_into(
                        "<QIIII",
                        self._buffer,
                        slot_offset + _SLOT_PAYLOAD_SIZE_OFFSET,
                        payload_size,
                        frame_width,
                        frame_height,
                        row_bytes,
                        (
                            FRAME_CHANNEL_PIXEL_FORMAT_BGRA
                            if pixel_format is VideoPixelFormat.BGRA
                            else FRAME_CHANNEL_PIXEL_FORMAT_NV12
                        ),
                    )
                    payload_offset = slot_offset + FRAME_CHANNEL_SLOT_HEADER_SIZE
                    target = payload_offset
                    for plane, stride, rows in zip(
                        normalized_planes,
                        plane_strides,
                        plane_rows,
                        strict=True,
                    ):
                        plane_size = row_bytes * rows
                        if stride == row_bytes:
                            self._buffer[target : target + plane_size] = plane[:plane_size]
                            target += plane_size
                            continue
                        for row in range(rows):
                            source_offset = row * stride
                            self._buffer[target : target + row_bytes] = plane[
                                source_offset : source_offset + row_bytes
                            ]
                            target += row_bytes
                    struct.pack_into("<Q", self._buffer, marker_offset, sequence * 2)
                    struct.pack_into(
                        "<Q",
                        self._buffer,
                        _PUBLISHED_SEQUENCE_OFFSET,
                        sequence,
                    )
                    self._sequence = sequence
                    return sequence
        finally:
            released: set[int] = set()
            for plane in (*normalized_planes, *frame_planes):
                identity = id(plane)
                if identity in released:
                    continue
                released.add(identity)
                plane.release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            close_error: OSError | None = None
            try:
                self._process_mutex.close()
            except OSError as error:
                close_error = error
            finally:
                self._buffer.release()
                self._memory.close()
                self._memory.unlink()
            if close_error is not None:
                raise close_error

    def __enter__(self) -> SharedMemoryVideoFramePublisher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def _initialize_mapping(self, generation: int) -> None:
        self._buffer[:FRAME_CHANNEL_HEADER_SIZE] = bytes(FRAME_CHANNEL_HEADER_SIZE)
        struct.pack_into(
            "<8sHHIIIIIQQQ",
            self._buffer,
            0,
            FRAME_CHANNEL_MAGIC,
            FRAME_CHANNEL_VERSION,
            FRAME_CHANNEL_HEADER_SIZE,
            FRAME_CHANNEL_SLOT_COUNT,
            self._width,
            self._height,
            self._stride,
            0 if self._dynamic_pixel_format else FRAME_CHANNEL_PIXEL_FORMAT_BGRA,
            generation,
            self._slot_size,
            self._mapping_size,
        )


class SharedMemoryBgraFramePublisher(SharedMemoryVideoFramePublisher):
    """Own a fixed-format BGRA channel used by compositor output tests/tools."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        channel_id: str | None = None,
        generation: int = 1,
        color_space: VideoColorSpace = VideoColorSpace.SRGB,
        color_range: VideoColorRange = VideoColorRange.FULL,
    ) -> None:
        super().__init__(
            width,
            height,
            channel_id=channel_id,
            generation=generation,
            color_space=color_space,
            color_range=color_range,
            _dynamic_pixel_format=False,
        )


class _SharedMemoryFrameSubscriber:
    """Own a bounded latest-frame mapping written by the native compositor."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        channel_id: str | None = None,
        generation: int = 1,
        color_space: VideoColorSpace = VideoColorSpace.SRGB,
        color_range: VideoColorRange = VideoColorRange.FULL,
        _dynamic_pixel_format: bool,
    ) -> None:
        if sys.platform != "win32":
            raise FrameChannelUnavailableError(
                "Shared-memory BGRA subscription is not implemented on this platform"
            )
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValueError("Frame width must be a positive integer")
        if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
            raise ValueError("Frame height must be a positive integer")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("Frame channel generation must be a non-negative integer")
        if not isinstance(color_space, VideoColorSpace):
            raise ValueError("Invalid frame channel color space")
        if not isinstance(color_range, VideoColorRange):
            raise ValueError("Invalid frame channel color range")
        frame_bytes = width * height * 4
        if frame_bytes > MAXIMUM_SHARED_MEMORY_FRAME_BYTES:
            raise ValueError("Shared-memory frame exceeds the native media budget")

        self._width = width
        self._height = height
        self._dynamic_pixel_format = _dynamic_pixel_format
        self._stride = 0 if _dynamic_pixel_format else width * 4
        self._frame_bytes = frame_bytes
        self._slot_size = FRAME_CHANNEL_SLOT_HEADER_SIZE + frame_bytes
        self._mapping_size = (
            FRAME_CHANNEL_HEADER_SIZE + FRAME_CHANNEL_SLOT_COUNT * self._slot_size
        )
        self._memory = shared_memory.SharedMemory(create=True, size=self._mapping_size)
        buffer = self._memory.buf
        if buffer is None:
            self._memory.close()
            self._memory.unlink()
            raise FrameChannelUnavailableError("Shared-memory mapping is unavailable")
        self._buffer: memoryview = buffer
        self._lock = threading.Lock()
        self._last_sequence = 0
        self._closed = False
        process_mutex: _WindowsNamedMutex | None = None
        try:
            process_mutex = _WindowsNamedMutex(
                f"{FRAME_CHANNEL_MUTEX_PREFIX}{self._memory.name}"
            )
            self._descriptor = FrameChannelDescriptor(
                channel_id=channel_id or f"solin-media-output-{uuid4().hex}",
                generation=generation,
                producer_kind=FrameProducerKind.NATIVE_COMPOSITOR,
                transport=(
                    FrameChannelTransport.SHARED_MEMORY_VIDEO
                    if _dynamic_pixel_format
                    else FrameChannelTransport.SHARED_MEMORY_BGRA
                ),
                handle_token=self._memory.name,
                width=width,
                height=height,
                pixel_format=(
                    VideoPixelFormat.DYNAMIC
                    if _dynamic_pixel_format
                    else VideoPixelFormat.BGRA
                ),
                color_space=color_space,
                color_range=color_range,
            )
            self._initialize_mapping(generation)
        except BaseException:  # noqa: BLE001 - partially initialized IPC cleanup boundary
            if process_mutex is not None:
                try:
                    process_mutex.close()
                except OSError:
                    pass
            self._buffer.release()
            self._memory.close()
            self._memory.unlink()
            raise
        self._process_mutex = process_mutex

    @property
    def descriptor(self) -> FrameChannelDescriptor:
        return self._descriptor

    def read_latest(self) -> VideoFrame | None:
        with self._lock:
            if self._closed:
                return None
            with self._process_mutex:
                sequence = struct.unpack_from(
                    "<Q", self._buffer, _PUBLISHED_SEQUENCE_OFFSET
                )[0]
                if sequence == 0 or sequence <= self._last_sequence:
                    return None
                if sequence > _MAXIMUM_SEQUENCE:
                    raise RuntimeError("Frame channel sequence is invalid")
                slot_index = (sequence - 1) % FRAME_CHANNEL_SLOT_COUNT
                slot_offset = (
                    FRAME_CHANNEL_HEADER_SIZE + slot_index * self._slot_size
                )
                marker, frame_sequence, timestamp, duration, produced = struct.unpack_from(
                    "<QQQQQ", self._buffer, slot_offset
                )
                payload_size, width, height, stride, pixel_format = struct.unpack_from(
                    "<QIIII", self._buffer, slot_offset + _SLOT_PAYLOAD_SIZE_OFFSET
                )
                bgra = pixel_format == FRAME_CHANNEL_PIXEL_FORMAT_BGRA
                nv12 = pixel_format == FRAME_CHANNEL_PIXEL_FORMAT_NV12
                row_bytes = width * (4 if bgra else 1)
                expected_payload_size = width * height * (4 if bgra else 3) // (
                    1 if bgra else 2
                )
                if (
                    marker != sequence * 2
                    or frame_sequence != sequence
                    or not 1 <= width <= self._width
                    or not 1 <= height <= self._height
                    or (nv12 and (width % 2 != 0 or height % 2 != 0))
                    or stride != row_bytes
                    or not (bgra or (self._dynamic_pixel_format and nv12))
                    or payload_size != expected_payload_size
                ):
                    return None
                payload_offset = slot_offset + FRAME_CHANNEL_SLOT_HEADER_SIZE
                pixels = bytes(
                    self._buffer[payload_offset : payload_offset + payload_size]
                )
                self._last_sequence = sequence
                return VideoFrame(
                    sequence=sequence,
                    presentation_timestamp_ns=timestamp,
                    duration_ns=duration,
                    produced_monotonic_ns=produced,
                    width=width,
                    height=height,
                    pixel_format=(
                        VideoPixelFormat.BGRA if bgra else VideoPixelFormat.NV12
                    ),
                    pixels=pixels,
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            close_error: OSError | None = None
            try:
                self._process_mutex.close()
            except OSError as error:
                close_error = error
            finally:
                self._buffer.release()
                self._memory.close()
                self._memory.unlink()
            if close_error is not None:
                raise close_error

    def _initialize_mapping(self, generation: int) -> None:
        self._buffer[:FRAME_CHANNEL_HEADER_SIZE] = bytes(FRAME_CHANNEL_HEADER_SIZE)
        struct.pack_into(
            "<8sHHIIIIIQQQ",
            self._buffer,
            0,
            FRAME_CHANNEL_MAGIC,
            FRAME_CHANNEL_VERSION,
            FRAME_CHANNEL_HEADER_SIZE,
            FRAME_CHANNEL_SLOT_COUNT,
            self._width,
            self._height,
            self._stride,
            (
                0
                if self._dynamic_pixel_format
                else FRAME_CHANNEL_PIXEL_FORMAT_BGRA
            ),
            generation,
            self._slot_size,
            self._mapping_size,
        )


class SharedMemoryBgraFrameSubscriber(_SharedMemoryFrameSubscriber):
    """Own a fixed BGRA mapping written by the native scene compositor."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        channel_id: str | None = None,
        generation: int = 1,
        color_space: VideoColorSpace = VideoColorSpace.SRGB,
        color_range: VideoColorRange = VideoColorRange.FULL,
    ) -> None:
        super().__init__(
            width,
            height,
            channel_id=channel_id,
            generation=generation,
            color_space=color_space,
            color_range=color_range,
            _dynamic_pixel_format=False,
        )


class SharedMemoryVideoFrameSubscriber(_SharedMemoryFrameSubscriber):
    """Own a dynamic BGRA/NV12 mapping written by the native compositor."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        channel_id: str | None = None,
        generation: int = 1,
        color_space: VideoColorSpace = VideoColorSpace.BT709,
        color_range: VideoColorRange = VideoColorRange.LIMITED,
    ) -> None:
        super().__init__(
            width,
            height,
            channel_id=channel_id,
            generation=generation,
            color_space=color_space,
            color_range=color_range,
            _dynamic_pixel_format=True,
        )


class _WindowsNamedMutex:
    _WAIT_OBJECT_0 = 0
    _WAIT_ABANDONED = 0x80
    _WAIT_TIMEOUT = 0x102
    _WAIT_BUDGET_MS = 100

    def __init__(self, name: str) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        release = kernel32.ReleaseMutex
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_bool
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool
        handle = create_mutex(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create frame channel mutex")
        self._handle: int | None = handle
        self._wait = wait
        self._release = release
        self._close_handle = close_handle

    def __enter__(self) -> _WindowsNamedMutex:
        handle = self._handle
        if handle is None:
            raise RuntimeError("Frame channel mutex is closed")
        result = self._wait(handle, self._WAIT_BUDGET_MS)
        if result == self._WAIT_TIMEOUT:
            raise TimeoutError("Timed out acquiring frame channel mutex")
        if result not in (self._WAIT_OBJECT_0, self._WAIT_ABANDONED):
            raise OSError(ctypes.get_last_error(), "Could not acquire frame channel mutex")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        handle = self._handle
        if handle is not None and not self._release(handle):
            raise OSError(ctypes.get_last_error(), "Could not release frame channel mutex")

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        if not self._close_handle(handle):
            raise OSError(ctypes.get_last_error(), "Could not close frame channel mutex")


def _non_negative_u64(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**64 - 1:
        raise ValueError(f"Invalid {field_name}")
    return value
