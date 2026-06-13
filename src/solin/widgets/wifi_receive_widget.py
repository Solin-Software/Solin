"""
wifi_receive_widget.py — Tela de recepção de mídias via Wi-Fi.

Layout:
  • Servidor PARADO  → tela idle centrada (tela inteira)
  • Servidor ATIVO   → zona superior com QR compacto + zona inferior com
                       grid de cards de mídias recebidas (com thumbnails)

Ciclo de vida do servidor:
  • Idle é a tela padrão. O usuário inicia manualmente.
  • Navegar para outra tela NÃO para o servidor.
  • Para apenas em: botão manual, inatividade 15 min, ou fechamento do app.

Sinais públicos:
  media_received(path, orig_name)
  request_add_to_playlist(path, title)
  request_add_all_to_playlist(items: list[{path, title}])
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import (
    Qt, Signal, Slot, QThread, QObject, QTimer, QSize, QEvent,
)
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QStackedWidget, QApplication, QScrollArea,
    QGridLayout,
)

from ..core.i18n.manager import LanguageManager
from ..core.foundation.exception_logging import log_ignored_exception
from ..core.foundation.qt_threads import stop_owned_qthread
from ..core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ..core.foundation.constants import (
    AUDIO_EXTS as _AUDIO_EXTS,
    JWPUB_EXTS as _JWPUB_EXTS,
    PDF_EXTS as _PDF_EXTS,
    PLAYLIST_EXTS as _JWL_EXTS,
    VIDEO_EXTS as _VIDEO_EXTS,
)
from ..core.jw.language_context import jw_media_language_context
from ..core.media.mime import mime_to_ext
from ..core.ingest.wifi_server import WifiReceiveServer
from ..styles.icons import make_icon
from .media_info_extractor import MediaInfoService

if TYPE_CHECKING:
    from ..core.ui.notifications import NotificationCenter

_PDF_EXTS_SET = _PDF_EXTS
_JWL_EXTS_SET = _JWL_EXTS
_JWPUB_EXTS_SET = _JWPUB_EXTS

# ── Paleta ────────────────────────────────────────────────────────────────────

_BG      = "#0d1117"
_SURFACE = "#161b22"
_SURFACE2= "#1c2128"
_BORDER  = "#21262d"
_BORDER2 = "#30363d"
_MUTED   = "#8b949e"
_MUTED2  = "#484f58"
_TEXT    = "#e6edf3"
_TEXT2   = "#c9d1d9"
_ACCENT  = "#388bfd"
_OK      = "#3fb950"
_ERR     = "#f85149"

_CARD_W  = 148
_CARD_H  = 168
_THUMB_H = 96
_QR_SIZE = 160

# ── SVG ───────────────────────────────────────────────────────────────────────

_I_WIFI = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M5 12.55a11 11 0 0 1 14.08 0"/>'
    '<path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
    '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>'
    '<line x1="12" y1="20" x2="12.01" y2="20" stroke-width="3" stroke-linecap="round"/>'
    '</svg>'
)
_I_COPY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="9" width="13" height="13" rx="2"/>'
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    '</svg>'
)
_I_STOP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    '</svg>'
)
_I_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
    '<polyline points="22 4 12 14.01 9 11.01"/>'
    '</svg>'
)
_I_PLAY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/>'
    '<polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/>'
    '</svg>'
)
_I_PLUS = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'
    '</svg>'
)
_I_LIST = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>'
    '<line x1="8" y1="18" x2="21" y2="18"/>'
    '<line x1="3" y1="6" x2="3.01" y2="6" stroke-width="3"/>'
    '<line x1="3" y1="12" x2="3.01" y2="12" stroke-width="3"/>'
    '<line x1="3" y1="18" x2="3.01" y2="18" stroke-width="3"/>'
    '</svg>'
)
_I_VIDEO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="3" width="20" height="14" rx="2"/>'
    '<polygon points="8,7 16,10 8,13" fill="currentColor" stroke="none"/>'
    '</svg>'
)
_I_AUDIO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9 18V5l12-2v13"/>'
    '<circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'
    '</svg>'
)
_I_IMAGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    '<circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" stroke="none"/>'
    '<polyline points="21,15 16,10 5,21"/>'
    '</svg>'
)
_I_PDF = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="16" y1="13" x2="8" y2="13"/>'
    '<line x1="16" y1="17" x2="8" y2="17"/>'
    '<polyline points="10 9 9 9 8 9"/>'
    '</svg>'
)
_I_PLAYLIST = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="12" y1="18" x2="12" y2="12"/>'
    '<line x1="9" y1="15" x2="15" y2="15"/>'
    '</svg>'
)


def _file_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in _VIDEO_EXTS: return "video"
    if ext in _AUDIO_EXTS: return "audio"
    if ext in _PDF_EXTS:   return "pdf"
    if ext in _JWL_EXTS:   return "playlist"
    return "image"


def _type_meta(path: str) -> tuple[str, str]:
    """Returns (icon_svg, accent_color) based on file type."""
    t = _file_media_type(path)
    if t == "video":    return _I_VIDEO,    "#79c0ff"
    if t == "audio":    return _I_AUDIO,    "#d2a8ff"
    if t == "pdf":      return _I_PDF,      "#ffa657"
    if t == "playlist": return _I_PLAYLIST, "#56d364"
    return _I_IMAGE, "#7ee787"


def _rounded_pixmap(src: QPixmap, w: int, h: int, radius: int = 10) -> QPixmap:
    """Scale + crop to w×h with rounded corners."""
    scaled = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    # Center-crop
    x = (scaled.width()  - w) // 2
    y = (scaled.height() - h) // 2
    cropped = scaled.copy(x, y, w, h)
    # Apply rounded mask
    result = QPixmap(w, h)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return result


# ── QR worker ─────────────────────────────────────────────────────────────────

class _QrWorker(QObject):
    done   = Signal(bytes)
    failed = Signal()

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    @Slot()
    def run(self) -> None:
        png_data = _generate_qr_png(self._url)
        self.done.emit(png_data) if png_data else self.failed.emit()


def _generate_qr_png(url: str) -> bytes | None:
    try:
        import qrcode  # type: ignore[import]
        import io as _io
        qr = qrcode.QRCode(version=None,
                           error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=6, border=2)
        qr.add_data(url); qr.make(fit=True)
        buf = _io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - qrcode/Pillow codec boundary
        log_ignored_exception(__name__, "Could not generate Wi-Fi QR code")
        return None


# ── Clickable frame (URL field) ───────────────────────────────────────────────

class _ClickableFrame(QFrame):
    """QFrame que emite clicked() ao ser clicado em qualquer ponto."""
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── Tipos que suportam reprodução direta ─────────────────────────────────────

_PLAYABLE_TYPES = frozenset({"video", "audio", "image"})


def _is_playable(path: str) -> bool:
    """Retorna True para tipos que suportam reprodução/visualização direta."""
    return _file_media_type(path) in _PLAYABLE_TYPES


# ── Thumbnail clicável com overlay de play ────────────────────────────────────

class _ThumbArea(QWidget):
    """
    Área de thumbnail que exibe um overlay de play ao hover (para mídia playável).
    Emite clicked() quando pressionado com o botão esquerdo.
    """
    clicked = Signal()

    _OVERLAY_SIZE = 34
    _OVERLAY_BG   = "rgba(0,0,0,180)"

    def __init__(self, w: int, h: int, playable: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(w, h)
        self._playable = playable

        # Label da imagem (ocupa tudo)
        self._lbl = QLabel(self)
        self._lbl.setFixedSize(w, h)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        if playable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            # Overlay de play (centralizado, não captura eventos)
            oz = self._OVERLAY_SIZE
            self._overlay = QLabel(self)
            self._overlay.setFixedSize(oz, oz)
            self._overlay.move((w - oz) // 2, (h - oz) // 2)
            self._overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._overlay.setStyleSheet(
                f"background:{self._OVERLAY_BG};border-radius:{oz // 2}px;"
            )
            self._overlay.setPixmap(make_icon(_I_PLAY, 18, "#ffffff").pixmap(18, 18))
            self._overlay.hide()
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._overlay = None  # type: ignore[assignment]

    @property
    def label(self) -> QLabel:
        return self._lbl

    # ── Hover ──────────────────────────────────────────────────────────────

    def enterEvent(self, event) -> None:
        if self._overlay:
            self._overlay.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._overlay:
            self._overlay.hide()
        super().leaveEvent(event)

    # ── Clique ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if self._playable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── Media card ────────────────────────────────────────────────────────────────

class _MediaCard(QFrame):
    """Card individual com thumbnail, nome, botão de playlist e play ao clicar."""
    add_to_playlist = Signal(str, str, str)   # path, title, orig_name
    play_requested  = Signal(str, str)         # path, title

    def __init__(self, path: str, title: str, lang: LanguageManager,
                 parent: QWidget | None = None, orig_name: str = "") -> None:
        super().__init__(parent)
        self._path      = path
        self._title     = title
        self._orig_name = orig_name or Path(path).name
        self.setObjectName("WifiCard")
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setStyleSheet(
            f"QFrame#WifiCard{{background:{_SURFACE};border-radius:12px;"
            f"border:1px solid {_BORDER};}}"
            f"QFrame#WifiCard:hover{{border-color:{_BORDER2};}}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 10)
        root.setSpacing(0)

        # ── Área de thumbnail (clicável para playable, inerte para outros) ─
        playable = _is_playable(path)
        self._thumb_area = _ThumbArea(_CARD_W, _THUMB_H, playable=playable, parent=self)
        self._thumb_lbl  = self._thumb_area.label
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_placeholder()
        if playable:
            self._thumb_area.clicked.connect(
                lambda: self.play_requested.emit(self._path, self._title)
            )
        root.addWidget(self._thumb_area)

        # ── Badge de tipo (video / audio / image) ──────────────────────────
        _mtype = _file_media_type(path)
        if _mtype in ("video", "audio", "image"):
            _, _badge_color = _type_meta(path)
            # Texto traduão tr
            video = self.tr("video")
            audio = self.tr("audio")
            image = self.tr("image")
            _badge_text = {"video": video, "audio": audio, "image": image}[_mtype]
            self._type_badge = QLabel(_badge_text, self._thumb_area)
            self._type_badge.setStyleSheet(
                f"background:rgba(13,17,23,0.78);border-radius:4px;"
                f"color:{_badge_color};font-size:7px;font-weight:700;"
                "padding:2px 6px;letter-spacing:0.5px;border:none;"
            )
            self._type_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._type_badge.adjustSize()
            self._type_badge.move(6, 6)
            self._type_badge.raise_()

        # ── Nome + botão ───────────────────────────────────────────────────
        bot = QHBoxLayout()
        bot.setContentsMargins(8, 7, 6, 0)
        bot.setSpacing(4)

        short = title if len(title) <= 22 else title[:20] + "…"
        self._name_lbl = QLabel(short)
        self._name_lbl.setToolTip(title)
        self._name_lbl.setStyleSheet(
            f"background:transparent;color:{_TEXT2};font-size:10px;"
            "font-weight:500;border:none;"
        )
        self._name_lbl.setWordWrap(False)
        name = self._name_lbl

        tip = self.tr("Add to playlist")
        pl_btn = QPushButton()
        pl_btn.setIcon(make_icon(_I_PLUS, 11, _MUTED))
        pl_btn.setIconSize(QSize(11, 11))
        pl_btn.setFixedSize(24, 24)
        pl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pl_btn.setToolTip(tip)
        pl_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:1px solid {_BORDER2};"
            "border-radius:5px;}"
            f"QPushButton:hover{{background:#1f3a5f;border-color:{_ACCENT};}}"
        )
        pl_btn.clicked.connect(lambda: self.add_to_playlist.emit(self._path, self._title, self._orig_name))

        bot.addWidget(name, stretch=1)
        bot.addWidget(pl_btn)
        root.addLayout(bot)

    # ── Thumbnail helpers ──────────────────────────────────────────────────

    def _set_placeholder(self) -> None:
        """Fundo escuro com ícone de tipo como placeholder."""
        svg, color = _type_meta(self._path)
        ph = QPixmap(_CARD_W, _THUMB_H)
        ph.fill(QColor(_SURFACE2))
        # Arredonda apenas cantos superiores
        rounded = QPixmap(_CARD_W, _THUMB_H)
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, _CARD_W, _THUMB_H + 12, 12, 12)
        p.setClipPath(path)
        p.drawPixmap(0, 0, ph)
        p.end()
        # Sobrepõe ícone centralizado
        ico = make_icon(svg, 32, _MUTED2).pixmap(32, 32)
        final = QPixmap(_CARD_W, _THUMB_H)
        final.fill(Qt.GlobalColor.transparent)
        p2 = QPainter(final)
        p2.drawPixmap(0, 0, rounded)
        p2.drawPixmap((_CARD_W - 32) // 2, (_THUMB_H - 32) // 2, ico)
        p2.end()
        self._thumb_lbl.setPixmap(final)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        thumb = _rounded_pixmap_top(pixmap, _CARD_W, _THUMB_H, radius=12)
        self._thumb_lbl.setPixmap(thumb)

    def update_title(self, title: str) -> None:
        """Atualiza o titulo exibido e o titulo usado ao emitir add_to_playlist."""
        if not title:
            return
        self._title = title
        short = title if len(title) <= 22 else title[:20] + "…"
        self._name_lbl.setText(short)
        self._name_lbl.setToolTip(title)


def _rounded_pixmap_top(src: QPixmap, w: int, h: int, radius: int = 12) -> QPixmap:
    """Scale + crop to w×h com arredondamento só nos cantos superiores."""
    scaled = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    x = (scaled.width()  - w) // 2
    y = (scaled.height() - h) // 2
    cropped = scaled.copy(x, y, w, h)
    result = QPixmap(w, h)
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    # Cantos superiores arredondados, inferiores retos
    path.moveTo(radius, 0)
    path.lineTo(w - radius, 0)
    path.quadTo(w, 0, w, radius)
    path.lineTo(w, h)
    path.lineTo(0, h)
    path.lineTo(0, radius)
    path.quadTo(0, 0, radius, 0)
    path.closeSubpath()
    p.setClipPath(path)
    p.drawPixmap(0, 0, cropped)
    p.end()
    return result


# ── Widget principal ──────────────────────────────────────────────────────────

class WifiReceiveWidget(QWidget):
    media_received              = Signal(str, str)
    request_add_to_playlist     = Signal(str, str, str)   # path, title, orig_name
    request_add_all_to_playlist = Signal(list)
    request_play                = Signal(str, str)         # path, title

    def __init__(
        self,
        lang: LanguageManager,
        *,
        notifications: NotificationCenter,
        profile_paths: ProfilePaths,
        runtime_paths: RuntimePaths,
        media_cache_dir: str | os.PathLike[str],
        thumb_cache_dir: str | os.PathLike[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang            = lang
        self._notifications   = notifications
        self._profile_paths   = profile_paths
        self._runtime_paths   = runtime_paths
        self._media_cache_dir = media_cache_dir
        self._thumb_cache_dir = thumb_cache_dir
        self._server          = WifiReceiveServer(
            embedded_dir=profile_paths.embedded_dir,
            parent=self,
        )
        self._session_url     = ""
        self._received_files: list[dict] = []
        self._cards:          list[_MediaCard] = []
        self._thumb_service = MediaInfoService(
            media_cache_dir,
            thumb_cache_dir,
            self,
        )
        self._wifi_tmp_files: set[str] = set()   # temp files criados por PDF/JWL expansion
        self._qr_thread:      Optional[QThread]   = None
        self._qr_worker:      Optional[_QrWorker] = None

        self._server.server_started.connect(self._on_server_started)
        self._server.server_stopped.connect(self._on_server_stopped)
        self._server.file_received.connect(self._on_file_received)
        self._server.error_occurred.connect(self._on_error)
        self._server.inactivity_stopped.connect(self._on_inactivity_stopped)
        self._thumb_service.info_ready.connect(self._on_thumb_ready)

        self._build_ui()

    # ═══════════════════════════════════════════════════════════════════════
    # BUILD UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_header())

        # Stack principal — ocupa tudo abaixo do header
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{_BG};")
        self._stack.addWidget(self._make_starting_page())  # 0
        self._stack.addWidget(self._make_active_page())    # 1
        self._stack.addWidget(self._make_idle_page())      # 2
        self._stack.setCurrentIndex(2)
        root.addWidget(self._stack, stretch=1)

    # ── Header ────────────────────────────────────────────────────────────

    def _make_header(self) -> QFrame:
        hdr = QFrame()
        hdr.setStyleSheet(
            f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}"
        )
        hdr.setFixedHeight(54)
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(20, 0, 20, 0); lay.setSpacing(10)

        ico = QLabel()
        ico.setPixmap(make_icon(_I_WIFI, 18, _MUTED).pixmap(18, 18))
        ico.setStyleSheet("background:transparent;")

        self._header_title = QLabel(self.tr("Receive via Wi-Fi"))
        self._header_title.setStyleSheet(
            f"background:transparent;font-size:14px;font-weight:700;color:{_TEXT};"
        )

        lay.addWidget(ico)
        lay.addWidget(self._header_title)
        lay.addStretch()
        return hdr

    # ── Página 0: iniciando ────────────────────────────────────────────────

    def _make_starting_page(self) -> QWidget:
        w = QWidget(); w.setStyleSheet(f"background:{_BG};")
        lay = QVBoxLayout(w); lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._starting_lbl = QLabel(self.tr("Starting server…"))
        self._starting_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._starting_lbl.setStyleSheet(
            f"color:{_MUTED};font-size:13px;background:transparent;"
        )
        lay.addWidget(self._starting_lbl)
        return w

    # ── Página 1: ativa ────────────────────────────────────────────────────

    def _make_active_page(self) -> QWidget:
        """Zona superior (QR + URL + stop) + zona inferior (grid de cards)."""
        w = QWidget(); w.setStyleSheet(f"background:{_BG};")
        root = QVBoxLayout(w); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # ── Barra superior do servidor ────────────────────────────────────
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}"
        )
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(20, 12, 20, 12); bar_lay.setSpacing(16)

        # QR
        qr_frame = QFrame()
        qr_frame.setStyleSheet(
            "QFrame{background:#ffffff;border-radius:10px;padding:6px;}"
        )
        qr_frame.setFixedSize(_QR_SIZE + 14, _QR_SIZE + 14)
        qr_inner = QVBoxLayout(qr_frame)
        qr_inner.setContentsMargins(0, 0, 0, 0)
        qr_inner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_lbl = QLabel()
        self._qr_lbl.setFixedSize(_QR_SIZE, _QR_SIZE)
        self._qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_lbl.setStyleSheet("background:transparent;border:none;")
        self._set_qr_placeholder()
        qr_inner.addWidget(self._qr_lbl)
        bar_lay.addWidget(qr_frame)

        # Info direita
        info = QVBoxLayout(); info.setSpacing(8); info.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Instrução
        self._instruction_lbl = QLabel(self.tr("Scan the QR code with your phone.\nBoth devices must be on the same Wi-Fi network."))
        self._instruction_lbl.setWordWrap(True)
        self._instruction_lbl.setStyleSheet(
            f"background:transparent;color:{_TEXT2};font-size:12px;line-height:1.5;"
        )
        info.addWidget(self._instruction_lbl)

        # URL copiável — frame inteiro é clicável (click to copy)
        self._url_frame = _ClickableFrame()
        self._url_frame.setObjectName("UrlFrame")
        self._url_frame.setToolTip(self.tr("Copy link"))
        self._url_frame.setStyleSheet(
            f"QFrame#UrlFrame{{background:{_BG};border-radius:8px;border:1px solid {_BORDER};}}"
        )
        url_lay = QHBoxLayout(self._url_frame)
        url_lay.setContentsMargins(10, 7, 6, 7); url_lay.setSpacing(6)
        self._url_lbl = QLabel("—")
        self._url_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_ACCENT};"
            "font-size:10px;font-family:monospace;"
        )
        self._url_lbl.setWordWrap(True)
        # Desativa seleção de texto para não conflitar com o clique de cópia
        self._url_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._url_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn = QPushButton()
        self._copy_btn.setIcon(make_icon(_I_COPY, 13, _MUTED))
        self._copy_btn.setIconSize(QSize(13, 13))
        self._copy_btn.setFixedSize(28, 28)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setToolTip(self.tr("Copy link"))
        self._copy_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:5px;}"
        )
        # Tanto o frame inteiro quanto o botão chamam _copy_url
        self._url_frame.clicked.connect(self._copy_url)
        self._copy_btn.clicked.connect(self._copy_url)
        url_lay.addWidget(self._url_lbl, stretch=1)
        url_lay.addWidget(self._copy_btn)
        info.addWidget(self._url_frame)

        # Dica inatividade
        self._inact_lbl = QLabel(self.tr("The server stops automatically after 15 minutes outside this screen."))
        self._inact_lbl.setWordWrap(True)
        self._inact_lbl.setStyleSheet(
            f"background:transparent;color:{_MUTED2};font-size:9px;"
        )
        info.addWidget(self._inact_lbl)

        info.addStretch()

        # Botão parar
        self._stop_btn = QPushButton()
        self._stop_btn.setIcon(make_icon(_I_STOP, 12, _ERR))
        self._stop_btn.setIconSize(QSize(12, 12))
        self._stop_btn.setText(self.tr("  Stop server"))
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setStyleSheet(
            "QPushButton{background:#2d1b1b;border:1px solid #3d2020;"
            "border-radius:7px;color:#ff7b72;font-size:11px;font-weight:600;padding:0 12px;}"
            "QPushButton:hover{background:#3d2424;border-color:#f85149;color:#f85149;}"
        )
        self._stop_btn.clicked.connect(lambda: self._server.stop())
        info.addWidget(self._stop_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        bar_lay.addLayout(info, stretch=1)
        root.addWidget(bar)

        # ── Cabeçalho da seção de mídias ──────────────────────────────────
        media_hdr = QWidget()
        media_hdr.setStyleSheet(f"background:{_BG};")
        mh_lay = QHBoxLayout(media_hdr)
        mh_lay.setContentsMargins(20, 12, 20, 8); mh_lay.setSpacing(8)

        self._section_lbl = QLabel(self.tr("Received media"))
        self._section_lbl.setStyleSheet(
            f"background:transparent;font-size:11px;font-weight:600;color:{_MUTED};"
        )
        self._count_badge = QLabel("")
        self._count_badge.setStyleSheet(
            f"background:{_BORDER2};color:{_MUTED};font-size:9px;font-weight:600;"
            "border-radius:7px;padding:1px 7px;"
        )
        self._count_badge.hide()

        self._send_all_btn = QPushButton()
        self._send_all_btn.setIcon(make_icon(_I_LIST, 12, _MUTED))
        self._send_all_btn.setIconSize(QSize(12, 12))
        self._send_all_btn.setText(self.tr("  Send all to playlist"))
        self._send_all_btn.setFixedHeight(26)
        self._send_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_all_btn.setEnabled(False)
        self._send_all_btn.setStyleSheet(
            f"QPushButton{{background:{_BORDER};border:1px solid {_BORDER2};"
            "border-radius:6px;color:#484f58;font-size:10px;font-weight:600;"
            "padding:0 10px;}"
            "QPushButton:enabled{color:#8b949e;}"
            f"QPushButton:enabled:hover{{background:{_BORDER2};color:{_TEXT2};"
            f"border-color:{_ACCENT};}}"
        )
        self._send_all_btn.clicked.connect(self._on_send_all)

        mh_lay.addWidget(self._section_lbl)
        mh_lay.addWidget(self._count_badge)
        mh_lay.addStretch()
        mh_lay.addWidget(self._send_all_btn)
        root.addWidget(media_hdr)

        # ── Scroll + grid de cards ─────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{_BG};border:none;}}"
            "QScrollBar:vertical{background:#0d1117;width:4px;border-radius:2px;margin:0;}"
            "QScrollBar::handle:vertical{background:#21262d;border-radius:2px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        # Wrapper que contém placeholder centralizado E grid de cards
        self._content_stack = QStackedWidget()
        self._content_stack.setStyleSheet(f"background:{_BG};")

        # Página 0 do stack: placeholder centralizado
        ph_page = QWidget()
        ph_page.setStyleSheet(f"background:{_BG};")
        ph_lay = QVBoxLayout(ph_page)
        ph_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder = QLabel(self.tr("Files sent from your phone will appear here."))
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet(
            f"color:{_MUTED2};font-size:12px;background:transparent;padding:32px 0;"
        )
        ph_lay.addWidget(self._placeholder)
        self._content_stack.addWidget(ph_page)   # índice 0

        # Página 1 do stack: grid de cards
        self._grid_container = QWidget()
        self._grid_container.setStyleSheet(f"background:{_BG};")
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setContentsMargins(20, 0, 20, 20)
        self._grid_layout.setSpacing(12)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._content_stack.addWidget(self._grid_container)  # índice 1

        self._content_stack.setCurrentIndex(0)  # começa no placeholder
        scroll.setWidget(self._content_stack)
        root.addWidget(scroll, stretch=1)
        return w

    # ── Página 2: idle ─────────────────────────────────────────────────────

    def _make_idle_page(self) -> QWidget:
        """Tela centrada, tela inteira, servidor parado."""
        w = QWidget(); w.setStyleSheet(f"background:{_BG};")
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setContentsMargins(40, 0, 40, 0); lay.setSpacing(0)

        # Ícone grande
        ico = QLabel()
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico.setPixmap(make_icon(_I_WIFI, 56, _BORDER2).pixmap(56, 56))
        ico.setStyleSheet("background:transparent;")
        lay.addWidget(ico)

        lay.addSpacing(20)

        self._idle_title = QLabel(self.tr("Receive media via Wi-Fi"))
        self._idle_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_title.setStyleSheet(
            f"background:transparent;font-size:18px;font-weight:700;color:{_TEXT};"
        )
        lay.addWidget(self._idle_title)

        lay.addSpacing(8)

        self._idle_subtitle = QLabel(self.tr("Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."))
        self._idle_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_subtitle.setWordWrap(True)
        self._idle_subtitle.setStyleSheet(
            f"background:transparent;font-size:12px;color:{_MUTED};"
            "max-width:340px;"
        )
        lay.addWidget(self._idle_subtitle)

        lay.addSpacing(28)

        self._start_btn = QPushButton()
        self._start_btn.setIcon(make_icon(_I_PLAY, 16, "#ffffff"))
        self._start_btn.setIconSize(QSize(16, 16))
        self._start_btn.setText(self.tr("  Start server"))
        self._start_btn.setFixedHeight(44)
        self._start_btn.setMinimumWidth(180)
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};border:none;border-radius:12px;"
            "color:#fff;font-size:13px;font-weight:700;padding:0 28px;}"
            "QPushButton:hover{background:#1f6feb;}"
            "QPushButton:pressed{background:#1158c7;}"
        )
        self._start_btn.clicked.connect(self._start_server)
        lay.addWidget(self._start_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    # ═══════════════════════════════════════════════════════════════════════
    # SERVIDOR
    # ═══════════════════════════════════════════════════════════════════════

    def _build_html_labels(self) -> dict[str, str]:
        return {
            "title":     self.tr("Send Media"),
            "subtitle":  self.tr("Select or drag photos, videos or audio files"),
            "btn_label": self.tr("Send media"),
            "drop_hint": self.tr("Drag files here"),
            "success":   self.tr("✓ File sent!"),
            "error":     self.tr("Upload error"),
        }

    def _start_server(self) -> None:
        self._stack.setCurrentIndex(0)
        self._server.start(self._build_html_labels())

    # ═══════════════════════════════════════════════════════════════════════
    # SLOTS DO SERVIDOR
    # ═══════════════════════════════════════════════════════════════════════

    @Slot(str, int, str)
    def _on_server_started(self, ip: str, port: int, url: str) -> None:
        self._session_url = url
        self._url_lbl.setText(url)
        self._stack.setCurrentIndex(1)
        self._start_qr_generation(url)

    @Slot()
    def _on_server_stopped(self) -> None:
        self._session_url = ""
        self._cancel_qr_generation()
        self.clear_all_received()
        self._idle_subtitle.setText(self.tr("Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."))
        self._stack.setCurrentIndex(2)

    @Slot(str, str)
    def _on_file_received(self, path: str, orig_name: str) -> None:
        ext = Path(path).suffix.lower()

        if ext in _PDF_EXTS_SET:
            self._expand_pdf(path, orig_name)
            return

        if ext in _JWL_EXTS_SET:
            self._expand_jwlplaylist(path, orig_name)
            return

        if ext in _JWPUB_EXTS_SET:
            self._expand_jwpub(path, orig_name)
            return

        title = Path(orig_name).stem or Path(path).stem
        self._received_files.append({"path": path, "title": title, "orig_name": orig_name})
        self._add_card(path, title, orig_name=orig_name)
        short = orig_name if len(orig_name) <= 34 else orig_name[:32] + "…"
        self._notifications.success(short)
        self.media_received.emit(path, orig_name)

    # ── Expansão de PDF ────────────────────────────────────────────────────

    def _expand_pdf(self, path: str, orig_name: str) -> None:
        """Converte o PDF em imagens de páginas e adiciona cada uma como card."""
        from ..core.rendering.pdf import PdfConvertThread, cached_pages
        pdf_stem = Path(orig_name).stem or Path(path).stem

        pages = cached_pages(path, self._runtime_paths.pdf_pages_dir)
        if pages:
            self._on_pdf_pages_ready(pages, pdf_stem, orig_name)
            return

        thread = PdfConvertThread(
            path,
            self._runtime_paths.pdf_pages_dir,
            parent=self,
        )
        thread.pages_ready.connect(
            lambda pages, stem, n=orig_name: self._on_pdf_pages_ready(pages, stem, n)
        )
        thread.conversion_failed.connect(
            lambda err, n=orig_name: self._notifications.error(
                f"Erro ao converter PDF: {Path(n).name}\n{err}"
            )
        )
        # Manter referência viva enquanto roda
        if not hasattr(self, "_pdf_threads"):
            self._pdf_threads: list = []
        self._pdf_threads.append(thread)
        thread.finished.connect(lambda t=thread: self._pdf_threads.remove(t) if t in self._pdf_threads else None)
        thread.start()

    def _on_pdf_pages_ready(self, pages: list, pdf_stem: str, orig_name: str) -> None:
        if not pages:
            return
        n = len(pages)
        for i, page_path in enumerate(pages):
            title = f"{pdf_stem} — p. {i + 1}"
            self._received_files.append({"path": page_path, "title": title, "orig_name": title + ".jpg"})
            self._add_card(page_path, title, orig_name=title + ".jpg")
        short_name = orig_name if len(orig_name) <= 28 else orig_name[:26] + "…"
        self._notifications.success(f"{short_name}  ({n} p.)")

    # ── Expansão de JWPUB ──────────────────────────────────────────────────

    def _expand_jwpub(self, path: str, orig_name: str) -> None:
        """
        Parse a .jwpub received via Wi-Fi and add each media item as a card.
        Uses JwpubImportThread (QThread) so signals reach the main thread reliably.
        Images are copied to data/images (persistent). Videos resolved via API.
        """
        from ..core.jw.publication_reader import JwpubImportThread

        stem = Path(orig_name).stem or Path(path).stem
        self._notifications.information(f"{stem}…")
        lang = jw_media_language_context(self._lang).api_code

        thread = JwpubImportThread.create(
            path,
            lang=lang,
            dest_images_dir=os.fspath(self._profile_paths.images_dir),
            parent=self,
        )

        if not hasattr(self, "_jwpub_threads"):
            self._jwpub_threads: list = []
        self._jwpub_threads.append(thread)
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t)
            if t in self._jwpub_threads else None
        )

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str):
            added = 0
            for raw in items:
                item_path  = raw.get("url", "")
                item_title = raw.get("title", file_stem)
                if not item_path:
                    continue
                self._received_files.append({
                    "path":      item_path,
                    "title":     item_title,
                    "orig_name": item_title,
                })
                self._add_card(item_path, item_title, orig_name=item_title)
                added += 1
            if added:
                self._notifications.success(f"{file_stem}  ({added} items)")
            else:
                self._notifications.warning(f"No media in {file_stem}")

        @thread.failed.connect
        def _on_fail(err: str):
            self._notifications.error(f"Could not open {stem}\n{err}")

        thread.start()

    # ── Expansão de JWL Playlist ───────────────────────────────────────────

    def _expand_jwlplaylist(self, path: str, orig_name: str) -> None:
        """Lê o .jwlplaylist e adiciona cada item como card individual."""
        import zipfile as _zipmod
        from ..core.playlists.reader import read_jwlplaylist

        fallback_lang = jw_media_language_context(self._lang).fallback_code

        try:
            parsed = read_jwlplaylist(path, fallback_lang_code=fallback_lang)
        except (_zipmod.BadZipFile, OSError, ValueError) as exc:
            self._notifications.error(
                f"Erro ao ler playlist: {Path(orig_name).name}\n{exc}"
            )
            return

        def _best_ext(item: dict, default_mime: str) -> str:
            filename = item.get("filename", "")
            if filename:
                orig_ext = Path(filename).suffix.lower()
                if orig_ext:
                    return orig_ext
            return mime_to_ext(item.get("mime_type", default_mime))

        def _write_tmp(data: bytes, suffix: str) -> str:
            """Write embedded media to data/embedded/ for persistence."""
            import uuid as _uuid
            self._profile_paths.embedded_dir.mkdir(parents=True, exist_ok=True)
            uid  = _uuid.uuid4().hex
            path = self._profile_paths.embedded_dir / f"{uid}{suffix}"
            with path.open("wb") as f:
                f.write(data)
            # Track for cleanup if user discards without adding to playlist
            path_str = os.fspath(path)
            self._wifi_tmp_files.add(path_str)
            return path_str

        added = 0
        skipped = []
        for item in parsed.get("items", []):
            itype  = item.get("type", "video")
            source = item.get("source", "")
            title  = item.get("title", "Item")

            if source == "embedded":
                data = item.get("data")
                if not data:
                    skipped.append(title)
                    continue
                default_mime = "image/jpeg" if itype == "image" else "video/mp4"
                ext = _best_ext(item, default_mime)
                tmp_path = _write_tmp(data, ext)
                self._received_files.append({
                    "path":      tmp_path,
                    "title":     title,
                    "orig_name": title + ext,
                    "type":      itype,
                })
                self._add_card(tmp_path, title, orig_name=title + ext)
                added += 1

            elif source == "jworg":
                url = item.get("jworg_url") or item.get("url") or ""
                if not url:
                    skipped.append(title)
                    continue
                # Store all JW metadata so it survives the trip to the playlist
                self._received_files.append({
                    "path":       url,
                    "title":      title,
                    "orig_name":  title,
                    "type":       itype,
                    "key_symbol": item.get("key_symbol"),
                    "track":      item.get("track"),
                    "issue_tag":  item.get("issue_tag"),
                    "doc_id":     item.get("doc_id"),
                    "meps_language": item.get("meps_language", 0),
                })
                self._add_card(url, title, orig_name=title)
                added += 1

        pl_name = parsed.get("name") or Path(orig_name).stem
        if added:
            self._notifications.success(f"{pl_name}  ({added} itens)")
        if skipped:
            self._notifications.warning(
                f"{len(skipped)} item(ns) não resolvido(s)"
            )

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._idle_subtitle.setText(self.tr("Could not start the server.") + f"\n{msg}")
        self._stack.setCurrentIndex(2)
        self._notifications.error(
            msg,
            title=self.tr("Could not start the server."),
            dedupe_key=f"wifi-server:{msg}",
        )

    @Slot()
    def _on_inactivity_stopped(self) -> None:
        self._idle_subtitle.setText(self.tr("Server stopped due to inactivity."))
        self._stack.setCurrentIndex(2)

    # ═══════════════════════════════════════════════════════════════════════
    # GRID DE CARDS
    # ═══════════════════════════════════════════════════════════════════════

    def _cols(self) -> int:
        """Calcula colunas baseado na largura disponível."""
        w = self._grid_container.width() or self.width()
        usable = max(w - 40, _CARD_W)   # 40 = margens
        return max(1, usable // (_CARD_W + 12))

    def _add_card(self, path: str, title: str, orig_name: str = "") -> None:
        n = len(self._cards)
        # Remove placeholder na primeira mídia
        if n == 0:
            self._content_stack.setCurrentIndex(1)  # mostra grid

        card = _MediaCard(path, title, self._lang, self._grid_container, orig_name=orig_name)
        card.add_to_playlist.connect(self._on_card_add_to_playlist)
        card.play_requested.connect(self.request_play)
        self._cards.append(card)

        # Posição no grid
        cols = self._cols()
        row  = n // cols
        col  = n %  cols
        self._grid_layout.addWidget(card, row, col)

        # Solicita thumbnail (não para tipos que não são mídia direta)
        mtype = _file_media_type(path)
        if mtype not in ("pdf", "playlist"):
            self._thumb_service.request(path, mtype)

        # Badge + botão
        cnt = len(self._cards)
        self._count_badge.setText(str(cnt))
        self._count_badge.show()
        self._send_all_btn.setEnabled(True)

    def _on_send_all(self) -> None:
        items = [{"path": f["path"], "title": f["title"]} for f in self._received_files]
        if items:
            self.request_add_all_to_playlist.emit(items)

    def _on_card_add_to_playlist(self, path: str, title: str, orig_name: str) -> None:
        """Apenas emite o sinal — remoção do card ocorre só após confirmar no diálogo."""
        self.request_add_to_playlist.emit(path, title, orig_name)

    def _rebuild_grid(self) -> None:
        """Reposiciona todos os cards no grid após remoção."""
        cols = self._cols()
        for i, card in enumerate(self._cards):
            self._grid_layout.removeWidget(card)
            self._grid_layout.addWidget(card, i // cols, i % cols)

    # ── API pública — chamada pelo main_window após confirmação no diálogo ─

    def remove_received_file(self, path: str) -> None:
        """Remove o card individual. Chamado pelo main_window após o usuário confirmar."""
        card_to_remove = None
        for card in self._cards:
            if card._path == path:
                card_to_remove = card
                break
        if card_to_remove:
            self._grid_layout.removeWidget(card_to_remove)
            card_to_remove.hide()
            card_to_remove.deleteLater()
            self._cards.remove(card_to_remove)
            self._received_files = [f for f in self._received_files if f["path"] != path]
            self._rebuild_grid()
            # Remove temp file se existir
            import os as _os
            if path in self._wifi_tmp_files:
                try:
                    _os.unlink(path)
                except OSError:
                    pass
                self._wifi_tmp_files.discard(path)
            cnt = len(self._cards)
            if cnt == 0:
                self._content_stack.setCurrentIndex(0)  # mostra placeholder
                self._count_badge.hide()
                self._send_all_btn.setEnabled(False)
            else:
                self._count_badge.setText(str(cnt))

    def clear_all_received(self) -> None:
        """Remove todos os cards. Chamado ao confirmar 'enviar todas' ou ao parar o servidor."""
        for card in self._cards:
            self._grid_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._cards.clear()
        self._received_files.clear()
        self._content_stack.setCurrentIndex(0)  # mostra placeholder
        self._count_badge.hide()
        self._send_all_btn.setEnabled(False)
        # Limpa arquivos temporários criados por expansão de JWL/PDF
        import os as _os
        for tmp in list(self._wifi_tmp_files):
            try:
                _os.unlink(tmp)
            except OSError:
                pass
        self._wifi_tmp_files.clear()

    @Slot(str, QPixmap, str)
    def _on_thumb_ready(self, path: str, pixmap: QPixmap, title: str) -> None:
        for card in self._cards:
            if card._path == path:
                card.set_thumbnail(pixmap)
                # Atualiza titulo do card com o resolvido pelos metadados
                if title and title != card._title:
                    card.update_title(title)
                break
        # Atualiza _received_files para que enviar todos use o titulo correto
        if title:
            for entry in self._received_files:
                if entry["path"] == path and title != entry["title"]:
                    entry["title"] = title
                    break

    # ═══════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _set_qr_placeholder(self) -> None:
        ph = QPixmap(_QR_SIZE, _QR_SIZE); ph.fill(Qt.GlobalColor.white)
        self._qr_lbl.setPixmap(ph)

    def _copy_url(self) -> None:
        if not self._session_url: return
        QApplication.clipboard().setText(self._session_url)
        self._copy_btn.setIcon(make_icon(_I_CHECK, 13, _OK))
        # Flash sutil: só o texto do link escurece levemente e volta
        self._url_lbl.setStyleSheet(
            "background:transparent;border:none;color:#1f4f99;"
            "font-size:10px;font-family:monospace;"
        )
        QTimer.singleShot(350, lambda: self._url_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_ACCENT};"
            "font-size:10px;font-family:monospace;"
        ))
        QTimer.singleShot(1800, lambda: self._copy_btn.setIcon(
            make_icon(_I_COPY, 13, _MUTED)
        ))

    # ── QR ────────────────────────────────────────────────────────────────

    def _start_qr_generation(self, url: str) -> None:
        self._cancel_qr_generation()
        self._set_qr_placeholder()
        worker = _QrWorker(url)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.done.connect(self._on_qr_done)
        worker.failed.connect(self._on_qr_failed)
        thread.started.connect(worker.run)
        thread.finished.connect(thread.deleteLater)
        worker.done.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.start()
        self._qr_thread = thread; self._qr_worker = worker

    def _cancel_qr_generation(self) -> None:
        if self._qr_thread and self._qr_thread.isRunning():
            self._qr_thread.quit(); self._qr_thread.wait(500)
            if self._qr_thread.isRunning():
                self._qr_thread.setParent(None)
                self._qr_thread.finished.connect(self._qr_thread.deleteLater)
        self._qr_thread = None; self._qr_worker = None

    @Slot(bytes)
    def _on_qr_done(self, png_data: bytes) -> None:
        pix = QPixmap()
        if not pix.loadFromData(png_data):
            self._on_qr_failed()
            return
        pix = pix.scaled(
            _QR_SIZE,
            _QR_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._qr_lbl.setStyleSheet("background:transparent;border:none;")
        self._qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_lbl.setWordWrap(False)
        self._qr_lbl.setPixmap(pix)
        self._qr_thread = None; self._qr_worker = None

    @Slot()
    def _on_qr_failed(self) -> None:
        self._qr_lbl.setStyleSheet(
            "background:#f6f8fa;border:none;font-size:8px;"
            "font-family:monospace;color:#0d1117;padding:6px;border-radius:6px;"
        )
        self._qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_lbl.setWordWrap(True)
        self._qr_lbl.setText(self._session_url)
        self._qr_thread = None; self._qr_worker = None

    # ── Reflow do grid ao redimensionar ───────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_cards") and self._cards:
            cols = self._cols()
            for i, card in enumerate(self._cards):
                self._grid_layout.removeWidget(card)
                self._grid_layout.addWidget(card, i // cols, i % cols)

    # ═══════════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════════

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Informa ao servidor que a janela está visível → suspende o contador
        # de inatividade e zera o tempo para dar 15 min completos ao voltar.
        self._server.set_window_visible(True)
        if self._server.is_running:
            self._url_lbl.setText(self._session_url)
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(2)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        # Janela saiu da tela → o contador de inatividade começa a correr agora.
        self._server.set_window_visible(False)
        self._cancel_qr_generation()

    # ═══════════════════════════════════════════════════════════════════════
    # i18n
    # ═══════════════════════════════════════════════════════════════════════

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._header_title.setText(self.tr("Receive via Wi-Fi"))
        self._starting_lbl.setText(self.tr("Starting server…"))
        self._instruction_lbl.setText(self.tr("Scan the QR code with your phone.\nBoth devices must be on the same Wi-Fi network."))
        self._inact_lbl.setText(self.tr("The server stops automatically after 15 minutes outside this screen."))
        self._stop_btn.setText(self.tr("  Stop server"))
        self._idle_title.setText(self.tr("Receive media via Wi-Fi"))
        self._idle_subtitle.setText(self.tr("Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."))
        self._start_btn.setText(self.tr("  Start server"))
        self._copy_btn.setToolTip(self.tr("Copy link"))
        self._section_lbl.setText(self.tr("Received media"))
        self._send_all_btn.setText(self.tr("  Send all to playlist"))
        self._placeholder.setText(self.tr("Files sent from your phone will appear here."))

    def refresh_language(self) -> None:
        """Alias de compatibilidade → retranslateUi()."""
        self.retranslateUi()

    def cleanup(self) -> None:
        """Para servidor e threads auxiliares antes da janela ser destruída."""
        try:
            self._server.stop(wait=True)
        except Exception:  # noqa: BLE001 - background server shutdown boundary
            log_ignored_exception(__name__, "Could not stop Wi-Fi receive server")
        self._cancel_qr_generation()
        for attr in ("_pdf_threads", "_jwpub_threads"):
            for thread in list(getattr(self, attr, [])):
                stop_owned_qthread(
                    thread,
                    wait_ms=3_000,
                    label="Wi-Fi helper",
                )
            getattr(self, attr, []).clear()
