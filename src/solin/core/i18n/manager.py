"""
language_manager.py
───────────────────
Motor de i18n do Solin — baseado no sistema nativo Qt (QTranslator / .qm).

Responsabilidades
─────────────────
• Carregar metadados de idioma dos arquivos JSON (code, name, api_code,
  wol_*, date_format, time_with_seconds_format) — dados que não passam pelo Qt i18n.
• Instalar/desinstalar QTranslators no QCoreApplication quando o idioma
  muda, garantindo que:
    – strings da aplicação  (solin_<code>.qm)  sejam traduzidas via tr();
    – strings nativas do Qt (qtbase_<code>.qm) também mudem.

Strings de UI
─────────────
Todos os widgets usam self.tr("English source") diretamente — o padrão nativo
do Qt. O método t(key) foi removido. O fluxo de atualização de traduções é:

    código com self.tr("…")
      → lupdate  → .ts (atualiza entradas automaticamente)
      → Qt Linguist (traduzir)
      → lrelease → .qm

Arquivos necessários em produção
─────────────────────────────────
  translations/solin_<code>.qm   ← binários compilados pelo lrelease
  translations/locales/<code>.json ← metadados (code, name, api_code…)

Arquivos de desenvolvimento (NÃO embarcados em produção)
─────────────────────────────────────────────────────────
  translations/solin_<code>.ts   ← fonte para lupdate / Qt Linguist / lrelease
"""

from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QLocale,
    QObject,
    Signal,
)
from solin.core.foundation.settings_store import GlobalSettingsStore
from solin.core.profiles.settings import ProfileSettings

log = logging.getLogger(__name__)

# Importação lazy para evitar ciclo (jw.languages importa constants)
def _get_jw_language_service_class():
    from solin.core.jw.languages import JWLanguageService
    return JWLanguageService

# ── resolução de caminhos ─────────────────────────────────────────────────────
def _translation_root() -> Path:
    package_root = Path(__file__).resolve().parents[2]
    for root in (package_root.parent, package_root.parents[1], Path.cwd()):
        candidate = root / "translations"
        if (candidate / "locales").is_dir():
            return candidate
    return package_root.parents[1] / "translations"


_TRANS_DIR    = os.fspath(_translation_root())
_LANG_DIR     = os.path.join(_TRANS_DIR, "locales")
_QT_TRANS_DIR = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)


# ── LanguageManager ───────────────────────────────────────────────────────────

class LanguageManager(QObject):
    """
    Gerencia o idioma ativo e os QTranslators instalados na aplicação.

    Signals
    -------
    language_changed(code: str)
        Emitido DEPOIS de os translators serem instalados, para que
        os widgets chamem retranslateUi() conforme necessário.
    """

    language_changed = Signal(str)

    _app_translator: Optional[object] = None  # QTranslator
    _qt_translator:  Optional[object] = None  # QTranslator (qtbase_*.qm)

    def __init__(
        self,
        *,
        global_settings: GlobalSettingsStore,
        jw_languages_cache_file: str | Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._meta:        dict[str, dict] = {}
        self.current_code: str = "pt_BR"
        self._global_settings = global_settings
        self._profile_settings: ProfileSettings | None = None

        # Serviço de idiomas JW.org (lista completa para mídia)
        JWLanguageService  = _get_jw_language_service_class()
        self._jw_lang_svc = JWLanguageService(
            cache_file=jw_languages_cache_file,
            parent=self,
        )

        self._load_meta()
        self._restore_saved_language()

    # ── inicialização ─────────────────────────────────────────────────────────

    def _load_meta(self) -> None:
        """Carrega apenas a seção 'meta' de cada JSON (leve, sem strings de UI)."""
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
        self._jw_lang_svc.activate_profile(profile_settings)
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

    # ── troca de idioma ───────────────────────────────────────────────────────

    def set_language(self, code: str) -> None:
        """Troca o idioma, instala QTranslators e emite language_changed."""
        if code not in self._meta:
            log.warning("Unknown language: %r", code)
            return
        if self._profile_settings is not None:
            self._profile_settings.app_settings().set_app_language(code)
        self._global_settings.set_bootstrap_language(code)
        if code == self.current_code:
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
        Substitui os QTranslators ativos:
          1. solin_<code>.qm  — strings da aplicação
          2. qtbase_<code>.qm — strings nativas do Qt (botões, diálogos, etc.)
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

        # 1. Translator da aplicação (solin_<code>.qm)
        qm_path = os.path.join(_TRANS_DIR, f"solin_{code}.qm")
        app_tr  = QTranslator(app)
        if os.path.isfile(qm_path) and app_tr.load(qm_path):
            app.installTranslator(app_tr)
            self._app_translator = app_tr
        else:
            log.info("Application translation not found: %s", qm_path)

        # 2. Translator nativo do Qt — tenta qtbase_* depois qt_*
        qt_tr = QTranslator(app)
        if (qt_tr.load(f"qtbase_{code}", _QT_TRANS_DIR) or
                qt_tr.load(f"qt_{code}",     _QT_TRANS_DIR)):
            app.installTranslator(qt_tr)
            self._qt_translator = qt_tr

    # ── serviço de idiomas JW.org ─────────────────────────────────────────────

    @property
    def jw_lang_service(self):
        """Retorna o JWLanguageService (lista de idiomas JW para mídia)."""
        return self._jw_lang_svc

    @property
    def media_api_code(self) -> str:
        """
        Código api JW do idioma de mídia selecionado.
        Retorna o api_code da interface se nenhum idioma de mídia for definido.
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
        True se o idioma de mídia selecionado for uma língua gestual.
        Consulta JWLanguageService.is_media_sign_language.
        Os idiomas da interface (fallback) NUNCA são gestuais.
        """
        return (
            self._jw_lang_svc.is_media_sign_language
            if self._profile_settings is not None
            else False
        )

    # ── metadados (não passam pelo Qt i18n) ───────────────────────────────────

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
        """Retorna [(code, name)] para todos os idiomas disponíveis."""
        return [(code, m.get("name", code))
                for code, m in self._meta.items()]

    # ── compatibilidade: dict 'languages' (leitura) ───────────────────────────

    @property
    def languages(self) -> dict[str, dict]:
        """Compatibilidade com código que acessa lang.languages[code]['meta']."""
        return {code: {"meta": m} for code, m in self._meta.items()}
