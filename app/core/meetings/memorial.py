"""
memorial.py ─ Solin
==============================
Serviço de mídia da Celebração do Memorial JW.

Responsabilidades:
  1. Calcular a data exata do Memorial do ano atual (algoritmo JW moderno)
  2. Determinar a semana em que o Memorial cai
  3. Baixar e extrair o JWPUB mi<YY> com curl_cffi (browser-friendly)
  4. Consultar SQLite: capa (CategoryType=26) + vídeos intro (CategoryType=-1)
  5. Resolver URLs dos vídeos via API GETPUBMEDIALINKS
  6. Armazenar em cache (estrutura idêntica ao JwpubCache)
  7. Emitir sinais para a UI (main thread via QueuedConnection)

Threading:
  _MemorialWorker  — QObject num QThread dedicado (toda I/O bloqueante aqui)
  MemorialService  — QObject na main thread (API pública para a UI)

Política de fetch:
  O worker só busca mídias se a data atual está a ≤ 7 dias do Memorial.
  Fora desse período, emite memorial_not_yet/memorial_past (UI pode ignorar).
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

log = logging.getLogger(__name__)

from app.core.network.http import urlopen as _urlopen

# ── Reutiliza constantes e helpers de publications.py ─────────────────────────
from .publications import (
    MEDIA_API, _UA, _TIMEOUT,
    JwpubCache, get_checksum_store,
    MeetingMedia,
    _get_json as _jwpub_get_json,   # urllib simples — API pública não precisa de UA spoofing
)

# ── HTTP com curl_cffi (browser-friendly) ──────────────────────────────────────

try:
    from curl_cffi import requests as _cffi  # type: ignore[reportMissingImports]
    _HAS_CFFI = True
except ImportError:
    import urllib.request as _urllib_req  # type: ignore
    _HAS_CFFI = False


class MemorialDownloadError(RuntimeError):
    """Transport-independent failure while downloading Memorial resources."""


def _chrome_headers() -> dict:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language":  "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control":    "no-cache",
        "Pragma":           "no-cache",
        "Sec-Ch-Ua": (
            '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        ),
        "Sec-Ch-Ua-Mobile":    "?0",
        "Sec-Ch-Ua-Platform":  '"Windows"',
        "Sec-Fetch-Dest":      "document",
        "Sec-Fetch-Mode":      "navigate",
        "Sec-Fetch-Site":      "none",
        "Sec-Fetch-User":      "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    """
    GET com TLS browser impersonation via curl_cffi.
    Fallback para urllib quando curl_cffi não estiver disponível.
    """
    import time
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if _HAS_CFFI:
                r = _cffi.get(
                    url, headers=_chrome_headers(),
                    timeout=timeout, impersonate="chrome124",
                    allow_redirects=True,
                )
                r.raise_for_status()
                return r.content
            else:
                req = _urllib_req.Request(url, headers={"User-Agent": _UA})
                with _urlopen(req, timeout=timeout) as resp:
                    return resp.read()
        except Exception as exc:  # noqa: BLE001 - curl_cffi/urllib transport boundary
            last = exc
            if attempt < retries:
                time.sleep(2.0 ** attempt)
    if last is None:
        raise MemorialDownloadError(f"No download attempt was made for {url}")
    raise MemorialDownloadError(f"Could not download {url}: {last}") from last


def _http_get_json(url: str) -> Optional[dict]:
    try:
        data = _http_get(url, timeout=_TIMEOUT)
        return json.loads(data.decode())
    except (
        MemorialDownloadError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        log.warning("GET JSON %s → %s", url, exc)
        return None


# ── Memorial date calculator (algoritmo JW moderno, validado 2008-2028) ───────

def memorial_date_for_year(year: int) -> Optional[date]:
    """
    Calcula a data exata do Memorial JW para o ano dado.
    Algoritmo reconstruído por engenharia reversa — 21/21 anos modernos ✅
    Baseado em memorial_dates_calc.py.
    """
    try:
        import ephem
    except ImportError:
        log.error("ephem não instalado — não é possível calcular a data do Memorial")
        return None

    LAG_MIN  = 49.0   # crescent lag mínimo (min)
    LAG_MAX  = 150.0  # crescent lag máximo (min)
    AGE_MIN  = 22.0   # idade mínima da lua (h)

    jerusalem = ephem.Observer()
    jerusalem.lat       = "31.7683"
    jerusalem.lon       = "35.2137"
    jerusalem.elevation = 754

    try:
        equinox    = ephem.next_vernal_equinox(f"{year}/01/01")
        luna_prev  = ephem.previous_new_moon(equinox)
        luna_next  = ephem.next_new_moon(equinox)

        dist_prev  = abs(equinox.datetime() - luna_prev.datetime())
        dist_next  = abs(equinox.datetime() - luna_next.datetime())
        new_moon   = luna_prev.datetime() if dist_prev < dist_next else luna_next.datetime()

        test_day = new_moon
        for _ in range(35):
            jerusalem.date = test_day
            sunset = jerusalem.next_setting(ephem.Sun())

            jerusalem.date = sunset
            moonset = jerusalem.next_setting(ephem.Moon())

            lag = (moonset.datetime() - sunset.datetime()).total_seconds() / 60.0
            age = (sunset.datetime() - new_moon).total_seconds() / 3600.0

            if LAG_MIN <= lag <= LAG_MAX and age >= AGE_MIN:
                nisan1   = sunset.datetime()
                memorial = nisan1 + timedelta(days=13)
                if memorial < datetime(year, 3, 22):
                    test_day = sunset.datetime() + timedelta(hours=20)
                    continue
                return memorial.date()

            test_day = sunset.datetime() + timedelta(hours=20)

    except Exception:  # noqa: BLE001 - ephem exposes implementation-specific exceptions
        log.exception("Erro ao calcular data do Memorial %d", year)

    return None


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class MemorialData:
    year:         int             = 0
    memorial_date: Optional[date] = None   # data exata (14 Nisan)
    memorial_week: Optional[date] = None   # segunda-feira da semana
    # Capa: bytes PNG/JPG para exibir no card
    cover_bytes:  Optional[bytes] = None
    # Vídeos: lista de MeetingMedia (resolvidos ou a resolver)
    videos:       list            = field(default_factory=list)
    # URL do thumbnail quadrado (para a capa do card, ~270px)
    thumb_url:    str             = ""
    # Status: "idle" | "loading" | "ready" | "empty" | "error"
    #        | "not_yet" | "past" | "not_found" | "deleted"
    #  not_found → API confirmed the pub doesn't exist (yet) for this year/lang
    #  deleted   → API 404 + memorial date already passed (JW removed the files)
    status:       str             = "idle"
    pub_dir:      Optional[Path]  = None


# ── JWPUB helpers (worker-thread only) ────────────────────────────────────────

def _mi_pub(year: int) -> str:
    """mi26, mi27, ..."""
    return f"mi{str(year)[2:]}"


def _parse_mi_jwpub_response(data: dict, lang: str) -> tuple[Optional[str], str, str]:
    """
    Extrai (download_url, thumb_url, checksum) de uma resposta da GETPUBMEDIALINKS.
    Aceita o idioma solicitado ou faz fallback para "E" se necessário.
    checksum é '' quando a API não o fornece.
    """
    files_root = data.get("files", {})
    # Tenta o idioma pedido; se vazio, tenta inglês como fallback
    lang_files = files_root.get(lang) or files_root.get("E") or {}
    jwpub_list: list = lang_files.get("JWPUB", [])
    if not jwpub_list:
        return None, "", ""

    item      = jwpub_list[0]
    file_obj  = item.get("file", {})
    dl_url    = file_obj.get("url", "")
    checksum  = file_obj.get("checksum", "") or ""
    images    = item.get("images", {})

    # Thumb quadrado — estrutura típica: images.sqr/wss/lsr.{sm,md,lg}.url
    thumb = ""
    for key in ("sqr", "wss", "lsr"):
        section = images.get(key, {})
        for size in ("sm", "md", "lg"):
            candidate = section.get(size, {}).get("url", "")
            if candidate:
                thumb = candidate
                break
        if thumb:
            break

    return (dl_url or None), thumb, checksum


def _get_mi_jwpub_url(pub: str, lang: str) -> tuple[Optional[str], str, str, bool]:
    """
    Busca URL de download + thumbnail quadrado + checksum para o pub mi<YY>.
    Retorna (download_url, thumb_url, checksum, not_found).

    not_found=True  → API respondeu com JSON mas sem arquivos para este pub/lang
                       (publicação genuinamente ausente — equivalente a HTTP 404).
    not_found=False → URL encontrada, ou falha de rede (não conseguimos confirmar).

    Estratégia (production-grade):
      1. API direta via urllib simples — o endpoint é público e não requer
         browser fingerprinting; igual ao que publications.py usa em _get_json.
      2. Fallback com curl_cffi (browser impersonation) caso a chamada simples
         falhe por qualquer razão (bloqueio de rede, TLS restritivo, etc.).
    """
    params = {
        "pub":         pub,
        "issue":       "0",   # mi<YY> não tem número de edição; "0" é o valor correto
        "langwritten": lang,
        "fileformat":  "JWPUB",
        "output":      "json",
        "alllangs":    "0",
        "txtCMSLang":  "E",
    }

    _any_api_response = False   # tracks whether any attempt got a valid JSON reply

    # ── Passo 1: API direta (urllib — sem UA spoofing) ────────────────────────
    data = _jwpub_get_json(MEDIA_API, params)
    if data:
        _any_api_response = True
        result = _parse_mi_jwpub_response(data, lang)
        if result[0]:                          # dl_url presente → sucesso
            log.debug("_get_mi_jwpub_url: URL obtida via API direta para %s/%s", pub, lang)
            return result[0], result[1], result[2], False

    # ── Passo 2: Fallback — browser impersonation via curl_cffi ──────────────
    log.warning(
        "_get_mi_jwpub_url: API direta falhou para %s/%s — tentando com browser impersonation",
        pub, lang,
    )
    import urllib.parse
    url  = MEDIA_API + "?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if data:
        _any_api_response = True
        result = _parse_mi_jwpub_response(data, lang)
        if result[0]:
            log.debug("_get_mi_jwpub_url: URL obtida via fallback (curl_cffi) para %s/%s", pub, lang)
            return result[0], result[1], result[2], False

    # Both strategies failed to return a URL.
    # If at least one strategy got a valid JSON response the API is reachable,
    # meaning the publication simply doesn't exist → not_found=True.
    not_found = _any_api_response
    log.error("_get_mi_jwpub_url: nenhuma estratégia retornou URL para %s/%s", pub, lang)
    return None, "", "", not_found


def _extract_jwpub_to_dir(pub: str, lang: str, issue: str,
                           cache: JwpubCache) -> Optional[Path]:
    """Extrai o JWPUB (estrutura ZIP duplo) para o diretório de cache."""
    jwpub_path = cache.jwpub_path(pub, lang, issue)
    if not jwpub_path.exists():
        return None
    ep = cache.extract_dir(pub, lang, issue)
    if ep.exists() and any(ep.glob("*.db")):
        return ep   # já extraído
    ep.mkdir(parents=True, exist_ok=True)
    try:
        raw = jwpub_path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as outer:
            outer.extractall(ep)
        contents = ep / "contents"
        if contents.is_file():
            with zipfile.ZipFile(contents) as inner:
                inner.extractall(ep)
        if any(ep.glob("*.db")):
            return ep
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        log.error("extract_jwpub %s: %s", jwpub_path, exc)
    return None


def _query_memorial_sqlite(pub_dir: Path) -> dict:
    """
    Consulta SQLite do mi<YY>:
      CategoryType = 26  → capa (bg) + thumb sqr
      CategoryType = -1  → vídeos de introdução (BeginParagraphOrdinal IS NULL)
    """
    dbs = list(pub_dir.glob("*.db"))
    if not dbs:
        return {"cover": None, "videos": [], "thumb_path": None}

    db_path = dbs[0]
    result = {"cover": None, "videos": [], "thumb_path": None}
    conn: sqlite3.Connection | None = None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Verifica se DocumentMultimedia existe
        has_dm = bool(conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='DocumentMultimedia'"
        ).fetchone()[0])

        join   = ("INNER JOIN DocumentMultimedia dm "
                  "ON m.MultimediaId = dm.MultimediaId") if has_dm else ""
        dm_col = ", dm.BeginParagraphOrdinal AS par" if has_dm else ""

        # Capa (CategoryType=26) — imagem ctv 1920×1080
        row = conn.execute(f"""
            SELECT m.*{dm_col}
            FROM Multimedia m {join}
            WHERE m.CategoryType = 26
            LIMIT 1
        """).fetchone()
        if row:
            result["cover"] = dict(row)

        # Thumb quadrado: procura arquivo *_univ_sqr* no diretório
        for f in pub_dir.iterdir():
            if f.is_file() and "univ_sqr" in f.name:
                result["thumb_path"] = f
                break

        # Vídeos de introdução (CategoryType=-1, BeginParagraphOrdinal IS NULL)
        null_cond = " AND dm.BeginParagraphOrdinal IS NULL" if has_dm else ""
        rows = conn.execute(f"""
            SELECT m.*{dm_col}
            FROM Multimedia m {join}
            WHERE m.CategoryType = -1{null_cond}
        """).fetchall()
        result["videos"] = [dict(r) for r in rows]
    except (OSError, sqlite3.Error) as exc:
        log.error("query_memorial_sqlite %s: %s", db_path, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                log.debug("Could not close memorial database %s", db_path, exc_info=True)

    return result


def _make_memorial_media(v: dict, pub_dir: Path) -> MeetingMedia:
    mime  = (v.get("MimeType") or "").lower()
    fp    = v.get("FilePath") or ""
    abs_fp = str(pub_dir / fp) if fp and mime.startswith("image") else fp
    return MeetingMedia(
        multimedia_id = v.get("MultimediaId") or 0,
        mime_type     = mime,
        file_path     = abs_fp,
        label         = v.get("Label") or "",
        caption       = v.get("Caption") or "",
        key_symbol    = v.get("KeySymbol") or "",
        track         = v.get("Track") or 0,
        issue_tag     = v.get("IssueTagNumber") or 0,
        meps_doc_id   = v.get("MepsDocumentId") or 0,
        section       = "memorial",
    )


# ── Worker ────────────────────────────────────────────────────────────────────

class _MemorialWorker(QObject):
    """
    Roda num QThread dedicado.
    Toda I/O bloqueante (HTTP, zip, SQLite, resolução de vídeo) acontece aqui.
    Comunica com MemorialService via sinais (QueuedConnection automática).
    """
    memorial_done  = Signal(object)   # MemorialData
    progress       = Signal(int)      # 0-100
    error          = Signal(str)      # mensagem

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache           = JwpubCache()
        self._checksum_store  = get_checksum_store()   # process-wide singleton
        self._lang            = "T"

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(int)
    def load_memorial(self, year: int):
        """Busca e resolve as mídias do Memorial para o ano dado."""
        lang  = self._lang
        pub   = _mi_pub(year)
        issue = "0"   # mi<YY> não tem issue numérico — usa "0" como chave de cache

        md = MemorialData(year=year)

        # ── Calcular data ──────────────────────────────────────────────────────
        memorial_date = memorial_date_for_year(year)
        if not memorial_date:
            md.status = "error"
            self.error.emit(f"Não foi possível calcular a data do Memorial {year}")
            self.memorial_done.emit(md)
            return

        md.memorial_date = memorial_date
        md.memorial_week = _monday_of(memorial_date)

        # ── Verificar janela de fetch ──────────────────────────────────────────
        today      = date.today()
        days_until = (memorial_date - today).days
        is_past    = days_until < -1   # more than 1 day after the memorial

        if days_until > 7:
            md.status = "not_yet"
            self.memorial_done.emit(md)
            return

        # NOTE: we intentionally do NOT bail out early for is_past here.
        # If we have a cached copy it should still be shown; and if not,
        # we need to reach the API to decide whether to show "not_found"
        # or "deleted" (media removed by JW).

        self.progress.emit(5)

        # ── Obter URL + checksum do Memorial (sempre chama API para detectar updates) ──
        dl_url, thumb_url, checksum, not_found = _get_mi_jwpub_url(pub, lang)
        md.thumb_url = thumb_url

        is_cached = self._cache.is_cached(pub, lang, issue)

        needs_download = (
            not is_cached
            or self._checksum_store.has_changed(pub, lang, issue, checksum)
        )

        if needs_download:
            if not dl_url:
                if is_cached:
                    # API unreachable but we have a local copy → use stale cache.
                    log.warning(
                        "memorial %s/%s: API unreachable, falling back to stale cache",
                        pub, lang,
                    )
                    self.progress.emit(60)
                else:
                    # No URL AND no local copy — determine the right user message.
                    if is_past and not_found:
                        # API confirmed pub absent + date already passed
                        # → JW has removed the media from their servers.
                        md.status = "deleted"
                    elif not_found:
                        # API confirmed pub absent but date is still upcoming/current
                        # → publication not yet available; user can retry later.
                        md.status = "not_found"
                    else:
                        # Pure network error — we can't tell whether the pub exists.
                        if is_past:
                            md.status = "deleted"  # assume deleted if past and we can't confirm
                        else:
                            md.status = "error"
                            self.error.emit(f"URL não encontrada para {pub} lang={lang}")
                    self.memorial_done.emit(md)
                    return
            else:
                dest = self._cache.jwpub_path(pub, lang, issue)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    raw = _http_get(dl_url, timeout=120)
                    self.progress.emit(60)
                    dest.write_bytes(raw)
                    # New .jwpub on disk — wipe the stale extract dir so that
                    # _extract_jwpub_to_dir() below unpacks the fresh content.
                    self._cache.invalidate_extract(pub, lang, issue)
                    self._checksum_store.save(pub, lang, issue, checksum)
                except (MemorialDownloadError, OSError) as exc:
                    if is_cached:
                        # Download failed but stale cache exists → use it.
                        log.warning(
                            "memorial %s/%s: download failed, using stale cache: %s",
                            pub, lang, exc,
                        )
                        self.progress.emit(60)
                    else:
                        md.status = "error"
                        self.error.emit(f"Download falhou: {exc}")
                        self.memorial_done.emit(md)
                        return
        else:
            self.progress.emit(60)

        self.progress.emit(70)

        # ── Extração ───────────────────────────────────────────────────────────
        pub_dir = _extract_jwpub_to_dir(pub, lang, issue, self._cache)
        if not pub_dir:
            md.status = "error"
            self.error.emit("Extração do JWPUB falhou")
            self.memorial_done.emit(md)
            return

        md.pub_dir = pub_dir
        self.progress.emit(80)

        # ── Consulta SQLite ────────────────────────────────────────────────────
        sqlite_data = _query_memorial_sqlite(pub_dir)

        # Capa: usa arquivo *univ_sqr* local se disponível, senão CategoryType=26
        if sqlite_data["thumb_path"] and sqlite_data["thumb_path"].exists():
            try:
                md.cover_bytes = sqlite_data["thumb_path"].read_bytes()
            except OSError:
                log.debug("Failed to read memorial thumbnail bytes", exc_info=True)

        if not md.cover_bytes and sqlite_data["cover"]:
            fp = sqlite_data["cover"].get("FilePath", "")
            if fp:
                cover_path = pub_dir / fp
                if cover_path.exists():
                    try:
                        md.cover_bytes = cover_path.read_bytes()
                    except OSError:
                        log.debug("Failed to read memorial cover bytes", exc_info=True)

        self.progress.emit(85)

        # ── Construir itens de mídia (sem pré-resolução de URL) ───────────────
        # A resolução de URL é feita lazily por _MediaRow + JwpubService
        # quando o usuário abre o detalhe, seguindo o mesmo padrão das reuniões.
        media_items = []
        
        # 1. Adiciona a imagem principal (ctv) na lista de mídias projetáveis
        if sqlite_data.get("cover"):
            media_items.append(_make_memorial_media(sqlite_data["cover"], pub_dir))
            
        # 2. Adiciona os vídeos de introdução
        media_items.extend([_make_memorial_media(v, pub_dir) for v in sqlite_data["videos"]])

        md.videos = media_items
        md.status = "ready" if (media_items or md.cover_bytes) else "empty"
        self.progress.emit(100)
        self.memorial_done.emit(md)


# ── MemorialService — vive na main thread ────────────────────────────────────

class MemorialService(QObject):
    """
    API pública para a UI.  Vive na main thread.
    Expõe sinais para o widget consumir resultados do worker.

    Uso típico:
        svc = MemorialService(self)
        svc.set_lang("T")
        svc.memorial_ready.connect(self._on_memorial)
        svc.load()                           # dispara worker se estiver na janela
        monday = svc.memorial_week()         # para saber em qual semana mostrar
    """

    memorial_ready   = Signal(object)     # MemorialData (status="ready")
    memorial_status  = Signal(str)        # status string (p/ UI genérica)
    memorial_progress = Signal(int)       # 0-100

    _sig_load     = Signal(int)
    _sig_set_lang = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lang   = "T"
        self._year   = date.today().year
        self._data:  Optional[MemorialData] = None

        self._thread = QThread(self)
        self._worker = _MemorialWorker()
        self._worker.moveToThread(self._thread)

        self._worker.memorial_done.connect(self._on_done)
        self._worker.progress.connect(self.memorial_progress)
        self._worker.error.connect(self._on_error)

        self._sig_load.connect(self._worker.load_memorial)
        self._sig_set_lang.connect(self._worker.set_lang)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread do Memorial."""
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
        except Exception:  # noqa: BLE001 - destructors must never raise during shutdown
            log.debug("Failed to shutdown MemorialService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        """Define o idioma de mídia JW (não o idioma da interface)."""
        if lang == self._lang:
            return
        self._lang = lang
        self._data  = None
        self._sig_set_lang.emit(lang)

    def get_lang(self) -> str:
        return self._lang

    def load(self, force: bool = False):
        """
        Dispara o worker para carregar/verificar as mídias do Memorial.
        Se force=False e já temos dados prontos, emite o sinal imediatamente.
        """
        if not force and self._data and self._data.status == "ready":
            self.memorial_ready.emit(self._data)
            return
        self._sig_load.emit(self._year)

    def get_data(self) -> Optional[MemorialData]:
        return self._data

    def memorial_date(self) -> Optional[date]:
        """Data calculada do Memorial (pode ser None se ainda não calculada)."""
        if self._data:
            return self._data.memorial_date
        # Cálculo síncrono leve para uso imediato pela UI (sem bloquear)
        return memorial_date_for_year(self._year)

    def memorial_week(self) -> Optional[date]:
        """Segunda-feira da semana do Memorial."""
        d = self.memorial_date()
        return _monday_of(d) if d else None

    def is_memorial_week(self, monday: date) -> bool:
        """Verdadeiro se a semana dada é a semana do Memorial."""
        mw = self.memorial_week()
        return mw is not None and mw == monday

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot(object)
    def _on_done(self, data: MemorialData):
        self._data = data
        self.memorial_status.emit(data.status)
        if data.status == "ready":
            self.memorial_ready.emit(data)

    @Slot(str)
    def _on_error(self, msg: str):
        log.error("MemorialService: %s", msg)
