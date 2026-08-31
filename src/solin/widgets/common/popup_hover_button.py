"""Compact, opt-in header control for a popup's hover preference."""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QToolButton, QWidget

from solin.styles.icons import ICON_CURSOR_HOVER, make_icon
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip


class PopupHoverButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PopupHoverButton")
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setFixedSize(28, 28)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Only a host that binds a preference exposes this control.
        self.hide()
        install_themed_tooltip(self)
        self.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.setAccessibleName(self.tr("Open on hover"))
        description = (
            self.tr("Opens when you hover over its toolbar icon. Click to disable.")
            if self.isChecked()
            else self.tr("Open this panel by hovering over its toolbar icon. Click to enable.")
        )
        self.setToolTip(description)
        self.setAccessibleDescription(description)
        self.apply_theme()

    def apply_theme(self) -> None:
        color = PALETTE.accent_text if self.isChecked() else PALETTE.text_muted
        self.setIcon(make_icon(ICON_CURSOR_HOVER, 16, color))
        self.setStyleSheet(
            "QToolButton#PopupHoverButton { background:transparent; border:1px solid transparent;"
            " border-radius:6px; padding:0; }"
            f"QToolButton#PopupHoverButton:hover {{ background:{PALETTE.surface_hover}; }}"
            "QToolButton#PopupHoverButton:checked {"
            f" background:{qss_rgba(PALETTE.accent, 0.10)};"
            f" border-color:{qss_rgba(PALETTE.accent, 0.28)}; }}"
            "QToolButton#PopupHoverButton:checked:hover {"
            f" background:{qss_rgba(PALETTE.accent, 0.16)}; }}"
            f"QToolButton#PopupHoverButton:pressed {{ background:{PALETTE.surface_hover_strong}; }}"
            f"QToolButton#PopupHoverButton:focus {{ border-color:{PALETTE.accent_alt}; }}"
        )

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh()
        super().changeEvent(event)
