"""Compatibility boundary for pinned pylibobs runtime defects."""

from __future__ import annotations

import platform
from typing import Any

_GRAPHICS_OWNER_BUG_VERSION = "0.1.2"
_VIDEO_INIT_ERRORS = {
    -1: "not supported (check graphics driver/module)",
    -2: "invalid parameters",
    -3: "video already active",
    -4: "graphics module not found",
    -5: "general failure",
}


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
