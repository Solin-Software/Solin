from __future__ import annotations

from typing import cast

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.meetings.schedule import (
    DEFAULT_MIDWEEK_TIME,
    DEFAULT_WEEKEND_TIME,
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    parse_time_text,
)
from ...styles.icons import ICON_CALENDAR, ICON_CHEVRON_DOWN, ICON_CHEVRON_UP, make_icon
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_ACCENT_HOVER,
    SETTINGS_ACCENT_PRESSED,
    SETTINGS_BG,
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
)

_DAY_DATA = tuple(range(7))


def _field_button_style() -> str:
    return (
        "QPushButton {"
        f" background: {SETTINGS_SURFACE};"
        f" color: {SETTINGS_TEXT};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 8px;"
        " padding: 0 10px;"
        " font-size: 12px;"
        " font-weight: 500;"
        "}"
        "QPushButton:hover {"
        f" background: {SETTINGS_BORDER};"
        f" border-color: {SETTINGS_ACCENT};"
        "}"
        "QPushButton:pressed {"
        f" background: {SETTINGS_BG};"
        "}"
        "QPushButton:disabled {"
        f" background: {SETTINGS_BG};"
        f" color: {SETTINGS_DIM};"
        f" border-color: {SETTINGS_BORDER};"
        "}"
    )


def _menu_style() -> str:
    return (
        "QMenu {"
        f" background: {SETTINGS_SURFACE};"
        f" color: {SETTINGS_TEXT};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 8px;"
        " padding: 6px;"
        "}"
        "QMenu::item {"
        " padding: 7px 28px 7px 12px;"
        " border-radius: 6px;"
        "}"
        "QMenu::item:selected {"
        f" background: {SETTINGS_BORDER};"
        "}"
        "QMenu::indicator { width: 14px; height: 14px; }"
    )


def _time_popup_card_style() -> str:
    return (
        "QFrame#ScheduleTimePickerCard {"
        f" background: {SETTINGS_SURFACE};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 12px;"
        "}"
    )


def _time_label_style() -> str:
    return f"font-size: 11px; color: {SETTINGS_MUTED}; background: transparent; border: none;"


def _time_preview_style() -> str:
    return (
        f"font-size: 19px; font-weight: 700; color: {SETTINGS_TEXT};"
        " background: transparent; border: none; padding: 0;"
    )


def _stepper_card_style() -> str:
    return (
        "QFrame {"
        f" background: {SETTINGS_BG};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 8px;"
        "}"
    )


def _stepper_value_style() -> str:
    return (
        f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT};"
        " background: transparent; border: none;"
    )


def _stepper_button_style() -> str:
    return (
        "QToolButton {"
        " background: transparent;"
        " border: none;"
        " border-radius: 5px;"
        " padding: 0;"
        "}"
        f"QToolButton:hover {{ background: {SETTINGS_BORDER}; }}"
        f"QToolButton:pressed {{ background: {SETTINGS_BORDER_STRONG}; }}"
    )


def _time_apply_style() -> str:
    return (
        "QPushButton {"
        f" background: {SETTINGS_ACCENT};"
        " color: white;"
        " border: none;"
        " border-radius: 8px;"
        " font-weight: 600;"
        "}"
        f"QPushButton:hover {{ background: {SETTINGS_ACCENT_HOVER}; }}"
        f"QPushButton:pressed {{ background: {SETTINGS_ACCENT_PRESSED}; }}"
    )


def _time_cancel_style() -> str:
    return (
        "QPushButton {"
        f" background: {SETTINGS_BORDER};"
        f" color: {SETTINGS_TEXT};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 8px;"
        "}"
        f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; }}"
    )


class _ScheduleDayButton(QPushButton):
    dayChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = UNCONFIGURED_WEEKDAY
        self._options: list[tuple[str, int]] = []
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setFixedWidth(154)
        self.setStyleSheet(_field_button_style())
        self.clicked.connect(self._open_menu)

    def set_options(self, options: list[tuple[str, int]]) -> None:
        self._options = options
        self.set_day(self._value)

    def current_day(self) -> int:
        return self._value

    def set_day(self, value: int, *, emit: bool = False) -> None:
        normalized = value if 0 <= value <= 6 else UNCONFIGURED_WEEKDAY
        changed = normalized != self._value
        self._value = normalized
        self.setText(self._label_for(normalized))
        if emit and changed:
            self.dayChanged.emit(normalized)

    def _label_for(self, value: int) -> str:
        for label, day in self._options:
            if day == value:
                return label
        return self._options[0][0] if self._options else ""

    def _open_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_menu_style())
        for label, value in self._options:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == self._value)
            action.triggered.connect(
                lambda _checked=False, selected=value: self.set_day(selected, emit=True)
            )
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))


class _TimeStepper(QWidget):
    valueChanged = Signal(int)

    def __init__(self, label: str, minimum: int, maximum: int, value: int, parent=None):
        super().__init__(parent)
        self._minimum = minimum
        self._maximum = maximum
        self._value = minimum

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        title = QLabel(label)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(_time_label_style())
        layout.addWidget(title)

        card = QFrame()
        card.setFixedSize(70, 92)
        card.setStyleSheet(_stepper_card_style())
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(5, 5, 5, 5)
        card_layout.setSpacing(2)

        up_btn = self._step_button(cast(str, ICON_CHEVRON_UP), self.tr("Increase"))
        down_btn = self._step_button(cast(str, ICON_CHEVRON_DOWN), self.tr("Decrease"))
        self._value_label = QLabel()
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value_label.setStyleSheet(_stepper_value_style())
        self._value_label.setMinimumHeight(30)

        card_layout.addWidget(up_btn)
        card_layout.addWidget(self._value_label, stretch=1)
        card_layout.addWidget(down_btn)
        layout.addWidget(card)

        up_btn.clicked.connect(lambda: self._shift(1))
        down_btn.clicked.connect(lambda: self._shift(-1))
        self.set_value(value)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int, *, emit: bool = False) -> None:
        normalized = self._wrap(value)
        changed = normalized != self._value
        self._value = normalized
        self._value_label.setText(f"{normalized:02d}")
        if emit and changed:
            self.valueChanged.emit(normalized)

    def _shift(self, delta: int) -> None:
        self.set_value(self._value + delta, emit=True)

    def _wrap(self, value: int) -> int:
        if value < self._minimum:
            return self._maximum
        if value > self._maximum:
            return self._minimum
        return value

    def _step_button(self, icon_svg: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(60, 24)
        button.setToolTip(tooltip)
        button.setStyleSheet(_stepper_button_style())
        button.setIcon(make_icon(icon_svg, size=14, color=SETTINGS_MUTED))
        button.setIconSize(QSize(14, 14))
        return button


class _TimePickerPopup(QWidget):
    timeSelected = Signal(int)

    def __init__(self, minutes: int, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(190)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QFrame()
        card.setObjectName("ScheduleTimePickerCard")
        card.setStyleSheet(_time_popup_card_style())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        self._preview = QLabel(f"{minutes // 60:02d}:{minutes % 60:02d}")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(_time_preview_style())
        layout.addWidget(self._preview)

        spin_row = QHBoxLayout()
        spin_row.setSpacing(10)
        self._hour_stepper = _TimeStepper(self.tr("Hour"), 0, 23, minutes // 60, card)
        self._minute_stepper = _TimeStepper(self.tr("Minute"), 0, 59, minutes % 60, card)
        spin_row.addWidget(self._hour_stepper)
        spin_row.addWidget(self._minute_stepper)
        layout.addLayout(spin_row)

        self._hour_stepper.valueChanged.connect(lambda _value: self._update_preview())
        self._minute_stepper.valueChanged.connect(lambda _value: self._update_preview())

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        cancel_btn = QPushButton(self.tr("Cancel"))
        cancel_btn.setMinimumHeight(32)
        cancel_btn.setStyleSheet(_time_cancel_style())
        apply_btn = QPushButton(self.tr("Apply"))
        apply_btn.setMinimumHeight(32)
        apply_btn.setStyleSheet(_time_apply_style())
        button_row.addWidget(cancel_btn)
        button_row.addWidget(apply_btn)
        layout.addLayout(button_row)
        root.addWidget(card)

        cancel_btn.clicked.connect(self.close)
        apply_btn.clicked.connect(self._apply)

    def _selected_minutes(self) -> int:
        return self._hour_stepper.value() * 60 + self._minute_stepper.value()

    def _update_preview(self) -> None:
        self._preview.setText(
            f"{self._hour_stepper.value():02d}:{self._minute_stepper.value():02d}"
        )

    def _apply(self) -> None:
        self.timeSelected.emit(self._selected_minutes())
        self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._apply()
            return
        super().keyPressEvent(event)


class _ScheduleTimeButton(QPushButton):
    timeChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._minutes = 0
        self._popup: _TimePickerPopup | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setFixedWidth(86)
        self.setStyleSheet(_field_button_style())
        self.clicked.connect(self._open_picker)

    def minutes(self) -> int:
        return self._minutes

    def time_text(self) -> str:
        return f"{self._minutes // 60:02d}:{self._minutes % 60:02d}"

    def set_minutes(self, minutes: int, *, emit: bool = False) -> None:
        normalized = max(0, min(23 * 60 + 59, int(minutes)))
        changed = normalized != self._minutes
        self._minutes = normalized
        self.setText(self.time_text())
        if emit and changed:
            self.timeChanged.emit(normalized)

    def _open_picker(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.close()
            return

        popup = _TimePickerPopup(self._minutes, self)
        popup.timeSelected.connect(lambda minutes: self.set_minutes(minutes, emit=True))
        popup.destroyed.connect(lambda _obj=None: setattr(self, "_popup", None))
        self._popup = popup
        popup.adjustSize()
        popup.move(self._popup_position(popup))
        popup.show()
        popup.setFocus()

    def _popup_position(self, popup: QWidget) -> QPoint:
        size = popup.sizeHint()
        button_bottom_left = self.mapToGlobal(QPoint(0, self.height() + 4))
        x = button_bottom_left.x() + self.width() - size.width()
        y = button_bottom_left.y()

        screen = self.screen()
        if screen is None:
            return QPoint(x, y)

        available = screen.availableGeometry()
        margin = 8
        x = max(available.left() + margin, min(x, available.right() - size.width() - margin))
        if y + size.height() > available.bottom() - margin:
            y = self.mapToGlobal(QPoint(0, -size.height() - 4)).y()
            y = max(available.top() + margin, y)
        return QPoint(x, y)


class MeetingScheduleSectionMixin:
    """Builds meeting day/time settings shared by meeting-aware features."""

    def _build_meeting_schedule_card(self):
        card, lay = self._card()
        self._schedule_rows: dict[str, tuple[_ScheduleDayButton, _ScheduleTimeButton]] = {}

        self._schedule_hint_lbl = QLabel(self.tr(
            "Used by automatic features that depend on the meeting start time."
        ))
        self._schedule_hint_lbl.setWordWrap(True)
        self._schedule_hint_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
            " padding: 12px 14px 2px 14px;"
        )
        lay.addWidget(self._schedule_hint_lbl)

        lay.addWidget(self._schedule_row(
            MIDWEEK,
            self.tr("Midweek meeting"),
            self.tr("Day and time for the midweek meeting."),
            DEFAULT_MIDWEEK_TIME,
        ))
        lay.addWidget(self._divider())
        lay.addWidget(self._schedule_row(
            WEEKEND,
            self.tr("Weekend meeting"),
            self.tr("Day and time for the weekend meeting."),
            DEFAULT_WEEKEND_TIME,
        ))
        return card

    def _schedule_row(
        self,
        kind: str,
        title: str,
        desc: str,
        default_time: str,
    ) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(
            make_icon(cast(str, ICON_CALENDAR), size=18, color=SETTINGS_MUTED).pixmap(18, 18)
        )
        icon_label.setFixedSize(20, 20)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_DIM}; background: transparent; border: none;"
        )
        text_col.addWidget(title_label)
        text_col.addWidget(desc_label)
        row_layout.addLayout(text_col, stretch=1)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        day_button = _ScheduleDayButton()
        self._populate_day_button(day_button)

        time_button = _ScheduleTimeButton()

        saved_day, saved_time = self._meeting_schedule_settings.slot_values(kind)
        self._apply_schedule_controls(day_button, time_button, saved_day, saved_time, default_time)

        def on_changed() -> None:
            self._on_schedule_control_changed(kind, default_time)

        day_button.dayChanged.connect(lambda _day: on_changed())
        time_button.timeChanged.connect(lambda _minutes: on_changed())

        controls.addWidget(day_button)
        controls.addWidget(time_button)
        row_layout.addLayout(controls)

        setattr(self, f"_{kind}_schedule_title_lbl", title_label)
        setattr(self, f"_{kind}_schedule_desc_lbl", desc_label)
        self._schedule_rows[kind] = (day_button, time_button)
        return row

    def _apply_schedule_controls(
        self,
        day_button: _ScheduleDayButton,
        time_button: _ScheduleTimeButton,
        saved_day,
        saved_time: str,
        default_time: str,
    ) -> None:
        day = int(saved_day) if str(saved_day).lstrip("-").isdigit() else UNCONFIGURED_WEEKDAY
        day_button.set_day(day if 0 <= day <= 6 else UNCONFIGURED_WEEKDAY)

        minutes = parse_time_text(saved_time)
        if minutes is None:
            minutes = parse_time_text(default_time)
        if minutes is None:
            minutes = 0
        time_button.set_minutes(minutes)
        time_button.setEnabled(0 <= day <= 6)

    def _on_schedule_control_changed(
        self,
        kind: str,
        default_time: str,
    ) -> None:
        day_button, time_button = self._schedule_rows[kind]
        day = day_button.current_day()
        configured = 0 <= day <= 6
        time_button.setEnabled(configured)
        if configured:
            time_text = time_button.time_text()
            self._meeting_schedule_settings.set_slot(kind, day, time_text or default_time)
        else:
            self._meeting_schedule_settings.set_slot(kind, UNCONFIGURED_WEEKDAY, "")
        self._sync_background_song_desc()
        self.meeting_schedule_changed.emit()

    def _populate_day_button(self, day_button: _ScheduleDayButton) -> None:
        current = day_button.current_day()
        day_button.blockSignals(True)
        options = [(self.tr("Not configured"), UNCONFIGURED_WEEKDAY)]
        for idx, text in zip(_DAY_DATA, self._weekday_names(), strict=True):
            options.append((text, idx))
        day_button.set_options(options)
        day_button.set_day(current)
        day_button.blockSignals(False)

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
        for day_button, _time_button in self._schedule_rows.values():
            self._populate_day_button(day_button)

    def get_meeting_schedule(self):
        return self._meeting_schedule_settings.load()

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
