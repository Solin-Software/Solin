"""
publications.py ─ Solin
==========================
Downloads, caches, extracts and parses .jwpub files for the Meetings widget.

Threading model (production-grade):
────────────────────────────────────
REGRA ABSOLUTA: nada que bloqueie (HTTP, disk I/O pesado, SQLite) roda na
main thread. Toda comunicação entre worker e UI é via Signal/Slot com
QueuedConnection automática (QThread garante a thread affinity correta).

_JwpubWorker   — QObject que vive num QThread dedicado.
                 Faz toda a lógica: fetch de URL, download, extração zip,
                 parse SQLite. Emite sinais de resultado para a main thread.
JwpubService   — QObject na main thread. Cria/gerencia o worker thread,
                 recebe sinais e repassa para a UI.

Isso elimina:
  • HTTP bloqueante na main thread (_get_jwpub_url era síncrono)
  • QRunnable + _Signals com moveToThread (AutoConnection frágil)
  • threading.Thread emitindo signals (sem QThread wrapper = DirectConnection)
  • resolve_video() síncrono chamado de _MediaRow no __init__
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QObject, QThread, Signal, Slot,
)

from solin.core.network.http import HttpError, stream_get
from solin.core.jw.publication_links import (
    DEFAULT_USER_AGENT,
    JwpubMediaRequest,
    PublicationMediaRequest,
    PublicationMediaResolver,
)
from solin.core.media.cache import MediaCacheManager
from solin.core.media.download_storage import is_url_cached
from solin.core.media.settings import MediaSettingsStore
from . import models as meeting_models
from .jwpub_cache import JwpubCache, JwpubChecksumStore
from .meeting_weeks import (
    current_monday,
    mwb_issue_for_week,
    watchtower_issue_candidates,
)
from .publication_content import (
    parse_publication_ref_items,
    read_mwb_week_content,
    read_watchtower_study_content,
    sync_cbs_from_publication_refs,
    watchtower_issue_contains_week,
)

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_UA       = DEFAULT_USER_AGENT
_PUBLICATION_MEDIA_RESOLVER = PublicationMediaResolver()

def _get_jwpub_info(pub: str, lang: str, issue: str) -> tuple[Optional[str], str, bool]:
    """
    Query the JW pub-media API and return (download_url, checksum, not_found).

    not_found=True  → The API responded successfully but has no files for this
                       pub/lang/issue.  The publication genuinely does not exist
                       (equivalent to an HTTP 404).
    not_found=False → Either a URL was found, or the request failed due to a
                       network / parse error (we cannot confirm existence).

    Both url and checksum come from the same API call; checksum is '' when the
    server does not supply one.  Returns (None, '', False) on any network error.
    """
    media_info = _PUBLICATION_MEDIA_RESOLVER.resolve_jwpub(
        JwpubMediaRequest(pub=pub, language=lang, issue=issue)
    )
    return media_info.download_url, media_info.checksum, media_info.not_found


def _resolve_video(key_symbol: str, track: int, issue_tag: int,
                   meps_doc_id: int, lang: str,
                   is_sign_language: bool = False) -> dict:
    """
    Resolve video URL — deve ser chamado APENAS de worker threads.

    is_sign_language: quando True e key_symbol for 'sjjm', substitui por 'sjj'
    (língua gestual não usa a versão com música).
    """
    result = {"url": "", "title": "", "thumbnail": ""}
    try:
        media_file = _PUBLICATION_MEDIA_RESOLVER.resolve_video(
            PublicationMediaRequest(
                key_symbol=key_symbol,
                track=track,
                issue_tag=issue_tag,
                meps_doc_id=meps_doc_id,
                language=lang,
                is_sign_language=is_sign_language,
            )
        )
        if media_file is not None:
            result["url"] = media_file.url
            result["title"] = media_file.title
            result["thumbnail"] = media_file.thumbnail_url
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        log.debug("Could not parse resolved video metadata", exc_info=True)
    return result


# ── Worker — toda lógica bloqueante aqui, nunca na main thread ────────────────

class _JwpubWorker(QObject):
    """
    Roda num QThread dedicado. Toda operação bloqueante (HTTP, zip, SQLite)
    acontece aqui. Comunica com JwpubService exclusivamente via sinais —
    QThread garante QueuedConnection automática, sem moveToThread manual.

    Sinais emitidos para a main thread:
      mwb_done(key, WeekData)
      wt_done(key, WeekData)
      cbs_done(key, WeekData)
      progress(key, pub, pct)
      error(key, pub, msg)
      video_resolved(request_id, url, title, thumbnail)
      prefetch_requested(url)   — pede ao JwpubService para iniciar prefetch
    """
    mwb_done          = Signal(str, object)
    wt_done           = Signal(str, object)
    cbs_done          = Signal(str, object)
    progress          = Signal(str, str, int)
    error             = Signal(str, str, str)
    video_resolved    = Signal(str, str, str, str)   # request_id, url, title, thumb
    prefetch_requested = Signal(str)                  # url para prefetch

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_cache_dir = os.fspath(media_cache_dir)
        self._cache           = JwpubCache(jwpub_cache_dir)
        self._checksum_store  = checksum_store
        self._lang            = "T"
        self._is_sign_language = False

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(bool)
    def set_sign_language(self, is_sign: bool):
        """Informa se o idioma de mídia é gestual (afeta resolução de cânticos)."""
        self._is_sign_language = is_sign

    # ── Checksum-aware download gate ──────────────────────────────────────────

    def _needs_download(self, pub: str, lang: str, issue: str,
                        checksum: str, force: bool) -> bool:
        """
        Returns True when the file must be (re-)downloaded:
          • no local extract/archive exists yet
          • checksum changed compared with the real local archive
        Returns False when the cached copy is confirmed up-to-date.
        """
        has_extract = self._cache.is_cached(pub, lang, issue)
        archive = self._cache.jwpub_path(pub, lang, issue)
        has_archive = archive.is_file()

        if not has_extract and not has_archive:
            return True
        if not checksum:
            return False

        stored = self._checksum_store.get(pub, lang, issue)
        if stored == checksum:
            return False

        local_checksum = self._local_jwpub_checksum(archive) if has_archive else ""
        if local_checksum and local_checksum == checksum:
            self._checksum_store.save(pub, lang, issue, checksum)
            return False

        if not stored and has_extract and not has_archive:
            # Legacy cache: extracted DB exists but the original archive/checksum
            # does not. Trust the local usable cache and seed the remote checksum
            # so future launches do not redownload forever.
            self._checksum_store.save(pub, lang, issue, checksum)
            return False

        return True

    def _local_jwpub_checksum(self, path: Path) -> str:
        try:
            digest = hashlib.md5()  # nosec B324 - JW API exposes MD5 checksums
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    # ── Load week ─────────────────────────────────────────────────────────────

    @Slot(object, bool)
    def load_week(self, monday: date, force: bool = False):
        """
        Entry point: carrega MWB + WT para a semana dada.

        Stale-while-revalidate (SWR)
        ────────────────────────────
        Fase 1 — SERVIR: se houver cópia local utilizável, renderiza as duas
                 reuniões IMEDIATAMENTE, sem tocar na rede. É isto que torna um
                 launch "quente" instantâneo — nunca bloqueamos a tela só para
                 confirmar um arquivo que não mudou.
        Fase 2 — REVALIDAR: consulta a API em segundo plano (thread do worker).
                 Só baixa e re-emite quando o checksum do servidor realmente
                 mudou. Se nada mudou, nada acontece (sem segundo emit).

        force=True (retry de erro / troca de idioma) pula a fase de servir e faz
        uma revalidação limpa, mantendo a semântica de "recarregar de verdade".
        """
        mwb_served = False
        wt_served: Optional[str] = None
        if not force:
            mwb_served = self._serve_mwb_cached(monday)
            wt_served  = self._serve_wt_cached(monday)
        self._revalidate_mwb(monday, force, mwb_served)
        self._revalidate_wt(monday, force, wt_served)

    # ── MWB ───────────────────────────────────────────────────────────────────

    def _serve_mwb_cached(self, monday: date) -> bool:
        """Fase 1 (SWR): renderiza o MWB do cache local sem rede. True se servido."""
        issue = mwb_issue_for_week(monday)
        lang  = self._lang
        if not self._cache.is_cached("mwb", lang, issue):
            return False
        self._parse_mwb(
            meeting_models.WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
            monday, issue, lang,
        )
        return True

    def _revalidate_mwb(self, monday: date, force: bool, served: bool):
        """
        Fase 2 (SWR): consulta a API e só re-emite se o conteúdo do servidor
        mudou. ``served`` indica se a fase 1 já mostrou uma cópia do cache.
        """
        key   = monday.isoformat()
        issue = mwb_issue_for_week(monday)
        lang  = self._lang

        url, checksum, not_found = _get_jwpub_info("mwb", lang, issue)

        if not url:
            # API inalcançável, ou a publicação não existe para esta semana/idioma.
            if served:
                return  # já mostramos o cache — nada a fazer
            if self._cache.is_cached("mwb", lang, issue):
                log.warning("mwb %s: API unreachable, falling back to cached copy", issue)
                self._parse_mwb(
                    meeting_models.WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
                    monday, issue, lang,
                )
                return
            self.error.emit(
                key, "mwb", "NOT_FOUND" if not_found else f"No URL for mwb {issue}"
            )
            return

        if not self._needs_download("mwb", lang, issue, checksum, force):
            # Cópia local confirmada atual. Se já a servimos, "nada acontece".
            if not served:
                self._parse_mwb(
                    meeting_models.WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
                    monday, issue, lang,
                )
            return

        # Conteúdo mudou no servidor (ou nada em cache ainda) → baixa e re-renderiza.
        if not self._download("mwb", lang, issue, url, key, "mwb", emit_error=not served):
            return  # falhou; se já servimos cache, ele permanece na tela
        self._checksum_store.save("mwb", lang, issue, checksum)
        self._parse_mwb(
            meeting_models.WeekData(monday=monday, mwb_status="loading", mwb_issue=issue),
            monday, issue, lang,
        )

    def _parse_mwb(self, wd: meeting_models.WeekData, monday: date, issue: str, lang: str):
        key     = monday.isoformat()
        pub_dir = self._ensure_extract("mwb", lang, issue)
        db_path = self._cache.db_path("mwb", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.mwb_status = "error"
            self.error.emit(key, "mwb", "Extraction failed")
            return
        try:
            content = read_mwb_week_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse MWB publication %s/%s", lang, issue)
            wd.mwb_status = "error"
            self.error.emit(key, "mwb", str(exc))
            return
        if content is None:
            wd.mwb_status = "empty"
            self.mwb_done.emit(key, wd)
            return

        wd.mwb_pub_dir     = pub_dir
        wd.mwb_date_label  = content.date_label
        wd.mwb_week_title  = content.date_label
        wd.mwb_all_media   = content.media_items
        wd.mwb_publication_refs = content.publication_refs
        wd.mwb_cover_bytes = content.cover_bytes
        wd.mwb_status      = "ready"
        wd.cbs_ref         = content.cbs_ref
        sync_cbs_from_publication_refs(wd)
        if content.publication_refs:
            wd.cbs_status = "loading"
        self.mwb_done.emit(key, wd)
        if content.publication_refs:
            self._load_mwb_publication_refs(monday, wd, content.publication_refs)

    # ── WT ────────────────────────────────────────────────────────────────────

    def _serve_wt_cached(self, monday: date) -> Optional[str]:
        """
        Fase 1 (SWR): renderiza o WT do cache local sem rede.

        A edição de A Sentinela que contém a semana de estudo não é determinística
        (a edição de um mês pode conter estudos de outro), então tentamos cada
        candidato em ordem e servimos o PRIMEIRO que já está em cache E contém a
        semana. Retorna a edição servida, ou None se nenhuma cópia local serve.
        """
        lang = self._lang
        for issue in watchtower_issue_candidates(monday):
            if not self._cache.is_cached("w", lang, issue):
                continue
            wd = meeting_models.WeekData(monday=monday, wt_status="loading")
            if self._try_wt_cached(wd, monday, issue, lang):
                return issue
        return None

    def _revalidate_wt(self, monday: date, force: bool, served_issue: Optional[str]):
        """
        Fase 2 (SWR). Se já servimos uma edição do cache, revalida APENAS ela
        (a única que importa) e só re-emite se mudou. Caso contrário, cai no
        resolvedor frio que sonda os candidatos pela rede e baixa o correto.
        """
        lang = self._lang
        if served_issue is not None:
            key = monday.isoformat()
            url, checksum, _ = _get_jwpub_info("w", lang, served_issue)
            if url and self._needs_download("w", lang, served_issue, checksum, force):
                if self._download("w", lang, served_issue, url, key, "wt", emit_error=False):
                    self._checksum_store.save("w", lang, served_issue, checksum)
                    self._try_wt_cached(
                        meeting_models.WeekData(monday=monday, wt_status="loading"),
                        monday, served_issue, lang,
                    )
            return
        # Caminho frio: sem cache utilizável → sonda candidatos e baixa pela rede.
        self._download_wt_chain(
            monday,
            lang,
            watchtower_issue_candidates(monday)[:],
            force=force,
        )

    def _try_wt_cached(self, wd: meeting_models.WeekData, monday: date,
                        issue: str, lang: str) -> bool:
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            return False
        try:
            if not watchtower_issue_contains_week(db_path, monday):
                return False
        except (OSError, sqlite3.Error):
            return False
        self._parse_wt(wd, monday, issue, lang)
        return True

    def _download_wt_chain(
        self,
        monday: date,
        lang: str,
        candidates: list[str],
        _had_api_response: bool = False,
        *,
        force: bool = False,
    ):
        """
        Try each WT issue candidate in order.  Each candidate is independently
        cache/ checksum gated so a valid local issue is never downloaded again.

        When all candidates are exhausted:
          • _had_api_response=True  → at least one API call replied but had no files
                                       → emit NOT_FOUND (pub absent for this week)
          • _had_api_response=False → all calls failed with network errors
                                       → emit generic error (connectivity problem)
        """
        key = monday.isoformat()
        had_api = _had_api_response

        for issue in candidates:
            is_cached = self._cache.is_cached("w", lang, issue)
            url, checksum, not_found = _get_jwpub_info("w", lang, issue)
            had_api = had_api or not_found or bool(url)

            if not url:
                if is_cached:
                    log.warning(
                        "wt %s: API unreachable, falling back to cached copy",
                        issue,
                    )
                    wd = meeting_models.WeekData(monday=monday, wt_status="loading")
                    if self._try_wt_cached(wd, monday, issue, lang):
                        return
                continue

            if self._needs_download("w", lang, issue, checksum, force):
                if not self._download("w", lang, issue, url, key, "wt"):
                    continue
                self._checksum_store.save("w", lang, issue, checksum)

            wd = meeting_models.WeekData(monday=monday, wt_status="loading")
            if self._try_wt_cached(wd, monday, issue, lang):
                return

        wd = meeting_models.WeekData(monday=monday, wt_status="not_found" if had_api else "error")
        msg = "NOT_FOUND" if had_api else "No WT issue found for this week"
        self.error.emit(key, "wt", msg)

    def _parse_wt(self, wd: meeting_models.WeekData, monday: date, issue: str, lang: str):
        key     = monday.isoformat()
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.wt_status = "error"
            self.error.emit(key, "wt", "Extraction failed")
            return
        try:
            content = read_watchtower_study_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse WT publication %s/%s", lang, issue)
            wd.wt_status = "error"
            self.error.emit(key, "wt", str(exc))
            return
        if content is None:
            wd.wt_status = "empty"
            self.wt_done.emit(key, wd)
            return
        wd.wt_pub_dir     = pub_dir
        wd.wt_issue       = issue
        wd.wt_study_title = content.title
        wd.wt_all_media   = content.media_items
        wd.wt_cover_bytes = content.cover_bytes
        wd.wt_status      = "ready"
        self.wt_done.emit(key, wd)

    def _sync_loaded_publication_refs(self, wd: meeting_models.WeekData, lang: str) -> None:
        sync_cbs_from_publication_refs(wd)
        cbs_ref = next(
            (
                ref for ref in (wd.mwb_publication_refs or [])
                if getattr(ref, "is_cbs", False)
            ),
            None,
        )
        if cbs_ref:
            wd.cbs_pub_dir = self._cache.extract_dir(
                cbs_ref.pub,
                lang,
                cbs_ref.issue or "0",
            )

    def _load_mwb_publication_refs(
        self,
        monday: date,
        wd: meeting_models.WeekData,
        refs: list[meeting_models.MeetingPublicationRef],
    ):
        key = monday.isoformat()
        lang = self._lang

        def build(cache_only: bool) -> tuple[list[meeting_models.MeetingPublicationRef], bool]:
            out: list[meeting_models.MeetingPublicationRef] = []
            downloaded = False
            for ref in refs:
                items, did_dl = self._load_publication_ref_items(
                    key, lang, ref, cache_only=cache_only
                )
                downloaded = downloaded or did_dl
                if items:
                    ref.items = items
                    out.append(ref)
            return out, downloaded

        # ── Fase 1 (SWR): monta as referências do cache, sem rede ─────────────
        cached, _ = build(cache_only=True)
        if cached:
            wd.mwb_publication_refs = list(cached)
            self._sync_loaded_publication_refs(wd, lang)
            wd.cbs_status = "ready"
            self.cbs_done.emit(key, wd)

        # ── Fase 2 (SWR): revalida/baixa; só re-emite se algo mudou ───────────
        loaded, downloaded = build(cache_only=False)
        if loaded and (downloaded or len(loaded) != len(cached)):
            wd.mwb_publication_refs = loaded
            self._sync_loaded_publication_refs(wd, lang)
            wd.cbs_status = "ready"
            self.cbs_done.emit(key, wd)

    def _load_publication_ref_items(
        self,
        key: str,
        lang: str,
        ref: meeting_models.MeetingPublicationRef,
        cache_only: bool = False,
    ) -> tuple[list[meeting_models.MeetingMedia], bool]:
        """
        Resolve os itens de mídia de uma referência de publicação.

        cache_only=True  → fase de servir (SWR): usa só o cache local, sem rede.
        cache_only=False → fase de revalidar: consulta a API e baixa se mudou.

        Retorna (items, downloaded) — downloaded indica se um arquivo novo foi
        baixado nesta chamada (usado para decidir se re-emitir o lote).
        """
        pub = ref.pub
        issue = ref.issue or "0"

        if cache_only:
            for cand in ([issue] if issue == "0" else [issue, "0"]):
                if self._cache.is_cached(pub, lang, cand):
                    if cand != issue:
                        ref.issue = cand
                    return self._parse_ref_items(pub, lang, cand, ref), False
            return [], False

        url, checksum, _ = _get_jwpub_info(pub, lang, issue)
        if not url and issue != "0":
            fallback_url, fallback_checksum, _ = _get_jwpub_info(pub, lang, "0")
            if fallback_url or self._cache.is_cached(pub, lang, "0"):
                url = fallback_url
                checksum = fallback_checksum
                issue = "0"
                ref.issue = "0"

        is_cached = self._cache.is_cached(pub, lang, issue)
        if not url and not is_cached:
            return [], False

        downloaded = False
        if url and self._needs_download(pub, lang, issue, checksum, False):
            if not self._download(pub, lang, issue, url, key, "mwb", emit_error=False):
                return [], False
            self._checksum_store.save(pub, lang, issue, checksum)
            downloaded = True
        elif not url and is_cached:
            log.warning(
                "mwb ref %s/%s: API unreachable, using stale cache", pub, issue
            )

        return self._parse_ref_items(pub, lang, issue, ref), downloaded

    def _parse_ref_items(
        self, pub: str, lang: str, issue: str, ref: meeting_models.MeetingPublicationRef
    ) -> list[meeting_models.MeetingMedia]:
        pub_dir = self._ensure_extract(pub, lang, issue)
        db_path = self._cache.db_path(pub, lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            return []
        return parse_publication_ref_items(
            pub_dir,
            db_path,
            ref.meps_doc_id,
            "cbs" if ref.is_cbs else ref.section,
            ref.caption,
        )

    # ── Video URL resolution (async, called from main thread via signal) ──────

    @Slot(str, str, int, int, int, str)
    def resolve_video_async(self, request_id: str, key_symbol: str,
                             track: int, issue_tag: int,
                             meps_doc_id: int, lang: str):
        """Resolve URL de vídeo em background. Resultado via video_resolved signal."""
        result = _resolve_video(key_symbol, track, issue_tag, meps_doc_id, lang,
                                is_sign_language=self._is_sign_language)
        self.video_resolved.emit(
            request_id,
            result.get("url", ""),
            result.get("title", ""),
            result.get("thumbnail", ""),
        )

    # ── Auto-prefetch ─────────────────────────────────────────────────────────

    @Slot(object)
    def prefetch_week_media(self, wd: meeting_models.WeekData):
        """Resolve URLs e emite prefetch_requested para cada item não cacheado."""
        all_items = list(wd.mwb_all_media) + list(wd.wt_all_media)
        ref_items: list[meeting_models.MeetingMedia] = []
        for ref in getattr(wd, "mwb_publication_refs", []):
            ref_items.extend(getattr(ref, "items", []) or [])
        all_items.extend(ref_items or list(wd.cbs_items))
        for item in all_items:
            if "image" in (item.mime_type or ""):
                continue
            resolved = _resolve_video(
                item.key_symbol, item.track, item.issue_tag,
                item.meps_doc_id, self._lang,
                is_sign_language=self._is_sign_language,
            )
            url = resolved.get("url", "")
            if url and not is_url_cached(url, self._media_cache_dir):
                self.prefetch_requested.emit(url)

    # ── Internal download helper ───────────────────────────────────────────────

    def _download(self, pub: str, lang: str, issue: str,
                  url: str, key: str, pub_ui: str,
                  emit_error: bool = True) -> bool:
        """
        Baixa o arquivo, emitindo progress. Retorna True se sucesso.

        emit_error: quando False, uma falha de download NÃO emite o sinal de
        erro. Usado no caminho stale-while-revalidate: se já servimos uma cópia
        local e a revalidação em segundo plano falha (ex.: rede caiu no meio do
        download), mantemos o que já está na tela em vez de sobrescrever com erro.
        """
        dest = self._cache.jwpub_path(pub, lang, issue)
        try:
            with stream_get(url, timeout=60, headers={"User-Agent": _UA}) as resp:
                total  = int(resp.headers.get("Content-Length") or 0)
                done   = 0
                chunks = []
                last_pct = -1
                for chunk in resp.iter_bytes(256 * 1024):
                    chunks.append(chunk)
                    done += len(chunk)
                    pct = int(done / total * 100) if total else 0
                    if pct != last_pct:
                        last_pct = pct
                        self.progress.emit(key, pub_ui, pct)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"".join(chunks))
            # New .jwpub on disk — wipe the stale extract dir so the next
            # _ensure_extract() call unpacks the fresh content instead of
            # returning the old x_<issue> directory.
            self._cache.invalidate_extract(pub, lang, issue)
            return True
        except (HttpError, OSError, ValueError) as exc:
            if emit_error:
                self.error.emit(key, pub_ui, str(exc))
            else:
                log.warning("%s %s/%s: background re-download failed, keeping "
                            "served cache: %s", pub_ui, pub, issue, exc)
            return False

    def _ensure_extract(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        ep = self._cache.extract_dir(pub, lang, issue)
        if ep.exists() and any(ep.glob("*.db")):
            return ep
        return self._cache.extract(pub, lang, issue)


# ── JwpubService — vive na main thread, gerencia o worker thread ──────────────

class JwpubService(QObject):
    """
    API pública para a UI. Vive na main thread.
    Cria um _JwpubWorker num QThread dedicado e encaminha pedidos via sinais.

    Sinais para a UI:
      mwb_ready(key, WeekData)
      wt_ready(key, WeekData)
      cbs_ready(key, WeekData)
      week_ready(key, WeekData)
      progress(key, pub, pct)
      error_sig(key, pub, msg)
      video_resolved(request_id, url, title, thumbnail)
    """
    mwb_ready      = Signal(str, object)
    wt_ready       = Signal(str, object)
    cbs_ready      = Signal(str, object)
    week_ready     = Signal(str, object)
    progress       = Signal(str, str, int)
    error_sig      = Signal(str, str, str)
    video_resolved = Signal(str, str, str, str)   # request_id, url, title, thumb

    # Sinais internos para o worker (despacham para a worker thread)
    _sig_load_week         = Signal(object, bool)
    _sig_set_lang          = Signal(str)
    _sig_set_sign_language = Signal(bool)
    _sig_resolve           = Signal(str, str, int, int, int, str)
    _sig_prefetch_wd       = Signal(object)

    def __init__(
        self,
        media_settings: MediaSettingsStore,
        cache_manager: MediaCacheManager,
        jwpub_cache_dir: str | os.PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_settings = media_settings
        self._cache_manager = cache_manager
        self._active: dict[str, meeting_models.WeekData] = {}
        self._lang   = "T"
        self._is_sign_language = False

        # Cria worker + thread dedicada
        self._thread = QThread(self)
        self._worker = _JwpubWorker(
            cache_manager.media_cache_dir,
            jwpub_cache_dir,
            checksum_store,
        )
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        # Worker → JwpubService (main thread, QueuedConnection automática)
        self._worker.mwb_done.connect(self._on_mwb_done)
        self._worker.wt_done.connect(self._on_wt_done)
        self._worker.cbs_done.connect(self._on_cbs_done)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.error.connect(self._on_worker_error)
        self._worker.video_resolved.connect(self.video_resolved)
        self._worker.prefetch_requested.connect(self._on_prefetch_requested)

        # JwpubService → Worker (worker thread, QueuedConnection automática)
        self._sig_load_week.connect(self._worker.load_week)
        self._sig_set_lang.connect(self._worker.set_lang)
        self._sig_set_sign_language.connect(self._worker.set_sign_language)
        self._sig_resolve.connect(self._worker.resolve_video_async)
        self._sig_prefetch_wd.connect(self._worker.prefetch_week_media)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread de reuniões."""
        thread: QThread | None = getattr(self, "_thread", None)
        if thread is None:
            return
        try:
            running = thread.isRunning()
        except RuntimeError:
            self._thread = None
            return

        if running:
            thread.quit()
            thread.wait(wait_ms)
            try:
                still_running = thread.isRunning()
            except RuntimeError:
                self._thread = None
                return
            if still_running and delete_when_stopped:
                self.setParent(None)
                thread.finished.connect(self.deleteLater)

    def __del__(self):
        try:
            self.shutdown(wait_ms=0)
        except Exception:  # noqa: BLE001 - destructors must never raise during interpreter shutdown
            log.debug("Failed to shutdown JwpubService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        self._lang = lang
        self._sig_set_lang.emit(lang)

    def set_sign_language(self, is_sign: bool) -> None:
        """
        Informa ao worker se o idioma de mídia é gestual.
        Quando True, cânticos com key_symbol='sjjm' são resolvidos como 'sjj'.
        Deve ser chamado sempre que o idioma de mídia mudar.
        """
        self._is_sign_language = bool(is_sign)
        self._sig_set_sign_language.emit(is_sign)

    def get_lang(self) -> str:
        return self._lang

    def is_sign_language(self) -> bool:
        return self._is_sign_language

    def load_week(self, monday: date, force: bool = False):
        key = monday.isoformat()
        if not force and key in self._active:
            # Já existe entrada — só recarrega se alguma metade da semana falhou.
            existing = self._active[key]
            if existing.mwb_status not in ("error",) and existing.wt_status not in ("error",):
                return
        wd = meeting_models.WeekData(monday=monday)
        self._active[key] = wd
        self._sig_load_week.emit(monday, force)

    def get_week_data(self, monday: date) -> Optional[meeting_models.WeekData]:
        return self._active.get(monday.isoformat())

    def resolve_video_async(self, request_id: str, item: "meeting_models.MeetingMedia"):
        """
        Resolve URL de vídeo de forma assíncrona.
        Resultado chega via signal video_resolved(request_id, url, title, thumb).
        NUNCA bloqueia a main thread.
        """
        self._sig_resolve.emit(
            request_id,
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
        )

    def resolve_video(self, item: "meeting_models.MeetingMedia") -> dict:
        """
        Resolve URL de vídeo de forma SÍNCRONA (bloqueia a main thread ~200ms).
        Use apenas para ações pontuais do usuário (ex: clique em reproduzir),
        nunca durante construção de widgets ou loops.
        Para uso não-bloqueante, prefira resolve_video_async().
        """
        return _resolve_video(
            item.key_symbol, item.track, item.issue_tag,
            item.meps_doc_id, self._lang,
            is_sign_language=self._is_sign_language,
        )

    def clear_week(self, monday: date):
        self._active.pop(monday.isoformat(), None)

    def auto_download_if_enabled(self):
        if not self._media_settings.meetings_auto_download():
            return
        mon      = current_monday()
        next_mon = mon + timedelta(weeks=1)

        # Conecta antes de disparar load_week — garante que não perdemos o sinal
        # caso o load seja muito rápido (cache quente).
        if not getattr(self, "_auto_dl_connected", False):
            self._auto_dl_connected = True
            self.mwb_ready.connect(self._on_auto_dl_ready)
            self.wt_ready.connect(self._on_auto_dl_ready)
            self.cbs_ready.connect(self._on_auto_dl_ready)

        # Se a semana atual já está pronta (cache quente), o mwb_ready já foi emitido
        # antes desta conexão — dispara o prefetch diretamente.
        mon_wd = self._active.get(mon.isoformat())
        if mon_wd and mon_wd.mwb_status == "ready":
            self._sig_prefetch_wd.emit(mon_wd)

        # A semana atual já foi carregada por _navigate_to — não recarregar.
        # Apenas carrega a semana seguinte (que ainda não foi pedida).
        self.load_week(next_mon)

    @Slot(str, object)
    def _on_auto_dl_ready(self, key: str, wd: object):
        if not self._media_settings.meetings_auto_download():
            return
        mon = current_monday()
        target_keys = {mon.isoformat(), (mon + timedelta(weeks=1)).isoformat()}
        if key in target_keys:
            self._sig_prefetch_wd.emit(wd)

    # ── Worker callbacks (chegam na main thread via QueuedConnection) ─────────

    @Slot(str, object)
    def _on_mwb_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.mwb_pub_dir     = wd.mwb_pub_dir
            existing.mwb_cover_bytes = wd.mwb_cover_bytes
            existing.mwb_date_label  = wd.mwb_date_label
            existing.mwb_week_title  = wd.mwb_week_title
            existing.mwb_all_media   = wd.mwb_all_media
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.mwb_status      = wd.mwb_status
            existing.mwb_issue       = wd.mwb_issue
            existing.cbs_ref         = wd.cbs_ref
            existing.cbs_status      = wd.cbs_status
            self.mwb_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.mwb_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_wt_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.wt_pub_dir     = wd.wt_pub_dir
            existing.wt_cover_bytes = wd.wt_cover_bytes
            existing.wt_study_title = wd.wt_study_title
            existing.wt_issue       = wd.wt_issue
            existing.wt_all_media   = wd.wt_all_media
            existing.wt_status      = wd.wt_status
            self.wt_ready.emit(key, existing)
            self._check_complete(key)
        else:
            self._active[key] = wd
            self.wt_ready.emit(key, wd)
            self._check_complete(key)

    @Slot(str, object)
    def _on_cbs_done(self, key: str, wd: meeting_models.WeekData):
        existing = self._active.get(key)
        if existing:
            existing.cbs_pub_dir = wd.cbs_pub_dir
            existing.cbs_items   = wd.cbs_items
            existing.mwb_publication_refs = wd.mwb_publication_refs
            existing.cbs_status  = wd.cbs_status
            self.cbs_ready.emit(key, existing)
        else:
            self._active[key] = wd
            self.cbs_ready.emit(key, wd)

    @Slot(str, str, int)
    def _on_worker_progress(self, key: str, pub: str, pct: int):
        self.progress.emit(key, pub, pct)

    @Slot(str, str, str)
    def _on_worker_error(self, key: str, pub: str, msg: str):
        # "NOT_FOUND" is a sentinel emitted by the worker when the JW API
        # confirmed the publication does not exist (empty files list).
        # Any other message means a generic connectivity / extraction failure.
        status = "not_found" if msg == "NOT_FOUND" else "error"
        wd = self._active.get(key)
        if wd:
            if pub == "mwb":
                wd.mwb_status = status
            elif pub == "wt":
                wd.wt_status = status
        self.error_sig.emit(key, pub, msg)

    @Slot(str)
    def _on_prefetch_requested(self, url: str):
        """Recebe pedido de prefetch do worker — já estamos na main thread."""
        mgr = self._cache_manager
        if not mgr.is_cached(url) and not mgr.is_prefetching(url):
            mgr.prefetch(url)

    def _check_complete(self, key: str):
        wd = self._active.get(key)
        if wd and wd.mwb_status in ("ready", "empty") \
                and wd.wt_status in ("ready", "empty"):
            self.week_ready.emit(key, wd)


# ── Public helpers ────────────────────────────────────────────────────────────

