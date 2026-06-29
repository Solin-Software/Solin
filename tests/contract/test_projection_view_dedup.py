"""Structural guard rails for the projection-surface DRY refactor.

These tests do not need a running QApplication — they only inspect the class
hierarchy and method ownership.  Their job is to make sure the shared content
behaviour lives in BaseProjectionView exactly once and is *inherited* (not
re-copied) by both window classes, so the ~400 lines of duplication that used
to exist can never silently creep back in.
"""

from solin.projection.window import (
    BaseProjectionView,
    FloatingPreviewWindow,
    ProjectionWindow,
)

# Every content method must be defined on the base and inherited as-is.
SHARED_METHODS = [
    "set_yearly_text",
    "begin_video",
    "update_frame",
    "show_image_from_url_data",
    "show_image_from_pixmap",
    "show_image_from_qimage",
    "_show_image",
    "show_timer",
    "update_timer",
    "set_timer_blink",
    "_clear_timer_presentation",
    "show_sermon_theme",
    "update_sermon_theme",
    "set_image_transform",
    "reset_image_transform_instant",
    "clear",
    "_on_media_fade_out_done",
    "_switch_to_idle_with_fade",
    "_stop_all_anims",
    "set_idle_active",
    "clear_idle",
    "update_idle_image",
]


def test_both_windows_inherit_base_projection_view():
    assert issubclass(ProjectionWindow, BaseProjectionView)
    assert issubclass(FloatingPreviewWindow, BaseProjectionView)


def test_shared_methods_are_defined_only_on_the_base():
    """No shared content method may be redefined on either subclass — they must
    resolve to the *same* function object that BaseProjectionView declares."""
    for name in SHARED_METHODS:
        base_fn = getattr(BaseProjectionView, name)
        assert getattr(ProjectionWindow, name) is base_fn, (
            f"ProjectionWindow re-defines '{name}' instead of inheriting it"
        )
        assert getattr(FloatingPreviewWindow, name) is base_fn, (
            f"FloatingPreviewWindow re-defines '{name}' instead of inheriting it"
        )


def test_shared_methods_are_not_duplicated_in_class_dicts():
    for name in SHARED_METHODS:
        assert name not in ProjectionWindow.__dict__
        assert name not in FloatingPreviewWindow.__dict__


def test_window_specific_chrome_stays_on_its_own_class():
    # On-monitor window owns fullscreen / monitor lifecycle behaviour.
    for name in ("refit_to_screen", "fade_out_and_close", "_on_screen_geometry_changed"):
        assert name in ProjectionWindow.__dict__
        assert name not in FloatingPreviewWindow.__dict__
        assert not hasattr(BaseProjectionView, name)

    # Floating preview owns resize / drag / zoom-break behaviour.
    for name in ("trigger_zoom_break", "_do_resize", "_install_child_tracking",
                 "resizeEvent", "mousePressEvent", "toggle_fullscreen",
                 "enter_fullscreen", "exit_fullscreen", "keyPressEvent",
                 "mouseDoubleClickEvent"):
        assert name in FloatingPreviewWindow.__dict__
        assert name not in ProjectionWindow.__dict__


def test_page_indices_are_shared_constants():
    # Both windows must agree on the page layout (restore/replay depends on it).
    assert BaseProjectionView._PAGE_MEDIA == 0
    assert BaseProjectionView._PAGE_TIMER == 1
    assert BaseProjectionView._PAGE_YEARLY == 2
    assert BaseProjectionView._PAGE_THEME == 3
    assert BaseProjectionView._PAGE_IDLE_MEDIA == 4
