"""Compact header control that attaches a floating popup to the window bottom."""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QToolButton, QWidget

from solin.styles.icons import ICON_PANEL_BOTTOM, make_icon
from solin.styles.theme import PALETTE, qss_rgba
from solin.ui.themed_tooltip import install_themed_tooltip


class PopupDockButton(QToolButton):
    """Toggles a popup between floating and docked to the main window's bottom.

    Sits beside :class:`~solin.widgets.common.popup_hover_button.PopupHoverButton`
    in a popup header and matches its metrics, so the two read as one control
    group. Checked means "attached"; the host owns what that does.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PopupDockButton")
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setFixedSize(28, 28)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        install_themed_tooltip(self)
        self.toggled.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        self.setAccessibleName(self.tr("Attach to the window"))
        description = (
            self.tr("Attached to the bottom of the window. Click to detach.")
            if self.isChecked()
            else self.tr("Attach this panel to the bottom of the window.")
        )
        self.setToolTip(description)
        self.setAccessibleDescription(description)
        self.apply_theme()

    def apply_theme(self) -> None:
        color = PALETTE.accent_text if self.isChecked() else PALETTE.text_muted
        self.setIcon(make_icon(ICON_PANEL_BOTTOM, 16, color))
        self.setStyleSheet(
            "QToolButton#PopupDockButton { background:transparent; border:1px solid transparent;"
            " border-radius:6px; padding:0; }"
            f"QToolButton#PopupDockButton:hover {{ background:{PALETTE.surface_hover}; }}"
            "QToolButton#PopupDockButton:checked {"
            f" background:{qss_rgba(PALETTE.accent, 0.10)};"
            f" border-color:{qss_rgba(PALETTE.accent, 0.28)}; }}"
            "QToolButton#PopupDockButton:checked:hover {"
            f" background:{qss_rgba(PALETTE.accent, 0.16)}; }}"
            f"QToolButton#PopupDockButton:pressed {{ background:{PALETTE.surface_hover_strong}; }}"
            f"QToolButton#PopupDockButton:focus {{ border-color:{PALETTE.accent_alt}; }}"
        )

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh()
        super().changeEvent(event)
