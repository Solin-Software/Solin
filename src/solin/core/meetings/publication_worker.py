"""Qt worker for loading meeting JWPUB publications off the UI thread."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.publication_archive import (
    JwpubArchiveDownloadError,
    download_jwpub_archive,
    resolve_jwpub_archive,
    resolve_meeting_video,
)
from solin.core.media.download_storage import is_url_cached

from . import models as meeting_models
from .jwpub_cache import JwpubCache, JwpubChecksumStore, needs_jwpub_download
from .meeting_weeks import mwb_issue_for_week, watchtower_issue_candidates
from .publication_content import (
    parse_publication_ref_items,
    read_mwb_week_content,
    read_watchtower_study_content,
    sync_cbs_from_publication_refs,
    watchtower_issue_contains_week,
)

log = logging.getLogger(__name__)


# ── Worker — toda lógica bloqueante aqui, nunca na main thread ────────────────

class JwpubWorker(QObject):
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
        self._revalidate_mwb(monday, mwb_served)
        self._revalidate_wt(monday, wt_served)

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

    def _revalidate_mwb(self, monday: date, served: bool):
        """
        Fase 2 (SWR): consulta a API e só re-emite se o conteúdo do servidor
        mudou. ``served`` indica se a fase 1 já mostrou uma cópia do cache.
        """
        key   = monday.isoformat()
        issue = mwb_issue_for_week(monday)
        lang  = self._lang

        archive_info = resolve_jwpub_archive("mwb", lang, issue)
        url = archive_info.download_url
        checksum = archive_info.checksum
        not_found = archive_info.not_found

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

        if not needs_jwpub_download(
            self._cache, self._checksum_store, "mwb", lang, issue, checksum
        ):
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

    def _revalidate_wt(self, monday: date, served_issue: Optional[str]):
        """
        Fase 2 (SWR). Se já servimos uma edição do cache, revalida APENAS ela
        (a única que importa) e só re-emite se mudou. Caso contrário, cai no
        resolvedor frio que sonda os candidatos pela rede e baixa o correto.
        """
        lang = self._lang
        if served_issue is not None:
            key = monday.isoformat()
            archive_info = resolve_jwpub_archive("w", lang, served_issue)
            url = archive_info.download_url
            checksum = archive_info.checksum
            if url and needs_jwpub_download(
                self._cache, self._checksum_store, "w", lang, served_issue, checksum
            ):
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
            archive_info = resolve_jwpub_archive("w", lang, issue)
            url = archive_info.download_url
            checksum = archive_info.checksum
            not_found = archive_info.not_found
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

            if needs_jwpub_download(
                self._cache, self._checksum_store, "w", lang, issue, checksum
            ):
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

        archive_info = resolve_jwpub_archive(pub, lang, issue)
        url = archive_info.download_url
        checksum = archive_info.checksum
        if not url and issue != "0":
            fallback_info = resolve_jwpub_archive(pub, lang, "0")
            fallback_url = fallback_info.download_url
            fallback_checksum = fallback_info.checksum
            if fallback_url or self._cache.is_cached(pub, lang, "0"):
                url = fallback_url
                checksum = fallback_checksum
                issue = "0"
                ref.issue = "0"

        is_cached = self._cache.is_cached(pub, lang, issue)
        if not url and not is_cached:
            return [], False

        downloaded = False
        if url and needs_jwpub_download(
            self._cache, self._checksum_store, pub, lang, issue, checksum
        ):
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
        result = resolve_meeting_video(key_symbol, track, issue_tag, meps_doc_id, lang,
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
            resolved = resolve_meeting_video(
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
            download_jwpub_archive(
                url,
                dest,
                progress=lambda pct: self.progress.emit(key, pub_ui, pct),
            )
            # New .jwpub on disk — wipe the stale extract dir so the next
            # _ensure_extract() call unpacks the fresh content instead of
            # returning the old x_<issue> directory.
            self._cache.invalidate_extract(pub, lang, issue)
            return True
        except JwpubArchiveDownloadError as exc:
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

__all__ = ["JwpubWorker"]
