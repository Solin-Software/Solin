"""Offscreen ``obs_source_t`` → BGRA readback for the libobs egress paths.

The preview and thumbnail egresses need a scene rendered into CPU memory. Published
``pylibobs`` wheels expose no source-to-CPU helper (only ``render_main_texture_letterboxed``,
which draws the *main mix* into a live ``Display``), so this implements the standard libobs
readback — texrender → stage surface → map — against ``pylibobs._ffi``.

``render_source_to_bgra`` intentionally mirrors the signature the egresses already call, so
if a future ``pylibobs`` ships its own version the call sites can prefer it unchanged.

Rendering happens under ``obs_enter_graphics()``. The texrender is drawn at the requested
output size while ``gs_ortho`` spans the *canvas* coordinate space, so a 1920x1080 scene
scales into whatever cell the caller asked for. Stage surfaces are cached per size because
allocating one per frame would thrash GPU memory at preview frame rates.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# libobs enum values. pylibobs's cdef declares these enums without values, so cffi cannot
# expose them as constants (`lib.GS_CLEAR_COLOR` is absent); the libobs values are inlined.
# gs_color_format: GS_UNKNOWN, GS_A8, GS_R8, GS_RGBA, GS_BGRX, GS_BGRA, ...
_GS_BGRA = 5
# gs_zstencil_format: GS_ZS_NONE is 0.
_GS_ZS_NONE = 0
# gs_clear_flags: GS_CLEAR_COLOR is 1.
_GS_CLEAR_COLOR = 1
# gs_blend_type: GS_BLEND_ZERO, GS_BLEND_ONE, ...
_GS_BLEND_ZERO = 0
_GS_BLEND_ONE = 1

_lock = threading.Lock()
_texrender: Any = None
_stage_surfaces: dict[tuple[int, int], Any] = {}
_casts: dict[str, Any] = {}


def _bind() -> tuple[Any, Any]:
    """Return ``(ffi, lib)`` and prepare casts for the unprototyped entry points.

    pylibobs declares ``gs_texrender_create``, ``gs_stagesurface_create`` and
    ``gs_texrender_get_texture`` as ``void(*)()`` — cffi refuses to call those with
    arguments — so each is cast to its real libobs signature once and cached.
    """
    from pylibobs._ffi import ffi, get_lib

    lib: Any = get_lib()
    if not _casts:
        _casts["texrender_create"] = ffi.cast(
            "struct gs_texrender_s **(*)(int, int)", lib.gs_texrender_create
        )
        _casts["stagesurface_create"] = ffi.cast(
            "struct gs_stagesurf_s **(*)(uint32_t, uint32_t, int)", lib.gs_stagesurface_create
        )
        _casts["texrender_get_texture"] = ffi.cast(
            "gs_texture_t *(*)(struct gs_texrender_s **)", lib.gs_texrender_get_texture
        )
    return ffi, lib


def _stage_surface_for(ffi: Any, width: int, height: int) -> Any:
    surface = _stage_surfaces.get((width, height))
    if surface is None:
        surface = _casts["stagesurface_create"](width, height, _GS_BGRA)
        if not surface:
            raise RuntimeError("gs_stagesurface_create returned NULL")
        _stage_surfaces[(width, height)] = surface
    return surface


def render_source_to_bgra(
    source: Any,
    width: int,
    height: int,
    *,
    canvas_width: int,
    canvas_height: int,
) -> tuple[bytes, int] | None:
    """Render ``source`` into a ``width`` x ``height`` BGRA buffer.

    Returns ``(data, stride)`` where ``stride`` is the mapped row pitch in bytes (which the
    driver may pad beyond ``width * 4``), or ``None`` when the frame could not be rendered.
    """
    pointer = getattr(source, "_ptr", None)
    if pointer is None or width <= 0 or height <= 0:
        return None
    if canvas_width <= 0 or canvas_height <= 0:
        canvas_width, canvas_height = width, height

    global _texrender
    ffi, lib = _bind()

    with _lock:
        lib.obs_enter_graphics()
        try:
            if _texrender is None:
                _texrender = _casts["texrender_create"](_GS_BGRA, _GS_ZS_NONE)
                if not _texrender:
                    raise RuntimeError("gs_texrender_create returned NULL")
            surface = _stage_surface_for(ffi, width, height)

            lib.gs_texrender_reset(_texrender)
            if not lib.gs_texrender_begin(_texrender, width, height):
                return None
            try:
                # struct vec4 is opaque in pylibobs's cdef, so the four floats are
                # allocated directly and reinterpreted — vec4 is exactly float[4].
                clear_color = ffi.cast("struct vec4 *", ffi.new("float[4]"))
                lib.gs_clear(_GS_CLEAR_COLOR, clear_color, 0.0, 0)
                # Span the canvas coordinate space so the scene scales into the target.
                lib.gs_ortho(
                    0.0, float(canvas_width), 0.0, float(canvas_height), -100.0, 100.0
                )
                # Straight copy: the source already carries composited alpha, and letting
                # it blend against the cleared target would darken semi-transparent pixels.
                lib.gs_blend_state_push()
                lib.gs_blend_function(_GS_BLEND_ONE, _GS_BLEND_ZERO)
                try:
                    lib.obs_source_video_render(pointer)
                finally:
                    lib.gs_blend_state_pop()
            finally:
                lib.gs_texrender_end(_texrender)

            texture = _casts["texrender_get_texture"](_texrender)
            if not texture:
                return None
            lib.gs_stage_texture(surface, texture)

            data_pointer = ffi.new("uint8_t **")
            linesize = ffi.new("uint32_t *")
            if not lib.gs_stagesurface_map(surface, data_pointer, linesize):
                return None
            try:
                stride = int(linesize[0])
                if stride <= 0 or data_pointer[0] == ffi.NULL:
                    return None
                data = bytes(ffi.buffer(data_pointer[0], stride * height))
            finally:
                lib.gs_stagesurface_unmap(surface)
            return data, stride
        except Exception:  # noqa: BLE001 - libobs graphics boundary
            log.debug("render_source_to_bgra failed", exc_info=True)
            return None
        finally:
            lib.obs_leave_graphics()


def shutdown() -> None:
    """Release the cached texrender and stage surfaces (call before obs_shutdown)."""
    global _texrender
    if _texrender is None and not _stage_surfaces:
        return
    try:
        _ffi, lib = _bind()
    except Exception:  # noqa: BLE001 - runtime already gone
        _texrender = None
        _stage_surfaces.clear()
        return
    with _lock:
        lib.obs_enter_graphics()
        try:
            for surface in _stage_surfaces.values():
                try:
                    lib.gs_stagesurface_destroy(surface)
                except Exception:  # noqa: BLE001 - teardown must be total
                    log.debug("gs_stagesurface_destroy failed", exc_info=True)
            _stage_surfaces.clear()
            if _texrender is not None:
                try:
                    lib.gs_texrender_destroy(_texrender)
                except Exception:  # noqa: BLE001 - teardown must be total
                    log.debug("gs_texrender_destroy failed", exc_info=True)
                _texrender = None
        finally:
            lib.obs_leave_graphics()


def resolve_render_source_to_bgra():
    """Return the source→BGRA renderer to use.

    A ``pylibobs`` build that ships its own ``render_source_to_bgra`` wins, so this module
    only fills the gap left by the published wheels. Resolved once per egress loop rather
    than per frame.
    """
    import pylibobs

    return getattr(pylibobs, "render_source_to_bgra", render_source_to_bgra)


__all__ = ["render_source_to_bgra", "resolve_render_source_to_bgra", "shutdown"]
