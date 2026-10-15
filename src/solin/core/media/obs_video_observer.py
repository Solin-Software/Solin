"""Passive async-frame metadata observation at the public libobs filter boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)
_SOURCE_ID = "solin_video_observer"


class ObsVideoObserverRegistration:
    """Own callbacks until the OBS context shuts down, and one filter per observer.

    The size-delimited obs_source_info prefix is the public ABI registration
    mechanism. Frames remain borrowed: the filter returns the identical pointer
    without copying pixels or consuming the source's selected render frame.
    """

    def __init__(self, ob: Any) -> None:
        import cffi
        from pylibobs._ffi import ffi, get_lib

        self._ob = ob
        self._callbacks: dict[int, Callable[[int, int, int], None]] = {}
        head = self._head = cffi.FFI()
        head.cdef("""
            typedef struct obs_source obs_source_t;
            typedef struct obs_data obs_data_t;
            struct frame_head {
                uint8_t *data[8]; uint32_t linesize[8];
                uint32_t width; uint32_t height; uint64_t timestamp;
            };
            struct source_info_head {
                const char *id; int type; uint32_t output_flags;
                const char *(*get_name)(void *);
                void *(*create)(obs_data_t *, obs_source_t *);
                void (*destroy)(void *);
                void *get_width; void *get_height; void *get_defaults;
                void *get_properties; void *update;
                void *activate; void *deactivate; void *show; void *hide;
                void *video_tick; void *video_render;
                struct frame_head *(*filter_video)(void *, struct frame_head *);
            };
        """)
        identifier = head.new("char[]", _SOURCE_ID.encode())

        @head.callback("const char *(void *)")
        def get_name(_data):
            return identifier

        @head.callback("void *(obs_data_t *, obs_source_t *)")
        def create(_settings, source):
            return head.cast("void *", source)

        @head.callback("void (void *)")
        def destroy(data):
            self._callbacks.pop(int(head.cast("uintptr_t", data)), None)

        @head.callback("struct frame_head *(void *, struct frame_head *)")
        def filter_video(data, frame):
            callback = self._callbacks.get(int(head.cast("uintptr_t", data)))
            if callback is not None and frame != head.NULL:
                try:
                    callback(int(frame.width), int(frame.height), int(frame.timestamp))
                except Exception:  # noqa: BLE001 - never cross a C callback boundary
                    log.exception("Could not observe source video metadata")
            return frame

        info: Any = head.new("struct source_info_head *")
        info.id, info.type, info.output_flags = identifier, 1, 5
        info.get_name, info.create, info.destroy = get_name, create, destroy
        info.filter_video = filter_video
        self._owners = (identifier, info, get_name, create, destroy, filter_video)
        lib: Any = get_lib()
        lib.obs_register_source_s(
            ffi.cast("struct obs_source_info *", int(head.cast("uintptr_t", info))),
            head.sizeof("struct source_info_head"),
        )

    def observe(self, source: Any, callback: Callable[[int, int, int], None]) -> Callable[[], None]:
        from pylibobs._ffi import ffi

        observer = self._ob.Filter.create(_SOURCE_ID, "solin-frame-observer", {})
        key = int(ffi.cast("uintptr_t", observer._ptr))
        self._callbacks[key] = callback
        try:
            source.add_filter(observer)
        except BaseException:  # noqa: BLE001 - release filter before propagating attachment failure
            self._callbacks.pop(key, None)
            observer.release()
            raise
        closed = False

        def close() -> None:
            nonlocal closed
            if closed:
                return
            # Removal drains native filter callbacks; do not hold their locks.
            source.remove_filter(observer)
            self._callbacks.pop(key, None)
            observer.release()
            closed = True

        return close
