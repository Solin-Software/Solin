"""Shared controls used by settings pages."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    Property,
    Qt,
    QPropertyAnimation,
    QRect,
    QSize,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...core.jw.languages import JWLanguageService
from ...core.i18n.manager import LanguageManager
from ...styles.theme import SCROLLBAR_STYLESHEET

SETTINGS_BG = "#0d1117"
SETTINGS_SURFACE = "#161b22"
SETTINGS_CARD = "#161b22"
SETTINGS_BORDER = "#21262d"
SETTINGS_ACCENT = "#388bfd"
SETTINGS_BORDER_STRONG = "#30363d"
SETTINGS_MUTED = "#8b949e"
SETTINGS_TEXT = "#e6edf3"
SETTINGS_DIM = "#484f58"
SETTINGS_SUCCESS = "#3fb950"
SETTINGS_DANGER = "#f85149"

__all__ = (
    "SETTINGS_ACCENT",
    "SETTINGS_BG",
    "SETTINGS_BORDER",
    "SETTINGS_BORDER_STRONG",
    "SETTINGS_CARD",
    "SETTINGS_DANGER",
    "SETTINGS_DIM",
    "SETTINGS_MUTED",
    "SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET",
    "SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET",
    "SETTINGS_SURFACE",
    "SETTINGS_SUCCESS",
    "SETTINGS_TEXT",
    "SettingsToggleSwitch",
    "create_interface_language_picker",
    "create_jw_language_picker",
)


class SettingsToggleSwitch(QWidget):
    toggled = Signal(bool)
    _TRACK_ON = QColor(SETTINGS_ACCENT)
    _TRACK_OFF = QColor(SETTINGS_BORDER_STRONG)
    _THUMB = QColor("#ffffff")
    _W, _H = 40, 22

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._anim_value: float = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"_thumb_pos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_thumb(self) -> float:
        return self._anim_value

    def _set_thumb(self, value: float) -> None:
        self._anim_value = value
        self.update()

    _thumb_pos = Property(float, _get_thumb, _set_thumb)

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool, animate: bool = True) -> None:
        if self._checked == value:
            return
        self._checked = value
        target = 1.0 if value else 0.0
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._anim_value)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim_value = target
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            new_state = not self._checked
            self.set_checked(new_state)
            self.toggled.emit(new_state)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._anim_value
        track = QColor(
            int(self._TRACK_OFF.red() + (self._TRACK_ON.red() - self._TRACK_OFF.red()) * t),
            int(self._TRACK_OFF.green() + (self._TRACK_ON.green() - self._TRACK_OFF.green()) * t),
            int(self._TRACK_OFF.blue() + (self._TRACK_ON.blue() - self._TRACK_OFF.blue()) * t),
        )
        painter.setBrush(track)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, self._W, self._H, self._H // 2, self._H // 2)
        margin = 3
        diameter = self._H - 2 * margin
        travel = self._W - 2 * margin - diameter
        x = margin + int(travel * t)
        painter.setBrush(self._THUMB)
        painter.drawEllipse(x, margin, diameter, diameter)
        painter.end()


class _LangItemDelegate(QStyledItemDelegate):
    _SELECTED_BG = QColor("#1f3a6e")
    _HOVER_BG = QColor(SETTINGS_BORDER)
    _TEXT_PRIMARY = QColor(SETTINGS_TEXT)
    _TEXT_SECONDARY = QColor(SETTINGS_MUTED)
    _CHECK_COLOR = QColor(SETTINGS_ACCENT)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 52)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 2, -4, -2)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver) and not is_selected
        if is_selected:
            painter.setBrush(self._SELECTED_BG)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
        elif is_hover:
            painter.setBrush(self._HOVER_BG)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 6, 6)
        x = rect.x() + 14
        y_top = rect.y()
        h = rect.height()
        is_current = index.data(Qt.ItemDataRole.UserRole + 10)
        if is_current:
            painter.setPen(QPen(self._CHECK_COLOR, 2))
            font = painter.font()
            font.setPixelSize(14)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRect(x, y_top, 18, h), Qt.AlignmentFlag.AlignVCenter, "\u2713")
            x += 22
        else:
            x += 4
        primary_text = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        font = painter.font()
        font.setPixelSize(13)
        font.setBold(is_selected)
        painter.setFont(font)
        painter.setPen(self._TEXT_PRIMARY)
        painter.drawText(
            QRect(x, y_top + 6, rect.width() - x - 10, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            primary_text,
        )
        sub_text = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(self._TEXT_SECONDARY)
        painter.drawText(
            QRect(x, y_top + 26, rect.width() - x - 10, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            sub_text,
        )
        painter.restore()


SETTINGS_PICKER_SEARCH_STYLESHEET = (
    "QLineEdit {"
    f"  background: {SETTINGS_SURFACE};"
    f"  color: {SETTINGS_TEXT};"
    f"  border: 1px solid {SETTINGS_BORDER_STRONG};"
    "  border-radius: 8px;"
    "  padding: 0 12px;"
    "}"
    f"QLineEdit:focus {{ border-color: {SETTINGS_ACCENT}; }}"
)

SETTINGS_PICKER_LIST_STYLESHEET = (
    f"QListWidget {{ background: {SETTINGS_BG}; border: 1px solid {SETTINGS_BORDER_STRONG};"
    f" border-radius: 10px; }}"
    f"QListWidget::item {{ padding: 0; border-radius: 6px; color: {SETTINGS_TEXT}; }}"
    f"QListWidget::item:selected {{ background: transparent; }}"
    f"QListWidget::item:hover:!selected {{ background: transparent; }}"
) + SCROLLBAR_STYLESHEET

SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {SETTINGS_ACCENT}; color: white;"
    " border: none; border-radius: 8px; font-weight: 600; }"
    "QPushButton:hover { background: #58a6ff; }"
    "QPushButton:pressed { background: #2f7be0; }"
)

SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {SETTINGS_BORDER}; color: #c9d1d9;"
    f" border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 8px; }}"
    f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; color: {SETTINGS_TEXT}; }}"
)


def _make_picker_btn_row(dialog, ok_label: str):
    row = QHBoxLayout()
    row.setSpacing(8)
    cancel = QPushButton(dialog.tr("Cancel"))
    cancel.setMinimumHeight(36)
    cancel.setStyleSheet(SETTINGS_PICKER_SECONDARY_BUTTON_STYLESHEET)
    cancel.clicked.connect(dialog.reject)
    ok = QPushButton(ok_label)
    ok.setMinimumHeight(36)
    ok.setDefault(True)
    ok.setStyleSheet(SETTINGS_PICKER_PRIMARY_BUTTON_STYLESHEET)
    row.addWidget(cancel)
    row.addWidget(ok)
    return row, ok


class _LanguagePicker(QDialog):
    def __init__(
        self,
        lang_manager: LanguageManager,
        parent=None,
        current_code: str | None = None,
    ):
        super().__init__(parent)
        self.lang = lang_manager
        self._current = current_code or lang_manager.current_code
        self.chosen_code: str = self._current
        self.setWindowTitle(self.tr("Interface Language"))
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(520)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )
        self._build()
        self._populate()
        self._select_current()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        title = QLabel(self.tr("Interface Language"))
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT};")
        lay.addWidget(title)
        hint = QLabel(self.tr("Choose the language for the app interface."))
        hint.setStyleSheet(f"font-size: 12px; color: {SETTINGS_MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search\u2026"))
        self._search.setMinimumHeight(36)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(SETTINGS_PICKER_SEARCH_STYLESHEET)
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(0)
        self._list.setMinimumHeight(300)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setStyleSheet(SETTINGS_PICKER_LIST_STYLESHEET)
        self._list.setItemDelegate(_LangItemDelegate(self._list))
        self._list.itemActivated.connect(self._accept_item)
        lay.addWidget(self._list)
        row, ok = _make_picker_btn_row(self, self.tr("Select"))
        ok.clicked.connect(self._accept_selected)
        lay.addLayout(row)

    def _populate(self):
        self._list.clear()
        for code, name in sorted(self.lang.available_languages(), key=lambda x: x[1]):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setData(Qt.ItemDataRole.UserRole + 1, name)
            item.setData(Qt.ItemDataRole.UserRole + 2, code)
            item.setData(Qt.ItemDataRole.UserRole + 10, code == self._current)
            item.setSizeHint(QSize(0, 52))
            self._list.addItem(item)

    def _select_current(self):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == self._current:
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                break

    def _filter(self, text: str):
        text = text.lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            code = item.data(Qt.ItemDataRole.UserRole) or ""
            name = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            item.setHidden(not (text in name.lower() or text in code.lower()))

    def _accept_item(self, item: QListWidgetItem):
        self.chosen_code = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _accept_selected(self):
        item = self._list.currentItem()
        if item:
            self.chosen_code = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


class _JWLanguagePicker(QDialog):
    def __init__(
        self,
        jw_lang_service: JWLanguageService,
        current_code: str,
        parent=None,
    ):
        super().__init__(parent)
        self._svc = jw_lang_service
        self.chosen_code = current_code
        self._current = current_code
        self.setWindowTitle(self.tr("Media Language"))
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setMaximumWidth(560)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )
        self._build()
        self._start()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        title = QLabel(self.tr("Media Language"))
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {SETTINGS_TEXT};")
        lay.addWidget(title)
        hint = QLabel(self.tr(
            "Language for media from JW.org (songs, clips, meetings). "
            "Falls back to the interface language if unavailable."
        ))
        hint.setStyleSheet(f"font-size: 12px; color: {SETTINGS_MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search\u2026"))
        self._search.setMinimumHeight(36)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(SETTINGS_PICKER_SEARCH_STYLESHEET)
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(0)
        self._list.setMinimumHeight(320)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setStyleSheet(SETTINGS_PICKER_LIST_STYLESHEET)
        self._list.setItemDelegate(_LangItemDelegate(self._list))
        self._list.itemActivated.connect(self._accept_item)
        lay.addWidget(self._list)
        self._loading_frame = QFrame(self._list)
        self._loading_frame.setStyleSheet(f"background: {SETTINGS_BG}; border-radius: 10px;")
        lf_lay = QVBoxLayout(self._loading_frame)
        lf_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_spinner = QLabel("\u23f3")
        self._loading_spinner.setStyleSheet(
            f"font-size: 24px; background: transparent; color: {SETTINGS_MUTED};"
        )
        self._loading_spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lf_lay.addWidget(self._loading_spinner)
        self._loading_lbl = QLabel(self.tr("Loading languages\u2026"))
        self._loading_lbl.setStyleSheet(
            f"font-size: 13px; color: {SETTINGS_MUTED}; background: transparent;"
        )
        self._loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lf_lay.addWidget(self._loading_lbl)
        row, self._ok_btn = _make_picker_btn_row(self, self.tr("Select"))
        self._ok_btn.clicked.connect(self._accept_selected)
        lay.addLayout(row)
        self._spinner_frames = ["\u23f3", "\u231b"]
        self._spinner_idx = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(600)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._set_loading(True)

    def _start(self):
        if self._svc.has_data:
            self._populate(self._svc.languages)
        else:
            self._svc.languages_ready.connect(self._on_languages_ready)
            self._svc.fetch_failed.connect(self._on_fetch_failed)
            self._svc.fetch_if_needed()

    def _on_languages_ready(self, languages: list):
        self._svc.languages_ready.disconnect(self._on_languages_ready)
        try:
            self._svc.fetch_failed.disconnect(self._on_fetch_failed)
        except RuntimeError:
            pass
        self._populate(languages)

    def _on_fetch_failed(self, error: str):
        try:
            self._svc.languages_ready.disconnect(self._on_languages_ready)
        except RuntimeError:
            pass
        self._svc.fetch_failed.disconnect(self._on_fetch_failed)
        self._set_loading(False)
        self._loading_spinner.setText("\u26a0\ufe0f")
        self._loading_lbl.setText(self.tr("Could not load languages."))
        self._loading_frame.show()

    def _populate(self, languages: list):
        self._list.clear()
        for lang in sorted(languages, key=lambda x: x.get("vernacular") or x.get("name") or ""):
            code = lang.get("code", "")
            vernacular = lang.get("vernacular") or lang.get("name") or code
            name_en = lang.get("name") or ""
            is_rtl = lang.get("isRTL", False)
            if not code:
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setData(Qt.ItemDataRole.UserRole + 1, vernacular)
            sub = name_en if name_en and name_en.lower() != vernacular.lower() else code
            if is_rtl:
                sub += "  \u200e[RTL]"
            item.setData(Qt.ItemDataRole.UserRole + 2, sub)
            item.setData(Qt.ItemDataRole.UserRole + 10, code == self._current)
            item.setSizeHint(QSize(0, 52))
            self._list.addItem(item)
        self._set_loading(False)
        self._select_current()

    def _select_current(self):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == self._current:
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                break

    def _set_loading(self, loading: bool):
        if loading:
            self._loading_frame.setGeometry(self._list.rect())
            self._loading_frame.raise_()
            self._loading_frame.show()
            self._search.setEnabled(False)
            self._ok_btn.setEnabled(False)
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
            self._loading_frame.hide()
            self._search.setEnabled(True)
            self._ok_btn.setEnabled(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_loading_frame"):
            self._loading_frame.setGeometry(self._list.rect())

    def _tick_spinner(self):
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        self._loading_spinner.setText(self._spinner_frames[self._spinner_idx])

    def _filter(self, text: str):
        text = text.lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            code = (item.data(Qt.ItemDataRole.UserRole) or "").lower()
            vernacular = (item.data(Qt.ItemDataRole.UserRole + 1) or "").lower()
            name_en = (item.data(Qt.ItemDataRole.UserRole + 2) or "").lower()
            item.setHidden(not (text in code or text in vernacular or text in name_en))

    def _accept_item(self, item: QListWidgetItem):
        self.chosen_code = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _accept_selected(self):
        item = self._list.currentItem()
        if item:
            self.chosen_code = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


def create_interface_language_picker(
    lang_manager: LanguageManager,
    parent=None,
    current_code: str | None = None,
) -> QDialog:
    return _LanguagePicker(lang_manager, parent=parent, current_code=current_code)


def create_jw_language_picker(
    jw_lang_service: JWLanguageService,
    current_code: str,
    parent=None,
) -> QDialog:
    return _JWLanguagePicker(jw_lang_service, current_code, parent=parent)
