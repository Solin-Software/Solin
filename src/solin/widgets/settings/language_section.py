from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel

from ...core.jw.language_context import jw_media_language_context
from ...styles.icons import ICON_BOOK, ICON_NAV_BROWSER
from ._shared import create_interface_language_picker, create_jw_language_picker


class LanguageSectionMixin:
    """Builds and manages interface/JW media language settings."""

    def _build_lang_card(self):
        card, lay = self._card()
        meta = self.lang.meta
        name = meta.get("name", self.lang.current_code)
        row, self._lang_name_lbl, self._ui_lang_label, self._lang_code_lbl = self._clickable_row(
            ICON_NAV_BROWSER,
            self.tr("Interface"),
            name,
            subtitle=self.lang.current_code,
        )
        row.mousePressEvent = lambda _: self._open_lang_picker()
        lay.addWidget(row)
        lay.addWidget(self._divider())
        row2, self._media_lang_name_lbl, self._media_lang_label, _ = self._clickable_row(
            ICON_BOOK,
            self.tr("JW Media"),
            "",
        )
        self._media_lang_code_lbl = QLabel()
        row2.mousePressEvent = lambda _: self._open_media_lang_picker()
        lay.addWidget(row2)
        self._refresh_media_lang_row()
        svc = self.lang.jw_lang_service
        svc.languages_ready.connect(self._on_jw_languages_ready)
        svc.fetch_if_needed()
        self._lang_card = card
        return card

    def _refresh_lang_row(self):
        meta = self.lang.meta
        name = meta.get("name", self.lang.current_code)
        self._lang_name_lbl.setText(name)
        if self._lang_code_lbl is not None:
            self._lang_code_lbl.setText(self.lang.current_code)

    def _refresh_media_lang_row(self):
        svc = self.lang.jw_lang_service
        media_code = svc.media_api_code
        if media_code:
            lang_data = svc.get_language(media_code)
            if lang_data:
                name = lang_data.get("vernacular") or lang_data.get("name") or media_code
                self._media_lang_name_lbl.setText(name)
                return
            self._media_lang_name_lbl.setText(media_code)
        else:
            meta = self.lang.meta
            name = meta.get("name", self.lang.current_code)
            self._media_lang_name_lbl.setText(
                name + "  " + self.tr("(same as interface)")
            )

    def _on_jw_languages_ready(self, _languages):
        self._refresh_media_lang_row()

    def _open_lang_picker(self):
        picker = create_interface_language_picker(self.lang, parent=self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            chosen = picker.chosen_code
            if chosen and chosen != self.lang.current_code:
                self.lang.set_language(chosen)
                self._refresh_lang_row()
                self.language_changed.emit(chosen)

    def _open_media_lang_picker(self):
        svc = self.lang.jw_lang_service
        current_code = jw_media_language_context(self.lang).api_code
        picker = create_jw_language_picker(svc, current_code, parent=self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            chosen = picker.chosen_code
            if chosen:
                svc.set_media_api_code(chosen)
                self._refresh_media_lang_row()

    def _select_lang(self, code):
        self.lang.set_language(code)
        self._refresh_lang_row()
        self.language_changed.emit(code)
