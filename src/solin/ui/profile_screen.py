"""Profile selection, migration, and QML onboarding shell."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QT_TRANSLATE_NOOP, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from solin.core.onboarding.application import OnboardingService
from solin.core.profiles.application import ProfileService
from solin.core.profiles.settings import ProfileSettings
from solin.styles.icons import ICON_EDIT, ICON_PACKAGE, ICON_TRASH, make_icon
from solin.styles.theme import PALETTE
from solin.ui.helpers import fade_in
from solin.ui.profile_widgets import (
    AddProfileCard,
    PROFILE_ACCENT,
    PROFILE_BG,
    PROFILE_BORDER,
    PROFILE_DANGER,
    PROFILE_MUTED,
    PROFILE_SURFACE,
    PROFILE_TEXT,
    ProfileCard,
    ProfileFlowLayout,
    ProfileNameDialog,
    profile_button,
    profile_field,
    profile_label,
)
from solin.ui.qml.onboarding import OnboardingQmlHost


class ProfileScreen(QWidget):
    """Display profile management and host the profile onboarding flow."""

    profile_ready = Signal(str)

    def __init__(
        self,
        lang_manager=None,
        *,
        profile_service: ProfileService,
        profile_settings_for: Callable[[str], ProfileSettings],
        onboarding_service: OnboardingService,
        obs_probe,
        target_picker_factory: Callable[..., object] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang_manager
        self._profiles = profile_service
        self._profile_settings_for = profile_settings_for
        self._creating_additional_profile = False
        self._tr_labels: list[tuple[QLabel, str]] = []
        self._tr_buttons: list[tuple[QWidget, str]] = []
        self._tr_placeholders: list[tuple[QLineEdit, str]] = []

        self.setStyleSheet(f"background: {PROFILE_BG}; color: {PROFILE_TEXT};")
        self.setMinimumSize(800, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._page_selector = self._build_selector_page()
        self._onboarding_host = OnboardingQmlHost(
            language_manager=lang_manager,
            onboarding_service=onboarding_service,
            obs_probe=obs_probe,
            target_picker_factory=target_picker_factory,
            parent=self,
        )
        self._onboarding_host.completed.connect(self._on_onboarding_completed)
        self._onboarding_host.cancelled.connect(self._cancel_onboarding)
        self._page_migration = self._build_migration_page()
        self._stack.addWidget(self._page_selector)
        self._stack.addWidget(self._onboarding_host)
        self._stack.addWidget(self._page_migration)

        if self._lang is not None:
            self._lang.language_changed.connect(lambda _code: self.retranslateUi())

    def _tr_label(self, label: QLabel, source: str) -> QLabel:
        label.setText(self.tr(source))
        self._tr_labels.append((label, source))
        return label

    def _tr_button(self, button: QWidget, source: str) -> QWidget:
        button.setText(self.tr(source))
        self._tr_buttons.append((button, source))
        return button

    def _tr_placeholder(self, field: QLineEdit, source: str) -> QLineEdit:
        field.setPlaceholderText(self.tr(source))
        self._tr_placeholders.append((field, source))
        return field

    def _build_selector_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {PROFILE_BG};")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(72)
        header.setStyleSheet(
            f"background: {PROFILE_SURFACE};"
            f" border-bottom: 1px solid {PALETTE.border_muted};"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(32, 0, 32, 0)
        logo = QLabel("Solin")
        logo.setStyleSheet(
            f"color: {PROFILE_TEXT}; font-size: 22px; font-weight: 700;"
            " background: transparent;"
        )
        header_layout.addWidget(logo)
        header_layout.addStretch()
        layout.addWidget(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        body_layout.setContentsMargins(40, 60, 40, 40)
        body_layout.setSpacing(0)
        self._selector_title = self._tr_label(
            profile_label("", 28, PROFILE_TEXT, 700, Qt.AlignmentFlag.AlignHCenter),
            QT_TRANSLATE_NOOP("ProfileScreen", "Who is using Solin?"),
        )
        body_layout.addWidget(self._selector_title)
        body_layout.addSpacing(40)
        self._selector_grid = QWidget()
        self._grid_lay = ProfileFlowLayout(
            self._selector_grid,
            h_spacing=18,
            v_spacing=18,
        )
        body_layout.addWidget(self._selector_grid, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(body, 1)
        return page

    def _populate_selector(self) -> None:
        while self._grid_lay.count():
            item = self._grid_lay.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        for profile in self._profiles.profiles:
            card = ProfileCard(profile)
            card.clicked.connect(self._on_profile_selected)
            card.context_requested.connect(self._show_profile_context_menu)
            self._grid_lay.addWidget(card)
        self._selector_add_card = AddProfileCard(self.tr("New Profile"))
        self._selector_add_card.clicked.connect(self.start_new_profile)
        self._grid_lay.addWidget(self._selector_add_card)

    def _build_migration_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {PROFILE_BG};")
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(60, 60, 60, 60)

        card = QFrame()
        card.setMaximumWidth(480)
        card.setStyleSheet(
            "QFrame {"
            f" background: {PROFILE_SURFACE}; border: 1px solid {PALETTE.border_muted};"
            " border-radius: 14px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 36, 36, 36)
        card_layout.setSpacing(16)

        icon_label = QLabel()
        icon_label.setFixedSize(52, 52)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet(
            f"background: {PALETTE.surface_card};"
            f" border: 1px solid {PALETTE.border_muted}; border-radius: 14px;"
        )
        icon_label.setPixmap(
            make_icon(ICON_PACKAGE, size=24, color=PROFILE_ACCENT).pixmap(24, 24)
        )
        icon_row = QHBoxLayout()
        icon_row.addStretch()
        icon_row.addWidget(icon_label)
        icon_row.addStretch()
        card_layout.addLayout(icon_row)
        card_layout.addWidget(
            self._tr_label(
                profile_label(
                    "",
                    18,
                    PROFILE_TEXT,
                    700,
                    Qt.AlignmentFlag.AlignHCenter,
                ),
                QT_TRANSLATE_NOOP("ProfileScreen", "Existing settings found"),
            )
        )
        card_layout.addWidget(
            self._tr_label(
                profile_label(
                    "",
                    13,
                    PROFILE_MUTED,
                    400,
                    Qt.AlignmentFlag.AlignHCenter,
                ),
                QT_TRANSLATE_NOOP(
                    "ProfileScreen",
                    "Solin found settings and playlists from a previous version. "
                    "Name this profile to continue with your data:",
                ),
            )
        )
        card_layout.addSpacing(8)
        card_layout.addWidget(
            self._tr_label(
                profile_label("", 12, PROFILE_MUTED, 500),
                QT_TRANSLATE_NOOP("ProfileScreen", "Profile name"),
            )
        )
        self._mig_name_field = profile_field()
        self._tr_placeholder(
            self._mig_name_field,
            QT_TRANSLATE_NOOP("ProfileScreen", "Example: Central Congregation"),
        )
        card_layout.addWidget(self._mig_name_field)
        self._mig_error = profile_label("", 12, PROFILE_DANGER)
        self._mig_error.hide()
        card_layout.addWidget(self._mig_error)
        confirm_button = profile_button("")
        self._tr_button(
            confirm_button,
            QT_TRANSLATE_NOOP("ProfileScreen", "Confirm and migrate data"),
        )
        confirm_button.clicked.connect(self._on_migrate_confirm)
        self._mig_name_field.returnPressed.connect(self._on_migrate_confirm)
        card_layout.addWidget(confirm_button)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    def start(self) -> None:
        if not self._profiles.has_profiles():
            if self._profiles.has_legacy_settings():
                self._stack.setCurrentWidget(self._page_migration)
                fade_in(self)
                return
            self._start_onboarding(self.tr("Profile 1"), allow_cancel=False)
            return
        if len(self._profiles.profiles) == 1:
            self._activate_and_emit(self._profiles.profiles[0].id)
            return
        self._populate_selector()
        self._stack.setCurrentWidget(self._page_selector)
        fade_in(self)

    def start_new_profile(self) -> None:
        if not self._profiles.has_profiles():
            self.start()
            return
        self._populate_selector()
        profile_number = len(self._profiles.profiles) + 1
        profile_name = self.tr("Profile {n}").replace("{n}", str(profile_number))
        self._start_onboarding(profile_name, allow_cancel=True)

    def _start_onboarding(self, profile_name: str, *, allow_cancel: bool) -> None:
        self._creating_additional_profile = allow_cancel
        self._onboarding_host.start(profile_name, allow_cancel=allow_cancel)
        self._stack.setCurrentWidget(self._onboarding_host)
        QTimer.singleShot(80, self._onboarding_host.setFocus)

    def _cancel_onboarding(self) -> None:
        if not self._creating_additional_profile:
            return
        self._creating_additional_profile = False
        self._populate_selector()
        self._stack.setCurrentWidget(self._page_selector)

    def _on_onboarding_completed(self, profile_id: str) -> None:
        self._creating_additional_profile = False
        if self._lang is not None:
            self._lang.activate_profile(self._profile_settings_for(profile_id))
        self.profile_ready.emit(profile_id)

    def _on_profile_selected(self, profile_id: str) -> None:
        self._activate_and_emit(profile_id)

    def _activate_and_emit(self, profile_id: str) -> None:
        self._profiles.set_active(profile_id)
        if self._lang is not None:
            self._lang.activate_profile(self._profile_settings_for(profile_id))
        self.profile_ready.emit(profile_id)

    def _show_profile_context_menu(self, profile_id: str, global_pos) -> None:
        profile = self._profiles.get_profile(profile_id)
        if profile is None:
            return
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {PROFILE_SURFACE};
                color: {PROFILE_TEXT};
                border: 1px solid {PROFILE_BORDER};
                padding: 6px;
                font-size: 13px;
            }}
            QMenu::item {{
                padding: 8px 18px 8px 12px;
            }}
            QMenu::item:selected {{
                background: {PALETTE.bg2};
                color: {PROFILE_TEXT};
            }}
            QMenu::separator {{
                height: 1px;
                background: {PROFILE_BORDER};
                margin: 6px 8px;
            }}
        """)
        rename_action = QAction(
            make_icon(ICON_EDIT, 15, PROFILE_MUTED),
            self.tr("Rename"),
            menu,
        )
        delete_action = QAction(
            make_icon(ICON_TRASH, 15, PROFILE_DANGER),
            self.tr("Delete"),
            menu,
        )
        delete_action.setEnabled(len(self._profiles.profiles) > 1)
        menu.addAction(rename_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        action = menu.exec(global_pos)
        if action is rename_action:
            self._rename_profile(profile_id)
        elif action is delete_action:
            self._delete_profile(profile_id)

    def _rename_profile(self, profile_id: str) -> None:
        profile = self._profiles.get_profile(profile_id)
        if profile is None:
            return
        dialog = ProfileNameDialog(
            current=profile.name,
            parent=self,
            title=self.tr("Rename profile"),
            label=self.tr("Profile name"),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_name():
            self._profiles.rename_profile(profile_id, dialog.result_name())
            self._populate_selector()

    def _delete_profile(self, profile_id: str) -> None:
        profile = self._profiles.get_profile(profile_id)
        if profile is None or len(self._profiles.profiles) <= 1:
            return
        message = QMessageBox(self)
        message.setWindowTitle(self.tr("Delete profile"))
        message.setText(self.tr('Delete "{name}"?').replace("{name}", profile.name))
        message.setInformativeText(
            self.tr(
                "This will delete playlists, images, received media, browser cache, "
                "and settings for this profile. This action cannot be undone."
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        message.setDefaultButton(QMessageBox.StandardButton.No)
        if message.exec() == QMessageBox.StandardButton.Yes:
            if self._profiles.delete_profile(profile_id):
                self._populate_selector()

    def _on_migrate_confirm(self) -> None:
        name = self._mig_name_field.text().strip()
        if not name:
            self._mig_error.setText(self.tr("Please enter a profile name."))
            self._mig_error.show()
            return
        self._mig_error.hide()
        profile = self._profiles.migrate_legacy(name)
        self._activate_and_emit(profile.id)

    def retranslateUi(self) -> None:
        self.setWindowTitle(self.tr("Solin"))
        for label, source in self._tr_labels:
            label.setText(self.tr(source))
        for button, source in self._tr_buttons:
            button.setText(self.tr(source))
        for field, source in self._tr_placeholders:
            field.setPlaceholderText(self.tr(source))
        if hasattr(self, "_selector_add_card"):
            self._selector_add_card.set_label(self.tr("New Profile"))

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key.Key_Escape
            and self._stack.currentWidget() is self._onboarding_host
            and self._creating_additional_profile
        ):
            self._onboarding_host.bridge.cancel()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._onboarding_host.shutdown()
        super().closeEvent(event)


__all__ = ["ProfileScreen"]
