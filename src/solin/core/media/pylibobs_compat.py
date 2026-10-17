"""Compatibility boundary for pinned pylibobs runtime defects."""

from __future__ import annotations

import platform
import logging
import threading
from collections.abc import Callable
from typing import Any

_GRAPHICS_OWNER_BUG_VERSION = "0.1.2"
_VIDEO_INIT_ERRORS = {
    -1: "not supported (check graphics driver/module)",
    -2: "invalid parameters",
    -3: "video already active",
    -4: "graphics module not found",
    -5: "general failure",
}


class ObsTaskBridgeCompat:
    """Own concrete task callbacks and async handles hidden by opaque typedefs."""

    def __init__(self, dispatch: Callable[[Callable[[], None], bool], None]) -> None:
        from pylibobs._ffi import ffi, get_lib

        self._lib: Any = get_lib()
        self._ffi: Any = ffi
        self._handles: dict[int, Any] = {}
        self._lock = threading.Lock()

        @ffi.callback("void(void (*)(void *), void *, bool)")
        def handler(task: Any, param: Any, wait: bool) -> None:
            dispatch(lambda: task(param), wait)

        self._callback: Any = handler
        self._pointer = ffi.cast("obs_task_handler_t", self._callback)

        @ffi.callback("void(void *)")
        def execute(param: Any) -> None:
            try:
                ffi.from_handle(param)()
            except BaseException:  # noqa: BLE001 - native queue must signal its waiter
                logging.getLogger(__name__).exception("OBS task trampoline failed")
            finally:
                with self._lock:
                    self._handles.pop(int(ffi.cast("uintptr_t", param)), None)

        self._task_callback: Any = execute
        self._task_pointer = ffi.cast("obs_task_t", self._task_callback)

    def install(self) -> None:
        """Register after startup; retain this owner until native shutdown ends."""
        self._lib.obs_set_ui_task_handler(self._pointer)

    def inside(self) -> bool:
        return bool(self._lib.obs_in_task_thread(3))  # OBS_TASK_DESTROY

    def queue(self, callback: Callable[[], None], *, wait: bool) -> None:
        handle = self._ffi.new_handle(callback)
        key = int(self._ffi.cast("uintptr_t", handle))
        with self._lock:
            self._handles[key] = handle
        try:
            self._lib.obs_queue_task(3, self._task_pointer, handle, wait)
        except BaseException:
            with self._lock:
                self._handles.pop(key, None)
            raise


def complete_frame_rate_type_compat(ffi: Any) -> None:
    """Complete pylibobs 0.1.2's opaque OBS media frame-rate declaration.

    OBS returns this two-uint32 structure by value from property range getters.
    The pinned binding declares only its name, making those calls unusable.
    Rechecking its size makes completion idempotent and permits fixed bindings.
    """
    try:
        ffi.sizeof("struct media_frames_per_second")
    except ValueError:
        ffi.cdef("struct media_frames_per_second { uint32_t numerator; uint32_t denominator; };")


def set_video_compat(
    context: Any,
    *,
    binding: Any,
    width: int,
    height: int,
    fps_num: int,
    graphics_module: str | None = None,
) -> Any | None:
    """Initialize libobs video while isolating the pylibobs 0.1.2 CFFI lifetime bug.

    pylibobs 0.1.2 assigns a temporary ``ffi.new("char[]", ...)`` directly to
    ``obs_video_info.graphics_module``. The pointer field does not own that CFFI
    allocation, so the temporary can be released before ``obs_reset_video()``
    dereferences it. The Intel macOS runner exposes this as ``os_dlopen(->.so)``.

    Return the graphics buffer owner for the caller to retain until shutdown:
    OBS copies the pointer into its video configuration without taking ownership.
    Keep an explicit owner alive through the native call on every platform:
    the pointer assignment is shared by all pylibobs 0.1.2 builds. The project
    is pinned to 0.1.2, so the guard deliberately matches that version exactly.
    Any pylibobs upgrade must re-audit this call before changing or removing
    the compatibility boundary.
    """
    if getattr(binding, "__version__", None) != _GRAPHICS_OWNER_BUG_VERSION:
        if graphics_module is None:
            context.set_video(width, height, fps_num=fps_num)
        else:
            context.set_video(
                width,
                height,
                fps_num=fps_num,
                graphics_module=graphics_module,
            )
        return

    from pylibobs._ffi import ffi, get_lib

    graphics_name = graphics_module or (
        "libobs-d3d11" if platform.system() == "Windows" else "libobs-opengl"
    )
    graphics_owner = ffi.new("char[]", graphics_name.encode())
    video: Any = ffi.new("struct obs_video_info *")
    video.graphics_module = graphics_owner
    video.fps_num = fps_num
    video.fps_den = 1
    video.base_width = width
    video.base_height = height
    video.output_width = width
    video.output_height = height
    video.output_format = int(binding.VideoFormat.NV12)
    video.adapter = 0
    video.gpu_conversion = True
    video.colorspace = int(binding.ColorSpace.DEFAULT)
    video.range = int(binding.VideoRange.DEFAULT)
    video.scale_type = int(binding.ScaleType.BICUBIC)

    lib: Any = get_lib()
    error = lib.obs_reset_video(video)
    if error != 0:
        message = _VIDEO_INIT_ERRORS.get(error, f"unknown error {error}")
        raise RuntimeError(f"obs_reset_video failed: {message}")
    return graphics_owner
