"""QML-backed quick access toolbar."""

from __future__ import annotations

import sys

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QRect,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QGuiApplication, QRegion
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QWidget

from solin.core.integrations.automation.settings import (
    CameraSettingsStore,
    OBSSettingsStore,
)
from solin.core.integrations.camera_options import CameraOption
from solin.ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from solin.ui.macos_layer import apply_corner_radius
from solin.ui.background_song_status import translate_background_song_status
from solin.styles.theme import PALETTE
from solin.ui.themed_tooltip import hide_themed_tooltip, show_themed_tooltip
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.quick_toolbar import QuickToolbarBridge, SvgIconProvider
from solin.widgets.background_song_popup import BackgroundSongPopup
from solin.widgets.camera_popup import CameraPopup
from solin.widgets.obs_scene_popup import OBSScenePopup
from solin.widgets.zoom_panel import ZoomPanel


# Quick-Access Toolbar (floating, bottom-center)

_QAT_H = 40
_QAT_MARGIN_B = 16
_QAT_ANIM_MS = 250
_QAT_BAR_H = 48
_QAT_MINI_W = 26
_QAT_MINI_H = 30
# The QQuickWidget is fixed-width; QML handles the actual pill width.
_QAT_MAX_W = 240

# Pill / mini corner radii used for the native macOS layer clip (see below).
_QAT_PILL_RADIUS = 20
_QAT_MINI_RADIUS = 8

# macOS composites the QQuickWidget's transparent pixels as opaque black, so on
# macOS the toolbar runs in "solid mode": the widget is sized to its content and
# the pill fills it opaquely (no transparent pixels → no black box). Rounded
# corners are recovered by clipping the native layer. Windows keeps the proven
# fixed-size + translucent + mask path untouched.
_MAC = sys.platform == "darwin"


def _icon_hex(color: str) -> str:
    return color.lstrip("#")


class QuickAccessToolbar(QQuickWidget):
    """
    Floating toolbar centered at the bottom of the content area.

    Rendered in QML for proper GPU-composited alpha transparency — the pill
    always keeps its rounded corners, even over browser/web-engine content
    (no black-corner artefacts).

    Expanded: centered pill with monitor + OBS buttons + minimize chevron.
    Minimized: a small tab pinned to the right edge with a left-arrow to expand.
    """

    monitor_clicked = Signal(object)
    obs_scene_change = Signal(str)
    obs_return_scene_change = Signal(str)
    obs_stream_requested = Signal()
    obs_camera_stream_requested = Signal()
    camera_stream_requested = Signal()
    camera_selection_changed = Signal(object)

    def __init__(
        self,
        obs_service,
        zoom_service,
        camera_service=None,
        parent=None,
        *,
        obs_settings: OBSSettingsStore,
        camera_settings: CameraSettingsStore,
        background_song_service=None,
    ):
        super().__init__(None)
        self._obs = obs_service
        self._zoom = zoom_service
        self._camera = camera_service
        self._obs_settings = obs_settings
        self._camera_settings = camera_settings
        self._background_song = background_song_service
        self._minimized = False
        self._obs_connected = False
        self._screen_count = 0
        self._zoom_connected = False
        self._camera_enabled = False
        self._camera_stream_active = False
        self._qml_pointer_depth = 0

        # ── QQuickWidget setup: transparent, always on top ────────────────
        #
        # CRITICAL: the widget is created at a FIXED size (_QAT_MAX_W × _QAT_H)
        # and is NEVER resized afterwards.  Calling setGeometry() with a
        # different width on Windows forces Qt to rebuild the OpenGL backing
        # surface, which drops the alpha-buffer — the classic "black corners"
        # artefact over web-engine / QML content behind the toolbar.
        #
        # Instead, position changes use move() only, and the visible pill
        # width is driven entirely by QML layout.
        #
        if parent is not None:
            self.setParent(parent)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")
        if _MAC:
            # Solid mode: the widget is resized to its content (no transparent
            # padding). Start at a sane size; _reposition() sets the exact one.
            self.resize(_QAT_MAX_W, _QAT_H)
        else:
            self.setFixedSize(_QAT_MAX_W, _QAT_H)  # fixed size — never changes
        self.installEventFilter(self)

        # ── Bridge (Python ↔ QML) ─────────────────────────────────────────
        self._bridge = QuickToolbarBridge(self)
        self._bridge.monitorClicked.connect(
            lambda: self.monitor_clicked.emit(self._monitor_btn)
        )
        self._bridge.backgroundSongClicked.connect(self._on_background_song_clicked)
        self._bridge.obsClicked.connect(self._on_obs_clicked)
        self._bridge.zoomClicked.connect(self._on_zoom_clicked)
        self._bridge.cameraClicked.connect(self._on_camera_clicked)
        self._bridge.minimizeToggled.connect(self._toggle_minimize)
        self._bridge.pointerEntered.connect(self._begin_qml_pointer_cursor)
        self._bridge.pointerExited.connect(self._end_qml_pointer_cursor)
        self._bridge.tooltipRequested.connect(self._show_native_tooltip)
        self._bridge.tooltipHidden.connect(hide_themed_tooltip)

        if _MAC:
            self._bridge.set_solid_mode(True)

        # ── QML engine: image provider + context ──────────────────────────
        configure_qml_host(
            self,
            type_name="QuickAccessToolbar",
            clear_color=QColor(0, 0, 0, 0),
            image_providers={"icons": SvgIconProvider()},
            context_properties={"bridge": self._bridge},
            mouse_tracking=True,
        )

        # ── Monitor-button proxy (anchor for popup positioning) ───────────
        self._monitor_btn = QWidget(self)
        self._monitor_btn.setFixedSize(30, 30)
        self._monitor_btn.move(6, (_QAT_H - 30) // 2)
        self._monitor_btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._monitor_btn.setStyleSheet("background: transparent; border: none;")

        # Pill proxy — the QQuickWidget itself is the pill, so popups that
        # call show_above(self._pill) will centre above the toolbar.
        self._pill = self

        # ── Background Song Popup ────────────────────────────────────────
        self._background_song_panel = (
            BackgroundSongPopup(background_song_service, self)
            if background_song_service is not None else None
        )
        if self._background_song is not None:
            self._background_song.enabled_changed.connect(
                lambda _enabled: self._sync_background_song_state()
            )
            self._background_song.playback_changed.connect(
                lambda _playing: self._sync_background_song_state()
            )
            self._background_song.current_song_changed.connect(
                lambda _title: self._sync_background_song_state()
            )
            self._background_song.status_changed.connect(
                lambda _status: self._sync_background_song_state()
            )
            QTimer.singleShot(0, self._sync_background_song_state)

        # ── Zoom Panel (native QWidget popup) ─────────────────────────────
        self._zoom_panel = ZoomPanel(self)
        if self._zoom:
            self._zoom_panel.open_audio_requested.connect(
                self._zoom.request_open_audio_for_all
            )

        # ── OBS Scene Popup ───────────────────────────────────────────────
        self._scene_popup = OBSScenePopup(self)
        self._scene_popup.scene_change_requested.connect(self.obs_scene_change)
        self._scene_popup.return_scene_requested.connect(self.obs_return_scene_change)
        self._scene_popup.stream_requested.connect(self.obs_stream_requested)
        self._scene_popup.camera_stream_requested.connect(self.obs_camera_stream_requested)
        self._scene_popup.set_obs_service(obs_service)

        # ── Camera Panel ──────────────────────────────────────────────────
        self._camera_panel = (
            CameraPopup(camera_service, self._camera_settings, self)
            if camera_service else None
        )
        if self._camera_panel:
            self._camera_panel.stream_requested.connect(self.camera_stream_requested)
            self._camera_panel.camera_changed.connect(self.camera_selection_changed)

        # ── Slide animation ───────────────────────────────────────────────
        # Windows animates position only (fixed-size widget). macOS animates
        # the full geometry because the widget is content-sized in solid mode.
        anim_prop = b"geometry" if _MAC else b"pos"
        self._slide_anim = QPropertyAnimation(self, anim_prop)
        self._slide_anim.setDuration(_QAT_ANIM_MS)
        self._slide_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def _begin_qml_pointer_cursor(self):
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self)

    def _end_qml_pointer_cursor(self):
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self)

    def _reset_qml_pointer_cursor(self):
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self)
        hide_themed_tooltip()

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_mac_corners()

    def _apply_mac_corners(self) -> None:
        """macOS: clip the opaque solid-mode pill to rounded corners natively.

        No-op/safe off macOS. If the native layer can't be reached it silently
        falls back to the guaranteed square rectangle. Deferred once so it runs
        after any pending resize has settled the layer bounds.
        """
        if not _MAC:
            return
        radius = _QAT_MINI_RADIUS if self._minimized else _QAT_PILL_RADIUS
        apply_corner_radius(self, radius)
        QTimer.singleShot(0, lambda: apply_corner_radius(self, radius))

    def _show_native_tooltip(
        self,
        text: str,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        if not text:
            hide_themed_tooltip()
            return

        rect = QRect(round(x), round(y), round(width), round(height))
        anchor = self.mapToGlobal(QPoint(rect.center().x(), rect.top()))
        top_left = self.mapToGlobal(QPoint(rect.left(), rect.top()))

        screen = QGuiApplication.screenAt(anchor)
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()

        margin = 8
        tooltip_gap = 25
        cursor_clearance = 16
        available = screen.availableGeometry() if screen else QRect()
        metrics = QFontMetrics(self.font())
        tooltip_w = metrics.horizontalAdvance(text) + 18
        if available.isValid():
            tooltip_w = min(tooltip_w, max(24, available.width() - margin * 2))
        tooltip_h = metrics.lineSpacing() + 12

        pos_x = anchor.x() - tooltip_w // 2
        preferred_above_y = top_left.y() - tooltip_h - tooltip_gap
        preferred_below_y = top_left.y() + rect.height() + tooltip_gap
        pos_y = preferred_above_y

        if available.isValid():
            pos_x = max(
                available.left() + margin,
                min(pos_x, available.right() - tooltip_w - margin),
            )
            if pos_y < available.top() + margin:
                pos_y = preferred_below_y

            cursor_pos = QCursor.pos()

            def clamp_y(candidate: int) -> int:
                return max(
                    available.top() + margin,
                    min(candidate, available.bottom() - tooltip_h - margin),
                )

            def clears_cursor(candidate: int) -> bool:
                tooltip_rect = QRect(pos_x, candidate, tooltip_w, tooltip_h)
                return not tooltip_rect.adjusted(
                    -cursor_clearance,
                    -cursor_clearance,
                    cursor_clearance,
                    cursor_clearance,
                ).contains(cursor_pos)

            candidates = [
                clamp_y(pos_y),
                clamp_y(preferred_above_y),
                clamp_y(preferred_below_y),
                clamp_y(cursor_pos.y() - tooltip_h - cursor_clearance),
                clamp_y(cursor_pos.y() + cursor_clearance),
            ]
            for candidate in candidates:
                if clears_cursor(candidate):
                    pos_y = candidate
                    break

        show_themed_tooltip(QPoint(pos_x, pos_y), text)

    # ── Public API (identical to the old QWidget version) ─────────────────

    def set_screen_count(self, n: int):
        self._screen_count = n
        color = PALETTE.text_secondary if n > 0 else PALETTE.text_muted
        self._bridge.set_monitor_icon_color(_icon_hex(color))

    def set_obs_connected(self, connected: bool):
        self._obs_connected = connected
        self._bridge.set_obs_visible(connected)
        self._bridge.set_obs_dot_visible(connected)
        if connected:
            self._bridge.set_obs_icon_color(_icon_hex(PALETTE.text_muted))
        self._update_separator()
        self._reposition()

    def set_zoom_connected(self, connected: bool):
        self._zoom_connected = connected
        self._zoom_panel.set_connected(connected)
        self._bridge.set_zoom_visible(connected)
        self._bridge.set_zoom_icon_color(_icon_hex(PALETTE.text_muted))
        self._update_separator()
        self._reposition()

    def set_zoom_participants(self, count: int, names: list[str]) -> None:
        self._zoom_panel.set_participants(count, names)

    def set_zoom_sharing(self, sharing: bool) -> None:
        self._zoom_panel.set_sharing(sharing)

    def set_camera_enabled(self, enabled: bool):
        self._camera_enabled = bool(enabled)
        self._bridge.set_camera_visible(self._camera_enabled)
        self._bridge.set_camera_icon_color(
            _icon_hex(PALETTE.text_muted if enabled else PALETTE.text_dim)
        )
        self._update_separator()
        self._reposition()

    def set_camera_stream_active(self, active: bool):
        self._camera_stream_active = bool(active)
        self._bridge.set_camera_icon_color(
            _icon_hex(PALETTE.accent if active else PALETTE.text_muted)
        )
        if self._camera_panel:
            self._camera_panel.set_stream_active(active)
        self._scene_popup.set_camera_stream_active(active)

    def _sync_background_song_state(self):
        service = self._background_song
        visible = bool(service is not None and service.is_enabled)
        self._bridge.set_background_song_visible(visible)
        if not visible:
            self._bridge.set_background_song_icon_color(_icon_hex(PALETTE.text_dim))
            self._bridge.set_background_song_tooltip(self.tr("Background Song"))
            if self._background_song_panel and self._background_song_panel.isVisible():
                self._background_song_panel.close()
        else:
            self._bridge.set_background_song_icon_color(
                _icon_hex(PALETTE.accent if service.is_playing else PALETTE.text_muted)
            )
            status_text = translate_background_song_status(service.status_text)
            tooltip = service.current_title or status_text or self.tr("Background Song")
            self._bridge.set_background_song_tooltip(tooltip)
        self._reposition()

    def set_obs_current_scene(self, scene_name: str):
        if scene_name:
            self._bridge.set_obs_tooltip(f"OBS: {scene_name}")
        if self._scene_popup.isVisible() and self._obs and self._obs.is_connected:
            scenes = self._obs.scenes
            idle_scene = self._obs_settings.default_scene()
            media_scene = self._obs_settings.media_window_scene()
            self._scene_popup.populate(scenes, scene_name or "", idle_scene, media_scene)

    def set_obs_scenes(self, scenes: list[str]):
        """Refresh the scene popup when the scene list changes."""
        if self._scene_popup.isVisible() and self._obs and self._obs.is_connected:
            current = self._obs.current_scene or ""
            idle_scene = self._obs_settings.default_scene()
            media_scene = self._obs_settings.media_window_scene()
            self._scene_popup.populate(scenes, current, idle_scene, media_scene)

    def set_obs_stream_available(self, available: bool):
        self._scene_popup.set_stream_available(available)

    def set_obs_stream_active(self, active: bool):
        self._scene_popup.set_stream_active(active)

    def set_obs_camera_stream_available(self, available: bool):
        self._scene_popup.set_camera_stream_available(available)

    def current_camera_option(self) -> CameraOption | None:
        if self._camera_panel:
            return self._camera_panel.selected_camera()
        return None

    def reposition(self):
        self._reposition()

    def apply_theme(self) -> None:
        hide_themed_tooltip()
        apply_qml_theme(self, clear_color=QColor(0, 0, 0, 0))
        self.set_screen_count(self._screen_count)
        if self._obs_connected:
            self.set_obs_connected(True)
        if self._zoom_connected:
            self.set_zoom_connected(True)
        self.set_camera_enabled(self._camera_enabled)
        if self._camera_stream_active:
            self.set_camera_stream_active(True)
        if self._background_song_panel is not None:
            self._background_song_panel.apply_theme()
        self._zoom_panel.apply_theme()
        self._scene_popup.apply_theme()
        if self._camera_panel is not None:
            self._camera_panel.apply_theme()
        self._sync_background_song_state()
        self._bridge.stateChanged.emit()

    def set_browser_rect_mode(self, enabled: bool):
        # Windows only: when the native webview is visible, transparent QML
        # pixels composite as black (the DWM can't blend them against a foreign
        # native surface). Switching to a flat rectangle hides the artefact.
        #
        # macOS doesn't need this: solid mode uses CALayer.cornerRadius so the
        # compositor clips an opaque surface — no transparent pixels exist,
        # so the corners reveal whatever is behind (including the native webview)
        # cleanly. There is no Windows equivalent with antialiased corners:
        # SetWindowRgn clips are 1-bit (aliased), and DWM rounding only applies
        # to top-level windows. So browserRectMode intentionally stays
        # Windows-only and is the correct solution for that platform.
        self._bridge.set_browser_rect_mode(enabled)

    # ── Internal ──────────────────────────────────────────────────────────

    def _update_separator(self):
        visible = (
            self._bridge._obs_visible
            or self._bridge._zoom_visible
            or self._bridge._camera_visible
        )
        self._bridge.set_separator_visible(visible)

    def _calc_pill_width(self) -> int:
        """Calculate total pill width from visible buttons."""
        items: list[int] = [30]  # monitor btn is always visible
        if self._bridge._background_song_visible:
            items.append(30)
        if self._bridge._separator_visible:
            items.append(1)
        if self._bridge._obs_visible:
            items.append(30)
        if self._bridge._camera_visible:
            items.append(30)
        if self._bridge._zoom_visible:
            items.append(30)
        items.append(22)  # minimize chevron
        # 6 px left margin + 6 px right margin + 3 px spacing between items
        return sum(items) + (len(items) - 1) * 3 + 12 + 4

    def _reposition(self):
        """Reposition the toolbar within its parent.

        IMPORTANT: only the *position* (x, y) changes — the widget size is
        permanently fixed at ``_QAT_MAX_W × _QAT_H`` to avoid recreating the
        OpenGL surface, which would destroy the alpha buffer on Windows.

        A QRegion mask is applied so that mouse events on the transparent
        padding either side of the pill pass through to the widgets behind.
        ``setMask()`` is safe here: it only changes the input/paint clip
        region, NOT the surface dimensions, so the alpha buffer is preserved.
        """
        p = self.parent()
        if not p:
            return

        base_y = p.height() - _QAT_BAR_H - _QAT_MARGIN_B - _QAT_H

        if _MAC:
            self._reposition_solid(p, base_y)
            return

        pill_w = self._calc_pill_width()
        if self._minimized:
            # Position so that only the miniTab (right-aligned in QML) is
            # visible at the parent's right edge.
            x = p.width() - _QAT_MAX_W
            self.move(x, base_y)
            # Mask: only the miniTab area at the right edge is interactive.
            self.setMask(QRegion(
                _QAT_MAX_W - _QAT_MINI_W,
                (_QAT_H - _QAT_MINI_H) // 2,
                _QAT_MINI_W,
                _QAT_MINI_H,
            ))
        else:
            # Centre the *visible pill* within the parent.
            # The pill is centred inside the fixed-width QML root, so we
            # centre the entire QQuickWidget based on _QAT_MAX_W.
            x = (p.width() - _QAT_MAX_W) // 2
            self.move(x, base_y)
            # Mask: the pill is centred in the QQuickWidget; expose only that
            # rectangle so surrounding transparent pixels pass clicks through.
            pill_x = (_QAT_MAX_W - pill_w) // 2
            self.setMask(QRegion(pill_x, 0, pill_w, _QAT_H))
            # Keep the monitor-button popup anchor aligned with the pill.
            self._monitor_btn.move(pill_x + 6, (_QAT_H - 30) // 2)

    def _reposition_solid(self, p, base_y: int) -> None:
        """macOS solid-mode placement.

        The widget is sized to its visible content so there is no transparent
        padding (which would render black). No mask is needed — the widget *is*
        the pill. Rounded corners are clipped on the native layer afterwards.
        """
        self.clearMask()
        if self._minimized:
            w, h = _QAT_MINI_W, _QAT_MINI_H
            x = p.width() - w
            y = base_y + (_QAT_H - _QAT_MINI_H) // 2
        else:
            w, h = self._calc_pill_width(), _QAT_H
            x = (p.width() - w) // 2
            y = base_y
            self._monitor_btn.move(6, (_QAT_H - 30) // 2)
        self.setFixedSize(w, h)
        self.move(x, y)
        self._apply_mac_corners()

    def _toggle_minimize(self):
        hide_themed_tooltip()
        p = self.parent()
        if not p:
            return

        base_y = p.height() - _QAT_BAR_H - _QAT_MARGIN_B - _QAT_H

        if _MAC:
            self._toggle_minimize_solid(p, base_y)
            return

        if self._minimized:
            # ── Expand ────────────────────────────────────────────────────
            self._minimized = False
            self._bridge.set_mini_visible(False)
            self._bridge.set_pill_visible(True)

            target_x = (p.width() - _QAT_MAX_W) // 2

            # Start at the right edge so it slides in from the right.
            start_x = p.width() - _QAT_MAX_W
            self.move(start_x, base_y)
            self.clearMask()  # full widget visible during animation

            self._slide_anim.stop()
            self._slide_anim.setStartValue(QPoint(start_x, base_y))
            self._slide_anim.setEndValue(QPoint(target_x, base_y))
            self._slide_anim.start()
            QTimer.singleShot(_QAT_ANIM_MS + 20, self._reposition)
        else:
            # ── Minimize ──────────────────────────────────────────────────
            self._minimized = True
            target_x = p.width() - _QAT_MAX_W

            self.clearMask()  # full widget visible during animation
            self._slide_anim.stop()
            self._slide_anim.setStartValue(self.pos())
            self._slide_anim.setEndValue(QPoint(target_x, base_y))
            self._slide_anim.finished.connect(self._on_min_done, Qt.ConnectionType.UniqueConnection)
            self._slide_anim.start()

    def _on_min_done(self):
        if self._minimized:
            self._bridge.set_pill_visible(False)
            self._bridge.set_mini_visible(True)
            self._reposition()

    def _toggle_minimize_solid(self, p, base_y: int) -> None:
        """macOS minimize/expand: animate the full geometry (content-sized)."""
        pill_w = self._calc_pill_width()
        pill_geo = QRect((p.width() - pill_w) // 2, base_y, pill_w, _QAT_H)
        mini_geo = QRect(
            p.width() - _QAT_MINI_W,
            base_y + (_QAT_H - _QAT_MINI_H) // 2,
            _QAT_MINI_W,
            _QAT_MINI_H,
        )

        # Unlock the fixed size so the geometry animation can change it.
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_QAT_MAX_W, _QAT_H)
        self.clearMask()
        self._slide_anim.stop()

        if self._minimized:
            # ── Expand ────────────────────────────────────────────────────
            self._minimized = False
            self._bridge.set_mini_visible(False)
            self._bridge.set_pill_visible(True)
            self.setGeometry(mini_geo)
            self._slide_anim.setStartValue(mini_geo)
            self._slide_anim.setEndValue(pill_geo)
            self._slide_anim.start()
            QTimer.singleShot(_QAT_ANIM_MS + 20, self._reposition)
        else:
            # ── Minimize ──────────────────────────────────────────────────
            self._minimized = True
            self._slide_anim.setStartValue(self.geometry())
            self._slide_anim.setEndValue(mini_geo)
            self._slide_anim.finished.connect(
                self._on_min_done, Qt.ConnectionType.UniqueConnection
            )
            self._slide_anim.start()

    def _on_obs_clicked(self):
        hide_themed_tooltip()
        if not self._obs or not self._obs.is_connected:
            return
        scenes = self._obs.scenes
        current = self._obs.current_scene or ""
        idle_scene = self._obs_settings.default_scene()
        media_scene = self._obs_settings.media_window_scene()
        self._scene_popup.populate(scenes, current, idle_scene, media_scene)
        self._scene_popup.show_above(self)
        if not scenes:
            self._obs.request_scenes_refresh()

    def _on_background_song_clicked(self):
        hide_themed_tooltip()
        if not self._background_song_panel or not self._background_song:
            return
        if not self._background_song.is_enabled:
            return
        self._background_song_panel.show_above(self)

    def _on_zoom_clicked(self):
        hide_themed_tooltip()
        if not self._zoom or not self._zoom.is_connected:
            return
        self._zoom_panel.show_above(self)

    def _on_camera_clicked(self):
        hide_themed_tooltip()
        if not self._camera_panel:
            return
        self._camera_panel.show_above(self)

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._bridge.update_translations()
            self._sync_background_song_state()
        super().changeEvent(event)

