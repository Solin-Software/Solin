"""
languages.py — Solin
════════════════════════════════
Busca e armazena em cache a lista completa de idiomas disponíveis no JW.org.

Endpoint : https://b.jw-cdn.org/apis/mediator/v1/languages/E/all
Cache    : 30 dias  →  cache/jw_languages.json
Formato  : { "languages": [ {code, locale, vernacular, name, script,
                              isLangPair, isSignLanguage, isRTL}, … ] }

Uso
───
    svc = JWLanguageService(
        cache_file=runtime_paths.cache_dir / "jw_languages.json",
        parent=self,
    )
    svc.languages_ready.connect(self._on_langs)
    svc.fetch_if_needed()

Propriedades chave de cada idioma retornado:
    code            – código api JW (ex: "T", "E", "S", "CHS")
    vernacular      – nome nativo   (ex: "Português (Brasil)")
    name            – nome inglês   (ex: "Portuguese (Brazil)")
    locale          – BCP-47 locale (ex: "pt")
    isRTL           – bool
    isSignLanguage  – bool  (true para línguas gestuais; usam pub=sjj, não sjjm)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from solin.core.jw.language_settings import JWLanguageSettingsStore
from solin.core.storage.json_files import read_json_file, write_json_atomic
from solin.core.network.http import get_json

log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────

_LANGUAGES_URL  = "https://b.jw-cdn.org/apis/mediator/v1/languages/E/all"
_CACHE_TTL_DAYS = 30
_FETCH_TIMEOUT  = 15


# ── Worker de fetch ─────────────────────────────────────────────────────────────

class _FetchSignals(QObject):
    succeeded = Signal(list)       # lista de dicts de idioma
    failed    = Signal(str)        # mensagem de erro


class _FetchWorker(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = _FetchSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            data = get_json(
                _LANGUAGES_URL,
                timeout=_FETCH_TIMEOUT,
                headers={"User-Agent": "Solin/1.0"},
            )

            languages: list[dict] = data.get("languages", [])
            if not languages:
                self.signals.failed.emit("Resposta vazia da API de idiomas")
                return

            # Filtra apenas idiomas publicáveis (exclui pares de idiomas raros)
            filtered = [
                lang for lang in languages
                if isinstance(lang, dict)
                and not lang.get("isLangPair", False)
                and lang.get("code")
                and lang.get("vernacular")
            ]
            self.signals.succeeded.emit(filtered)

        except Exception as exc:  # noqa: BLE001 - QRunnable reports all failures via signal
            log.exception("[JWLanguageService] Fetch worker failed")
            self.signals.failed.emit(str(exc))


# ── JWLanguageService ───────────────────────────────────────────────────────────

class JWLanguageService(QObject):
    """
    Gerencia a lista de idiomas JW.org e o idioma de mídia selecionado.

    Signals
    ───────
    languages_ready(list)   – emitido quando a lista estiver disponível
                              (do cache ou do fetch)
    fetch_started()         – fetch de rede iniciado
    fetch_failed(str)       – fetch falhou; erro como string
    """

    languages_ready = Signal(list)
    fetch_started   = Signal()
    fetch_failed    = Signal(str)
    media_language_changed = Signal(str)   # emitido quando o código de mídia muda

    def __init__(
        self,
        *,
        cache_file: str | Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._cache_file = Path(cache_file)
        self._languages: list[dict]  = []
        self._is_loading: bool       = False
        self._by_code: dict[str, dict] = {}
        self._media_language_settings: JWLanguageSettingsStore | None = None
        self._thread_pool = QThreadPool(self)
        self._worker: _FetchWorker | None = None

        # Carrega do cache imediatamente (síncrono, rápido)
        cached = self._load_cache()
        if cached:
            self._set_languages(cached)

    # ── Acesso aos dados ────────────────────────────────────────────────────────

    @property
    def languages(self) -> list[dict]:
        """Lista de idiomas disponíveis. Pode estar vazia se ainda carregando."""
        return self._languages

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    @property
    def has_data(self) -> bool:
        return bool(self._languages)

    def get_language(self, code: str) -> Optional[dict]:
        """Retorna o dict do idioma pelo código api JW (ex: 'T', 'E')."""
        return self._by_code.get(code)

    def vernacular_for(self, code: str, fallback: str = "") -> str:
        """Nome nativo do idioma para o código api JW."""
        lang = self._by_code.get(code)
        if lang:
            return lang.get("vernacular") or lang.get("name") or fallback
        return fallback

    # ── Idioma de mídia selecionado ─────────────────────────────────────────────

    def activate_settings(self, settings: JWLanguageSettingsStore) -> None:
        self._media_language_settings = settings

    def _require_media_language_settings(self) -> JWLanguageSettingsStore:
        if self._media_language_settings is None:
            raise RuntimeError("JW media language requested before profile activation.")
        return self._media_language_settings

    @property
    def media_api_code(self) -> str:
        return self._require_media_language_settings().media_language_code()

    def set_media_api_code(self, code: str) -> None:
        store = self._require_media_language_settings()
        old = store.media_language_code()
        store.set_media_language_code(code)
        log.debug("[JWLanguageService] Media language: %s", code)
        if code != old:
            self.media_language_changed.emit(code)

    @property
    def is_media_sign_language(self) -> bool:
        """
        Retorna True se o idioma de mídia selecionado for uma língua gestual
        (isSignLanguage=true na API JW.org).

        Implicações:
          • cânticos JW  → pub=sjj  (sem trilha de música separada)
          • clipes osg   → fileformat=MP4 (vídeo em língua gestual)
          • cânticos da interface (fallback) NUNCA são gestuais (sempre sjjm).
        """
        code = self.media_api_code
        if not code:
            return False
        lang = self._by_code.get(code)
        if lang is None:
            return False
        return bool(lang.get("isSignLanguage", False))

    # ── Fetch ───────────────────────────────────────────────────────────────────

    def fetch_if_needed(self) -> None:
        """
        Inicia fetch de rede se o cache expirou ou não existe.
        Se o cache ainda for válido, emite languages_ready imediatamente.
        """
        if self._is_loading:
            return

        if self._is_cache_valid():
            if self._languages:
                # já carregado no __init__
                self.languages_ready.emit(self._languages)
            else:
                cached = self._load_cache()
                if cached:
                    self._set_languages(cached)
                    self.languages_ready.emit(self._languages)
                else:
                    self._start_fetch()
        else:
            self._start_fetch()

    def force_refresh(self) -> None:
        """Força um novo fetch ignorando o cache."""
        if not self._is_loading:
            self._start_fetch()

    def _start_fetch(self) -> None:
        self._is_loading = True
        self.fetch_started.emit()
        worker = _FetchWorker()
        worker.signals.succeeded.connect(self._on_fetch_success)
        worker.signals.failed.connect(self._on_fetch_failed)
        self._worker = worker
        self._thread_pool.start(worker)

    def _on_fetch_success(self, languages: list) -> None:
        self._worker = None
        self._is_loading = False
        self._set_languages(languages)
        self._save_cache(languages)
        self.languages_ready.emit(self._languages)

    def _on_fetch_failed(self, error: str) -> None:
        self._worker = None
        self._is_loading = False
        self.fetch_failed.emit(error)
        # Se tiver cache antigo, ainda assim emite (melhor que nada)
        if self._languages:
            self.languages_ready.emit(self._languages)

    def shutdown(self) -> None:
        """Stop queued work and wait for an active language fetch."""
        self._thread_pool.clear()
        self._thread_pool.waitForDone()
        self._worker = None
        self._is_loading = False

    def _set_languages(self, languages: list) -> None:
        self._languages = languages
        self._by_code   = {
            lang["code"]: lang
            for lang in languages
            if isinstance(lang, dict) and lang.get("code")
        }

    # ── Cache em disco ──────────────────────────────────────────────────────────

    def _is_cache_valid(self) -> bool:
        if not self._cache_file.is_file():
            return False
        try:
            data = read_json_file(self._cache_file)
            age_days = (time.time() - data.get("_fetched_at", 0)) / 86400
            return age_days < _CACHE_TTL_DAYS and bool(data.get("languages"))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            return False

    def _load_cache(self) -> Optional[list]:
        try:
            data = read_json_file(self._cache_file)
            return data.get("languages") or None
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            AttributeError,
            TypeError,
        ):
            return None

    def _save_cache(self, languages: list) -> None:
        try:
            write_json_atomic(
                self._cache_file,
                {"_fetched_at": time.time(), "languages": languages},
            )
        except (OSError, TypeError, ValueError) as exc:
            log.warning("[JWLanguageService] Failed to save cache: %s", exc)
