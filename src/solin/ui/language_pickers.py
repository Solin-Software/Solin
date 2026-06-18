from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QRect, QSize
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from solin.core.i18n.manager import LanguageManager
from solin.core.jw.languages import JWLanguageService
from solin.styles.theme import SCROLLBAR_STYLESHEET

_BG = "#0d1117"
_SURFACE = "#161b22"
_BORDER = "#21262d"
_BORDER_STRONG = "#30363d"
_MUTED = "#8b949e"
_TEXT = "#e6edf3"
_ACCENT = "#388bfd"

PICKER_PRIMARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {_ACCENT}; color: white;"
    " border: none; border-radius: 8px; font-weight: 600; }"
    "QPushButton:hover { background: #58a6ff; }"
    "QPushButton:pressed { background: #2f7be0; }"
)

PICKER_SECONDARY_BUTTON_STYLESHEET = (
    f"QPushButton {{ background: {_BORDER}; color: #c9d1d9;"
    f" border: 1px solid {_BORDER_STRONG}; border-radius: 8px; }}"
    f"QPushButton:hover {{ background: {_BORDER_STRONG}; color: {_TEXT}; }}"
)

_PICKER_SEARCH_STYLESHEET = (
    "QLineEdit {"
    f"  background: {_SURFACE};"
    f"  color: {_TEXT};"
    f"  border: 1px solid {_BORDER_STRONG};"
    "  border-radius: 8px;"
    "  padding: 0 12px;"
    "}"
    f"QLineEdit:focus {{ border-color: {_ACCENT}; }}"
)

_PICKER_LIST_STYLESHEET = (
    f"QListWidget {{ background: {_BG}; border: 1px solid {_BORDER_STRONG};"
    f" border-radius: 10px; }}"
    f"QListWidget::item {{ padding: 0; border-radius: 6px; color: {_TEXT}; }}"
    "QListWidget::item:selected { background: transparent; }"
    "QListWidget::item:hover:!selected { background: transparent; }"
) + SCROLLBAR_STYLESHEET


class _LangItemDelegate(QStyledItemDelegate):
    _SELECTED_BG = QColor("#1f3a6e")
    _HOVER_BG = QColor(_BORDER)
    _TEXT_PRIMARY = QColor(_TEXT)
    _TEXT_SECONDARY = QColor(_MUTED)
    _CHECK_COLOR = QColor(_ACCENT)

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


def _make_picker_btn_row(dialog, ok_label: str):
    row = QHBoxLayout()
    row.setSpacing(8)
    cancel = QPushButton(dialog.tr("Cancel"))
    cancel.setMinimumHeight(36)
    cancel.setStyleSheet(PICKER_SECONDARY_BUTTON_STYLESHEET)
    cancel.clicked.connect(dialog.reject)
    ok = QPushButton(ok_label)
    ok.setMinimumHeight(36)
    ok.setDefault(True)
    ok.setStyleSheet(PICKER_PRIMARY_BUTTON_STYLESHEET)
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
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {_TEXT};")
        lay.addWidget(title)
        hint = QLabel(self.tr("Choose the language for the app interface."))
        hint.setStyleSheet(f"font-size: 12px; color: {_MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search\u2026"))
        self._search.setMinimumHeight(36)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(_PICKER_SEARCH_STYLESHEET)
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(0)
        self._list.setMinimumHeight(300)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setStyleSheet(_PICKER_LIST_STYLESHEET)
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
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {_TEXT};")
        lay.addWidget(title)
        hint = QLabel(self.tr(
            "Language for media from JW.org (songs, clips, meetings). "
            "Falls back to the interface language if unavailable."
        ))
        hint.setStyleSheet(f"font-size: 12px; color: {_MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search\u2026"))
        self._search.setMinimumHeight(36)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(_PICKER_SEARCH_STYLESHEET)
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(0)
        self._list.setMinimumHeight(320)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setStyleSheet(_PICKER_LIST_STYLESHEET)
        self._list.setItemDelegate(_LangItemDelegate(self._list))
        self._list.itemActivated.connect(self._accept_item)
        lay.addWidget(self._list)
        self._loading_frame = QLabel(self._list)
        self._loading_frame.setStyleSheet(f"background: {_BG}; border-radius: 10px;")
        self._loading_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_frame.setText(self.tr("Loading languages\u2026"))
        self._loading_frame.setStyleSheet(
            f"font-size: 13px; color: {_MUTED}; background: {_BG}; border-radius: 10px;"
        )
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
        self._loading_frame.setText(self.tr("Could not load languages."))
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
        self._loading_frame.setText(
            f"{self._spinner_frames[self._spinner_idx]} {self.tr('Loading languages\u2026')}"
        )

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
