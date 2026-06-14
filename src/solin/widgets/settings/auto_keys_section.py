from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...core.integrations.automation.shortcuts import (
    AutoKeyAction,
    event_label,
)
from ...styles.icons import (
    ICON_EDIT,
    ICON_KEYBOARD,
    ICON_PLUS,
    ICON_TRASH,
    make_icon,
)
from ._shared import (
    _ACCENT,
    _BG,
    _BORDER,
    _BORDER2,
    _DIM,
    _GREEN,
    _MUTED,
    _PICKER_OK_SS,
    _RED,
    _SURF,
    _TEXT,
    _ToggleSwitch,
)
from .auto_key_dialog import _AutoKeyEditorDialog


class AutoKeysSectionMixin:
    """Builds and manages the Automatic Shortcuts settings section."""

    def _build_auto_keys_card(self):
        card, layout = self._card()

        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(make_icon(ICON_KEYBOARD, size=18, color=_MUTED).pixmap(18, 18))
        icon.setFixedSize(20, 20)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self._auto_keys_header_lbl = QLabel(self.tr("Automatic Shortcuts"))
        self._auto_keys_header_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        text_col.addWidget(self._auto_keys_header_lbl)
        self._auto_keys_header_desc = QLabel(
            self.tr("Sends keyboard shortcuts when visual media changes state.")
        )
        self._auto_keys_header_desc.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        self._auto_keys_header_desc.setWordWrap(True)
        text_col.addWidget(self._auto_keys_header_desc)
        header_layout.addLayout(text_col, stretch=1)

        enabled = self._auto_key_settings.is_enabled()
        self._auto_keys_toggle = _ToggleSwitch(checked=enabled)
        self._auto_keys_toggle.toggled.connect(self._on_auto_keys_toggled)
        header_layout.addWidget(self._auto_keys_toggle)
        layout.addWidget(header)

        self._auto_keys_container = QFrame()
        self._auto_keys_container.setStyleSheet(
            f"background: {_BG}; border: none; border-top: 1px solid {_BORDER};"
        )
        container_layout = QVBoxLayout(self._auto_keys_container)
        container_layout.setContentsMargins(14, 12, 14, 12)
        container_layout.setSpacing(10)

        self._auto_keys_hint_lbl = QLabel(
            self.tr("Create one or more shortcuts for start, end, pause and resume events.")
        )
        self._auto_keys_hint_lbl.setStyleSheet(
            f"font-size: 11px; color: {_MUTED}; background: transparent; border: none;"
        )
        self._auto_keys_hint_lbl.setWordWrap(True)
        container_layout.addWidget(self._auto_keys_hint_lbl)

        self._auto_keys_list = QVBoxLayout()
        self._auto_keys_list.setSpacing(8)
        container_layout.addLayout(self._auto_keys_list)

        self._auto_keys_empty_lbl = QLabel(self.tr("No shortcuts configured."))
        self._auto_keys_empty_lbl.setStyleSheet(
            f"font-size: 12px; color: {_DIM}; background: transparent; border: none;"
        )
        container_layout.addWidget(self._auto_keys_empty_lbl)

        add_row = QHBoxLayout()
        add_row.addStretch()
        self._auto_keys_add_btn = QPushButton(self.tr("Add shortcut"))
        self._auto_keys_add_btn.setIcon(make_icon(ICON_PLUS, 14, "#ffffff"))
        self._auto_keys_add_btn.setMinimumHeight(34)
        self._auto_keys_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_keys_add_btn.setStyleSheet(_PICKER_OK_SS)
        self._auto_keys_add_btn.clicked.connect(self._add_auto_key_action)
        add_row.addWidget(self._auto_keys_add_btn)
        container_layout.addLayout(add_row)

        self._auto_keys_expanded = enabled
        self._auto_keys_anim: QPropertyAnimation | None = None
        self._auto_keys_container.setMaximumHeight(0 if not enabled else 16777215)
        layout.addWidget(self._auto_keys_container)
        if enabled:
            self._auto_keys_container.setMaximumHeight(16777215)

        self._refresh_auto_keys_list()
        return card

    def _on_auto_keys_toggled(self, checked: bool):
        self._auto_key_settings.set_enabled(checked)
        self._auto_keys_expanded = checked
        if self._auto_keys_anim is not None:
            self._auto_keys_anim.stop()
            self._auto_keys_anim.deleteLater()
            self._auto_keys_anim = None
        anim = QPropertyAnimation(self._auto_keys_container, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        current_h = self._auto_keys_container.maximumHeight()
        if checked:
            self._auto_keys_container.setMaximumHeight(16777215)
            target_h = self._auto_keys_container.sizeHint().height()
            self._auto_keys_container.setMaximumHeight(current_h)
            anim.setStartValue(current_h)
            anim.setEndValue(target_h)
            anim.finished.connect(lambda: self._auto_keys_container.setMaximumHeight(16777215))
        else:
            anim.setStartValue(
                current_h if current_h < 16777215 else self._auto_keys_container.height()
            )
            anim.setEndValue(0)
        anim.start()
        self._auto_keys_anim = anim

    def _event_label(self, event: str) -> str:
        return event_label(event)

    def _auto_key_actions(self) -> list[AutoKeyAction]:
        return self._auto_key_settings.actions()

    def _save_auto_key_actions(self, actions: list[AutoKeyAction]) -> None:
        self._auto_key_settings.save_actions(actions)
        self._refresh_auto_keys_list()

    def _clear_auto_keys_list(self):
        while self._auto_keys_list.count():
            item = self._auto_keys_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_auto_keys_list(self):
        self._clear_auto_keys_list()
        actions = self._auto_key_actions()
        self._auto_keys_empty_lbl.setVisible(not actions)
        for action in actions:
            self._auto_keys_list.addWidget(self._make_auto_key_row(action))

    def _make_auto_key_row(self, action: AutoKeyAction) -> QFrame:
        row = QFrame()
        row.setStyleSheet(
            f"background: {_SURF}; border: 1px solid {_BORDER};"
            " border-radius: 8px;"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(10)

        icon = QLabel()
        icon.setPixmap(make_icon(ICON_KEYBOARD, 16, _MUTED).pixmap(16, 16))
        icon.setFixedSize(18, 18)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        event_label_widget = QLabel(self._event_label(action.event))
        event_label_widget.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        text_col.addWidget(event_label_widget)
        sequence_label = QLabel(action.sequence)
        sequence_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {_MUTED};"
            " background: transparent; border: none; padding: 0;"
        )
        text_col.addWidget(sequence_label, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(text_col, stretch=1)

        status = QLabel()
        status.setFixedSize(8, 8)
        status.setToolTip(self.tr("Enabled") if action.enabled else self.tr("Disabled"))
        status.setStyleSheet(
            f"background-color: {_GREEN if action.enabled else _BORDER2};"
            " border: none; border-radius: 4px;"
        )
        layout.addWidget(status)

        edit_btn = self._auto_key_icon_button(ICON_EDIT, self.tr("Edit"))
        edit_btn.clicked.connect(
            lambda _=False, action_id=action.id: self._edit_auto_key_action(action_id)
        )
        layout.addWidget(edit_btn)

        delete_btn = self._auto_key_icon_button(ICON_TRASH, self.tr("Delete"), danger=True)
        delete_btn.clicked.connect(
            lambda _=False, action_id=action.id: self._delete_auto_key_action(action_id)
        )
        layout.addWidget(delete_btn)
        return row

    def _auto_key_icon_button(self, icon_svg, tooltip: str, danger: bool = False) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(28, 28)
        button.setIcon(make_icon(icon_svg, 14, _RED if danger else _MUTED))
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ background-color: {_BG}; border: 1px solid {_BORDER2};"
            " border-radius: 6px; padding: 0px; }"
            f" QPushButton:hover {{ border-color: {_RED if danger else _ACCENT}; }}"
        )
        return button

    def _add_auto_key_action(self):
        dialog = _AutoKeyEditorDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_action:
            actions = self._auto_key_actions()
            actions.append(dialog.result_action)
            self._save_auto_key_actions(actions)

    def _edit_auto_key_action(self, action_id: str):
        actions = self._auto_key_actions()
        action = next((candidate for candidate in actions if candidate.id == action_id), None)
        if action is None:
            return
        dialog = _AutoKeyEditorDialog(self, action)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_action:
            updated = [
                dialog.result_action if candidate.id == action_id else candidate
                for candidate in actions
            ]
            self._save_auto_key_actions(updated)

    def _delete_auto_key_action(self, action_id: str):
        actions = [action for action in self._auto_key_actions() if action.id != action_id]
        self._save_auto_key_actions(actions)
