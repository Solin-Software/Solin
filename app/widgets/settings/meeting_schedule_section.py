from __future__ import annotations

from PySide6.QtCore import QTime
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTimeEdit, QVBoxLayout

from ...core.foundation.settings_keys import SettingsKey
from ...core.meetings.schedule import (
    DEFAULT_MIDWEEK_TIME,
    DEFAULT_WEEKEND_TIME,
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    load_meeting_schedule,
    parse_time_text,
)
from ...styles.icons import ICON_CALENDAR, make_icon
from ..common import NoScrollComboBox as _NoScrollComboBox
from ._shared import _BORDER2, _DIM, _MUTED, _SURF, _TEXT

_DAY_DATA = tuple(range(7))


class MeetingScheduleSectionMixin:
    """Builds meeting day/time settings shared by meeting-aware features."""

    def _build_meeting_schedule_card(self):
        card, lay = self._card()
        self._schedule_rows: dict[str, tuple[_NoScrollComboBox, QTimeEdit]] = {}

        self._schedule_hint_lbl = QLabel(self.tr(
            "Used by automatic features that depend on the meeting start time."
        ))
        self._schedule_hint_lbl.setWordWrap(True)
        self._schedule_hint_lbl.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
            " padding: 12px 14px 2px 14px;"
        )
        lay.addWidget(self._schedule_hint_lbl)

        lay.addWidget(self._schedule_row(
            MIDWEEK,
            self.tr("Midweek meeting"),
            self.tr("Day and time for the midweek meeting."),
            SettingsKey.MEETING_MIDWEEK_DAY,
            SettingsKey.MEETING_MIDWEEK_TIME,
            DEFAULT_MIDWEEK_TIME,
        ))
        lay.addWidget(self._divider())
        lay.addWidget(self._schedule_row(
            WEEKEND,
            self.tr("Weekend meeting"),
            self.tr("Day and time for the weekend meeting."),
            SettingsKey.MEETING_WEEKEND_DAY,
            SettingsKey.MEETING_WEEKEND_TIME,
            DEFAULT_WEEKEND_TIME,
        ))
        return card

    def _schedule_row(
        self,
        kind: str,
        title: str,
        desc: str,
        day_key: str,
        time_key: str,
        default_time: str,
    ) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(make_icon(ICON_CALENDAR, size=18, color=_MUTED).pixmap(18, 18))
        icon_label.setFixedSize(20, 20)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        text_col.addWidget(title_label)
        text_col.addWidget(desc_label)
        row_layout.addLayout(text_col, stretch=1)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        day_combo = _NoScrollComboBox()
        day_combo.setMinimumHeight(34)
        day_combo.setFixedWidth(148)
        day_combo.setStyleSheet(self._schedule_combo_style())
        self._populate_day_combo(day_combo)

        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        time_edit.setMinimumHeight(34)
        time_edit.setFixedWidth(82)
        time_edit.setStyleSheet(self._schedule_time_style())

        saved_day = self._prefs.value(day_key, UNCONFIGURED_WEEKDAY)
        saved_time = self._prefs.value(time_key, "", str)
        self._apply_schedule_controls(day_combo, time_edit, saved_day, saved_time, default_time)

        def on_changed() -> None:
            self._on_schedule_control_changed(kind, day_key, time_key, default_time)

        day_combo.currentIndexChanged.connect(lambda _idx: on_changed())
        time_edit.timeChanged.connect(lambda _time: on_changed())

        controls.addWidget(day_combo)
        controls.addWidget(time_edit)
        row_layout.addLayout(controls)

        setattr(self, f"_{kind}_schedule_title_lbl", title_label)
        setattr(self, f"_{kind}_schedule_desc_lbl", desc_label)
        self._schedule_rows[kind] = (day_combo, time_edit)
        return row

    def _apply_schedule_controls(
        self,
        day_combo: _NoScrollComboBox,
        time_edit: QTimeEdit,
        saved_day,
        saved_time: str,
        default_time: str,
    ) -> None:
        day = int(saved_day) if str(saved_day).lstrip("-").isdigit() else UNCONFIGURED_WEEKDAY
        index = day_combo.findData(day if 0 <= day <= 6 else UNCONFIGURED_WEEKDAY)
        day_combo.setCurrentIndex(max(0, index))

        minutes = parse_time_text(saved_time)
        if minutes is None:
            minutes = parse_time_text(default_time)
        if minutes is None:
            minutes = 0
        time_edit.setTime(QTime(minutes // 60, minutes % 60))
        time_edit.setEnabled(0 <= day <= 6)

    def _on_schedule_control_changed(
        self,
        kind: str,
        day_key: str,
        time_key: str,
        default_time: str,
    ) -> None:
        day_combo, time_edit = self._schedule_rows[kind]
        day = day_combo.currentData()
        if not isinstance(day, int):
            day = UNCONFIGURED_WEEKDAY
        configured = 0 <= day <= 6
        time_edit.setEnabled(configured)
        if configured:
            time_text = time_edit.time().toString("HH:mm")
            self._prefs.setValue(day_key, day)
            self._prefs.setValue(time_key, time_text or default_time)
        else:
            self._prefs.setValue(day_key, UNCONFIGURED_WEEKDAY)
            self._prefs.setValue(time_key, "")
        self._sync_background_song_desc()
        self.meeting_schedule_changed.emit()

    def _populate_day_combo(self, combo: _NoScrollComboBox) -> None:
        current = combo.currentData() if combo.count() else UNCONFIGURED_WEEKDAY
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("Not configured"), UNCONFIGURED_WEEKDAY)
        for idx, text in zip(_DAY_DATA, self._weekday_names(), strict=True):
            combo.addItem(text, idx)
        index = combo.findData(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _refresh_meeting_schedule_texts(self) -> None:
        self._schedule_hint_lbl.setText(self.tr(
            "Used by automatic features that depend on the meeting start time."
        ))
        self._midweek_schedule_title_lbl.setText(self.tr("Midweek meeting"))
        self._midweek_schedule_desc_lbl.setText(
            self.tr("Day and time for the midweek meeting.")
        )
        self._weekend_schedule_title_lbl.setText(self.tr("Weekend meeting"))
        self._weekend_schedule_desc_lbl.setText(
            self.tr("Day and time for the weekend meeting.")
        )
        for combo, _time_edit in self._schedule_rows.values():
            self._populate_day_combo(combo)

    def get_meeting_schedule(self):
        return load_meeting_schedule(self._prefs)

    def _meeting_schedule_configured(self) -> bool:
        return self.get_meeting_schedule().has_configured_slot

    def _weekday_names(self) -> tuple[str, ...]:
        return (
            self.tr("Monday"),
            self.tr("Tuesday"),
            self.tr("Wednesday"),
            self.tr("Thursday"),
            self.tr("Friday"),
            self.tr("Saturday"),
            self.tr("Sunday"),
        )

    @staticmethod
    def _schedule_combo_style() -> str:
        return (
            f"QComboBox {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; border-radius: 8px;"
            " padding: 0px 10px; font-size: 12px; }}"
            f"QComboBox:focus {{ border-color: #388bfd; }}"
            "QComboBox::drop-down { border: none; width: 24px; }"
            "QComboBox::down-arrow { image: none; width: 0px; height: 0px;"
            f" border-left: 4px solid transparent; border-right: 4px solid transparent;"
            f" border-top: 5px solid {_MUTED}; margin-right: 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; selection-background-color: #1f3a6e; }}"
        )

    @staticmethod
    def _schedule_time_style() -> str:
        return (
            f"QTimeEdit {{ background: {_SURF}; color: {_TEXT};"
            f" border: 1px solid {_BORDER2}; border-radius: 8px;"
            " padding: 0px 10px; font-size: 12px; }}"
            "QTimeEdit:focus { border-color: #388bfd; }"
            f"QTimeEdit:disabled {{ color: {_DIM}; border-color: #21262d; }}"
            "QTimeEdit::up-button, QTimeEdit::down-button { width: 0px; border: none; }"
        )
