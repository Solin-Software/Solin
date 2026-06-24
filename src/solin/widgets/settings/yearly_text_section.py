from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ...core.jw.language_context import jw_media_language_context
from ...styles.icons import (
    ICON_CLOUD_DONE,
    ICON_CLOUD_DOWNLOAD,
    ICON_SAVE_PLAYLIST,
    make_icon,
)
from .shared import (
    SETTINGS_ACCENT,
    SETTINGS_BG,
    SETTINGS_BORDER,
    SETTINGS_BORDER_STRONG,
    SETTINGS_DIM,
    SETTINGS_SUCCESS,
    SETTINGS_MUTED,
    SETTINGS_DANGER,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SETTINGS_TEXT_SECONDARY,
    settings_compact_secondary_button_stylesheet,
)


class YearlyTextSectionMixin:
    """Builds and manages the annual text settings section."""

    def _init_yearly_text_section(self) -> None:
        self._yt_service = self._yeartext_service_factory(self)
        self._yt_service.fetched.connect(self._on_yeartext_fetched)
        self._yt_service.fetch_failed.connect(self._on_yeartext_failed)
        self._yt_service.fetch_started.connect(self._on_fetch_started)
        self._manual_expanded = False
        self._manual_anim: QPropertyAnimation | None = None

    def _build_yearly_text_card(self):
        card, lay = self._card()
        inner = QFrame()
        inner.setStyleSheet("background: transparent; border: none;")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(14, 12, 14, 12)
        inner_lay.setSpacing(10)

        self._yearly_hint_lbl = QLabel(
            self.tr("Text shown on the projection screen when idle.")
        )
        self._yearly_hint_lbl.setStyleSheet(
            f"font-size: 12px; color: {SETTINGS_MUTED}; background: transparent;"
        )
        self._yearly_hint_lbl.setWordWrap(True)
        inner_lay.addWidget(self._yearly_hint_lbl)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._yt_icon_lbl = QLabel()
        self._yt_icon_lbl.setFixedSize(20, 20)
        self._yt_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._yt_icon_lbl.setStyleSheet("background: transparent;")
        status_row.addWidget(self._yt_icon_lbl)

        yt_text_col = QVBoxLayout()
        yt_text_col.setSpacing(2)
        self._yt_status_lbl = QLabel(self.tr("Fetching annual text\u2026"))
        self._yt_status_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent;"
        )
        yt_text_col.addWidget(self._yt_status_lbl)
        self._yt_preview_lbl = QLabel()
        self._yt_preview_lbl.setStyleSheet(
            f"font-size: 11px; color: {SETTINGS_MUTED}; background: transparent;"
        )
        self._yt_preview_lbl.setWordWrap(True)
        self._yt_preview_lbl.hide()
        yt_text_col.addWidget(self._yt_preview_lbl)
        status_row.addLayout(yt_text_col, stretch=1)

        self._yt_refresh_btn = QPushButton(self.tr("Update"))
        self._yt_refresh_btn.setFixedHeight(28)
        self._yt_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._yt_refresh_btn.setIcon(
            make_icon(ICON_CLOUD_DOWNLOAD, size=14, color=SETTINGS_MUTED)
        )
        self._yt_refresh_btn.setIconSize(QSize(14, 14))
        self._yt_refresh_btn.setStyleSheet(
            settings_compact_secondary_button_stylesheet()
        )
        self._yt_refresh_btn.clicked.connect(self._refresh_yeartext)
        status_row.addWidget(self._yt_refresh_btn)
        inner_lay.addLayout(status_row)

        self._manual_toggle_btn = QPushButton()
        self._manual_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_toggle_btn.setMinimumHeight(32)
        self._manual_toggle_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; padding: 0 4px; color: {SETTINGS_DIM};"
            f" font-size: 11px; background: transparent; border: none; }}"
            f"QPushButton:hover {{ color: {SETTINGS_TEXT}; }}"
        )
        self._manual_toggle_btn.clicked.connect(self._toggle_manual_section)
        inner_lay.addWidget(self._manual_toggle_btn)
        self._update_manual_toggle_label()

        self._manual_container = QFrame()
        self._manual_container.setStyleSheet(
            f"background: {SETTINGS_BG}; border: 1px solid {SETTINGS_BORDER};"
            " border-radius: 8px;"
        )
        m_lay = QVBoxLayout(self._manual_container)
        m_lay.setContentsMargins(12, 10, 12, 10)
        m_lay.setSpacing(8)

        self._yearly_quote_lbl = QLabel(self.tr("Scripture:"))
        self._yearly_quote_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        m_lay.addWidget(self._yearly_quote_lbl)

        self._yearly_quote_edit = QPlainTextEdit()
        self._yearly_quote_edit.setPlaceholderText(
            self.tr("E.g.: Happy are those conscious of their spiritual need.")
        )
        self._yearly_quote_edit.setMinimumHeight(70)
        self._yearly_quote_edit.setMaximumHeight(100)
        self._yearly_quote_edit.setStyleSheet(
            f"QPlainTextEdit {{ background: {SETTINGS_SURFACE}; border: 1px solid {SETTINGS_BORDER_STRONG};"
            f" border-radius: 6px; color: {SETTINGS_TEXT}; font-size: 12px; padding: 6px; }}"
            f"QPlainTextEdit:focus {{ border-color: {SETTINGS_ACCENT}; }}"
        )
        m_lay.addWidget(self._yearly_quote_edit)

        self._yearly_ref_lbl = QLabel(self.tr("Bible reference:"))
        self._yearly_ref_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {SETTINGS_TEXT};"
            " background: transparent; border: none;"
        )
        m_lay.addWidget(self._yearly_ref_lbl)

        self._yearly_ref_edit = QLineEdit()
        self._yearly_ref_edit.setPlaceholderText(self.tr("E.g.: Matthew 5:3."))
        self._yearly_ref_edit.setMinimumHeight(34)
        self._yearly_ref_edit.setStyleSheet(
            f"QLineEdit {{ background: {SETTINGS_SURFACE}; border: 1px solid {SETTINGS_BORDER_STRONG};"
            f" border-radius: 6px; color: {SETTINGS_TEXT}; font-size: 12px; padding: 0 8px; }}"
            f"QLineEdit:focus {{ border-color: {SETTINGS_ACCENT}; }}"
        )
        m_lay.addWidget(self._yearly_ref_edit)

        self._yearly_save_btn = QPushButton(self.tr("Save changes"))
        self._yearly_save_btn.setMinimumHeight(34)
        self._yearly_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._yearly_save_btn.setIcon(
            make_icon(ICON_SAVE_PLAYLIST, size=14, color=SETTINGS_TEXT_SECONDARY)
        )
        self._yearly_save_btn.setIconSize(QSize(14, 14))
        self._yearly_save_btn.setStyleSheet(
            f"QPushButton {{ border: 1px solid {SETTINGS_BORDER_STRONG}; border-radius: 6px;"
            f" background: {SETTINGS_BORDER}; color: {SETTINGS_TEXT_SECONDARY}; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {SETTINGS_BORDER_STRONG}; }}"
        )
        self._yearly_save_btn.clicked.connect(self._save_yearly_text)
        m_lay.addWidget(self._yearly_save_btn)

        self._manual_container.setMaximumHeight(0)
        inner_lay.addWidget(self._manual_container)
        self._manual_container.setVisible(True)

        saved_quote, saved_ref = self._yeartext_settings.text()
        if saved_quote:
            self._yearly_quote_edit.setPlainText(saved_quote)
        if saved_ref:
            self._yearly_ref_edit.setText(saved_ref)
        lay.addWidget(inner)
        return card

    def _current_year(self):
        return datetime.now().year

    def _current_api_code(self):
        return jw_media_language_context(self.lang).api_code

    def _fallback_api_code(self):
        return jw_media_language_context(self.lang).fallback_code

    def _check_and_fetch_yeartext(self):
        api_code = self._current_api_code()
        year = self._current_year()
        cached = self._yt_service.get_cached(api_code, year)
        if cached:
            quote, ref = cached
            self._apply_yeartext_to_ui(api_code, year, quote, ref)
        else:
            self._set_status_loading()
            self._yt_service.fetch_async(api_code, year)

    def _refresh_yeartext(self):
        api_code = self._current_api_code()
        year = self._current_year()
        if not self._yt_service.is_fetching(api_code):
            self._set_status_loading()
            self._yt_service.fetch_async(api_code, year)

    def _on_language_switched(self, _code):
        self._yt_preview_lbl.hide()
        self._yt_status_lbl.setText(self.tr("Fetching annual text\u2026"))
        self._yt_status_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent;"
        )
        self._yt_icon_lbl.setPixmap(
            make_icon(ICON_CLOUD_DOWNLOAD, size=16, color=SETTINGS_MUTED).pixmap(16, 16)
        )
        self._yearly_quote_edit.blockSignals(True)
        self._yearly_ref_edit.blockSignals(True)
        self._yearly_quote_edit.setPlainText("")
        self._yearly_ref_edit.setText("")
        self._yearly_quote_edit.blockSignals(False)
        self._yearly_ref_edit.blockSignals(False)
        self.yearly_text_changed.emit("", "", self._current_api_code())
        self._check_and_fetch_yeartext()

    def _on_fetch_started(self, api_code, _year):
        if api_code == self._current_api_code():
            self._set_status_loading()

    def _on_yeartext_fetched(self, api_code, year, quote, reference):
        fallback = getattr(self, "_yeartext_fallback_code", None)
        if api_code == self._current_api_code() or api_code == fallback:
            self._yeartext_fallback_code = None
            self._apply_yeartext_to_ui(api_code, year, quote, reference)

    def _on_yeartext_failed(self, api_code, year, message):
        fallback = getattr(self, "_yeartext_fallback_code", None)
        if api_code != self._current_api_code() and api_code != fallback:
            return
        fallback_code = self._fallback_api_code()
        if api_code != fallback_code and fallback is None:
            cached_fallback = self._yt_service.get_cached(fallback_code, year)
            if cached_fallback:
                self._apply_yeartext_to_ui(fallback_code, year, *cached_fallback)
                return
            if not self._yt_service.is_fetching(fallback_code):
                self._yt_service.fetch_async(fallback_code, year)
            self._yeartext_fallback_code = fallback_code
            return
        self._yeartext_fallback_code = None
        self._set_status_error(message)

    def _apply_yeartext_to_ui(self, api_code, year, quote, ref):
        self._yt_icon_lbl.setPixmap(
            make_icon(ICON_CLOUD_DONE, size=16, color=SETTINGS_SUCCESS).pixmap(16, 16)
        )
        self._yt_status_lbl.setText(
            self.tr("Annual text updated for {year}").replace("{year}", str(year))
        )
        self._yt_status_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_SUCCESS}; background: transparent;"
        )
        preview_text = quote if not ref else f"{quote}  \u2014  {ref}"
        preview = preview_text[:120] + ("\u2026" if len(preview_text) > 120 else "")
        self._yt_refresh_btn.setEnabled(True)
        self._yt_refresh_btn.setText(self.tr("Update"))
        self._yt_preview_lbl.setText(preview)
        self._yt_preview_lbl.show()
        self._yearly_quote_edit.blockSignals(True)
        self._yearly_ref_edit.blockSignals(True)
        self._yearly_quote_edit.setPlainText(quote)
        self._yearly_ref_edit.setText(ref)
        self._yearly_quote_edit.blockSignals(False)
        self._yearly_ref_edit.blockSignals(False)
        self._yeartext_settings.set_text(quote, ref)
        self.yearly_text_changed.emit(quote, ref, api_code)

    def _set_status_loading(self):
        self._yt_icon_lbl.setPixmap(
            make_icon(ICON_CLOUD_DOWNLOAD, size=16, color=SETTINGS_MUTED).pixmap(16, 16)
        )
        self._yt_status_lbl.setText(self.tr("Fetching annual text\u2026"))
        self._yt_status_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT}; background: transparent;"
        )
        self._yt_preview_lbl.hide()
        self._yt_refresh_btn.setEnabled(False)
        self._yt_refresh_btn.setText(self.tr("Loading\u2026"))

    def _set_status_error(self, message):
        self._yt_icon_lbl.setPixmap(
            make_icon(ICON_CLOUD_DOWNLOAD, size=16, color=SETTINGS_DANGER).pixmap(16, 16)
        )
        short_msg = message[:80] + ("\u2026" if len(message) > 80 else "")
        self._yt_status_lbl.setText(self.tr("Could not fetch annual text"))
        self._yt_status_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {SETTINGS_DANGER}; background: transparent;"
        )
        self._yt_preview_lbl.setText(short_msg)
        self._yt_preview_lbl.show()
        self._yt_refresh_btn.setEnabled(True)
        self._yt_refresh_btn.setText(self.tr("Retry"))

    def _toggle_manual_section(self):
        self._manual_expanded = not self._manual_expanded
        self._update_manual_toggle_label()
        target_h = (
            self._manual_container.sizeHint().height()
            if self._manual_expanded else 0
        )
        if self._manual_anim is not None:
            self._manual_anim.stop()
            self._manual_anim.deleteLater()
            self._manual_anim = None
        anim = QPropertyAnimation(self._manual_container, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.setStartValue(self._manual_container.maximumHeight())
        anim.setEndValue(target_h)
        anim.start()
        self._manual_anim = anim

    def _update_manual_toggle_label(self):
        if self._manual_expanded:
            self._manual_toggle_btn.setText(self.tr("\u25b2  Edit text manually"))
        else:
            self._manual_toggle_btn.setText(self.tr("\u25bc  Edit text manually"))

    def _save_yearly_text(self):
        quote = self._yearly_quote_edit.toPlainText().strip()
        ref = self._yearly_ref_edit.text().strip()
        self._yeartext_settings.set_text(quote, ref)
        self._yt_service.override_cache(
            self._current_api_code(), self._current_year(), quote, ref
        )
        self.yearly_text_changed.emit(quote, ref, self._current_api_code())

    def get_yearly_text(self):
        api_code = self._current_api_code()
        year = self._current_year()
        cached = self._yt_service.get_cached(api_code, year)
        if cached:
            return cached
        return self._yeartext_settings.text()
