from __future__ import annotations

import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...core.integrations.automation.auto_key_actions import (
    AUTO_KEY_EVENTS,
    AutoKeyAction,
)
from ...ui.auto_key_labels import auto_key_event_label
from ...ui.controls import NoScrollComboBox
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_BG,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DIM,
    SETTINGS_SUCCESS,
    SETTINGS_MUTED,
    SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET,
    SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET,
    SETTINGS_DANGER,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SettingsToggleSwitch,
)


class _ShortcutSequenceEdit(QPushButton):
    keySequenceChanged = Signal(QKeySequence)
    focus_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sequence = QKeySequence()
        self.setMinimumHeight(44)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("recording", False)
        self.setText(self.tr("Click to record"))
        self.clicked.connect(lambda: (self.setFocus(), self._set_recording(True)))

    def setMaximumSequenceLength(self, _value: int):
        pass

    def maximumSequenceLength(self) -> int:
        return 1

    def setKeySequence(self, sequence: QKeySequence):
        self._sequence = sequence
        self._refresh_text()

    def keySequence(self) -> QKeySequence:
        return self._sequence

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._set_recording(True)

    def focusOutEvent(self, event):
        self._set_recording(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_AltGr,
        ):
            event.accept()
            return

        modifiers = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        sequence_value = int(modifiers.value) | int(key)
        self._sequence = QKeySequence(sequence_value)
        self._refresh_text()
        self.keySequenceChanged.emit(self._sequence)
        self._set_recording(False)
        self.clearFocus()
        event.accept()

    def _refresh_text(self):
        text = self._sequence.toString(QKeySequence.SequenceFormat.PortableText)
        self.setText(text if text else self.tr("Click to record"))

    def _set_recording(self, recording: bool):
        if self.property("recording") == recording:
            return
        self.setProperty("recording", recording)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()
        self.focus_changed.emit(recording)


class AutoKeyEditorDialog(QDialog):
    def __init__(
        self,
        parent=None,
        action: AutoKeyAction | None = None,
        *,
        sequence: str = "",
        show_event: bool = True,
        show_enabled: bool = True,
        title: str | None = None,
        hint: str | None = None,
    ):
        super().__init__(parent)
        self._action = action
        self._sequence = sequence
        self._show_event = show_event
        self._show_enabled = show_enabled
        self._title = title or self.tr("Automatic Shortcut")
        self._hint = hint or self.tr("Choose an app event and press the shortcut to send.")
        self.result_action: AutoKeyAction | None = None
        self.result_sequence: str = ""
        self.setWindowTitle(self._title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(540)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )
        self._build()
        self._load_action()
        self._validate()

    def _build(self):
        self.setStyleSheet(
            f"QDialog {{ background: {SETTINGS_SURFACE}; }}"
            f"QLabel {{ color: {SETTINGS_TEXT}; background: transparent; }}"
            f"QPushButton#AutoKeyShortcut {{ background-color: {SETTINGS_BG}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px;"
            f" padding: 0px 14px; font-size: 13px; font-weight: 600;"
            f" text-align: left;"
            f" min-height: 42px; }}"
            f"QPushButton#AutoKeyShortcut:hover {{ border-color: {SETTINGS_MUTED}; }}"
            f"QPushButton#AutoKeyShortcut:focus {{ border-color: {SETTINGS_ACCENT}; }}"
            f"QPushButton#AutoKeyShortcut[recording=\"true\"] {{"
            f" background-color: #0f1a2a; border-color: {SETTINGS_ACCENT}; color: #ffffff; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)

        title = QLabel(self._title)
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT};")
        lay.addWidget(title)

        hint = QLabel(self._hint)
        hint.setStyleSheet(f"font-size: 12px; color: {SETTINGS_MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        if self._show_event:
            event_lbl = QLabel(self.tr("Event"))
            event_lbl.setStyleSheet(
                f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            )
            lay.addWidget(event_lbl)
            self._event_combo = NoScrollComboBox()
            self._event_combo.setMinimumHeight(40)
            self._event_combo.setStyleSheet(self._auto_key_combo_style())
            for event in AUTO_KEY_EVENTS:
                self._event_combo.addItem(auto_key_event_label(event), event)
            lay.addWidget(self._event_combo)
        else:
            self._event_combo = None

        shortcut_lbl = QLabel(self.tr("Shortcut"))
        shortcut_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        lay.addWidget(shortcut_lbl)
        self._shortcut_edit = _ShortcutSequenceEdit()
        self._shortcut_edit.setObjectName("AutoKeyShortcut")
        self._shortcut_edit.keySequenceChanged.connect(self._on_shortcut_changed)
        self._shortcut_edit.focus_changed.connect(self._on_shortcut_focus_changed)
        lay.addWidget(self._shortcut_edit)

        self._shortcut_hint_lbl = QLabel(self.tr("Click the field, then press one shortcut."))
        self._shortcut_hint_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        lay.addWidget(self._shortcut_hint_lbl)

        if self._show_enabled:
            enabled_row = QHBoxLayout()
            enabled_row.setSpacing(10)
            enabled_text = QLabel(self.tr("Enabled"))
            enabled_text.setStyleSheet(f"font-size: 13px; color: {SETTINGS_TEXT};")
            enabled_row.addWidget(enabled_text)
            enabled_row.addStretch()
            self._enabled_toggle = SettingsToggleSwitch(True)
            enabled_row.addWidget(self._enabled_toggle)
            lay.addLayout(enabled_row)
        else:
            self._enabled_toggle = None

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet(f"font-size: 11px; color: {SETTINGS_DANGER};")
        self._error_lbl.setWordWrap(True)
        lay.addWidget(self._error_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setMinimumHeight(34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET)
        cancel.clicked.connect(self.reject)
        self._save_btn = QPushButton(self.tr("Save"))
        self._save_btn.setMinimumHeight(34)
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setStyleSheet(SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET)
        self._save_btn.clicked.connect(self._accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(self._save_btn)
        lay.addLayout(btn_row)

    def _load_action(self):
        if self._action:
            if self._event_combo is not None:
                idx = self._event_combo.findData(self._action.event)
                if idx >= 0:
                    self._event_combo.setCurrentIndex(idx)
            sequence = self._action.sequence
            enabled = self._action.enabled
        else:
            sequence = self._sequence
            enabled = True
        if not sequence:
            return
        self._shortcut_edit.blockSignals(True)
        self._shortcut_edit.setKeySequence(QKeySequence(sequence))
        self._shortcut_edit.blockSignals(False)
        if self._enabled_toggle is not None:
            self._enabled_toggle.set_checked(enabled, animate=False)

    @staticmethod
    def _auto_key_combo_style():
        return (
            f"QComboBox {{ background-color: {SETTINGS_BG}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px;"
            f" padding: 0px 14px; font-size: 13px; min-height: 40px; }}"
            f"QComboBox:hover {{ border-color: {SETTINGS_MUTED}; }}"
            f"QComboBox:focus {{ border-color: {SETTINGS_ACCENT}; }}"
            f"QComboBox::drop-down {{ border: none; width: 30px; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px;"
            f" border-left: 4px solid transparent; border-right: 4px solid transparent;"
            f" border-top: 5px solid {SETTINGS_MUTED}; margin-right: 12px; }}"
            f"QComboBox QAbstractItemView {{ background-color: {SETTINGS_BG}; color: {SETTINGS_TEXT};"
            f" border: 1px solid {SETTINGS_BORDER_STRONG}; selection-background-color: #1f3a6e;"
            f" outline: none; }}"
        )

    def _sequence_text(self) -> str:
        text = self._shortcut_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        ).strip()
        return text.split(",", 1)[0].strip()

    def _validate(self, *_):
        valid = bool(self._sequence_text())
        self._save_btn.setEnabled(valid)
        self._error_lbl.setText("" if valid else self.tr("Press a shortcut before saving."))

    def _on_shortcut_focus_changed(self, recording: bool):
        if recording:
            self._shortcut_hint_lbl.setText(self.tr("Listening... press one shortcut."))
            self._shortcut_hint_lbl.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_ACCENT}; background: transparent; border: none;"
            )
        else:
            self._shortcut_hint_lbl.setText(self.tr("Click the field, then press one shortcut."))
            self._shortcut_hint_lbl.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
            )

    def _on_shortcut_changed(self, *_):
        self._validate()
        if self._sequence_text():
            self._shortcut_hint_lbl.setText(self.tr("Shortcut captured."))
            self._shortcut_hint_lbl.setStyleSheet(
                f"font-size: 11px; color: {SETTINGS_SUCCESS}; background: transparent; border: none;"
            )
            QTimer.singleShot(0, self._shortcut_edit.clearFocus)

    def _accept(self):
        sequence = self._sequence_text()
        if not sequence:
            self._validate()
            return
        if not self._show_event:
            self.result_sequence = sequence
            self.accept()
            return
        self.result_action = AutoKeyAction(
            id=self._action.id if self._action else uuid.uuid4().hex,
            event=self._event_combo.currentData() if self._event_combo is not None else AUTO_KEY_EVENTS[0],
            sequence=sequence,
            enabled=self._enabled_toggle.is_checked if self._enabled_toggle is not None else True,
        )
        self.accept()
