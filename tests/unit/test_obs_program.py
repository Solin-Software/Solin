"""Unit tests for the libobs projection program (ProjectionProgram).

libobs is fully mocked: fake transition/scene/source objects verify the
program's own logic (fade transition on channel 0, crossfade routing, scene
dedup + retire/release, in-place image update, shutdown) with no OBS context.
"""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtGui import QColor, QImage

from solin.core.media.obs_program import ProjectionProgram


class _TransitionMode(IntEnum):
    AUTO = 0
    MANUAL = 1


class _FakeSource:
    def __init__(self, kind: str, name: str, settings: dict) -> None:
        self.kind = kind
        self.name = name
        self.settings = dict(settings)
        self.released = 0
        self.updates: list[dict] = []

    def update(self, settings) -> None:
        self.updates.append(dict(settings))

    def release(self) -> None:
        self.released += 1


class _FakeMediaSource:
    """Stand-in for the engine's ffmpeg source handed to show_media/detach."""

    def __init__(self) -> None:
        self.muted = False
        self.stopped = 0
        self.released = 0

    def media_stop(self) -> None:
        self.stopped += 1

    def release(self) -> None:
        self.released += 1


class _FakeSceneItem:
    def __init__(self, source) -> None:
        self.source = source
        self.bounds_type = None
        self.bounds = None
        self.bounds_alignment = None
        self.alignment = None
        self.scale = None
        self.pos = None
        self.crop = None
        self._defer = 0

    def defer_update_begin(self) -> None:
        self._defer += 1

    def defer_update_end(self) -> None:
        self._defer -= 1


class _FakeScene:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[_FakeSceneItem] = []
        self.released = 0
        self._source = object()

    def add(self, source) -> _FakeSceneItem:
        item = _FakeSceneItem(source)
        self.items.append(item)
        return item

    def as_source(self):
        return self._source

    def release(self) -> None:
        self.released += 1


class _FakeTransition:
    def __init__(self) -> None:
        self.size = None
        self.set_source_calls: list = []
        self.starts: list = []
        self.cleared = 0

    def set_size(self, cx, cy) -> None:
        self.size = (cx, cy)

    def set_source(self, source) -> None:
        self.set_source_calls.append(source)

    def start(self, destination, duration_ms=500, mode=0) -> bool:
        self.starts.append((destination, duration_ms, mode))
        return True

    def clear(self) -> None:
        self.cleared += 1


class _Factory:
    def __init__(self, registry: list) -> None:
        self._registry = registry


class _SourceFactory(_Factory):
    def create(self, kind, name, settings) -> _FakeSource:
        s = _FakeSource(kind, name, settings)
        self._registry.append(s)
        return s


class _SceneFactory(_Factory):
    def create(self, name) -> _FakeScene:
        sc = _FakeScene(name)
        self._registry.append(sc)
        return sc


class _BoundsType:
    NONE = 0
    SCALE_INNER = 2


class _Alignment:
    CENTER = 0


class _Video:
    width = 1920
    height = 1080


class _Ob:
    def __init__(self) -> None:
        self.sources: list = []
        self.scenes: list = []
        self.transitions: list = []
        self.Source = _SourceFactory(self.sources)
        self.Scene = _SceneFactory(self.scenes)
        self.BoundsType = _BoundsType
        self.Alignment = _Alignment
        self.TransitionMode = _TransitionMode

        class _TransitionFactory:
            def __init__(_s, reg):
                _s._reg = reg

            def create(_s, kind, name):
                t = _FakeTransition()
                _s._reg.append(t)
                return t

        self.Transition = _TransitionFactory(self.transitions)


class _Runtime:
    def __init__(self) -> None:
        self.ob = _Ob()
        self.video = _Video()
        self.channels: dict[int, object] = {}
        self.ensured = 0

    def ensure_started(self, **_kwargs) -> None:
        self.ensured += 1

    def set_channel_source(self, channel, source) -> None:
        self.channels[channel] = source


def _img(color: QColor) -> QImage:
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def _program(defer=None):
    runtime = _Runtime()
    return ProjectionProgram(runtime, defer=defer), runtime


def test_ensure_puts_fade_transition_on_channel_0():
    prog, rt = _program()
    prog.ensure()

    assert len(rt.ob.transitions) == 1
    transition = rt.ob.transitions[0]
    assert transition.size == (1920, 1080)
    assert rt.channels[0] is transition  # the transition IS the program output
    assert prog.current_key == "__black__"
    # a black color scene is the initial "A" source
    assert transition.set_source_calls


def test_ensure_is_idempotent():
    prog, rt = _program()
    prog.ensure()
    prog.ensure()
    assert len(rt.ob.transitions) == 1


def test_show_media_crossfades_to_scene_wrapping_source():
    prog, rt = _program()
    prog.ensure()
    source = object()

    prog.show_media(source)

    transition = rt.ob.transitions[0]
    assert prog.current_key == "media"
    # crossfaded to a scene whose item wraps the engine's source, canvas-filled
    scene = rt.ob.scenes[-1]
    assert scene.items[0].source is source
    assert scene.items[0].bounds == (1920.0, 1080.0)
    assert scene.items[0].bounds_type == _BoundsType.SCALE_INNER
    assert transition.starts[-1][0] is scene.as_source()
    assert transition.starts[-1][1] == prog._crossfade_ms


def test_show_image_crossfades_to_image_scene():
    prog, rt = _program()
    prog.ensure()

    prog.show_image("idle", _img(QColor(255, 0, 0)))

    assert prog.current_key == "idle"
    img_sources = [s for s in rt.ob.sources if s.kind == "image_source"]
    assert len(img_sources) == 1
    assert "file" in img_sources[0].settings
    assert rt.ob.transitions[0].starts[-1][0] is rt.ob.scenes[-1].as_source()


def test_reshowing_same_key_releases_previous_only_after_the_crossfade():
    # The outgoing image is the scene the transition dissolves *from*; releasing
    # it inline would obs_source_remove its source mid-fade and cut to black, so
    # it must survive until the crossfade completes (same fix as media).
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    prog.show_image("image", _img(QColor(255, 0, 0)))
    first_scene = rt.ob.scenes[-1]
    first_source = [s for s in rt.ob.sources if s.kind == "image_source"][-1]

    prog.show_image("image", _img(QColor(0, 0, 255)))

    # still alive during the dissolve
    assert first_scene.released == 0
    assert first_source.released == 0
    assert len(deferred) == 1

    deferred[0][1]()  # crossfade completes → release the previous image
    assert first_scene.released == 1
    assert first_source.released == 1


def test_update_image_swaps_texture_in_place_without_crossfade():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("timer", _img(QColor(10, 10, 10)))
    starts_before = len(rt.ob.transitions[0].starts)
    img_source = [s for s in rt.ob.sources if s.kind == "image_source"][-1]

    ok = prog.update_image("timer", _img(QColor(20, 20, 20)))

    assert ok is True
    assert img_source.updates and "file" in img_source.updates[-1]  # texture swapped
    assert len(rt.ob.transitions[0].starts) == starts_before  # no new crossfade


def test_update_image_noop_when_key_not_live():
    prog, rt = _program()
    prog.ensure()
    assert prog.update_image("image", _img(QColor(1, 2, 3))) is False


def test_show_black_returns_to_black():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("idle", _img(QColor(255, 0, 0)))
    assert prog.current_key == "idle"

    prog.show_black()

    assert prog.current_key == "__black__"


def test_shutdown_clears_channel_and_releases_scenes():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("idle", _img(QColor(255, 0, 0)))

    prog.shutdown()

    assert rt.channels[0] is None
    assert prog.active is False
    assert prog.current_key is None


# ── media fade-out on stop/switch ─────────────────────────────────────────────


def test_detach_media_mutes_but_keeps_source_alive_for_the_fade():
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeMediaSource()
    prog.show_media(src)

    prog.detach_media()  # engine stopped playback → fade out, don't cut

    # Audio is silenced at once, but the source is NOT stopped/removed yet — it
    # must keep rendering so the crossfade dissolves from live video, not black.
    assert src.muted is True
    assert src.stopped == 0
    assert src.released == 0
    assert deferred == []  # nothing scheduled until the next crossfade


def test_crossfade_away_from_detached_media_disposes_after_the_fade():
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeMediaSource()
    prog.show_media(src)
    prog.detach_media()

    prog.show_image("idle", _img(QColor(0, 0, 0)))  # clear() → crossfade to idle

    # Disposal is deferred to after the crossfade completes, not run inline.
    assert src.stopped == 0 and src.released == 0
    assert len(deferred) == 1
    delay, run = deferred[0]
    assert delay >= prog._crossfade_ms

    run()  # the fade finished
    assert src.stopped == 1
    assert src.released == 1


def test_media_to_media_crossfades_and_disposes_previous():
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    first = _FakeMediaSource()
    second = _FakeMediaSource()
    prog.show_media(first)
    starts_before = len(rt.ob.transitions[0].starts)

    prog.show_media(second)  # different source under the same "media" key

    # The swap must actually crossfade (the old key-equality guard skipped it).
    assert len(rt.ob.transitions[0].starts) == starts_before + 1
    assert prog.current_key == "media"
    assert first.muted is True
    for _delay, run in deferred:
        run()
    assert first.stopped == 1 and first.released == 1  # previous disposed
    assert second.stopped == 0 and second.released == 0  # current kept


def test_reshowing_the_exact_same_entry_does_not_recrossfade():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("idle", _img(QColor(1, 2, 3)))
    starts_before = len(rt.ob.transitions[0].starts)

    prog._crossfade_to("idle", prog._entries["idle"])  # same entry again

    assert len(rt.ob.transitions[0].starts) == starts_before  # no-op


def test_shutdown_disposes_detached_media_awaiting_fade():
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeMediaSource()
    prog.show_media(src)
    prog.detach_media()  # pending disposal, fade never completed

    prog.shutdown()

    assert src.stopped == 1
    assert src.released == 1


# ── arbitrary capture source (browser window capture) ─────────────────────────


def test_show_source_crossfades_to_scene_owning_source():
    prog, rt = _program()
    prog.ensure()
    src = _FakeSource("xcomposite_input", "solin-browser-99", {})

    prog.show_source("browser", src)

    assert prog.current_key == "browser"
    scene = rt.ob.scenes[-1]
    assert scene.items[0].source is src
    assert rt.ob.transitions[0].starts[-1][0] is scene.as_source()


def test_show_source_unowned_keeps_source_when_scene_disposed():
    # The browser frame source is reused across re-shows, so owned=False: leaving
    # it disposes the wrapping scene but NOT the source (the driver keeps it).
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeSource("solin_frame_source", "cap", {})
    prog.show_source("browser", src, owned=False)
    scene = rt.ob.scenes[-1]

    prog.show_image("idle", _img(QColor(0, 0, 0)))  # crossfade away
    for _delay, run in deferred:
        run()

    assert scene.released == 1  # scene disposed
    assert src.released == 0  # source kept alive for reuse


def test_leaving_capture_scene_releases_it_after_the_fade():
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeSource("xcomposite_input", "cap", {})
    prog.show_source("browser", src)
    scene = rt.ob.scenes[-1]

    prog.show_image("idle", _img(QColor(0, 0, 0)))  # crossfade away from capture

    assert src.released == 0 and scene.released == 0  # kept alive during the fade
    assert "browser" not in prog._entries  # but retired from the live set
    for _delay, run in deferred:
        run()
    assert scene.released == 1 and src.released == 1  # stops capturing after fade


def test_shutdown_removes_temp_image_dir():
    import os

    prog, rt = _program()
    prog.ensure()
    prog.show_image("idle", _img(QColor(1, 2, 3)))  # writes a PNG under a temp dir
    image_dir = prog._image_dir
    assert image_dir is not None and os.path.isdir(image_dir)

    prog.shutdown()

    assert prog._image_dir is None
    assert not os.path.exists(image_dir)  # temp PNGs cleaned up deterministically


def test_leaving_idle_video_releases_its_ffmpeg_source_after_the_fade():
    # The custom idle background video is an owned ffmpeg_source treated as a
    # capture key, so crossfading away releases it → libobs stops decoding while
    # other content shows (re-created, restarting the loop, when idle returns).
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeSource("ffmpeg_source", "solin-idle-vid-1", {"looping": True})
    prog.show_source("idle_video", src, owned=True)
    scene = rt.ob.scenes[-1]

    prog.show_image("idle", _img(QColor(0, 0, 0)))  # crossfade back to yeartext

    assert "idle_video" not in prog._entries  # retired from the live set at once
    for _delay, run in deferred:
        run()
    assert scene.released == 1 and src.released == 1  # decoder stopped after fade


# ── image zoom / pan (sceneitem transform) ────────────────────────────────────


def _image_item(rt):
    scene = rt.ob.scenes[-1]
    return scene.items[0]


def test_zoomable_image_starts_at_identity_fit():
    prog, rt = _program()
    prog.ensure()
    # canvas 1920x1080, image 8x8 → fit = min(1920/8, 1080/8) = 135
    prog.show_image("image", _img(QColor(1, 2, 3)), zoomable=True)

    item = _image_item(rt)
    assert item.bounds_type == _BoundsType.NONE
    assert item.alignment == _Alignment.CENTER
    assert item.scale == (135.0, 135.0)
    assert item.pos == (960.0, 540.0)  # centred
    assert prog._current_entry.image_size == (8, 8)


def test_non_zoomable_image_uses_letterbox_bounds():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("idle", _img(QColor(1, 2, 3)))  # zoomable defaults False

    item = _image_item(rt)
    assert item.bounds_type == _BoundsType.SCALE_INNER
    assert prog._current_entry.item is None  # not transformable


def test_set_image_transform_applies_zoom_and_pan():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("image", _img(QColor(1, 2, 3)), zoomable=True)

    prog.set_image_transform(2.0, 0.1, -0.2)

    item = _image_item(rt)
    assert item.scale == (270.0, 270.0)  # fit(135) * zoom(2)
    # pan is a fraction of the canvas: 960 + 0.1*1920, 540 + (-0.2)*1080
    assert item.pos == (1152.0, 324.0)


def test_set_image_transform_clamps_zoom():
    prog, rt = _program()
    prog.ensure()
    prog.show_image("image", _img(QColor(1, 2, 3)), zoomable=True)

    prog.set_image_transform(99.0, 0.0, 0.0)
    assert _image_item(rt).scale == (1350.0, 135.0 * 10.0)  # clamped to 10×


def test_set_image_transform_noop_on_media():
    prog, rt = _program()
    prog.ensure()
    prog.show_media(_FakeMediaSource())  # media entry has no item/image_size

    prog.set_image_transform(2.0, 0.1, 0.1)  # must be a harmless no-op

    assert prog._current_entry.item is None


def test_shutdown_disposes_current_media_source():
    prog, rt = _program(defer=lambda ms, fn: None)
    prog.ensure()
    src = _FakeMediaSource()
    prog.show_media(src)

    prog.shutdown()

    assert src.released == 1


def test_shutdown_drains_scheduled_but_unfired_disposal():
    # A disposal a crossfade scheduled but whose timer has not fired yet must be
    # drained by shutdown — otherwise it runs against a freed OBS context (crash)
    # or leaks — and the late timer, if it still fires, must be a harmless no-op.
    deferred: list = []
    prog, rt = _program(defer=lambda ms, fn: deferred.append((ms, fn)))
    prog.ensure()
    src = _FakeMediaSource()
    prog.show_media(src)
    prog.detach_media()
    prog.show_image("idle", _img(QColor(0, 0, 0)))  # schedules src disposal
    assert deferred and src.released == 0

    prog.shutdown()
    assert src.stopped == 1 and src.released == 1  # drained by shutdown

    deferred[-1][1]()  # the stale timer fires post-shutdown
    assert src.released == 1  # not disposed twice
