from __future__ import annotations

from typing import cast

from PySide6.QtCore import QPoint, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.jw.congregation_lookup import (
    MINIMUM_QUERY_LENGTH,
    RATE_LIMITED,
    SEARCH_DEBOUNCE_MS,
)
from ...core.meetings.schedule import (
    DEFAULT_MIDWEEK_TIME,
    DEFAULT_WEEKEND_TIME,
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    parse_time_text,
)
from ...core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_not_configured_label,
    meeting_weekday_names,
)
from ...styles.icons import (
    ICON_CALENDAR,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_UP,
    ICON_CLOUD_DONE,
    ICON_CLOUD_DOWNLOAD,
    make_icon,
)
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_ACCENT_HOVER,
    SETTINGS_ACCENT_MUTED,
    SETTINGS_ACCENT_PRESSED,
    SETTINGS_BG,
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DANGER,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_SUCCESS,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SETTINGS_WARNING_TEXT,
)

_DAY_DATA = tuple(range(7))
# Lines the field up with the day and time controls below it.
_CONGREGATION_FIELD_WIDTH = 248


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


def _search_field_style() -> str:
    return (
        f"QLineEdit {{ background: {SETTINGS_SURFACE}; color: {SETTINGS_TEXT};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px;"
        f" padding: 0px 12px; font-size: 12px; }}"
        f"QLineEdit:focus {{ border-color: {SETTINGS_ACCENT}; }}"
    )


def _congregation_status_style(color) -> str:
    return (
        f"font-size: 11px; color: {color};"
        " background: transparent; border: none;"
    )


def _completer_popup_style() -> str:
    return (
        "QListView {"
        f" background: {SETTINGS_SURFACE};"
        f" color: {SETTINGS_TEXT};"
        f" border: 1px solid {SETTINGS_BORDER_STRONG};"
        " border-radius: 8px;"
        " font-size: 12px;"
        " outline: none;"
        "}"
        "QListView::item {"
        " padding: 7px 10px;"
        " border-radius: 6px;"
        "}"
        f"QListView::item:hover {{ background: {SETTINGS_BORDER}; }}"
        "QListView::item:selected {"
        f" background: {SETTINGS_ACCENT_MUTED};"
        f" color: {SETTINGS_TEXT};"
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
        self.apply_theme()
        self.clicked.connect(self._open_menu)

    def apply_theme(self) -> None:
        self.setStyleSheet(_field_button_style())

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

        self._title_label = QLabel(label)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet(_time_label_style())
        layout.addWidget(self._title_label)

        self._card = QFrame()
        self._card.setFixedSize(70, 92)
        self._card.setStyleSheet(_stepper_card_style())
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(5, 5, 5, 5)
        card_layout.setSpacing(2)

        self._up_btn = self._step_button(cast(str, ICON_CHEVRON_UP), self.tr("Increase"))
        self._down_btn = self._step_button(cast(str, ICON_CHEVRON_DOWN), self.tr("Decrease"))
        self._value_label = QLabel()
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value_label.setStyleSheet(_stepper_value_style())
        self._value_label.setMinimumHeight(30)

        card_layout.addWidget(self._up_btn)
        card_layout.addWidget(self._value_label, stretch=1)
        card_layout.addWidget(self._down_btn)
        layout.addWidget(self._card)

        self._up_btn.clicked.connect(lambda: self._shift(1))
        self._down_btn.clicked.connect(lambda: self._shift(-1))
        self.set_value(value)

    def apply_theme(self) -> None:
        self._title_label.setStyleSheet(_time_label_style())
        self._card.setStyleSheet(_stepper_card_style())
        self._value_label.setStyleSheet(_stepper_value_style())
        self._apply_step_button_theme(self._up_btn, cast(str, ICON_CHEVRON_UP))
        self._apply_step_button_theme(self._down_btn, cast(str, ICON_CHEVRON_DOWN))

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
        self._apply_step_button_theme(button, icon_svg)
        button.setIconSize(QSize(14, 14))
        return button

    @staticmethod
    def _apply_step_button_theme(button: QToolButton, icon_svg: str) -> None:
        button.setStyleSheet(_stepper_button_style())
        button.setIcon(make_icon(icon_svg, size=14, color=SETTINGS_MUTED))


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

        self._card = QFrame()
        self._card.setObjectName("ScheduleTimePickerCard")
        self._card.setStyleSheet(_time_popup_card_style())
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        self._preview = QLabel(f"{minutes // 60:02d}:{minutes % 60:02d}")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(_time_preview_style())
        layout.addWidget(self._preview)

        spin_row = QHBoxLayout()
        spin_row.setSpacing(10)
        self._hour_stepper = _TimeStepper(self.tr("Hour"), 0, 23, minutes // 60, self._card)
        self._minute_stepper = _TimeStepper(
            self.tr("Minute"),
            0,
            59,
            minutes % 60,
            self._card,
        )
        spin_row.addWidget(self._hour_stepper)
        spin_row.addWidget(self._minute_stepper)
        layout.addLayout(spin_row)

        self._hour_stepper.valueChanged.connect(lambda _value: self._update_preview())
        self._minute_stepper.valueChanged.connect(lambda _value: self._update_preview())

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self._cancel_btn = QPushButton(self.tr("Cancel"))
        self._cancel_btn.setMinimumHeight(32)
        self._cancel_btn.setStyleSheet(_time_cancel_style())
        self._apply_btn = QPushButton(self.tr("Apply"))
        self._apply_btn.setMinimumHeight(32)
        self._apply_btn.setStyleSheet(_time_apply_style())
        button_row.addWidget(self._cancel_btn)
        button_row.addWidget(self._apply_btn)
        layout.addLayout(button_row)
        root.addWidget(self._card)

        self._cancel_btn.clicked.connect(self.close)
        self._apply_btn.clicked.connect(self._apply)

    def apply_theme(self) -> None:
        self._card.setStyleSheet(_time_popup_card_style())
        self._preview.setStyleSheet(_time_preview_style())
        self._hour_stepper.apply_theme()
        self._minute_stepper.apply_theme()
        self._cancel_btn.setStyleSheet(_time_cancel_style())
        self._apply_btn.setStyleSheet(_time_apply_style())

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
        self.apply_theme()
        self.clicked.connect(self._open_picker)

    def apply_theme(self) -> None:
        self.setStyleSheet(_field_button_style())
        if self._popup is not None and self._popup.isVisible():
            self._popup.apply_theme()

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
        self._meeting_schedule_card = card
        self._schedule_rows: dict[str, tuple[_ScheduleDayButton, _ScheduleTimeButton]] = {}

        self._schedule_hint_lbl = QLabel(self.tr(
            "Used by automatic features that depend on the meeting start time."
        ))
        self._schedule_hint_lbl.setWordWrap(True)
        self._bind_theme_style(
            self._schedule_hint_lbl,
            lambda: (
                f"font-size: 11px; color: {SETTINGS_DIM};"
                " background: transparent; border: none;"
                " padding: 12px 14px 2px 14px;"
            ),
        )
        lay.addWidget(self._schedule_hint_lbl)

        lay.addWidget(self._congregation_lookup_row())
        lay.addWidget(self._divider())
        lay.addWidget(self._schedule_row(
            MIDWEEK,
            meeting_kind_label(MIDWEEK),
            self.tr("Day and time for the midweek meeting."),
            DEFAULT_MIDWEEK_TIME,
        ))
        lay.addWidget(self._divider())
        lay.addWidget(self._schedule_row(
            WEEKEND,
            meeting_kind_label(WEEKEND),
            self.tr("Day and time for the weekend meeting."),
            DEFAULT_WEEKEND_TIME,
        ))
        return card

    def _init_congregation_lookup(self) -> None:
        self._congregation_status_kind = "idle"
        self._congregation_applied_text = ""
        self._congregation_lookup = self._congregation_lookup_factory(self)
        self._congregation_lookup.suggestions_ready.connect(
            self._on_congregation_suggestions
        )
        self._congregation_lookup.schedule_ready.connect(
            self._on_congregation_schedule
        )
        self._congregation_lookup.failed.connect(self._on_congregation_failed)
        self._congregation_guids: dict[str, str] = {}

    def _congregation_lookup_row(self) -> QFrame:
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        self._congregation_icon_lbl = QLabel()
        self._congregation_icon_lbl.setFixedSize(20, 20)
        self._congregation_icon_lbl.setStyleSheet(
            "background: transparent; border: none;"
        )
        row_layout.addWidget(self._congregation_icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self._congregation_title_lbl = QLabel(self.tr("Fill in from jw.org"))
        self._bind_theme_style(
            self._congregation_title_lbl,
            lambda: (
                f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            ),
        )
        self._congregation_status_lbl = QLabel()
        self._congregation_status_lbl.setWordWrap(True)
        text_col.addWidget(self._congregation_title_lbl)
        text_col.addWidget(self._congregation_status_lbl)
        row_layout.addLayout(text_col, stretch=1)

        self._congregation_field = QLineEdit()
        self._congregation_field.setPlaceholderText(self.tr("Congregation name"))
        self._congregation_field.setMinimumHeight(36)
        self._congregation_field.setFixedWidth(_CONGREGATION_FIELD_WIDTH)
        self._bind_theme_style(self._congregation_field, _search_field_style)

        self._congregation_model = QStringListModel(self._congregation_field)
        completer = QCompleter(self._congregation_model, self._congregation_field)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        completer.activated.connect(self._on_congregation_activated)
        self._congregation_field.setCompleter(completer)

        self._bind_theme_style(completer.popup(), _completer_popup_style)

        self._congregation_debounce = QTimer(self._congregation_field)
        self._congregation_debounce.setSingleShot(True)
        self._congregation_debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._congregation_debounce.timeout.connect(self._start_congregation_search)
        self._congregation_field.textEdited.connect(
            lambda _text: self._congregation_debounce.start()
        )
        row_layout.addWidget(self._congregation_field)
        self._add_theme_binding(self._sync_congregation_ui)
        return row

    def _start_congregation_search(self) -> None:
        query = self._congregation_field.text().strip()
        if len(query) < MINIMUM_QUERY_LENGTH:
            self._set_congregation_status("idle")
            return
        self._set_congregation_status("searching")
        self._congregation_lookup.search(query)

    def _on_congregation_suggestions(self, matches: list) -> None:
        self._congregation_guids = {
            match.formatted_name: match.guid for match in matches
        }
        self._congregation_model.setStringList(list(self._congregation_guids))
        if not matches:
            self._set_congregation_status("empty")
            return
        self._set_congregation_status("idle")
        completer = self._congregation_field.completer()
        if completer is not None and self._congregation_field.hasFocus():
            completer.complete()

    def _on_congregation_activated(self, text: str) -> None:
        guid = self._congregation_guids.get(text, "")
        if not guid:
            return
        self._set_congregation_status("resolving")
        self._congregation_lookup.fetch_schedule(guid)

    def _on_congregation_schedule(self, schedule) -> None:
        if schedule is None:
            self._set_congregation_status("unpublished")
            return
        weekdays = self._weekday_names()
        applied: list[str] = []
        for slot in schedule.slots:
            if not slot.is_configured:
                continue
            day_button, time_button = self._schedule_rows[slot.kind]
            day_button.set_day(slot.weekday)
            time_button.set_minutes(slot.start_minutes or 0)
            self._on_schedule_control_changed(
                slot.kind,
                DEFAULT_MIDWEEK_TIME if slot.kind == MIDWEEK else DEFAULT_WEEKEND_TIME,
            )
            applied.append(f"{weekdays[slot.weekday]} {slot.time_text}")
        self._congregation_applied_text = "  ·  ".join(applied)
        self._set_congregation_status("filled")

    def _on_congregation_failed(self, reason: str) -> None:
        self._set_congregation_status(
                "rate_limited" if reason == RATE_LIMITED else "error"
        )

    def _set_congregation_status(self, kind: str) -> None:
        self._congregation_status_kind = kind
        self._sync_congregation_ui()

    def _sync_congregation_ui(self) -> None:
        label = getattr(self, "_congregation_status_lbl", None)
        if label is None:
            return
        text, tone, icon = self._congregation_status_presentation()
        label.setText(text)
        label.setStyleSheet(_congregation_status_style(tone))
        self._congregation_icon_lbl.setPixmap(
            make_icon(icon, size=18, color=tone).pixmap(18, 18)
        )

    def _congregation_status_presentation(self) -> tuple[str, str, str]:
        kind = self._congregation_status_kind
        download = cast(str, ICON_CLOUD_DOWNLOAD)
        if kind == "searching":
            return (self.tr("Searching jw.org…"), SETTINGS_WARNING_TEXT, download)
        if kind == "resolving":
            return (
                self.tr("Reading the meeting times…"),
                SETTINGS_WARNING_TEXT,
                download,
            )
        if kind == "empty":
            return (
                self.tr("No congregation found with that name."),
                SETTINGS_WARNING_TEXT,
                download,
            )
        if kind == "unpublished":
            return (
                self.tr("jw.org does not publish meeting times for this congregation."),
                SETTINGS_WARNING_TEXT,
                download,
            )
        if kind == "rate_limited":
            return (
                self.tr("Too many searches in a row. Wait a moment and type again."),
                SETTINGS_WARNING_TEXT,
                download,
            )
        if kind == "error":
            return (
                self.tr("Could not reach jw.org. Check the connection and try again."),
                SETTINGS_DANGER,
                download,
            )
        if kind == "filled":
            return (
                self._congregation_applied_text,
                SETTINGS_SUCCESS,
                cast(str, ICON_CLOUD_DONE),
            )
        return (self._congregation_hint_text(), SETTINGS_DIM, download)

    def _congregation_hint_text(self) -> str:
        return self.tr("Search your congregation to fill the days and times below.")

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
        self._bind_theme_pixmap(
            icon_label,
            cast(str, ICON_CALENDAR),
            size=18,
            color=SETTINGS_MUTED,
        )
        icon_label.setFixedSize(20, 20)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        self._bind_theme_style(
            title_label,
            lambda: (
                f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            ),
        )
        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        self._bind_theme_style(
            desc_label,
            lambda: (
                f"font-size: 11px; color: {SETTINGS_DIM};"
                " background: transparent; border: none;"
            ),
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
        options = [(meeting_not_configured_label(), UNCONFIGURED_WEEKDAY)]
        for idx, text in zip(_DAY_DATA, self._weekday_names(), strict=True):
            options.append((text, idx))
        day_button.set_options(options)
        day_button.set_day(current)
        day_button.blockSignals(False)

    def _refresh_meeting_schedule_texts(self) -> None:
        self._schedule_hint_lbl.setText(self.tr(
            "Used by automatic features that depend on the meeting start time."
        ))
        self._midweek_schedule_title_lbl.setText(meeting_kind_label(MIDWEEK))
        self._midweek_schedule_desc_lbl.setText(
            self.tr("Day and time for the midweek meeting.")
        )
        self._weekend_schedule_title_lbl.setText(meeting_kind_label(WEEKEND))
        self._weekend_schedule_desc_lbl.setText(
            self.tr("Day and time for the weekend meeting.")
        )
        self._congregation_title_lbl.setText(self.tr("Fill in from jw.org"))
        self._congregation_field.setPlaceholderText(self.tr("Congregation name"))
        self._sync_congregation_ui()
        for day_button, _time_button in self._schedule_rows.values():
            self._populate_day_button(day_button)

    def _apply_meeting_schedule_theme(self) -> None:
        for day_button, time_button in self._schedule_rows.values():
            day_button.apply_theme()
            time_button.apply_theme()

    def get_meeting_schedule(self):
        return self._meeting_schedule_settings.load()

    def _meeting_schedule_configured(self) -> bool:
        return self.get_meeting_schedule().has_configured_slot

    def _weekday_names(self) -> tuple[str, ...]:
        return meeting_weekday_names()
