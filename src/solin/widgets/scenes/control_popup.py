from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.controllers.program_recording_controller import ProgramRecordingController
from solin.core.scenes.composition import scene_uses_content_source
from solin.core.scenes.model import BusId
from solin.core.scenes.recording import ProgramRecordingStatus
from solin.styles.icons import (
    ICON_CLAPPERBOARD,
    ICON_OUTPUT_CAMERA,
    ICON_OUTPUT_SCREEN,
    ICON_REC_CIRCLE,
    ICON_REC_STOP,
    ICON_VIDEO,
    make_icon,
)
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.scene_engine_status import scene_engine_error_summary
from solin.ui.scene_recording_status import (
    scene_recording_audio_warning,
    scene_recording_error_summary,
)
from solin.widgets.common.button_feedback import ButtonSuccessFlash
from solin.widgets.common.popup_dock_button import PopupDockButton
from solin.widgets.common.popup_hover_button import PopupHoverButton


log = logging.getLogger(__name__)

# Qt's QWIDGETSIZE_MAX — clears a fixed size set with setFixedWidth/Height.
_MAX_WIDGET_SIZE = 16_777_215

# A card button routes its scene to exactly one output. Projection and program are
# independent: routing one must never move the other.
_ROUTING_BUSES = {
    "projection": BusId.MEDIA_WINDOWS,
    "program": BusId.VIRTUAL_CAMERA,
}


class _ScenePreview(QFrame):
    """The card's face: the scene's live frame, plus the on-air tally when idle.

    While the pointer is away the preview carries the card's whole status: an
    accent border when any output shows this scene, and one badge per output
    centred along the bottom edge. A freshly routed output's badge pulses for a
    few seconds so the change is noticed, then settles to solid.
    """

    _CORNER_RADIUS = 9  # matches the card's radius minus its 1px border
    _PLACEHOLDER_SIZE = 26
    _TALLY_WIDTH = 2
    _BADGE_HEIGHT = 17
    _BADGE_RADIUS = 4
    _BADGE_PAD = 5
    _BADGE_DOT = 6
    _BADGE_ICON = 12
    _BADGE_GAP = 4
    _BADGE_MARGIN = 5
    _PULSE_PERIOD_MS = 1250
    _PULSE_CYCLES = 4  # ~5s of pulsing, then the badge stays solid

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._placeholder = False
        self._live: tuple[str, ...] = ()
        self._pulsing: frozenset[str] = frozenset()
        self._dot_opacity = 1.0
        self._revealed = False
        self._badge_icons: dict[tuple[str, str], QIcon] = {}
        self._pulse = QVariantAnimation(self)
        self._pulse.setStartValue(0.0)
        self._pulse.setEndValue(1.0)
        self._pulse.setDuration(self._PULSE_PERIOD_MS)
        self._pulse.setLoopCount(self._PULSE_CYCLES)
        self._pulse.valueChanged.connect(self._on_pulse)
        self._pulse.finished.connect(self._end_pulse)

    def set_image(self, image: QImage | None) -> None:
        self._image = image if image is not None and not image.isNull() else None
        self.update()

    def set_placeholder(self, visible: bool) -> None:
        """Mark the scene's media slot as waiting rather than simply black."""
        if self._placeholder == visible:
            return
        self._placeholder = visible
        self.update()

    def set_live(self, roles: tuple[str, ...], *, pulse: tuple[str, ...] = ()) -> None:
        """Show which outputs carry this scene, in badge order; pulse the new ones."""
        roles = tuple(roles)
        pulsing = frozenset((self._pulsing & set(roles)) | set(pulse))
        changed = roles != self._live or pulsing != self._pulsing
        self._live = roles
        self._pulsing = pulsing
        if pulse:
            self._pulse.stop()
            self._pulse.start()
        elif not pulsing and self._pulse.state() is QVariantAnimation.State.Running:
            self._pulse.stop()
            self._dot_opacity = 1.0
        if changed:
            self.update()

    def set_revealed(self, revealed: bool) -> None:
        """Hand the face over to the hover controls, which carry the state then."""
        if self._revealed == revealed:
            return
        self._revealed = revealed
        self.update()

    def _on_pulse(self, value: float) -> None:
        # One slow breath per period: full, down to a faint glow, back to full.
        self._dot_opacity = 0.2 + 0.8 * (0.5 + 0.5 * math.cos(2 * math.pi * float(value)))
        self.update()

    def _end_pulse(self) -> None:
        self._pulsing = frozenset()
        self._dot_opacity = 1.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)  # keeps the stylesheet background/rounding
        image = self._image
        if image is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Clip to the card's own rounded corners: a plain rectangular draw
            # squares them off and the feed looks pasted on top of the card rather
            # than set into it.
            path = QPainterPath()
            radius = float(self._CORNER_RADIUS)
            path.addRoundedRect(QRectF(self.rect()), radius, radius)
            painter.setClipPath(path)
            painter.drawImage(QRectF(self.rect()), image)
            painter.end()
        self._paint_placeholder()
        if not self._revealed:
            self._paint_tally()

    def _paint_placeholder(self) -> None:
        """A faint media glyph so an empty media slot reads as waiting, not broken."""
        if not self._placeholder:
            return
        side = min(self._PLACEHOLDER_SIZE, self.width() // 3, self.height() // 2)
        if side < 8:
            return
        icon = make_icon(_svg(ICON_VIDEO), side, PALETTE.text_muted)
        painter = QPainter(self)
        painter.setOpacity(0.35)  # subtle: a hint, not a badge
        painter.drawPixmap(
            (self.width() - side) // 2, (self.height() - side) // 2,
            icon.pixmap(side, side),
        )
        painter.end()

    def _badge_icon(self, role: str) -> QIcon:
        key = (role, PALETTE.text_on_accent)
        icon = self._badge_icons.get(key)
        if icon is None:
            svg = ICON_OUTPUT_CAMERA if role == "program" else ICON_OUTPUT_SCREEN
            icon = make_icon(_svg(svg), self._BADGE_ICON, PALETTE.text_on_accent)
            self._badge_icons[key] = icon
        return icon

    def _paint_tally(self) -> None:
        if not self._live:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor(PALETTE.accent)
        inset = self._TALLY_WIDTH / 2
        painter.setPen(QPen(accent, self._TALLY_WIDTH))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = self._CORNER_RADIUS - inset
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), radius, radius
        )

        badge_width = self._BADGE_PAD * 2 + self._BADGE_DOT + 3 + self._BADGE_ICON
        total = len(self._live) * badge_width + (len(self._live) - 1) * self._BADGE_GAP
        x = (self.width() - total) / 2
        y = self.height() - self._BADGE_MARGIN - self._BADGE_HEIGHT
        on_accent = QColor(PALETTE.text_on_accent)
        for role in self._live:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(
                QRectF(x, y, badge_width, self._BADGE_HEIGHT),
                self._BADGE_RADIUS, self._BADGE_RADIUS,
            )
            dot_y = y + (self._BADGE_HEIGHT - self._BADGE_DOT) / 2
            painter.setOpacity(self._dot_opacity if role in self._pulsing else 1.0)
            painter.setBrush(on_accent)
            painter.drawEllipse(QRectF(x + self._BADGE_PAD, dot_y, self._BADGE_DOT, self._BADGE_DOT))
            painter.setOpacity(1.0)
            icon_x = round(x + self._BADGE_PAD + self._BADGE_DOT + 3)
            icon_y = round(y + (self._BADGE_HEIGHT - self._BADGE_ICON) / 2)
            painter.drawPixmap(
                icon_x, icon_y,
                self._badge_icon(role).pixmap(self._BADGE_ICON, self._BADGE_ICON),
            )
            x += badge_width + self._BADGE_GAP
        painter.end()


class _SceneRoutingButton(QPushButton):
    right_clicked = Signal()
    focus_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Only Tab navigation counts: Qt also hands focus to the first focusable
        # widget when a window activates, and that must not pin a card open.
        self.keyboard_focused = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.keyboard_focused = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
        )
        self.focus_changed.emit()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.keyboard_focused = False
        self.focus_changed.emit()


class _SceneCard(QFrame):
    """A scene: a canvas-proportioned preview whose controls appear on hover.

    Hovering splits the preview into one half per output the scene may use —
    virtual camera on the left, projection on the right; a single allowed
    output takes the whole face. Left clicks put this scene on that output;
    right clicks request that output's return scene after automatic media.
    Routing is radio-like across the strip — a single choice per output, so
    checking one scene unchecks the rest. Away from the pointer the preview
    shows the on-air tally instead (see ``_ScenePreview``).
    """

    routing_requested = Signal(str, str)  # (scene_id, "projection" | "program")
    routing_return_requested = Signal(str, str)

    _BORDER_WIDTH = 1
    _HALF_ICON_SIZE = 20
    _TITLE_HEIGHT = 18
    _TITLE_PADDING = 6

    def __init__(
        self,
        scene_id: str,
        name: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_id = scene_id
        self.setObjectName("SceneCard")
        self._scene_name = name
        self._hovered = False
        self._revealed = False
        # None until the first routing arrives: a card that opens already live
        # is not news, so only later changes pulse.
        self._live_roles: tuple[str, ...] | None = None
        layout = QVBoxLayout(self)
        # Inset by the card's own 1px border, or the preview paints straight over
        # it and the card looks borderless down its sides.
        layout.setContentsMargins(
            self._BORDER_WIDTH, self._BORDER_WIDTH, self._BORDER_WIDTH, self._BORDER_WIDTH
        )
        layout.setSpacing(0)

        self._preview = _ScenePreview()
        self._preview.setObjectName("SceneCardPreview")
        layout.addWidget(self._preview)

        halves = QHBoxLayout(self._preview)
        halves.setContentsMargins(0, 0, 0, 0)
        halves.setSpacing(0)
        self._program = self._routing_button("SceneCardProgram", "program")
        halves.addWidget(self._program)
        self._projection = self._routing_button("SceneCardProjection", "projection")
        halves.addWidget(self._projection)

        # The name only appears with the controls; it never takes a click.
        self._name = QLabel(self._preview)
        self._name.setObjectName("SceneCardName")
        self._name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._name.hide()
        self._sync_name()

    def _routing_button(self, object_name: str, role: str) -> _SceneRoutingButton:
        button = _SceneRoutingButton()
        button.setObjectName(object_name)
        button.setCheckable(True)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        button.setIconSize(QSize(self._HALF_ICON_SIZE, self._HALF_ICON_SIZE))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Keyboard users reach the halves with Tab; a mouse click must not leave
        # focus behind, or the controls would stay up after the pointer leaves.
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setProperty("revealed", False)
        button.setProperty("side", "full")
        button.clicked.connect(lambda: self.routing_requested.emit(self.scene_id, role))
        button.right_clicked.connect(
            lambda: self.routing_return_requested.emit(self.scene_id, role)
        )
        button.focus_changed.connect(self._sync_revealed)
        return button

    def set_name(self, name: str) -> None:
        if self._scene_name != name:
            self._scene_name = name
            self._sync_name()

    def _sync_name(self) -> None:
        width = max(0, self._preview.width() - 2 * self._TITLE_PADDING)
        self._name.setText(
            self._name.fontMetrics().elidedText(self._scene_name, Qt.TextElideMode.ElideRight, width)
        )
        self._name.setToolTip(self._scene_name)
        self.setAccessibleName(self._scene_name)
        for button in (self._program, self._projection):
            button.setAccessibleDescription(self._scene_name)

    def set_thumbnail(self, image: QImage | None) -> None:
        self._preview.set_image(image)

    def set_content_placeholder(self, visible: bool) -> None:
        self._preview.set_placeholder(visible)

    def set_canvas_aspect(self, preview_height: int, aspect: float) -> None:
        """Keep the preview at the scene's proportions; the card is only its border."""
        width = max(1, round(preview_height * aspect))
        self._preview.setFixedSize(width, preview_height)
        border = self._BORDER_WIDTH * 2
        self.setFixedSize(width + border, preview_height + border)
        self._name.setGeometry(0, 0, width, self._TITLE_HEIGHT)
        self._sync_name()

    def set_routing(
        self,
        *,
        on_projection: bool,
        on_program: bool,
        program_available: bool,
        projection_allowed: bool = True,
        program_allowed: bool = True,
    ) -> None:
        for button, active in (
            (self._projection, on_projection),
            (self._program, on_program),
        ):
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
        # A scene disabled for an output offers no half for it, and the
        # virtual-camera half only means anything while that output runs. The
        # remaining half spans the whole face.
        show_program = program_available and program_allowed
        show_projection = projection_allowed
        self._program.setVisible(show_program)
        self._projection.setVisible(show_projection)
        both = show_program and show_projection
        self._set_button_property(self._program, "side", "left" if both else "full")
        self._set_button_property(self._projection, "side", "right" if both else "full")
        program_label = QCoreApplication.translate("SceneControlPopup", "Virtual camera")
        projection_label = QCoreApplication.translate("SceneControlPopup", "Media windows")
        self._program.setToolTip(program_label)
        self._program.setAccessibleName(program_label)
        self._projection.setToolTip(projection_label)
        self._projection.setAccessibleName(projection_label)

        # Badge order mirrors the halves: camera left, projection right.
        live = tuple(
            role
            for role, on_air in (
                ("program", on_program and program_available),
                ("projection", on_projection),
            )
            if on_air
        )
        previous = self._live_roles
        self._live_roles = live
        pulse = () if previous is None else tuple(role for role in live if role not in previous)
        self._preview.set_live(live, pulse=pulse)

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self._hovered = True
        self._sync_revealed()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self._hovered = False
        self._sync_revealed()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # A hidden card gets no leave event; re-read the pointer on the way back.
        self._hovered = self.underMouse()
        self._sync_revealed()

    def _sync_revealed(self) -> None:
        revealed = (
            self._hovered
            or self._program.keyboard_focused
            or self._projection.keyboard_focused
        )
        if revealed == self._revealed:
            return
        self._revealed = revealed
        for button, svg in ((self._program, ICON_OUTPUT_CAMERA), (self._projection, ICON_OUTPUT_SCREEN)):
            button.setIcon(
                make_icon(_svg(svg), self._HALF_ICON_SIZE, PALETTE.text_on_accent)
                if revealed
                else QIcon()
            )
            self._set_button_property(button, "revealed", revealed)
        self._name.setVisible(revealed)
        self._name.raise_()
        self._preview.set_revealed(revealed)

    @staticmethod
    def _set_button_property(button: QPushButton, name: str, value: object) -> None:
        if button.property(name) == value:
            return
        button.setProperty(name, value)
        button.style().unpolish(button)
        button.style().polish(button)


class SceneControlPopup(QWidget):
    """Fast manual scene control without conflating desired and applied state."""

    _PREFERRED_WIDTH = 420
    _MIN_WIDTH = 340
    _SCREEN_MARGIN = 8
    _ANCHOR_GAP = 10
    # Square icon button; matches the sibling text buttons' 34px outer height so
    # the header row lines up. Also pinned in the stylesheet, whose min-height
    # would otherwise win over setFixedSize and stretch it into a tall sliver.
    _RECORD_BUTTON_SIZE = 34
    # A card is just its canvas-proportioned preview (name and controls float
    # over it on hover), so only the preview height is chosen here; the width
    # follows the scene's aspect.
    _SCENE_CARD_PREVIEW_HEIGHT = 108
    _SCENE_CARD_HEIGHT = (
        _SCENE_CARD_PREVIEW_HEIGHT
        + 2 * _SceneCard._BORDER_WIDTH  # the card's own border, or the strip clips it
    )
    _SCENE_CARD_SPACING = 8
    _SCENE_CARD_SCROLLBAR_ALLOWANCE = 12
    _SCENE_CARD_EDGE_PAD = 2

    def __init__(
        self,
        controller: SceneRuntimeController,
        parent: QWidget | None = None,
        *,
        recording: ProgramRecordingController | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._PREFERRED_WIDTH)
        self._controller = controller
        self._recording = recording
        self._floating_parent = parent
        self._docked = False
        self._hover_button_visible = False
        self._anchor_rect: QRect | None = None
        self._available_rect: QRect | None = None
        self._engine_visual_state = "unavailable"
        self._hidden_at: float | None = None
        self._scene_cards: dict[str, _SceneCard] = {}
        self._success_flash = ButtonSuccessFlash(self)
        # Live card thumbnails, requested only while the panel is on screen.
        from solin.controllers.scene_thumbnail_egress import SceneThumbnailEgressController

        self._thumbnails = SceneThumbnailEgressController(parent=self)
        self._thumbnails.thumbnail_ready.connect(self._on_thumbnail)
        # What the engine was actually told. Allocating the block is not the same
        # as the engine hearing about it: the panel can open before the engine is
        # ready, and that request has to be retried rather than assumed delivered.
        self._thumbnail_sent: tuple | None = None
        self._recording_clock = QTimer(self)
        self._recording_clock.setInterval(1000)
        self._recording_clock.timeout.connect(self._render_recording)

        # Fade the window itself, never a QGraphicsOpacityEffect: an effect on
        # this translucent frameless popup makes Qt rasterise it, which paints
        # black behind the rounded corners and can strand the panel fully
        # transparent. windowOpacity is ignored outright where unsupported, so
        # the worst case is no animation rather than an invisible panel.
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(160)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(lambda: self.setWindowOpacity(1.0))

        self._build_ui()
        controller.document_changed.connect(self._render)
        controller.desired_scenes_changed.connect(self._render)
        controller.applied_scenes_changed.connect(self._render)
        controller.engine_ready_changed.connect(self._render)
        controller.operational_state_changed.connect(self._render)
        controller.runtime_changed.connect(self._render)
        controller.scene_profiles_changed.connect(self._render)
        if recording is not None:
            recording.state_changed.connect(self._render)
        self.apply_theme()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._card = QFrame()
        self._card.setObjectName("SceneControlCard")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(11)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        header.addWidget(self._icon)
        self._title = QLabel(self.tr("Solin scenes"))
        self._title.setObjectName("SceneControlTitle")
        header.addWidget(self._title)
        header.addStretch()
        # Icon-only record control; its state lives in the tooltip/accessible name.
        self._recording_button = QPushButton()
        self._recording_button.setObjectName("SceneControlRecording")
        self._recording_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recording_button.setFixedSize(
            self._RECORD_BUTTON_SIZE, self._RECORD_BUTTON_SIZE
        )
        self._recording_button.setIconSize(QSize(15, 15))
        self._recording_button.clicked.connect(self._toggle_recording)
        header.addWidget(self._recording_button)
        self._output = QPushButton()
        self._output.setObjectName("SceneOutputToggle")
        self._output.setCheckable(True)
        self._output.clicked.connect(self._set_output_enabled)
        header.addWidget(self._output)
        self.hover_button = PopupHoverButton(self._card)
        header.addWidget(self.hover_button)
        self.dock_button = PopupDockButton(self._card)
        self.dock_button.toggled.connect(self.set_docked)
        header.addWidget(self.dock_button)
        layout.addLayout(header)

        # One horizontal strip of scene cards. It scrolls sideways rather than
        # wrapping, so the panel keeps a single predictable row however many
        # scenes exist.
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setObjectName("SceneCardStrip")
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._cards_host = QWidget()
        self._cards_host.setObjectName("SceneCardStripHost")
        self._cards_layout = QHBoxLayout(self._cards_host)
        # A hair of room on the right so the last card's border is not clipped
        # by the viewport edge (sizeHint counts it, so the panel grows to suit).
        self._cards_layout.setContentsMargins(0, 0, self._SCENE_CARD_EDGE_PAD, 0)
        self._cards_layout.setSpacing(self._SCENE_CARD_SPACING)
        self._cards_layout.addStretch()  # keeps the cards left-aligned
        self._cards_scroll.setWidget(self._cards_host)
        self._cards_scroll.setFixedHeight(
            self._SCENE_CARD_HEIGHT + self._SCENE_CARD_SCROLLBAR_ALLOWANCE
        )
        layout.addWidget(self._cards_scroll)
        root.addWidget(self._card)

    def _card_strip_chrome(self) -> int:
        """Pixels the panel spends around the card strip's viewport.

        Measured from the live widgets rather than assumed: the card's contents
        margins *and* its 1px frame border both eat into the viewport, and
        missing the border made the panel 2px too narrow, which showed a
        scrollbar (and clipped a card) when everything actually fitted.
        """
        viewport_width = self._cards_scroll.viewport().width()
        if viewport_width > 0 and self.width() > viewport_width:
            return self.width() - viewport_width
        card_layout = self._card.layout()
        margins = card_layout.contentsMargins() if card_layout is not None else None
        base = margins.left() + margins.right() if margins is not None else 0
        return base + 2 * self._card.frameWidth()

    def _sync_card_strip_height(self, content_width: int, viewport_width: int) -> None:
        """Only reserve room for the scrollbar when the cards actually overflow."""
        overflows = content_width > max(0, viewport_width)
        height = self._SCENE_CARD_HEIGHT + (
            self._SCENE_CARD_SCROLLBAR_ALLOWANCE if overflows else 0
        )
        self._cards_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if overflows
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if self._cards_scroll.height() != height:
            self._cards_scroll.setFixedHeight(height)

    def _on_thumbnail(self, scene_id: str, image: QImage) -> None:
        card = self._scene_cards.get(scene_id)
        if card is not None:
            card.set_thumbnail(image)

    def _sync_thumbnail_feed(self) -> None:
        """Ask the engine for thumbnails of exactly the cards now on screen."""
        on_screen = self.isVisible() and bool(self._scene_cards)
        scene_ids = tuple(self._scene_cards) if on_screen else ()
        cell = (
            self._scene_cards[scene_ids[0]]._preview.size() if scene_ids else None
        )
        if not scene_ids or cell is None or cell.width() <= 0:
            if self._thumbnail_sent is not None or self._thumbnails.descriptor is not None:
                self._thumbnails.stop()
                self._controller.set_thumbnail_egress(None, (), 0, 0)
                self._thumbnail_sent = None
            return
        width, height = cell.width(), cell.height()
        self._thumbnails.reconfigure(scene_ids, width, height)  # no-op if unchanged
        descriptor = self._thumbnails.descriptor
        if descriptor is None:
            return
        desired = (descriptor, scene_ids, width, height)
        if desired == self._thumbnail_sent:
            return
        if self._controller.set_thumbnail_egress(descriptor, scene_ids, width, height):
            self._thumbnail_sent = desired

    def _sync_scene_cards(self) -> None:
        """Match one card per scene, in document order, reusing existing cards."""
        document = self._controller.document
        scenes = document.scenes
        video = document.output(BusId.VIRTUAL_CAMERA).video_format
        aspect = (video.width / video.height) if video.height else 16 / 9

        for scene_id in tuple(self._scene_cards):
            if all(scene.id != scene_id for scene in scenes):
                card = self._scene_cards.pop(scene_id)
                self._cards_layout.removeWidget(card)
                card.deleteLater()

        # Routing is one choice per output, so deriving "checked" from the routed
        # scene makes the buttons mutually exclusive across the strip for free.
        # Track the *desired* scene, not the applied one: the button has to answer
        # the click immediately rather than waiting for the engine to confirm (and
        # with no engine attached, applied is never set at all).
        projection_scene = self._controller.desired_scene(BusId.MEDIA_WINDOWS)
        program_scene = self._controller.desired_scene(BusId.VIRTUAL_CAMERA)
        program_available = self._controller.runtime.state.output(
            BusId.VIRTUAL_CAMERA
        ).enabled
        # A scene whose media slot is empty renders black; mark those so the card
        # reads as "waiting for media" rather than broken.
        content_idle = not self._controller.content_is_playing

        for index, scene in enumerate(scenes):
            card = self._scene_cards.get(scene.id)
            if card is None:
                card = _SceneCard(
                    scene.id,
                    scene.name,
                    parent=self._cards_host,
                )
                card.routing_requested.connect(self._on_card_routing_requested)
                card.routing_return_requested.connect(self._on_card_routing_return_requested)
                self._scene_cards[scene.id] = card
                self._cards_layout.insertWidget(index, card)
            else:
                card.set_name(scene.name)
                if self._cards_layout.indexOf(card) != index:
                    self._cards_layout.removeWidget(card)
                    self._cards_layout.insertWidget(index, card)
            card.set_canvas_aspect(self._SCENE_CARD_PREVIEW_HEIGHT, aspect)
            card.set_content_placeholder(
                content_idle and scene_uses_content_source(document, scene.id)
            )
            card.set_routing(
                on_projection=scene.id == projection_scene,
                on_program=scene.id == program_scene,
                program_available=program_available,
            )
        self._sync_thumbnail_feed()

    def _on_card_routing_requested(self, scene_id: str, role: str) -> None:
        """Route a scene to one output, leaving the other where it is."""
        bus_id = _ROUTING_BUSES.get(role)
        if bus_id is None:
            return
        try:
            # The session selection takes effect now; the next projection
            # still enters the automatic media scene.
            self._controller.select_scene(bus_id, scene_id)
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not route the scene from its card")
        self._render()

    def _on_card_routing_return_requested(self, scene_id: str, role: str) -> None:
        """Save one output's return scene, acknowledging only a successful save."""
        bus_id = _ROUTING_BUSES.get(role)
        if bus_id is None:
            return
        try:
            if not self._controller.return_scene_override_available(bus_id):
                return
            self._controller.set_return_scene(bus_id, scene_id)
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not set the return scene from its card")
            return
        self._render()
        card = self._scene_cards.get(scene_id)
        if card is not None:
            button = card._projection if role == "projection" else card._program
            self._success_flash.flash(button)

    def _toggle_recording(self) -> None:
        if self._recording is None:
            return
        try:
            self._recording.toggle()
        except Exception:  # noqa: BLE001 - recording controller boundary
            log.exception("Could not toggle Program recording")
        self._render()

    def _render_recording(self) -> None:
        # Recording captures the virtual camera's mix, so the control only exists
        # while that output does. The controller stops a live recording when the
        # output goes off, so hiding the button never strands one running.
        if self._recording is None or not self._controller.program_output_enabled:
            self._recording_clock.stop()
            self._recording_button.hide()
            return
        state = self._recording.state
        status = state.status
        is_recording = status is ProgramRecordingStatus.RECORDING
        is_active = status in {
            ProgramRecordingStatus.STARTING,
            ProgramRecordingStatus.RECORDING,
            ProgramRecordingStatus.STOPPING,
        }
        if is_active and not self._recording_clock.isActive():
            self._recording_clock.start()
        elif not is_active:
            self._recording_clock.stop()
        if is_recording:
            started = state.started_at_monotonic
            elapsed = 0 if started is None else max(0, int(time.monotonic() - started))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            elapsed_text = (
                f"{hours}:{minutes:02d}:{seconds:02d}"
                if hours
                else f"{minutes:02d}:{seconds:02d}"
            )
            text = self.tr("Stop recording · %1").replace("%1", elapsed_text)
            icon = ICON_REC_STOP
            audio_warnings = tuple(
                value
                for value in (
                    scene_recording_audio_warning(
                        state.microphone_warning,
                        "microphone",
                    ),
                    scene_recording_audio_warning(
                        state.system_audio_warning,
                        "system_audio",
                    ),
                )
                if value
            )
            # Icon-only: the elapsed time lives in the tooltip now.
            tooltip = "\n".join((text, *audio_warnings))
        elif status is ProgramRecordingStatus.STARTING:
            text = self.tr("Starting recording…")
            icon = ICON_REC_CIRCLE
            tooltip = text
        elif status is ProgramRecordingStatus.STOPPING:
            text = self.tr("Finishing recording…")
            icon = ICON_REC_STOP
            tooltip = text
        elif status is ProgramRecordingStatus.FAILED:
            text = self.tr("Try recording again")
            icon = ICON_REC_CIRCLE
            tooltip = scene_recording_error_summary(state.error_code)
        else:
            text = self.tr("Start recording")
            icon = ICON_REC_CIRCLE
            tooltip = self.tr("Start recording the live output")
        self._recording_button.setIcon(make_icon(_svg(icon), 15, PALETTE.danger))
        self._recording_button.setEnabled(
            status is ProgramRecordingStatus.RECORDING
            or (
                self._controller.engine_ready
                and self._recording.supported
                and status
                in {
                    ProgramRecordingStatus.IDLE,
                    ProgramRecordingStatus.FAILED,
                }
            )
        )
        self._recording_button.setProperty("recording", is_active)
        if not self._recording_button.isEnabled():
            if not self._controller.engine_ready:
                tooltip = self.tr("The scene engine must be ready to record.")
            elif not self._recording.supported:
                tooltip = self.tr("Recording is unavailable in this scene engine.")
        self._recording_button.setToolTip(tooltip)
        self._recording_button.setAccessibleName(text)
        self._recording_button.setAccessibleDescription(tooltip)
        self._recording_button.show()
        self._repolish(self._recording_button)


    def _set_output_enabled(self, checked: bool) -> None:
        try:
            self._controller.set_output_enabled(BusId.VIRTUAL_CAMERA, bool(checked))
        except Exception:  # noqa: BLE001 - UI operation boundary
            log.exception("Could not update the scene output state")
        self._render()


    def _render(self, _value: object = None) -> None:
        self._render_recording()
        runtime = self._controller.runtime.state.output(BusId.VIRTUAL_CAMERA)
        engine_state = "unavailable"

        if self._controller.hydration_in_progress:
            engine_text = self.tr("Preparing…")
            engine_state = "preparing"
        elif self._controller.last_engine_error_code:
            error_code = self._controller.last_engine_error_code
            error_summary = scene_engine_error_summary(error_code)
            engine_text = f"{self.tr('Error')}: {error_summary} ({error_code})"
            engine_state = "error"
        elif self._controller.engine_ready:
            engine_text = self.tr("Ready")
            engine_state = "ready"
        elif self._controller.engine_configured:
            engine_text = self.tr("Starting…")
            engine_state = "preparing"
        else:
            engine_text = self.tr("Unavailable")
        self._set_engine_visual(engine_state, engine_text)

        self._output.blockSignals(True)
        self._output.setChecked(runtime.enabled)
        output_text = (
            self.tr("Virtual camera enabled")
            if runtime.enabled
            else self.tr("Virtual camera disabled")
        )
        self._output.setText(output_text)
        self._output.setToolTip(self.tr("Output on") if runtime.enabled else self.tr("Output off"))
        self._output.setAccessibleName(output_text)
        self._output.setAccessibleDescription(self._output.toolTip())
        self._output.blockSignals(False)
        self._sync_scene_cards()
        self._sync_geometry()


    def _set_engine_visual(self, state: str, description: str) -> None:
        self._engine_visual_state = state
        color = PALETTE.text_faint if state == "ready" else PALETTE.warning
        self._icon.setPixmap(make_icon(_svg(ICON_CLAPPERBOARD), 18, color).pixmap(18, 18))
        self._icon.setToolTip(description)
        self._icon.setAccessibleName(self.tr("Scene engine"))
        self._icon.setAccessibleDescription(description)


    @property
    def docked(self) -> bool:
        """True while the panel is attached to the bottom of the main window."""
        return self._docked

    def _dock_host(self) -> QWidget | None:
        """The container to attach to, found by walking up the parent chain.

        Any ancestor exposing ``scenes_dock_container()`` (the main window) can
        host the panel, so the toolbar in between needs no wiring.
        """
        widget: QWidget | None = self._floating_parent
        while widget is not None:
            provider = getattr(widget, "scenes_dock_container", None)
            if callable(provider):
                try:
                    host = provider()
                    return host if isinstance(host, QWidget) else None
                except Exception:  # noqa: BLE001 - a bad host must not break the toggle
                    log.exception("Could not resolve the scenes dock container")
                    return None
            widget = widget.parentWidget()
        return None

    def set_docked(self, docked: bool) -> None:
        """Attach the panel to the window bottom, or return it to floating."""
        docked = bool(docked)
        if docked == self._docked:
            return
        if docked:
            host = self._dock_host()
            layout = host.layout() if host is not None else None
            if host is None or layout is None:
                # Nothing can host it (e.g. a standalone popup in a test): stay
                # floating and put the button back rather than half-docking.
                self.dock_button.setChecked(False)
                return
            self._fade.stop()
            self.setWindowOpacity(1.0)
            self._docked = True
            # "Open on hover" only means something for a popup that opens; an
            # attached panel is always on screen. Remember whether the host had
            # revealed the control so detaching restores exactly that.
            self._hover_button_visible = self.hover_button.isVisibleTo(self)
            self.hover_button.hide()
            # A docked panel is an ordinary child widget: drop the popup window
            # flags (which also close it on outside clicks) and the fixed popup
            # width so it can stretch across the window.
            # Set the translucency attribute *before* reparenting: setParent
            # recreates the native window, and the platform picks the window's
            # visual from this attribute at creation time (see the undock path).
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setParent(host, Qt.WindowType.Widget)
            self.setMinimumWidth(self._MIN_WIDTH)
            self.setMaximumWidth(_MAX_WIDGET_SIZE)
            self.setMinimumHeight(0)
            self.setMaximumHeight(_MAX_WIDGET_SIZE)
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            layout.addWidget(self)
            host.show()
            self.show()
        else:
            self._docked = False
            if self._hover_button_visible:
                self.hover_button.show()
            host = self.parentWidget()
            host_layout = host.layout() if host is not None else None
            if host_layout is not None:
                host_layout.removeWidget(self)
            # Restore translucency *before* reparenting. setParent recreates the
            # native window, and X11 only gives it a 32-bit ARGB visual when this
            # attribute is already set — setting it afterwards left the window
            # opaque, so the rounded corners painted black for the rest of the
            # session. destroy() drops the stale handle so show() builds a fresh
            # one with the right visual.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.destroy()
            self.setParent(
                self._floating_parent,
                Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
            )
            self.setFixedWidth(self._PREFERRED_WIDTH)
            self.hide()
            if host is not None and host_layout is not None and not host_layout.count():
                host.hide()
        # Docked the card spans the window edge to edge, so its rounded corners
        # would cut into that edge — square them off while attached.
        self._card.setProperty("docked", self._docked)
        self._repolish(self._card)
        self._render()

    def show_above(self, anchor: QWidget) -> None:
        if self._docked:
            return  # already attached to the window; nothing to pop up
        top_left = anchor.mapToGlobal(anchor.rect().topLeft())
        self._anchor_rect = QRect(top_left.x(), top_left.y(), anchor.width(), anchor.height())
        screen = (
            QGuiApplication.screenAt(self._anchor_rect.center()) or QGuiApplication.primaryScreen()
        )
        self._available_rect = screen.availableGeometry() if screen is not None else None
        self._fade.stop()
        self.setWindowOpacity(0.0)
        self._render()
        self.show()
        QTimer.singleShot(0, self._finish_show)

    def _finish_show(self) -> None:
        if not self.isVisible():
            # Dismissed before the fade could start (a click straight back on the
            # toolbar does this). Reset the opacity we pre-set, or the panel would
            # be fully transparent the next time it opens.
            self.setWindowOpacity(1.0)
            return
        self._sync_geometry()
        self.raise_()
        self.activateWindow()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _sync_docked_geometry(self) -> None:
        """Lay out for the window-bottom dock: fill the width, never reposition.

        The floating path sizes against the screen and moves the popup next to its
        anchor; docked, the layout owns both position and width, so this only has
        to let the layouts settle at the new width.
        """
        self.ensurePolished()
        self._sync_card_strip_height(
            self._cards_host.sizeHint().width(),
            self.width() - self._card_strip_chrome(),
        )
        card_layout = self._card.layout()
        if card_layout is not None:
            card_layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_thumbnail_feed()

    def hideEvent(self, event) -> None:  # noqa: N802
        # Whatever interrupted the fade, a hidden panel must never stay
        # transparent — the next open has to be visible.
        self._fade.stop()
        self.setWindowOpacity(1.0)
        self._hidden_at = time.monotonic()
        if not self._docked:
            # Nobody is looking: stop paying for thumbnail renders.
            self._thumbnails.stop()
            self._controller.set_thumbnail_egress(None, (), 0, 0)
        super().hideEvent(event)

    def dismissed_within(self, milliseconds: int) -> bool:
        """Whether the panel closed in the last ``milliseconds``.

        Qt closes a popup on the click that lands outside it, so a click on the
        toolbar button that opened it arrives *after* the dismissal. A host uses
        this to tell that click apart from a fresh one and toggle closed instead
        of immediately reopening.
        """
        if self._hidden_at is None:
            return False
        return (time.monotonic() - self._hidden_at) * 1000 < milliseconds

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._docked and event.oldSize().width() != event.size().width():
            self._sync_docked_geometry()

    def _sync_geometry(self) -> None:
        if self._docked:
            self._sync_docked_geometry()
            return
        self.ensurePolished()
        available = (
            self._available_rect.adjusted(
                self._SCREEN_MARGIN,
                self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
                -self._SCREEN_MARGIN,
            )
            if self._available_rect is not None
            else None
        )
        # Grow to show as many scene cards as fit; the screen is the ceiling and
        # the strip scrolls sideways for whatever is left over.
        chrome = self._card_strip_chrome()
        content_width = self._cards_host.sizeHint().width()
        width = max(self._PREFERRED_WIDTH, content_width + chrome)
        if available is not None:
            width = max(self._MIN_WIDTH, min(width, available.width()))
        self.setFixedWidth(width)
        self._sync_card_strip_height(content_width, width - chrome)

        card_layout = self._card.layout()
        if card_layout is not None:
            card_layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()
        preferred_height = max(1, self.sizeHint().height())
        if self._anchor_rect is None:
            self.setFixedHeight(preferred_height)
            return

        place_above = True
        if available is not None:
            space_above = max(0, self._anchor_rect.top() - self._ANCHOR_GAP - available.top())
            space_below = max(
                0,
                available.bottom() - self._anchor_rect.bottom() - self._ANCHOR_GAP,
            )
            place_above = preferred_height <= space_above or space_above >= space_below
            preferred_height = min(preferred_height, available.height())
        self.setFixedHeight(preferred_height)

        x = self._anchor_rect.center().x() - self.width() // 2
        y = (
            self._anchor_rect.top() - self.height() - self._ANCHOR_GAP
            if place_above
            else self._anchor_rect.bottom() + self._ANCHOR_GAP + 1
        )
        if available is not None:
            x = max(available.left(), min(x, available.right() - self.width() + 1))
            y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def apply_theme(self) -> None:
        self.hover_button.apply_theme()
        self.dock_button.apply_theme()
        self._set_engine_visual(self._engine_visual_state, self._icon.toolTip())
        self.setStyleSheet(
            f"""
            QFrame#SceneControlCard {{ background:{PALETTE.surface}; border:1px solid {PALETTE.border}; border-radius:14px; }}
            QFrame#SceneControlCard[docked="true"] {{ border-radius:0; border-left:none; border-right:none; border-bottom:none; }}
            QWidget {{ background:transparent; color:{PALETTE.text_secondary}; }}
            QLabel#SceneControlTitle {{ color:{PALETTE.text_faint}; font-size:13px; font-weight:650; }}
            QPushButton {{ min-height:32px; padding:0 11px; border:1px solid {PALETTE.border}; border-radius:8px; background:{PALETTE.surface_card}; color:{PALETTE.text_secondary}; font-weight:550; }}
            QPushButton:hover {{ background:{PALETTE.surface_hover}; border-color:{PALETTE.border_strong}; color:{PALETTE.text_primary}; }}
            QPushButton:focus {{ border-color:{PALETTE.accent_alt}; }}
            QPushButton:pressed {{ background:{PALETTE.surface_alt}; }}
            QPushButton#SceneOutputToggle {{ min-width:84px; }}
            QPushButton#SceneOutputToggle:checked {{ background:{qss_rgba(PALETTE.accent, 0.10)}; border-color:{qss_rgba(PALETTE.accent, 0.42)}; color:{PALETTE.accent_text}; }}
            QPushButton#SceneControlRecording {{ min-width:{self._RECORD_BUTTON_SIZE - 2}px; max-width:{self._RECORD_BUTTON_SIZE - 2}px; min-height:{self._RECORD_BUTTON_SIZE - 2}px; max-height:{self._RECORD_BUTTON_SIZE - 2}px; padding:0; }}
            QPushButton#SceneControlRecording:hover {{ background:{qss_rgba(PALETTE.danger, 0.10)}; border-color:{qss_rgba(PALETTE.danger, 0.62)}; }}
            QPushButton#SceneControlRecording[recording="true"] {{ background:{qss_rgba(PALETTE.danger, 0.18)}; border-color:{qss_rgba(PALETTE.danger, 0.70)}; }}
            QPushButton:disabled {{ color:{PALETTE.text_faint}; border-color:{PALETTE.border_muted}; }}
            QScrollArea#SceneCardStrip {{ border:none; background:transparent; }}
            QWidget#SceneCardStripHost {{ background:transparent; }}
            QFrame#SceneCard {{ background:{qss_rgba(PALETTE.border, 0.75)}; border:none; border-radius:10px; }}
            QFrame#SceneCardPreview {{ background:{qss_rgba(PALETTE.surface_alt, 0.95)}; border:none; border-radius:9px; }}
            QLabel#SceneCardName {{ background:{qss_rgba(PALETTE.black, 0.85)}; color:{PALETTE.white}; font-size:11px; font-weight:550; padding:0 {_SceneCard._TITLE_PADDING}px; border-top-left-radius:9px; border-top-right-radius:9px; }}
            QPushButton#SceneCardProjection, QPushButton#SceneCardProgram {{ min-width:0; min-height:0; padding:{_SceneCard._TITLE_HEIGHT}px 0 0 0; border:none; border-radius:0; background:transparent; }}
            QPushButton#SceneCardProjection[revealed="true"], QPushButton#SceneCardProgram[revealed="true"] {{ background:{qss_rgba(PALETTE.black, 0.55)}; }}
            QPushButton#SceneCardProjection[revealed="true"]:hover, QPushButton#SceneCardProgram[revealed="true"]:hover {{ background:{qss_rgba(PALETTE.black, 0.30)}; }}
            QPushButton#SceneCardProjection[revealed="true"]:checked, QPushButton#SceneCardProgram[revealed="true"]:checked {{ background:{qss_rgba(PALETTE.accent, 0.75)}; }}
            QPushButton#SceneCardProjection[revealed="true"]:checked:hover, QPushButton#SceneCardProgram[revealed="true"]:checked:hover {{ background:{qss_rgba(PALETTE.accent, 0.88)}; }}
            QPushButton#SceneCardProjection[revealed="true"]:focus, QPushButton#SceneCardProgram[revealed="true"]:focus {{ border:1px solid {PALETTE.accent}; }}
            QPushButton#SceneCardProjection[side="full"], QPushButton#SceneCardProgram[side="full"] {{ border-radius:9px; }}
            QPushButton#SceneCardProgram[side="left"] {{ border-top-left-radius:9px; border-bottom-left-radius:9px; }}
            QPushButton#SceneCardProgram[side="left"][revealed="true"] {{ border-right:1px solid {qss_rgba(PALETTE.white, 0.25)}; }}
            QPushButton#SceneCardProjection[side="right"] {{ border-top-right-radius:9px; border-bottom-right-radius:9px; }}
            QScrollBar:horizontal {{ height:5px; margin-top:3px; background:transparent; }}
            QScrollBar::handle:horizontal {{ background:{PALETTE.border}; border-radius:2px; min-width:24px; }}
            QScrollBar::handle:horizontal:hover {{ background:{PALETTE.text_dim}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}
            """
        )

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.LanguageChange:
            self._title.setText(self.tr("Solin scenes"))
            self._render()
        super().changeEvent(event)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _svg(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Scene icon must be SVG text")
    return value
