"""
Solin i18n engine based on the native Qt system (QTranslator / .qm).

Responsibilities:
• Load language metadata from JSON files (code, name, api_code, wol_*,
  date_format, time_with_seconds_format), which bypasses Qt i18n.
• Install/remove QTranslators on QCoreApplication when the language changes:
    – translate application strings (solin_<code>.qm) via tr();
    – update native Qt strings (qtbase_<code>.qm) as well.

UI strings:
All widgets use self.tr("English source") directly, following Qt conventions.
The t(key) method has been removed. The translation update workflow is:

    code using self.tr("…")
      → lupdate → .ts (updates entries automatically)
      → Qt Linguist (translate)
      → lrelease → .qm

The English catalog is also loaded. Common strings can fall back to their
source, but numerus messages need solin_en.qm to turn neutral sources such
as "%n item(s)" into the English forms "item" and "items".

Production files:
  src/solin/resources/translations/solin_<code>.qm ← compiled by lrelease
  src/solin/resources/translations/locales/<code>.json ← language metadata

Development files (NOT included in production):
  src/solin/resources/translations/solin_<code>.ts ← lupdate / Qt Linguist / lrelease source
"""

from __future__ import annotations

import glob
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QLocale,
    QObject,
    Signal,
)
from solin.core.foundation.resources import application_translation_root
from solin.core.foundation.settings_store import GlobalSettingsStore
from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.profiles.settings import ProfileSettings

log = logging.getLogger(__name__)

# Lazy import to avoid a cycle (jw.languages imports constants).
def _get_jw_language_service_class():
    from solin.core.jw.languages import JWLanguageService
    return JWLanguageService

# Path resolution
def _translation_root() -> Path:
    return application_translation_root()


_TRANS_DIR    = os.fspath(_translation_root())
_LANG_DIR     = os.path.join(_TRANS_DIR, "locales")
_QT_TRANS_DIR = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)


# ── LanguageManager ───────────────────────────────────────────────────────────

class LanguageManager(QObject):
    """
    Manage the active language and the application's installed QTranslators.

    Signals
    -------
    language_changed(code: str)
        Emitted AFTER translators are installed so widgets can call
        retranslateUi() as needed.
    """

    language_changed = Signal(str)

    _app_translator: Optional[object] = None  # QTranslator
    _qt_translator:  Optional[object] = None  # QTranslator (qtbase_*.qm)

    def __init__(
        self,
        *,
        global_settings: GlobalSettingsStore,
        jw_languages_cache_file: str | Path,
        jw_language_settings_store_factory: Callable[
            [ProfileSettings], JWLanguageSettingsStore
        ],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._meta:        dict[str, dict] = {}
        self.current_code: str = "pt_BR"
        self._global_settings = global_settings
        self._profile_settings: ProfileSettings | None = None
        self._jw_language_settings_store_factory = jw_language_settings_store_factory

        # JW.org language service (complete media language list)
        JWLanguageService  = _get_jw_language_service_class()
        self._jw_lang_svc = JWLanguageService(
            cache_file=jw_languages_cache_file,
            parent=self,
        )

        self._load_meta()
        self._restore_saved_language()

    # Initialization

    def _load_meta(self) -> None:
        """Load only the 'meta' section of each JSON file (lightweight, no UI strings)."""
        for path in sorted(glob.glob(os.path.join(_LANG_DIR, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                code = data["meta"]["code"]
                self._meta[code] = data["meta"]
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                log.warning("Failed to load language metadata %s: %s", path, exc)

    def _restore_saved_language(self) -> None:
        bootstrap = self._global_settings.bootstrap_language()
        code = (
            bootstrap if bootstrap in self._meta
            else self._detect_system_language()
        )
        self.current_code = code
        self._install_translators(code)

    def activate_profile(self, profile_settings: ProfileSettings) -> None:
        self._profile_settings = profile_settings
        self._jw_lang_svc.activate_settings(
            self._jw_language_settings_store_factory(profile_settings)
        )
        saved = profile_settings.app_settings().app_language()
        if saved and saved in self._meta:
            self._global_settings.set_bootstrap_language(saved)
            self._apply_language(saved)

    def _detect_system_language(self) -> str:
        """Best-effort locale for profile-agnostic screens before a profile is active."""
        locale = QLocale.system()
        candidates = [
            locale.name(),
            locale.bcp47Name().replace("-", "_"),
            locale.name().split("_", 1)[0],
        ]
        for code in candidates:
            if code in self._meta:
                return code
        language_prefix = locale.name().split("_", 1)[0]
        for code in self._meta:
            if code.split("_", 1)[0] == language_prefix:
                return code
        return "en" if "en" in self._meta else next(iter(self._meta), "pt_BR")

    # Language switching

    def set_language(self, code: str) -> None:
        """Switch language, install QTranslators, and emit language_changed."""
        if code not in self._meta:
            log.warning("Unknown language: %r", code)
            return
        if self._profile_settings is not None:
            self._profile_settings.app_settings().set_app_language(code)
        self._global_settings.set_bootstrap_language(code)
        if code == self.current_code:
            return
        self._apply_language(code)

    def preview_language(self, code: str) -> None:
        """Apply a temporary language without mutating profile or bootstrap settings."""
        if code not in self._meta:
            log.warning("Unknown language: %r", code)
            return
        self._apply_language(code)

    def _apply_language(self, code: str) -> None:
        if code == self.current_code:
            return
        self.current_code = code
        self._install_translators(code)
        self.language_changed.emit(code)

    def _install_translators(self, code: str) -> None:
        """
        Replace the active QTranslators:
          1. solin_<code>.qm — application strings
          2. qtbase_<code>.qm — native Qt strings (buttons, dialogs, etc.)
        """
        from PySide6.QtCore import QTranslator

        app = QCoreApplication.instance()
        if app is None:
            return

        if self._app_translator is not None:
            app.removeTranslator(self._app_translator)
            self._app_translator = None
        if self._qt_translator is not None:
            app.removeTranslator(self._qt_translator)
            self._qt_translator = None

        # 1. Application translator (solin_<code>.qm)
        qm_path = os.path.join(_TRANS_DIR, f"solin_{code}.qm")
        app_tr  = QTranslator(app)
        if os.path.isfile(qm_path) and app_tr.load(qm_path):
            app.installTranslator(app_tr)
            self._app_translator = app_tr
        else:
            log.info("Application translation not found: %s", qm_path)

        # 2. Native Qt translator: try qtbase_* before qt_*.
        qt_tr = QTranslator(app)
        if (qt_tr.load(f"qtbase_{code}", _QT_TRANS_DIR) or
                qt_tr.load(f"qt_{code}",     _QT_TRANS_DIR)):
            app.installTranslator(qt_tr)
            self._qt_translator = qt_tr

    # JW.org language service

    @property
    def jw_lang_service(self):
        """Return the JWLanguageService (JW media language list)."""
        return self._jw_lang_svc

    @property
    def media_api_code(self) -> str:
        """
        JW API code for the selected media language.
        Fall back to the interface's api_code if no media language is set.
        """
        stored = (
            self._jw_lang_svc.media_api_code
            if self._profile_settings is not None
            else ""
        )
        return stored if stored else self.api_code

    @property
    def is_media_sign_language(self) -> bool:
        """
        True if the selected media language is a sign language.
        Consult JWLanguageService.is_media_sign_language.
        Interface languages used as fallbacks are NEVER sign languages.
        """
        return (
            self._jw_lang_svc.is_media_sign_language
            if self._profile_settings is not None
            else False
        )

    # Metadata (outside Qt i18n)

    @property
    def meta(self) -> dict:
        return self._meta.get(self.current_code, {})

    @property
    def api_code(self) -> str:
        return self.meta.get("api_code", "T")

    @property
    def wol_url(self) -> str:
        m      = self.meta
        lang   = m.get("wol_lang",   "pt")
        region = m.get("wol_region", "r5")
        lp     = m.get("wol_lp",     "lp-t")
        return f"https://wol.jw.org/{lang}/wol/meetings/{region}/{lp}"

    @property
    def date_format(self) -> str:
        return self.meta.get("date_format", "dd/MM/yyyy HH:mm")

    @property
    def time_with_seconds_format(self) -> str:
        return self.meta.get("time_with_seconds_format", "HH:mm:ss")

    def available_languages(self) -> list[tuple[str, str]]:
        """Return [(code, name)] for all available languages."""
        return [(code, m.get("name", code))
                for code, m in self._meta.items()]

    def api_code_for_language(self, code: str) -> str:
        return self._meta.get(code, {}).get("api_code", "")

    def shutdown(self) -> None:
        """Stop background work owned by language services."""
        self._jw_lang_svc.shutdown()
