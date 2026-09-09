from dataclasses import replace

import pytest

from PySide6.QtCore import QDateTime
from PySide6.QtGui import QImage

from solin.controllers import projection_window_controller as projection_controller
from solin.controllers.projection_window_controller import (
    ProjectionWindowContext,
    ProjectionWindowController,
)
from solin.core.projection.application import ProjectionSession
from solin.core.projection.idle_media import IdleMediaRequest
from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
)
from solin.core.timer.models import MediaCountdownPresentation


class _ProjectionIntegrationsStub:
    def __init__(self):
        self.synced = 0
        self.obs_syncs = []

    def sync_projection_integrations(self):
        self.synced += 1

    def sync_obs_scene(self, active):
        self.obs_syncs.append(active)


class _SettingsWidgetStub:
    def get_yearly_text(self):
        return "Quote", "Reference"

    def _current_api_code(self):
        return "E"


class _CountTargetStub:
    def __init__(self):
        self.counts = []
        self._monitor_btn = "monitor-button"

    def set_screen_count(self, count):
        self.counts.append(count)


class _MonitorPopupStub:
    def __init__(self):
        self.idle_paths = []
        self.populate_args = None
        self.anchor = None
        self.hidden = False

    def _sync_idle_ui(self, path):
        self.idle_paths.append(path)

    def populate(self, screens_info, *, floating_active, idle_media_path):
        self.populate_args = (screens_info, floating_active, idle_media_path)

    def show_above(self, anchor):
        self.anchor = anchor

    def hide_animated(self):
        self.hidden = True


class _ScreenStub:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _StubSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _StubIdleSource:
    """Stand-in for IdleMediaSource that records calls without needing Qt."""

    def __init__(self):
        self.media_set = []
        self.cleared = 0
        self.current_image = None
        self.playing_calls = []
        self.frame_ready = _StubSignal()

    def set_media(self, path):
        self.media_set.append(path)

    def clear(self):
        self.cleared += 1

    def set_playing(self, playing):
        self.playing_calls.append(playing)


class _ProjectionWindowStub:
    def __init__(self, screen=None, index=None):
        self._screen = screen
        self.index = index
        self.idle_active = 0
        self.cleared_idle = 0
        self.cleared = 0
        self.idle_images = []
        self.idle_visible_flag = False
        self.visibility_slot = None
        self.transforms = []
        self.faded = False
        self.video_started = False
        self.images = []
        self.image_initial_transforms = []
        self.timers = []
        self.yearly = []
        self.refit_to = []

    def screen(self):
        return self._screen

    def set_yearly_text(self, quote, reference, api_code=""):
        self.yearly.append((quote, reference, api_code))

    def set_idle_active(self):
        self.idle_active += 1

    def clear_idle(self):
        self.cleared_idle += 1

    def clear(self):
        self.cleared += 1

    def update_idle_image(self, image):
        self.idle_images.append(image)

    def bind_idle_visibility(self, slot):
        self.visibility_slot = slot

    def is_idle_visible(self):
        return self.idle_visible_flag

    def set_image_transform(self, zoom, norm_x, norm_y, *, animate=True):
        self.transforms.append((zoom, norm_x, norm_y, animate))

    def refit_to_screen(self, screen):
        self.refit_to.append(screen)

    def fade_out_and_close(self):
        self.faded = True

    def begin_video(self):
        self.video_started = True

    def show_image_from_url_data(self, data, *, initial_transform=None):
        self.images.append(data)
        self.image_initial_transforms.append(initial_transform)

    def show_timer(self, remaining, total, presentation):
        self.timers.append((remaining, total, presentation))


def _patch_projection_window(monkeypatch):
    """Replace the real ProjectionWindow with a stub factory and return a list
    that records every instance created (in construction order)."""
    created: list[_ProjectionWindowStub] = []

    def _factory(screen, index, _font_manager):
        win = _ProjectionWindowStub(screen, index)
        created.append(win)
        return win

    monkeypatch.setattr(
        "solin.controllers.projection_window_controller.ProjectionWindow",
        _factory,
    )
    return created


def _patch_secondary_screens(monkeypatch, screens):
    monkeypatch.setattr(
        "solin.controllers.projection_window_controller.ScreenManager.secondary_screens",
        staticmethod(lambda: screens),
    )


class _WindowStub:
    def __init__(self):
        self.projection_session = ProjectionSession()
        self._projection_integrations = _ProjectionIntegrationsStub()
        self.settings_widget = _SettingsWidgetStub()
        self.proj_bar = _CountTargetStub()
        self._quick_toolbar = _CountTargetStub()
        self._monitor_popup = _MonitorPopupStub()
        self.font_manager = object()
        self.program_frames = []
        self.program_refreshes = 0

    def tr(self, text):
        return text


def _projection_context(window: _WindowStub) -> ProjectionWindowContext:
    return ProjectionWindowContext(
        session=window.projection_session,
        font_manager=window.font_manager,
        secondary_screens=lambda: (
            projection_controller.ScreenManager.secondary_screens()
        ),
        sync_projection_integrations=(
            window._projection_integrations.sync_projection_integrations
        ),
        sync_obs_scene=window._projection_integrations.sync_obs_scene,
        yearly_text=lambda: (
            *window.settings_widget.get_yearly_text(),
            window.settings_widget._current_api_code(),
        ),
        content_frame_sink=window.program_frames.append,
        refresh_program_content=lambda: setattr(
            window, "program_refreshes", window.program_refreshes + 1
        ),
        set_projection_screen_count=window.proj_bar.set_screen_count,
        set_toolbar_screen_count=window._quick_toolbar.set_screen_count,
        monitor_popup=lambda: window._monitor_popup,
        monitor_anchor=lambda: window._quick_toolbar._monitor_btn,
        dialog_parent=window,
        timer_output=lambda: getattr(window, "timer_output", None),
        timer_bridge=lambda: getattr(window, "timer_bridge", None),
    )


def test_all_windows_includes_floating_preview_when_present():
    window = _WindowStub()
    secondary = _ProjectionWindowStub()
    floating = _ProjectionWindowStub()
    window.projection_session.projection_windows = [secondary]
    window.projection_session.floating_preview_window = floating

    controller = ProjectionWindowController(_projection_context(window))

    assert controller.all_windows() == [secondary, floating]


def test_tree_idle_request_uses_shared_confirmation_before_applying(
    tmp_path,
    monkeypatch,
):
    image = tmp_path / "idle.png"
    thumbnail = tmp_path / "idle-thumb.jpg"
    image.write_bytes(b"image")
    thumbnail_image = QImage(16, 9, QImage.Format.Format_RGB32)
    thumbnail_image.fill(0xFF224466)
    assert thumbnail_image.save(str(thumbnail), "JPEG")
    window = _WindowStub()
    controller = ProjectionWindowController(_projection_context(window))
    confirmations = []
    applied = []

    def confirm(title, *, pixmap, parent):
        confirmations.append((title, pixmap, parent))
        return True

    monkeypatch.setattr(projection_controller, "confirm_set_as_idle", confirm)
    controller.on_idle_media_changed = applied.append

    controller.request_idle_media(
        IdleMediaRequest(
            title="Idle image",
            path=str(image),
            media_type="image",
            thumbnail_path=str(thumbnail),
        )
    )

    assert len(confirmations) == 1
    assert confirmations[0][0] == "Idle image"
    assert confirmations[0][1] is not None
    assert confirmations[0][1].size() == thumbnail_image.size()
    assert confirmations[0][2] is window
    assert applied == [str(image)]


def test_tree_idle_request_rejects_a_source_removed_before_confirmation(
    tmp_path,
    monkeypatch,
):
    confirmations = []
    window = _WindowStub()
    controller = ProjectionWindowController(_projection_context(window))
    monkeypatch.setattr(
        projection_controller,
        "confirm_set_as_idle",
        lambda *_args, **_kwargs: confirmations.append(True),
    )

    controller.request_idle_media(
        IdleMediaRequest(
            title="Missing",
            path=str(tmp_path / "missing.mp4"),
            media_type="video",
        )
    )

    assert confirmations == []


@pytest.mark.parametrize("program_content_enabled", [True, False])
def test_on_idle_media_changed_activates_source_and_windows(tmp_path, program_content_enabled):
    img = tmp_path / "idle.png"
    img.write_bytes(b"fake")
    src = _StubIdleSource()
    window = _WindowStub()
    win = _ProjectionWindowStub()
    window.projection_session.projection_windows = [win]
    context = _projection_context(window)
    if not program_content_enabled:
        context = replace(context, content_frame_sink=None, refresh_program_content=None)
    controller = ProjectionWindowController(
        context,
        idle_source_factory=lambda: src,
    )

    controller.on_idle_media_changed(str(img))
    assert window.projection_session.idle_media_path == str(img)
    assert src.media_set == [str(img)]   # decoded once on the shared source
    assert win.idle_active == 1

    controller.on_idle_media_changed("")
    assert window.projection_session.idle_media_path == ""
    assert src.cleared == 1
    assert win.cleared_idle == 1
    assert window._monitor_popup.idle_paths == [str(img), ""]
    assert window.program_refreshes == (2 if program_content_enabled else 0)


def test_on_idle_media_changed_rejects_missing_file():
    src = _StubIdleSource()
    window = _WindowStub()
    win = _ProjectionWindowStub()
    window.projection_session.projection_windows = [win]
    controller = ProjectionWindowController(
        _projection_context(window),
        idle_source_factory=lambda: src,
    )

    controller.on_idle_media_changed("does-not-exist.png")

    assert window.projection_session.idle_media_path == ""
    assert src.media_set == []
    assert win.cleared_idle == 1


@pytest.mark.parametrize("program_content_enabled", [True, False])
def test_distribute_idle_frame_fans_out_to_all_surfaces(tmp_path, program_content_enabled):
    """The single shared decoder must hand the *same* frame to every surface —
    this is what keeps the monitors frame-locked instead of each decoding its own
    copy and drifting out of sync."""
    img = tmp_path / "idle.png"
    img.write_bytes(b"fake")
    src = _StubIdleSource()
    window = _WindowStub()
    secondary = _ProjectionWindowStub()
    floating = _ProjectionWindowStub()
    window.projection_session.projection_windows = [secondary]
    window.projection_session.floating_preview_window = floating
    context = _projection_context(window)
    if not program_content_enabled:
        context = replace(context, content_frame_sink=None, refresh_program_content=None)
    controller = ProjectionWindowController(
        context,
        idle_source_factory=lambda: src,
    )

    controller.on_idle_media_changed(str(img))   # creates + wires the source
    frame = object()
    src.frame_ready.emit(frame)                  # source decoded one frame

    assert secondary.idle_images == [frame]
    assert floating.idle_images == [frame]
    assert secondary.idle_images[0] is floating.idle_images[0]
    assert window.program_frames == ([frame] if program_content_enabled else [])


def test_apply_full_state_pushes_current_frame_to_new_surface(tmp_path):
    """A surface created while idle playback is active must paint the latest frame
    immediately (no black flash) and then receive subsequent frames."""
    img = tmp_path / "idle.png"
    img.write_bytes(b"fake")
    src = _StubIdleSource()
    window = _WindowStub()
    window.projection_session.set_idle_media_path(str(img))
    controller = ProjectionWindowController(
        _projection_context(window),
        idle_source_factory=lambda: src,
    )
    controller._ensure_idle_source()
    src.current_image = object()

    new_win = _ProjectionWindowStub()
    controller.apply_full_state_to_window(new_win)

    assert new_win.idle_active == 1
    assert new_win.idle_images == [src.current_image]


def test_idle_video_paused_while_hidden_and_resumed_when_visible(tmp_path):
    """A custom idle video must not burn CPU while a clip/image/timer is shown —
    the shared decoder is paused until some surface displays the idle screen."""
    vid = tmp_path / "idle.mp4"
    vid.write_bytes(b"fake")
    src = _StubIdleSource()
    window = _WindowStub()
    win = _ProjectionWindowStub()
    win.idle_visible_flag = False        # a clip is being projected → idle hidden
    window.projection_session.projection_windows = [win]
    controller = ProjectionWindowController(
        _projection_context(window),
        idle_source_factory=lambda: src,
    )

    controller.on_idle_media_changed(str(vid))
    assert src.playing_calls[-1] is False   # stays paused while hidden

    # The projection returns to idle → the page becomes visible.
    win.idle_visible_flag = True
    controller._on_idle_visibility_changed(True)
    assert src.playing_calls[-1] is True

    # Something visual is projected again → idle hidden → paused.
    win.idle_visible_flag = False
    controller._on_idle_visibility_changed(False)
    assert src.playing_calls[-1] is False


def test_native_idle_video_keeps_playing_during_audio_only_playback(tmp_path):
    vid = tmp_path / "idle.mp4"
    vid.write_bytes(b"fake")
    src = _StubIdleSource()
    window = _WindowStub()
    win = _ProjectionWindowStub()
    win.idle_visible_flag = False
    window.projection_session.projection_windows = [win]
    context = replace(
        _projection_context(window),
        program_content_requested=lambda: True,
    )
    controller = ProjectionWindowController(
        context,
        idle_source_factory=lambda: src,
    )
    controller.on_idle_media_changed(str(vid))
    window.projection_session.set_state(
        {"type": "video", "is_audio": True, "title": "Song"}
    )

    controller._sync_idle_playback()

    assert src.playing_calls[-1] is True


# ── image zoom/pan transform persistence ─────────────────────────────────────


def test_image_transform_replayed_to_new_surface_instantly():
    window = _WindowStub()
    window.projection_session.set_state({
        "type": "image",
        "data": b"img",
        "transform": (1.5, 0.2, -0.1),
    })
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    controller.apply_full_state_to_window(win)

    assert win.images == [b"img"]
    # Replayed without animation so the new surface matches the others at once.
    assert win.image_initial_transforms == [ImageTransform(1.5, 0.2, -0.1)]
    assert win.transforms == []


def test_identity_image_transform_is_not_replayed():
    window = _WindowStub()
    window.projection_session.set_state({
        "type": "image",
        "data": b"img",
        "transform": (1.0, 0.0, 0.0),
    })
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    controller.apply_full_state_to_window(win)

    assert win.images == [b"img"]
    assert win.image_initial_transforms == [IDENTITY_IMAGE_TRANSFORM]
    assert win.transforms == []


def test_restore_state_to_window_applies_visual_states():
    window = _WindowStub()
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    window.projection_session.set_state({"type": "video", "is_audio": False})
    controller.restore_state_to_window(win)
    window.projection_session.set_state({"type": "image", "data": b"image"})
    controller.restore_state_to_window(win)
    window.projection_session.set_state({
        "type": "timer",
        "target_dt": QDateTime.currentDateTime().addSecs(60),
        "total": 120,
        "presentation": MediaCountdownPresentation.YEARLY_TEXT.value,
    })
    controller.restore_state_to_window(win)

    assert win.video_started is True
    assert win.images == [b"image"]
    assert win.timers and win.timers[0][1:] == (
        120,
        MediaCountdownPresentation.YEARLY_TEXT,
    )


def test_on_monitor_manager_requested_populates_active_screens(monkeypatch):
    window = _WindowStub()
    screens = [_ScreenStub("A"), _ScreenStub("B")]
    window.projection_session.projection_windows = [_ProjectionWindowStub(screens[1])]
    window.projection_session.floating_preview_window = _ProjectionWindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    controller = ProjectionWindowController(_projection_context(window))
    monkeypatch.setattr(
        "solin.controllers.projection_window_controller.ScreenManager.secondary_screens",
        staticmethod(lambda: screens),
    )

    controller.on_monitor_manager_requested("button")

    screens_info, floating_active, idle_path = window._monitor_popup.populate_args
    assert [item["active"] for item in screens_info] == [False, True]
    assert floating_active is True
    assert idle_path == "idle.png"
    assert window._monitor_popup.anchor == "button"


def test_on_monitor_all_false_deactivates_every_screen(monkeypatch):
    window = _WindowStub()
    screens = [_ScreenStub("A"), _ScreenStub("B")]
    win = _ProjectionWindowStub(screens[0])
    window.projection_session.projection_windows = [win]
    controller = ProjectionWindowController(_projection_context(window))
    monkeypatch.setattr(
        "solin.controllers.projection_window_controller.ScreenManager.secondary_screens",
        staticmethod(lambda: screens),
    )

    controller.on_monitor_all(False)

    assert window.projection_session.media_hidden_screen_names == {"A", "B"}
    assert win.faded is True
    assert window.projection_session.projection_windows == []
    assert window.proj_bar.counts == [0]
    assert window._quick_toolbar.counts == [0]
    assert window._projection_integrations.synced == 1
    assert window._monitor_popup.hidden is True


# ── apply_full_state_to_window — the single source of truth ──────────────────


def test_apply_full_state_applies_yearly_idle_and_projection():
    window = _WindowStub()
    window.projection_session.set_idle_media_path("bg.png")
    window.projection_session.set_state({"type": "image", "data": b"img"})
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    controller.apply_full_state_to_window(win)

    assert win.yearly == [("Quote", "Reference", "E")]
    assert win.idle_active == 1
    assert win.cleared_idle == 0
    assert win.images == [b"img"]


def test_apply_full_state_clears_idle_when_no_path():
    window = _WindowStub()
    window.projection_session.set_idle_media_path("")
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    controller.apply_full_state_to_window(win)

    assert win.idle_active == 0
    assert win.cleared_idle == 1


def test_restore_audio_projection_keeps_visual_surface_on_idle():
    window = _WindowStub()
    window.projection_session.set_state({"type": "video", "is_audio": True})
    controller = ProjectionWindowController(_projection_context(window))
    win = _ProjectionWindowStub()

    controller.restore_state_to_window(win)

    assert win.cleared == 1
    assert not win.video_started


def test_normalize_expired_state_collapses_dead_timer():
    window = _WindowStub()
    window.projection_session.set_state({
        "type": "timer",
        "target_dt": QDateTime.currentDateTime().addSecs(-10),
        "total": 60,
    })
    controller = ProjectionWindowController(_projection_context(window))

    controller._normalize_expired_state()

    assert window.projection_session.state == {"type": "idle"}
    assert window._projection_integrations.obs_syncs == [False]


def test_normalize_expired_state_keeps_live_timer():
    window = _WindowStub()
    state = {
        "type": "timer",
        "target_dt": QDateTime.currentDateTime().addSecs(120),
        "total": 120,
    }
    window.projection_session.set_state(state)
    controller = ProjectionWindowController(_projection_context(window))

    controller._normalize_expired_state()

    assert window.projection_session.state == state
    assert window._projection_integrations.obs_syncs == []


# ── reconcile on hot-plug — the idle-media regression ────────────────────────


def test_reconcile_applies_idle_media_to_newly_connected_monitor(monkeypatch):
    """Regression: plugging a second monitor while a custom idle is active must
    push that idle to the new window (previously reconcile replayed yearly text
    and proj_state but forgot the idle media)."""
    window = _WindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    screen_a, screen_b = _ScreenStub("A"), _ScreenStub("B")
    existing = _ProjectionWindowStub(screen_a)
    window.projection_session.projection_windows = [existing]
    controller = ProjectionWindowController(_projection_context(window))

    created = _patch_projection_window(monkeypatch)
    _patch_secondary_screens(monkeypatch, [screen_a, screen_b])

    controller.reconcile_projection_windows()

    # Existing window kept and refit — never recreated, idle untouched.
    assert existing in window.projection_session.projection_windows
    assert existing.refit_to == [screen_a]
    # Exactly one new window for B, and it received the custom idle.
    assert len(created) == 1
    new_win = created[0]
    assert new_win.screen() is screen_b
    assert new_win.idle_active == 1
    assert new_win in window.projection_session.projection_windows


def test_reconcile_recreates_idle_after_screen_identity_swap(monkeypatch):
    """When the OS reshuffles QScreen identities on hot-plug, the old window may
    no longer match any connected screen: it is closed and fresh windows are
    created for every screen — all of which must get the custom idle."""
    window = _WindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    stale = _ScreenStub("OLD")
    screen_b, screen_c = _ScreenStub("B"), _ScreenStub("C")
    orphan = _ProjectionWindowStub(stale)
    window.projection_session.projection_windows = [orphan]
    controller = ProjectionWindowController(_projection_context(window))

    created = _patch_projection_window(monkeypatch)
    _patch_secondary_screens(monkeypatch, [screen_b, screen_c])

    controller.reconcile_projection_windows()

    assert orphan.faded is True
    assert orphan not in window.projection_session.projection_windows
    assert len(created) == 2
    assert {w.screen() for w in created} == {screen_b, screen_c}
    for w in created:
        assert w.idle_active == 1


def test_reconcile_skips_deactivated_screens(monkeypatch):
    window = _WindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    window.projection_session.hide_media_on_screen_name("B")
    screen_a, screen_b = _ScreenStub("A"), _ScreenStub("B")
    existing = _ProjectionWindowStub(screen_a)
    window.projection_session.projection_windows = [existing]
    controller = ProjectionWindowController(_projection_context(window))

    created = _patch_projection_window(monkeypatch)
    _patch_secondary_screens(monkeypatch, [screen_a, screen_b])

    controller.reconcile_projection_windows()

    assert created == []
    assert window.projection_session.projection_windows == [existing]


def test_on_monitor_toggle_activate_applies_idle(monkeypatch):
    window = _WindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    screen_a = _ScreenStub("A")
    controller = ProjectionWindowController(_projection_context(window))

    created = _patch_projection_window(monkeypatch)
    _patch_secondary_screens(monkeypatch, [screen_a])

    controller.on_monitor_toggle(0, make_active=True)

    assert len(created) == 1
    assert created[0].idle_active == 1
    assert created[0].yearly == [("Quote", "Reference", "E")]


def test_open_projection_windows_applies_idle_to_all(monkeypatch):
    window = _WindowStub()
    window.projection_session.set_idle_media_path("idle.png")
    screen_a, screen_b = _ScreenStub("A"), _ScreenStub("B")
    controller = ProjectionWindowController(_projection_context(window))

    created = _patch_projection_window(monkeypatch)
    _patch_secondary_screens(monkeypatch, [screen_a, screen_b])

    controller.open_projection_windows()

    assert len(created) == 2
    for w in created:
        assert w.idle_active == 1
        assert w.yearly == [("Quote", "Reference", "E")]
