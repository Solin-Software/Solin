"""Native controls composed by the Zoom toolbar popup."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from PySide6.QtCore import QEvent, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from solin.core.integrations.automation.zoom.state import AudioState, ParticipantAction, RaisedHand
from solin.styles.icons import ICON_MICROPHONE, ICON_MICROPHONE_OFF, make_icon
from solin.styles.theme import PALETTE
from solin.ui.themed_tooltip import install_themed_tooltip


_UNMUTE_ACKNOWLEDGEMENT_MS = 3000

_AUDIO_ACTION = {
    AudioState.MUTED: ParticipantAction.REQUEST_UNMUTE,
    AudioState.UNMUTED: ParticipantAction.MUTE_AND_LOWER,
}


def plain_label(parent: QWidget, object_name: str = "", *, wrap: bool = False) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    return label


def icon_label(parent: QWidget, size: int, object_name: str = "") -> QLabel:
    label = plain_label(parent, object_name)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedSize(size, size)
    return label


def set_icon(label: QLabel, svg: str, size: int, color: str) -> None:
    label.setPixmap(make_icon(svg, size, color).pixmap(size, size))


class ZoomWorkflowButton(QPushButton):
    def __init__(self, parent: QWidget, *, primary: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("ZoomWorkflow")
        self.setProperty("primary", primary)
        self.setMinimumHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(11, 8, 11, 8)
        self._layout.setSpacing(10)
        self._icon = icon_label(self, 28, "ZoomWorkflowIcon")
        self._icon.setProperty("primary", primary)
        self._layout.addWidget(self._icon)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self._title = plain_label(self, "ZoomItemTitle", wrap=True)
        self._description = plain_label(self, "ZoomSecondary", wrap=True)
        copy.addWidget(self._title)
        copy.addWidget(self._description)
        self._layout.addLayout(copy, 1)
        for label in (self._icon, self._title, self._description):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_copy(self, title: str, description: str) -> None:
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self._title.setText(title)
        self._description.setText(description)

    def apply_theme(self, icon: str, color: str) -> None:
        set_icon(self._icon, icon, 16, color)

    def fit(self, width: int) -> None:
        self.setFixedHeight(max(56, self._layout.totalHeightForWidth(width)))


class ZoomSwitch(QCheckBox):
    """Accessible check box with a switch on the trailing side of its label."""

    def __init__(self, parent: QWidget, *, description: bool = False) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(38)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(12)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self._label = plain_label(
            self, "ZoomItemTitle" if description else "ZoomSecondary", wrap=True
        )
        self._description = plain_label(self, "ZoomSecondary", wrap=True)
        self._description.setVisible(description)
        for label in (self._label, self._description):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            copy.addWidget(label)
        self._layout.addLayout(copy, 1)
        self._track = QWidget(self)
        self._track.setFixedSize(34, 20)
        self._track.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._layout.addWidget(self._track)
        self.toggled.connect(self.update)
        install_themed_tooltip(self)

    def set_copy(self, title: str, description: str = "") -> None:
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self._label.setText(title)
        self._description.setText(description)

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def fit(self, width: int) -> None:
        self.setFixedHeight(max(38, self._layout.totalHeightForWidth(width)))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self._track.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
        active = self.isChecked() and self.isEnabled()
        if self.hasFocus():
            painter.setPen(QPen(QColor(PALETTE.accent_alt), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 12, 12)
        painter.setPen(QColor(PALETTE.accent if active else PALETTE.border_strong))
        painter.setBrush(QColor(PALETTE.accent if active else PALETTE.surface_alt))
        painter.drawRoundedRect(track, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PALETTE.text_on_accent if active else PALETTE.text_muted))
        trailing = self.isChecked() != (self.layoutDirection() == Qt.LayoutDirection.RightToLeft)
        x = track.right() - 10 if trailing else track.left() + 10
        painter.drawEllipse(QPointF(x, track.center().y()), 6, 6)


class _ElidedLabel(QLabel):
    def __init__(self, parent: QWidget, object_name: str) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def set_full_text(self, text: str) -> None:
        if self._full_text != text:
            self._full_text = text
            self.setAccessibleName(text)
            self._elide(self.width())

    def _elide(self, width: int) -> None:
        text = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width)
        self.setText(text)
        self.setToolTip(f"<qt>{escape(self._full_text)}</qt>" if text != self._full_text else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._elide(event.size().width())
        super().resizeEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.FontChange:
            self._elide(self.width())
        super().changeEvent(event)


class ZoomRaisedHandRow(QFrame):
    requested = Signal(object, str)

    def __init__(self, hand: RaisedHand, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("ZoomHandRow")
        self._hand = hand
        self._can_act = False
        self._fit_widths: tuple[int, int, int] | None = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 7, 0, 7)
        self._layout.setSpacing(8)
        self._mic = icon_label(self, 29, "ZoomMic")
        mic_and_name = QHBoxLayout()
        mic_and_name.setSpacing(8)
        mic_and_name.addWidget(self._mic)
        identity = QVBoxLayout()
        identity.setSpacing(0)
        self._name = _ElidedLabel(self, "ZoomItemTitle")
        self._audio = _ElidedLabel(self, "ZoomAudio")
        identity.addWidget(self._name)
        identity.addWidget(self._audio)
        mic_and_name.addLayout(identity, 1)
        self._layout.addLayout(mic_and_name, 1)

        self._buttons: dict[ParticipantAction, QPushButton] = {}
        self._actions = QHBoxLayout()
        self._actions.setSpacing(5)
        self._audio_gap = QWidget(self)
        self._actions.addWidget(self._audio_gap)
        for action in (*_AUDIO_ACTION.values(), ParticipantAction.LOWER_HAND):
            button = QPushButton(self)
            button.setObjectName(
                "ZoomQuietAction" if action is ParticipantAction.LOWER_HAND else "ZoomAudioAction"
            )
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda _checked=False, action=action: self._request(action))
            self._actions.addWidget(button)
            self._buttons[action] = button
        self._layout.addLayout(self._actions)
        self._unmute_acknowledgement = QTimer(self, singleShot=True)
        self._unmute_acknowledgement.timeout.connect(self._refresh_actions)
        self._request_text = ""
        self._requested_text = ""
        self.set_hand(hand, "")

    @property
    def hand(self) -> RaisedHand:
        return self._hand

    def button(self, action: ParticipantAction) -> QPushButton:
        return self._buttons[action]

    def tab_order(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons.values())

    def set_copy(
        self, actions: Mapping[ParticipantAction, str], requested_text: str, audio_text: str
    ) -> None:
        self._request_text = actions[ParticipantAction.REQUEST_UNMUTE]
        self._requested_text = requested_text
        self._audio.set_full_text(audio_text)
        for action, button in self._buttons.items():
            button.setText(actions[action])
        self._refresh_actions()

    def set_hand(self, hand: RaisedHand, audio_text: str) -> None:
        previous = self._hand
        self._hand = hand
        if previous.audio != hand.audio:
            self._unmute_acknowledgement.stop()
            self.apply_theme()
        self._name.set_full_text(hand.name)
        for button in self._buttons.values():
            button.setAccessibleDescription(hand.name)
        self._audio.set_full_text(audio_text)
        self._refresh_actions()

    def set_actions_enabled(self, enabled: bool) -> None:
        can_act = enabled and self._hand.reference is not None
        if can_act != self._can_act:
            self._can_act = can_act
            self._refresh_actions()

    def acknowledge_unmute(self) -> None:
        if self._hand.audio is AudioState.MUTED:
            self._unmute_acknowledgement.start(_UNMUTE_ACKNOWLEDGEMENT_MS)
            self._refresh_actions()

    def _refresh_actions(self) -> None:
        audio_action = _AUDIO_ACTION.get(self._hand.audio)
        pending = self._unmute_acknowledgement.isActive()
        request = self._buttons[ParticipantAction.REQUEST_UNMUTE]
        request.setText(self._requested_text if pending else self._request_text)
        for action, button in self._buttons.items():
            shown = action is ParticipantAction.LOWER_HAND or action is audio_action
            button.setEnabled(self._can_act and shown and not (pending and button is request))
            button.setVisible(shown)
        self._audio_gap.setVisible(audio_action is None)

    def _request(self, action: ParticipantAction) -> None:
        if self._can_act and self._hand.reference is not None and self._buttons[action].isEnabled():
            self.requested.emit(action, self._hand.reference)

    def apply_theme(self) -> None:
        live = self._hand.audio is AudioState.UNMUTED
        self._mic.setStyleSheet(
            f"background:{PALETTE.accent_muted if live else PALETTE.surface_hover};"
            "border-radius:8px;"
        )
        set_icon(
            self._mic,
            ICON_MICROPHONE if live else ICON_MICROPHONE_OFF,
            16,
            PALETTE.accent_text if live else PALETTE.text_muted,
        )

    def fit(self, width: int, audio_width: int, lower_width: int) -> None:
        widths = (width, audio_width, lower_width)
        if self._fit_widths == widths:
            return
        self._fit_widths = widths
        stacked = width < audio_width + lower_width + 128
        self._layout.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        )
        self._actions.setAlignment(
            Qt.AlignmentFlag.AlignLeading if stacked else Qt.AlignmentFlag.AlignTrailing
        )
        audio_width = min(audio_width, max(1, width - lower_width - 5))
        for action in _AUDIO_ACTION.values():
            self._buttons[action].setFixedWidth(audio_width)
        self._audio_gap.setFixedWidth(audio_width)
        self._buttons[ParticipantAction.LOWER_HAND].setFixedWidth(lower_width)
        self.setFixedHeight(79 if stacked else 55)
