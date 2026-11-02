"""Profile selection presentation shared by startup and in-session switching."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial

from PySide6.QtCore import QCoreApplication, QEvent, QT_TRANSLATE_NOOP, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QApplication,
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from solin.core.profiles.models import ProfileInfo
from solin.styles.icons import ICON_LOG_OUT, ICON_OVERLAY_CLOSE, make_icon
from solin.styles.theme import PALETTE, scrollbar_stylesheet
from solin.ui.profile_widgets import AddProfileCard, ProfileCard

_TITLE = QT_TRANSLATE_NOOP("ProfileScreen", "Who is using Solin?")
_NEW_PROFILE = QT_TRANSLATE_NOOP("ProfileScreen", "New Profile")
_CANCEL = QT_TRANSLATE_NOOP("ProfileSwitchOverlay", "Cancel profile switch")
_LOGOUT = QT_TRANSLATE_NOOP("ProfileSelector", "Sign out of profile")
_CURRENT = QT_TRANSLATE_NOOP("ProfileSelector", "Current profile")


class ProfileSelector(QWidget):
    """Render profiles and emit intentions without owning application services."""

    profile_selected = Signal(str)
    create_profile_requested = Signal()
    context_requested = Signal(str, object)
    cancelled = Signal()
    logout_requested = Signal()

    _SPACING = 18
    _MAX_COLUMNS = 6

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current_profile_id: str | None = None,
        allow_management: bool = False,
        allow_cancel: bool = False,
        allow_logout: bool = False,
    ) -> None:
        super().__init__(parent)
        self._current_id = current_profile_id
        self._allow_management = allow_management
        self._cards: list[ProfileCard] = []
        self._columns = 0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {PALETTE.bg0}; color: {PALETTE.text_primary};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget(self)
        header.setObjectName("profileSelectorHeader")
        header.setFixedHeight(72)
        header.setStyleSheet(
            f"QWidget#profileSelectorHeader {{ background: {PALETTE.surface};"
            f" border-bottom: 1px solid {PALETTE.border_muted}; }}"
        )
        self._header_layout = QHBoxLayout(header)
        self._header_layout.setContentsMargins(32, 0, 24, 0)
        self._header_layout.setSpacing(8)
        logo = QLabel("Solin", header)
        logo.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 22px; font-weight: 700;"
            " background: transparent;"
        )
        self._header_layout.addWidget(logo)
        self._header_layout.addStretch()
        self._logout_button = self._header_button(ICON_LOG_OUT) if allow_logout else None
        self._close_button = self._header_button(ICON_OVERLAY_CLOSE) if allow_cancel else None
        if self._logout_button is not None:
            self._logout_button.clicked.connect(self.logout_requested)
        if self._close_button is not None:
            self._close_button.clicked.connect(self.cancelled)
        root.addWidget(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setStyleSheet(scrollbar_stylesheet())
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._body_layout.setSpacing(0)
        self._title = QLabel(self._body)
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._title.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 28px; font-weight: 700;"
            " background: transparent;"
        )
        self._body_layout.addWidget(self._title)
        self._body_layout.addSpacing(32)
        self._grid = QWidget(self._body)
        self._grid_layout = QGridLayout(self._grid)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setHorizontalSpacing(self._SPACING)
        self._grid_layout.setVerticalSpacing(self._SPACING)
        self._body_layout.addWidget(self._grid, 0, Qt.AlignmentFlag.AlignHCenter)
        self._body_layout.addStretch(1)
        self._scroll.setWidget(self._body)
        # setWidget enables palette filling; this surface is painted by the
        # stylesheet, including when an ancestor applies an opacity effect.
        self._body.setAutoFillBackground(False)
        root.addWidget(self._scroll, 1)
        self._scroll.viewport().installEventFilter(self)
        self._add_card = AddProfileCard("", self._grid)
        self._add_card.clicked.connect(self.create_profile_requested)
        self._add_card.installEventFilter(self)
        self.retranslateUi()
        self._reflow()

    def _header_button(self, icon: str) -> QToolButton:
        button = QToolButton(self)
        button.setFixedSize(40, 40)
        button.setIcon(make_icon(icon, 22, PALETTE.text_primary))
        button.setIconSize(QSize(22, 22))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(f"""
            QToolButton {{ background: transparent; border: 1px solid transparent;
                           border-radius: 10px; }}
            QToolButton:hover {{ background: {PALETTE.surface_hover_strong}; }}
            QToolButton:pressed {{ background: {PALETTE.bg2}; }}
            QToolButton:focus {{ border-color: {PALETTE.accent}; }}
        """)
        self._header_layout.addWidget(button)
        return button

    def set_profiles(self, profiles: Sequence[ProfileInfo]) -> None:
        focused = QApplication.focusWidget()
        focused_id = focused.profile.id if isinstance(focused, ProfileCard) else None
        add_focused = focused is self._add_card
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)
        for card in self._cards:
            card.hide()
            card.deleteLater()
        self._cards = []
        for profile in profiles:
            card = ProfileCard(
                profile,
                self._grid,
                is_current=profile.id == self._current_id,
                allow_management=self._allow_management,
            )
            card.clicked.connect(partial(self._select_profile, profile.id))
            card.context_requested.connect(self.context_requested)
            card.installEventFilter(self)
            card.show()
            self._cards.append(card)
        self._columns = 0
        self.retranslateUi()
        self._reflow()
        buttons = [*self._cards, self._add_card]
        for previous, following in zip(buttons, buttons[1:], strict=False):
            QWidget.setTabOrder(previous, following)
        if focused_id is not None:
            target = next((card for card in self._cards if card.profile.id == focused_id), None)
            (target or self._add_card).setFocus()
        elif add_focused:
            self._add_card.setFocus()

    def _select_profile(self, profile_id: str, _checked: bool = False) -> None:
        if profile_id == self._current_id:
            self.cancelled.emit()
        else:
            self.profile_selected.emit(profile_id)

    def focus_initial_card(self) -> None:
        current = next(
            (card for card in self._cards if card.profile.id == self._current_id),
            self._cards[0] if self._cards else self._add_card,
        )
        current.setFocus(Qt.FocusReason.OtherFocusReason)
        self._scroll.ensureWidgetVisible(current, 12, 12)

    def focusNextPrevChild(self, next: bool) -> bool:
        buttons = [
            button
            for button in (
                self._logout_button,
                self._close_button,
                *self._cards,
                self._add_card,
            )
            if button is not None and button.isVisible() and button.isEnabled()
        ]
        if not buttons:
            return False
        focused = QApplication.focusWidget()
        index = buttons.index(focused) if focused in buttons else (-1 if next else 0)
        buttons[(index + (1 if next else -1)) % len(buttons)].setFocus(
            Qt.FocusReason.TabFocusReason if next else Qt.FocusReason.BacktabFocusReason,
        )
        return True

    def _reflow(self) -> None:
        width = self._scroll.viewport().width()
        margin = 40 if width >= 800 else 24
        top = 48 if self._scroll.viewport().height() >= 520 else 24
        self._body_layout.setContentsMargins(margin, top, margin, 32)
        self._header_layout.setContentsMargins(margin, 0, 24, 0)
        buttons = [*self._cards, self._add_card]
        card_width = self._add_card.width()
        columns = min(
            self._MAX_COLUMNS,
            len(buttons),
            max(1, (width - margin * 2 + self._SPACING) // (card_width + self._SPACING)),
        )
        if columns != self._columns:
            while self._grid_layout.count():
                self._grid_layout.takeAt(0)
            for index, card in enumerate(buttons):
                self._grid_layout.addWidget(card, index // columns, index % columns)
            rows = (len(buttons) + columns - 1) // columns
            self._grid.setFixedSize(
                columns * card_width + (columns - 1) * self._SPACING,
                rows * self._add_card.height() + (rows - 1) * self._SPACING,
            )
            self._columns = columns
        self._body_layout.activate()
        self._grid_layout.activate()
        focused = QApplication.focusWidget()
        if focused in buttons:
            self._scroll.ensureWidgetVisible(focused, 12, 12)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._reflow()
        elif event.type() == QEvent.Type.FocusIn:
            self._scroll.ensureWidgetVisible(watched, 12, 12)
        return super().eventFilter(watched, event)

    def retranslateUi(self) -> None:
        translate = QCoreApplication.translate
        self._title.setText(translate("ProfileScreen", _TITLE))
        self._add_card.set_label(translate("ProfileScreen", _NEW_PROFILE))
        for card in self._cards:
            card.setAccessibleDescription(
                translate("ProfileSelector", _CURRENT)
                if card.profile.id == self._current_id
                else ""
            )
        for button, context, source in (
            (self._logout_button, "ProfileSelector", _LOGOUT),
            (self._close_button, "ProfileSwitchOverlay", _CANCEL),
        ):
            if button is not None:
                text = translate(context, source)
                button.setToolTip(text)
                button.setAccessibleName(text)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)
