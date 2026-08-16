"""Qt worker for loading meeting JWPUB publications off the UI thread."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import date
from os import PathLike
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.publication_archive import (
    JwpubArchiveDownloadError,
    download_jwpub_archive,
    resolve_jwpub_archive,
    resolve_meeting_media,
)

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
      media_resolved(request_id, metadata)
    """

    mwb_done = Signal(str, object)
    wt_done = Signal(str, object)
    cbs_done = Signal(str, object)
    load_finished = Signal(str, str, bool, int)
    progress = Signal(str, str, int, str, bool, int)
    error = Signal(str, str, str, str, bool, int)
    media_resolved = Signal(str, object)  # request_id, resolved metadata

    def __init__(
        self,
        jwpub_cache_dir: str | PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cache = JwpubCache(jwpub_cache_dir)
        self._checksum_store = checksum_store
        self._lang = "T"
        self._is_sign_language = False
        self._request_generation = 0
        self._cancelled = threading.Event()

    def request_cancel(self) -> None:
        """Request cancellation safely from the main thread."""
        self._cancelled.set()

    def _emit_if_active(self, signal, *args) -> None:
        if not self._cancelled.is_set():
            signal.emit(*args)

    def _new_week_data(self, monday: date, **values) -> meeting_models.WeekData:
        return meeting_models.WeekData(
            monday=monday,
            language_code=self._lang,
            is_sign_language=self._is_sign_language,
            request_generation=self._request_generation,
            **values,
        )

    def _emit_error(self, key: str, publication: str, message: str) -> None:
        self._emit_if_active(
            self.error,
            key,
            publication,
            message,
            self._lang,
            self._is_sign_language,
            self._request_generation,
        )

    def _emit_progress(self, key: str, publication: str, percent: int) -> None:
        self._emit_if_active(
            self.progress,
            key,
            publication,
            percent,
            self._lang,
            self._is_sign_language,
            self._request_generation,
        )

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(bool)
    def set_sign_language(self, is_sign: bool):
        """Informa se o idioma de mídia é gestual (afeta resolução de cânticos)."""
        self._is_sign_language = is_sign

    # ── Load week ─────────────────────────────────────────────────────────────

    @Slot(object, bool, str, bool, int, object, str, object, object)
    def load_week(
        self,
        monday: date,
        force: bool = False,
        language: str = "T",
        is_sign_language: bool = False,
        generation: int = 0,
        materialize_cached_publications: object = None,
        known_wt_issue: str = "",
        persisted_source_checksums: object = None,
        repair_cache_paths: object = None,
    ):
        """
        Entry point: carrega MWB + WT para a semana dada.

        Stale-while-revalidate (SWR)
        ────────────────────────────
        Fase 1 — MATERIALIZAR: se a árvore persistida daquela publicação ainda
                 não existe ou não possui baseline canônico, usa uma cópia local
                 confirmada. Uma árvore com baseline já persistido é a fonte de
                 verdade e não é reconstruída a partir do JWPUB cacheado.
        Fase 2 — REVALIDAR: consulta a API em segundo plano (thread do worker).
                 Só baixa, lê e re-emite quando o checksum do servidor realmente
                 mudou. Árvores antigas sem baseline recebem uma única
                 materialização confirmada para registrar a estrutura oficial.

        force=True (retry de erro / troca de idioma) pula a fase de servir e faz
        uma revalidação limpa, mantendo a semântica de "recarregar de verdade".
        """
        if self._cancelled.is_set():
            return
        self._lang = language or "T"
        self._is_sign_language = bool(is_sign_language)
        self._request_generation = max(0, int(generation))
        cached_publications = (
            materialize_cached_publications
            if isinstance(materialize_cached_publications, Iterable)
            and not isinstance(materialize_cached_publications, (str, bytes))
            else ()
        )
        materialize_cached = frozenset(
            str(pub_type) for pub_type in cached_publications if pub_type in {"mwb", "wt"}
        )
        persisted_checksums = (
            persisted_source_checksums if isinstance(persisted_source_checksums, Mapping) else {}
        )
        mwb_source_checksum = str(persisted_checksums.get("mwb") or "")
        wt_source_checksum = str(persisted_checksums.get("wt") or "")
        repair_paths = (
            repair_cache_paths
            if isinstance(repair_cache_paths, Iterable)
            and not isinstance(repair_cache_paths, (str, bytes))
            else ()
        )
        self._cache.repair_extracts_for_sources(
            source
            for source in repair_paths
            if isinstance(source, (str, PathLike))
        )
        try:
            mwb_served = False
            wt_served: Optional[str] = None
            if not force:
                if "mwb" in materialize_cached:
                    mwb_served = self._serve_mwb_cached(monday)
                if "wt" in materialize_cached:
                    wt_served = self._serve_wt_cached(monday)
            self._revalidate_mwb(
                monday,
                mwb_served,
                materialize_cached="mwb" in materialize_cached,
                persisted_source_checksum=mwb_source_checksum,
            )
            self._revalidate_wt(
                monday,
                wt_served,
                materialize_cached="wt" in materialize_cached,
                known_issue=str(known_wt_issue or ""),
                persisted_source_checksum=wt_source_checksum,
            )
        finally:
            self._emit_if_active(
                self.load_finished,
                monday.isoformat(),
                self._lang,
                self._is_sign_language,
                self._request_generation,
            )

    # ── MWB ───────────────────────────────────────────────────────────────────

    def _serve_mwb_cached(self, monday: date) -> bool:
        """Fase 1 (SWR): renderiza o MWB do cache local sem rede. True se servido."""
        issue = mwb_issue_for_week(monday)
        lang = self._lang
        if not self._cache.can_materialize("mwb", lang, issue):
            return False
        self._parse_mwb(
            self._new_week_data(
                monday,
                mwb_status="loading",
                mwb_issue=issue,
                mwb_source_checksum=self._checksum_store.get("mwb", lang, issue),
            ),
            monday,
            issue,
            lang,
        )
        return True

    def _revalidate_mwb(
        self,
        monday: date,
        served: bool,
        *,
        materialize_cached: bool,
        persisted_source_checksum: str,
    ):
        """
        Fase 2 (SWR): consulta a API e só re-emite se o conteúdo do servidor
        mudou. ``served`` indica se a fase 1 já mostrou uma cópia do cache.
        """
        key = monday.isoformat()
        issue = mwb_issue_for_week(monday)
        lang = self._lang

        archive_info = resolve_jwpub_archive("mwb", lang, issue)
        url = archive_info.download_url
        checksum = archive_info.checksum
        not_found = archive_info.not_found

        if not url:
            # API inalcançável, ou a publicação não existe para esta semana/idioma.
            if served or not materialize_cached:
                return  # já mostramos o cache — nada a fazer
            if self._cache.can_materialize("mwb", lang, issue):
                log.warning("mwb %s: API unreachable, falling back to cached copy", issue)
                self._parse_mwb(
                    self._new_week_data(
                        monday,
                        mwb_status="loading",
                        mwb_issue=issue,
                        mwb_source_checksum=self._checksum_store.get("mwb", lang, issue),
                    ),
                    monday,
                    issue,
                    lang,
                )
                return
            self._emit_error(key, "mwb", "NOT_FOUND" if not_found else f"No URL for mwb {issue}")
            return

        needs_materialization_download = materialize_cached and not served
        if not needs_materialization_download and not self._needs_jwpub_refresh(
            "mwb",
            lang,
            issue,
            checksum,
            persisted_source_checksum=persisted_source_checksum,
        ):
            if (not served and materialize_cached) or (
                checksum and checksum != persisted_source_checksum
            ):
                self._parse_mwb(
                    self._new_week_data(
                        monday,
                        mwb_status="loading",
                        mwb_issue=issue,
                        mwb_source_checksum=checksum,
                    ),
                    monday,
                    issue,
                    lang,
                )
            return

        # Conteúdo mudou no servidor (ou nada em cache ainda) → baixa e re-renderiza.
        if not self._download(
            "mwb",
            lang,
            issue,
            url,
            key,
            "mwb",
            emit_error=not (served or not materialize_cached),
        ):
            return  # falhou; se já servimos cache, ele permanece na tela
        self._checksum_store.save("mwb", lang, issue, checksum)
        self._parse_mwb(
            self._new_week_data(
                monday,
                mwb_status="loading",
                mwb_issue=issue,
                mwb_source_checksum=checksum,
            ),
            monday,
            issue,
            lang,
        )

    def _parse_mwb(self, wd: meeting_models.WeekData, monday: date, issue: str, lang: str):
        key = monday.isoformat()
        pub_dir = self._ensure_extract("mwb", lang, issue)
        db_path = self._cache.db_path("mwb", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.mwb_status = "error"
            self._emit_error(key, "mwb", "Extraction failed")
            return
        try:
            content = read_mwb_week_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse MWB publication %s/%s", lang, issue)
            wd.mwb_status = "error"
            self._emit_error(key, "mwb", str(exc))
            return
        if content is None:
            wd.mwb_status = "empty"
            self._emit_if_active(self.mwb_done, key, wd)
            return

        wd.mwb_pub_dir = pub_dir
        wd.mwb_date_label = content.date_label
        wd.mwb_week_title = content.date_label
        wd.mwb_all_media = content.media_items
        wd.mwb_publication_refs = content.publication_refs
        wd.mwb_cover_bytes = content.cover_bytes
        wd.mwb_status = "ready"
        wd.cbs_ref = content.cbs_ref
        sync_cbs_from_publication_refs(wd)
        if content.publication_refs:
            wd.cbs_status = "loading"
        self._emit_if_active(self.mwb_done, key, wd)
        if content.publication_refs:
            try:
                self._load_mwb_publication_refs(
                    monday,
                    wd,
                    content.publication_refs,
                )
            except Exception:  # noqa: BLE001 - worker boundary must terminate the context
                log.exception(
                    "Could not load MWB publication references for %s/%s",
                    lang,
                    issue,
                )
                wd.cbs_status = "error"
                self._emit_if_active(self.cbs_done, key, wd)

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
            if not self._cache.can_materialize("w", lang, issue):
                continue
            wd = self._new_week_data(
                monday,
                wt_status="loading",
                wt_source_checksum=self._checksum_store.get("w", lang, issue),
            )
            if self._try_wt_cached(wd, monday, issue, lang):
                return issue
        return None

    def _revalidate_wt(
        self,
        monday: date,
        served_issue: Optional[str],
        *,
        materialize_cached: bool,
        known_issue: str,
        persisted_source_checksum: str,
    ):
        """
        Fase 2 (SWR). Revalida a edição servida do cache ou a edição registrada
        na árvore persistida e só re-emite se mudou. Sem uma edição conhecida,
        cai no resolvedor frio que sonda os candidatos pela rede.
        """
        lang = self._lang
        issue = served_issue or known_issue
        if issue:
            key = monday.isoformat()
            archive_info = resolve_jwpub_archive("w", lang, issue)
            url = archive_info.download_url
            checksum = archive_info.checksum
            needs_materialization_download = materialize_cached and served_issue is None
            if url and (
                needs_materialization_download
                or self._needs_jwpub_refresh(
                    "w",
                    lang,
                    issue,
                    checksum,
                    persisted_source_checksum=persisted_source_checksum,
                )
            ):
                if self._download(
                    "w",
                    lang,
                    issue,
                    url,
                    key,
                    "wt",
                    emit_error=materialize_cached,
                ):
                    self._checksum_store.save("w", lang, issue, checksum)
                    self._try_wt_cached(
                        self._new_week_data(
                            monday,
                            wt_status="loading",
                            wt_source_checksum=checksum,
                        ),
                        monday,
                        issue,
                        lang,
                    )
            elif checksum and checksum != persisted_source_checksum:
                self._try_wt_cached(
                    self._new_week_data(
                        monday,
                        wt_status="loading",
                        wt_source_checksum=checksum,
                    ),
                    monday,
                    issue,
                    lang,
                )
            return
        if not materialize_cached:
            return
        # Caminho frio: sem cache utilizável → sonda candidatos e baixa pela rede.
        self._download_wt_chain(
            monday,
            lang,
            watchtower_issue_candidates(monday)[:],
        )

    def _try_wt_cached(
        self, wd: meeting_models.WeekData, monday: date, issue: str, lang: str
    ) -> bool:
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
            is_cached = self._cache.can_materialize("w", lang, issue)
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
                    wd = self._new_week_data(
                        monday,
                        wt_status="loading",
                        wt_source_checksum=self._checksum_store.get("w", lang, issue),
                    )
                    if self._try_wt_cached(wd, monday, issue, lang):
                        return
                continue

            if needs_jwpub_download(self._cache, self._checksum_store, "w", lang, issue, checksum):
                if not self._download("w", lang, issue, url, key, "wt"):
                    continue
                self._checksum_store.save("w", lang, issue, checksum)

            wd = self._new_week_data(
                monday,
                wt_status="loading",
                wt_source_checksum=checksum,
            )
            if self._try_wt_cached(wd, monday, issue, lang):
                return

        wd = self._new_week_data(
            monday,
            wt_status="not_found" if had_api else "error",
        )
        msg = "NOT_FOUND" if had_api else "No WT issue found for this week"
        self._emit_error(key, "wt", msg)

    def _needs_jwpub_refresh(
        self,
        publication: str,
        language: str,
        issue: str,
        checksum: str,
        *,
        persisted_source_checksum: str,
    ) -> bool:
        """Return whether a confirmed remote revision requires tree rebuilding."""

        if checksum and persisted_source_checksum and persisted_source_checksum == checksum:
            return False
        return needs_jwpub_download(
            self._cache,
            self._checksum_store,
            publication,
            language,
            issue,
            checksum,
        )

    def _parse_wt(self, wd: meeting_models.WeekData, monday: date, issue: str, lang: str):
        key = monday.isoformat()
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.wt_status = "error"
            self._emit_error(key, "wt", "Extraction failed")
            return
        try:
            content = read_watchtower_study_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse WT publication %s/%s", lang, issue)
            wd.wt_status = "error"
            self._emit_error(key, "wt", str(exc))
            return
        if content is None:
            wd.wt_status = "empty"
            self._emit_if_active(self.wt_done, key, wd)
            return
        wd.wt_pub_dir = pub_dir
        wd.wt_issue = issue
        wd.wt_study_title = content.title
        wd.wt_all_media = content.media_items
        wd.wt_cover_bytes = content.cover_bytes
        wd.wt_status = "ready"
        self._emit_if_active(self.wt_done, key, wd)

    def _sync_loaded_publication_refs(self, wd: meeting_models.WeekData, lang: str) -> None:
        sync_cbs_from_publication_refs(wd)
        cbs_ref = next(
            (ref for ref in (wd.mwb_publication_refs or []) if getattr(ref, "is_cbs", False)),
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
            self._emit_if_active(self.cbs_done, key, wd)

        # ── Fase 2 (SWR): revalida/baixa; só re-emite se algo mudou ───────────
        loaded, downloaded = build(cache_only=False)
        if loaded and (downloaded or len(loaded) != len(cached)):
            wd.mwb_publication_refs = loaded
            self._sync_loaded_publication_refs(wd, lang)
            wd.cbs_status = "ready"
            self._emit_if_active(self.cbs_done, key, wd)
        elif not cached and not loaded:
            wd.mwb_publication_refs = []
            wd.cbs_status = "empty"
            self._emit_if_active(self.cbs_done, key, wd)

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
            for cand in [issue] if issue == "0" else [issue, "0"]:
                if self._cache.can_materialize(pub, lang, cand):
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
            if fallback_url or self._cache.can_materialize(pub, lang, "0"):
                url = fallback_url
                checksum = fallback_checksum
                issue = "0"
                ref.issue = "0"

        is_cached = self._cache.can_materialize(pub, lang, issue)
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
            log.warning("mwb ref %s/%s: API unreachable, using stale cache", pub, issue)

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

    # ── Media URL resolution (async, called from main thread via signal) ──────

    @Slot(str, str, int, int, int, str, str, bool)
    def resolve_media_async(
        self,
        request_id: str,
        key_symbol: str,
        track: int,
        issue_tag: int,
        meps_doc_id: int,
        media_type: str,
        lang: str,
        is_sign_language: bool,
    ):
        """Resolve meeting media metadata in the worker thread."""
        result = resolve_meeting_media(
            key_symbol,
            track,
            issue_tag,
            meps_doc_id,
            lang,
            is_sign_language=is_sign_language,
            media_type=media_type,
        )
        self._emit_if_active(
            self.media_resolved,
            request_id,
            result,
        )

    # ── Internal download helper ───────────────────────────────────────────────

    def _download(
        self,
        pub: str,
        lang: str,
        issue: str,
        url: str,
        key: str,
        pub_ui: str,
        emit_error: bool = True,
    ) -> bool:
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
                progress=lambda pct: self._emit_progress(key, pub_ui, pct),
                cancelled=self._cancelled.is_set,
            )
            # Keep the currently presented files available until a complete
            # replacement has been extracted in staging.
            self._cache.mark_extract_stale(pub, lang, issue)
            return True
        except JwpubArchiveDownloadError as exc:
            if emit_error:
                self._emit_error(key, pub_ui, str(exc))
            else:
                log.warning(
                    "%s %s/%s: background re-download failed, keeping served cache: %s",
                    pub_ui,
                    pub,
                    issue,
                    exc,
                )
            return False

    def _ensure_extract(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        if self._cache.is_cached(pub, lang, issue):
            return self._cache.extract_dir(pub, lang, issue)
        return self._cache.extract(pub, lang, issue)


__all__ = ["JwpubWorker"]
