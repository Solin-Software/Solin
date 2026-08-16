"""
Bridge between the QML QuickAccessToolbar and Python services.

Exposes reactive state as Qt Properties and relays user actions as Signals.
Defines the toolbar bridge and its icon mapping for the shared SVG provider.
"""

from PySide6.QtCore import QObject, Property, Signal, Slot

from solin.styles.icons import (
    ICON_CAMERA,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_LEFT,
    ICON_MONITOR,
    ICON_MUSIC,
    ICON_OBS,
    ICON_CLAPPERBOARD,
    ICON_REMOTE_CONTROL,
    ICON_ZOOM,
)

# ── Icon mapping ──────────────────────────────────────────────────────────────

QUICK_TOOLBAR_ICON_SVGS: dict[str, object] = {
    "monitor": ICON_MONITOR,
    "background_song": ICON_MUSIC,
    "obs": ICON_OBS,
    "scenes": ICON_CLAPPERBOARD,
    "zoom": ICON_ZOOM,
    "camera": ICON_CAMERA,
    "remote_control": ICON_REMOTE_CONTROL,
    "chevron_down": ICON_CHEVRON_DOWN,
    "chevron_left": ICON_CHEVRON_LEFT,
}


# ── Bridge QObject ────────────────────────────────────────────────────────────


class QuickToolbarBridge(QObject):
    """Reactive bridge between the QML scene and the Python toolbar wrapper."""

    # ── Notify signals (property changes) ─────────────────────────────────
    stateChanged = Signal()
    tooltipsChanged = Signal()

    # ── Action signals (QML → Python) ─────────────────────────────────────
    monitorClicked = Signal()
    backgroundSongClicked = Signal()
    obsClicked = Signal()
    scenesClicked = Signal()
    zoomClicked = Signal()
    cameraClicked = Signal()
    remoteControlClicked = Signal()
    minimizeToggled = Signal()
    pointerEntered = Signal()
    pointerExited = Signal()
    tooltipRequested = Signal(str, float, float, float, float)
    tooltipHidden = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # Visual state
        self._monitor_icon_color: str = "8b949e"
        self._background_song_visible: bool = False
        self._background_song_icon_color: str = "484f58"
        self._obs_visible: bool = False
        self._obs_icon_color: str = "484f58"
        self._obs_dot_visible: bool = False
        self._scenes_visible: bool = False
        self._scenes_icon_color: str = "484f58"
        self._zoom_visible: bool = False
        self._zoom_icon_color: str = "484f58"
        self._camera_visible: bool = False
        self._camera_icon_color: str = "484f58"
        self._remote_control_visible: bool = False
        self._remote_control_icon_color: str = "484f58"
        self._remote_control_badge: str = ""
        self._remote_control_warning: bool = False
        self._separator_visible: bool = False
        self._pill_visible: bool = True
        self._mini_visible: bool = False
        self._browser_rect_mode: bool = False
        self._solid_mode: bool = False

        # Tooltips
        self._monitor_tooltip: str = ""
        self._background_song_tooltip: str = ""
        self._obs_tooltip: str = ""
        self._scenes_tooltip: str = ""
        self._zoom_tooltip: str = ""
        self._camera_tooltip: str = ""
        self._remote_control_tooltip: str = ""
        self._minimize_tooltip: str = ""
        self._expand_tooltip: str = ""
        self.update_translations()

    # ── QML Properties ────────────────────────────────────────────────────

    # -- Icon colours (hex without '#') --
    @Property(str, notify=stateChanged)
    def monitorIconColor(self) -> str:  # noqa: N802
        return self._monitor_icon_color

    @Property(str, notify=stateChanged)
    def backgroundSongIconColor(self) -> str:  # noqa: N802
        return self._background_song_icon_color

    @Property(str, notify=stateChanged)
    def obsIconColor(self) -> str:  # noqa: N802
        return self._obs_icon_color

    @Property(str, notify=stateChanged)
    def scenesIconColor(self) -> str:  # noqa: N802
        return self._scenes_icon_color

    @Property(str, notify=stateChanged)
    def zoomIconColor(self) -> str:  # noqa: N802
        return self._zoom_icon_color

    @Property(str, notify=stateChanged)
    def cameraIconColor(self) -> str:  # noqa: N802
        return self._camera_icon_color

    @Property(str, notify=stateChanged)
    def remoteControlIconColor(self) -> str:  # noqa: N802
        return self._remote_control_icon_color

    @Property(str, notify=stateChanged)
    def remoteControlBadge(self) -> str:  # noqa: N802
        return self._remote_control_badge

    # -- Visibility --
    @Property(bool, notify=stateChanged)
    def obsVisible(self) -> bool:  # noqa: N802
        return self._obs_visible

    @Property(bool, notify=stateChanged)
    def scenesVisible(self) -> bool:  # noqa: N802
        return self._scenes_visible

    @Property(bool, notify=stateChanged)
    def backgroundSongVisible(self) -> bool:  # noqa: N802
        return self._background_song_visible

    @Property(bool, notify=stateChanged)
    def obsDotVisible(self) -> bool:  # noqa: N802
        return self._obs_dot_visible

    @Property(bool, notify=stateChanged)
    def zoomVisible(self) -> bool:  # noqa: N802
        return self._zoom_visible

    @Property(bool, notify=stateChanged)
    def cameraVisible(self) -> bool:  # noqa: N802
        return self._camera_visible

    @Property(bool, notify=stateChanged)
    def remoteControlVisible(self) -> bool:  # noqa: N802
        return self._remote_control_visible

    @Property(bool, notify=stateChanged)
    def remoteControlWarning(self) -> bool:  # noqa: N802
        return self._remote_control_warning

    @Property(bool, notify=stateChanged)
    def separatorVisible(self) -> bool:  # noqa: N802
        return self._separator_visible

    @Property(bool, notify=stateChanged)
    def pillVisible(self) -> bool:  # noqa: N802
        return self._pill_visible

    @Property(bool, notify=stateChanged)
    def miniVisible(self) -> bool:  # noqa: N802
        return self._mini_visible

    @Property(bool, notify=stateChanged)
    def browserRectMode(self) -> bool:  # noqa: N802
        return self._browser_rect_mode

    @Property(bool, notify=stateChanged)
    def solidMode(self) -> bool:  # noqa: N802
        # macOS: the QQuickWidget composites transparent pixels as opaque black.
        # In solid mode the pill fills the (content-sized) widget opaquely so no
        # transparent pixels exist; rounded corners come from the native layer.
        return self._solid_mode

    # -- Tooltips --
    @Property(str, notify=tooltipsChanged)
    def monitorTooltip(self) -> str:  # noqa: N802
        return self._monitor_tooltip

    @Property(str, notify=tooltipsChanged)
    def backgroundSongTooltip(self) -> str:  # noqa: N802
        return self._background_song_tooltip

    @Property(str, notify=tooltipsChanged)
    def obsTooltip(self) -> str:  # noqa: N802
        return self._obs_tooltip

    @Property(str, notify=tooltipsChanged)
    def scenesTooltip(self) -> str:  # noqa: N802
        return self._scenes_tooltip

    @Property(str, notify=tooltipsChanged)
    def zoomTooltip(self) -> str:  # noqa: N802
        return self._zoom_tooltip

    @Property(str, notify=tooltipsChanged)
    def cameraTooltip(self) -> str:  # noqa: N802
        return self._camera_tooltip

    @Property(str, notify=tooltipsChanged)
    def remoteControlTooltip(self) -> str:  # noqa: N802
        return self._remote_control_tooltip

    @Property(str, notify=tooltipsChanged)
    def minimizeTooltip(self) -> str:  # noqa: N802
        return self._minimize_tooltip

    @Property(str, notify=tooltipsChanged)
    def expandTooltip(self) -> str:  # noqa: N802
        return self._expand_tooltip

    # ── QML Slots (user actions) ──────────────────────────────────────────

    @Slot()
    def onMonitorClicked(self) -> None:  # noqa: N802
        self.monitorClicked.emit()

    @Slot()
    def onBackgroundSongClicked(self) -> None:  # noqa: N802
        self.backgroundSongClicked.emit()

    @Slot()
    def onObsClicked(self) -> None:  # noqa: N802
        self.obsClicked.emit()

    @Slot()
    def onScenesClicked(self) -> None:  # noqa: N802
        self.scenesClicked.emit()

    @Slot()
    def onZoomClicked(self) -> None:  # noqa: N802
        self.zoomClicked.emit()

    @Slot()
    def onCameraClicked(self) -> None:  # noqa: N802
        self.cameraClicked.emit()

    @Slot()
    def onRemoteControlClicked(self) -> None:  # noqa: N802
        self.remoteControlClicked.emit()

    @Slot()
    def onMinimizeClicked(self) -> None:  # noqa: N802
        self.minimizeToggled.emit()

    @Slot()
    def onExpandClicked(self) -> None:  # noqa: N802
        self.minimizeToggled.emit()

    @Slot()
    def onPointerEntered(self) -> None:  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def onPointerExited(self) -> None:  # noqa: N802
        self.pointerExited.emit()

    @Slot(str, float, float, float, float)
    def showTooltip(
        self,
        text: str,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:  # noqa: N802
        self.tooltipRequested.emit(text, x, y, width, height)

    @Slot()
    def hideTooltip(self) -> None:  # noqa: N802
        self.tooltipHidden.emit()

    # ── Python setters (Python → QML) ────────────────────────────────────

    def set_monitor_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._monitor_icon_color != color:
            self._monitor_icon_color = color
            self.stateChanged.emit()

    def set_background_song_visible(self, visible: bool) -> None:
        if self._background_song_visible != visible:
            self._background_song_visible = visible
            self.stateChanged.emit()

    def set_background_song_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._background_song_icon_color != color:
            self._background_song_icon_color = color
            self.stateChanged.emit()

    def set_obs_visible(self, visible: bool) -> None:
        if self._obs_visible != visible:
            self._obs_visible = visible
            self.stateChanged.emit()

    def set_obs_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._obs_icon_color != color:
            self._obs_icon_color = color
            self.stateChanged.emit()

    def set_obs_dot_visible(self, visible: bool) -> None:
        if self._obs_dot_visible != visible:
            self._obs_dot_visible = visible
            self.stateChanged.emit()

    def set_scenes_visible(self, visible: bool) -> None:
        if self._scenes_visible != visible:
            self._scenes_visible = visible
            self.stateChanged.emit()

    def set_scenes_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._scenes_icon_color != color:
            self._scenes_icon_color = color
            self.stateChanged.emit()

    def set_zoom_visible(self, visible: bool) -> None:
        if self._zoom_visible != visible:
            self._zoom_visible = visible
            self.stateChanged.emit()

    def set_zoom_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._zoom_icon_color != color:
            self._zoom_icon_color = color
            self.stateChanged.emit()

    def set_camera_visible(self, visible: bool) -> None:
        if self._camera_visible != visible:
            self._camera_visible = visible
            self.stateChanged.emit()

    def set_camera_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._camera_icon_color != color:
            self._camera_icon_color = color
            self.stateChanged.emit()

    def set_remote_control_visible(self, visible: bool) -> None:
        if self._remote_control_visible != visible:
            self._remote_control_visible = visible
            self.stateChanged.emit()

    def set_remote_control_icon_color(self, color: str) -> None:
        color = color.lstrip("#")
        if self._remote_control_icon_color != color:
            self._remote_control_icon_color = color
            self.stateChanged.emit()

    def set_remote_control_badge(self, badge: str) -> None:
        badge = str(badge or "")[:2]
        if self._remote_control_badge != badge:
            self._remote_control_badge = badge
            self.stateChanged.emit()

    def set_remote_control_warning(self, warning: bool) -> None:
        if self._remote_control_warning != warning:
            self._remote_control_warning = warning
            self.stateChanged.emit()

    def set_separator_visible(self, visible: bool) -> None:
        if self._separator_visible != visible:
            self._separator_visible = visible
            self.stateChanged.emit()

    def set_pill_visible(self, visible: bool) -> None:
        if self._pill_visible != visible:
            self._pill_visible = visible
            self.stateChanged.emit()

    def set_mini_visible(self, visible: bool) -> None:
        if self._mini_visible != visible:
            self._mini_visible = visible
            self.stateChanged.emit()

    def set_browser_rect_mode(self, enabled: bool) -> None:
        if self._browser_rect_mode != enabled:
            self._browser_rect_mode = enabled
            self.stateChanged.emit()

    def set_solid_mode(self, enabled: bool) -> None:
        if self._solid_mode != enabled:
            self._solid_mode = enabled
            self.stateChanged.emit()

    def set_obs_tooltip(self, text: str) -> None:
        if self._obs_tooltip != text:
            self._obs_tooltip = text
            self.tooltipsChanged.emit()

    def set_scenes_tooltip(self, text: str) -> None:
        if self._scenes_tooltip != text:
            self._scenes_tooltip = text
            self.tooltipsChanged.emit()

    def set_background_song_tooltip(self, text: str) -> None:
        if self._background_song_tooltip != text:
            self._background_song_tooltip = text
            self.tooltipsChanged.emit()

    def set_remote_control_tooltip(self, text: str) -> None:
        if self._remote_control_tooltip != text:
            self._remote_control_tooltip = text
            self.tooltipsChanged.emit()

    # ── i18n ──────────────────────────────────────────────────────────────

    def update_translations(self) -> None:
        self._monitor_tooltip = self.tr("Manage monitors")
        self._background_song_tooltip = self.tr("Background Song")
        self._obs_tooltip = self.tr("OBS Scenes")
        self._scenes_tooltip = self.tr("Solin scenes")
        self._zoom_tooltip = self.tr("Zoom Settings")
        self._camera_tooltip = self.tr("Camera")
        self._remote_control_tooltip = self.tr("Remote control")
        self._minimize_tooltip = self.tr("Minimize")
        self._expand_tooltip = self.tr("Expand toolbar")
        self.tooltipsChanged.emit()
