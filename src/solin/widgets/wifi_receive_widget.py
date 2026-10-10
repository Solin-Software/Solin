"""
wifi_receive_widget.py — Wi-Fi media receive screen.

Layout:
  • Server STOPPED → centered full-screen idle view.
  • Server ACTIVE → compact QR area above a grid of received media cards
    with thumbnails.

Server lifecycle:
  • Idle is the default screen; the user starts the server manually.
  • Navigating elsewhere does NOT stop the server.
  • Stop only on manual request, 15 minutes of inactivity, or app closure.

Public signals:
  media_received(path, orig_name)
  request_add_to_destination(path, title)
  request_add_all_to_destination(items: list[received_media_entry])
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, Slot, QObject, QTimer, QSize, QEvent
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QStackedWidget,
    QApplication,
    QScrollArea,
    QGridLayout,
)

from ..core.i18n.manager import LanguageManager
from ..core.i18n.strings import tr_document_page_title
from ..core.foundation.exception_logging import log_ignored_exception
from ..core.foundation.qt_threads import stop_owned_qthread
from ..core.foundation.constants import (
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
)
from ..core.jw.language_context import jw_media_language_context
from ..core.media.formats import (
    MediaKind,
    media_kind_from_path,
)
from ..core.playlists.jwl_import import playlist_items_from_jwl_document_items
from ..styles.icons import make_icon
from ..styles.theme import PALETTE, palette_token, qss_rgba
from ..ui.media_info import MediaInfoService

if TYPE_CHECKING:
    from ..ui.qr_generation import QrGenerationSessionFactory
    from ..core.ingest.wifi_server import WifiReceiveServer
    from ..core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ..core.media.profile_store import ProfileMediaStore
    from ..core.rendering.document_conversion import DocumentConversionService
    from ..ui.notifications import NotificationCenter

# ── Paleta ────────────────────────────────────────────────────────────────────

_BG = palette_token("bg0")
_SURFACE = palette_token("surface")
_SURFACE2 = palette_token("surface_hover_strong")
_BORDER = palette_token("border_muted")
_BORDER2 = palette_token("border")
_MUTED = palette_token("text_muted")
_MUTED2 = palette_token("text_dim")
_TEXT = palette_token("text_primary")
_TEXT2 = palette_token("text_secondary")
_ACCENT = palette_token("accent")
_ACCENT_HOVER = palette_token("accent_selection")
_ACCENT_PRESSED = palette_token("accent_pressed")
_OK = palette_token("success")
_ERR = palette_token("danger")
_ERR_TEXT = palette_token("danger_text")
_ERR_SURFACE = palette_token("danger_surface")
_ERR_BORDER = palette_token("danger_border")
_ERR_HOVER = palette_token("danger_surface_hover")
_WHITE = palette_token("white")

_CARD_W = 148
_CARD_H = 168
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
    "</svg>"
)
_I_COPY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="9" width="13" height="13" rx="2"/>'
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    "</svg>"
)
_I_STOP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    "</svg>"
)
_I_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
    '<polyline points="22 4 12 14.01 9 11.01"/>'
    "</svg>"
)
_I_PLAY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/>'
    '<polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/>'
    "</svg>"
)
_I_PLUS = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'
    "</svg>"
)
_I_LIST = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>'
    '<line x1="8" y1="18" x2="21" y2="18"/>'
    '<line x1="3" y1="6" x2="3.01" y2="6" stroke-width="3"/>'
    '<line x1="3" y1="12" x2="3.01" y2="12" stroke-width="3"/>'
    '<line x1="3" y1="18" x2="3.01" y2="18" stroke-width="3"/>'
    "</svg>"
)
_I_VIDEO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="2" y="3" width="20" height="14" rx="2"/>'
    '<polygon points="8,7 16,10 8,13" fill="currentColor" stroke="none"/>'
    "</svg>"
)
_I_AUDIO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9 18V5l12-2v13"/>'
    '<circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'
    "</svg>"
)
_I_IMAGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    '<circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" stroke="none"/>'
    '<polyline points="21,15 16,10 5,21"/>'
    "</svg>"
)
_I_PDF = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="16" y1="13" x2="8" y2="13"/>'
    '<line x1="16" y1="17" x2="8" y2="17"/>'
    '<polyline points="10 9 9 9 8 9"/>'
    "</svg>"
)
_I_PLAYLIST = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="12" y1="18" x2="12" y2="12"/>'
    '<line x1="9" y1="15" x2="15" y2="15"/>'
    "</svg>"
)


def _file_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    media_kind = media_kind_from_path(path)
    if media_kind is not MediaKind.UNKNOWN:
        return media_kind.value
    if ext in PDF_EXTS:
        return "pdf"
    if ext in PLAYLIST_EXTS:
        return "playlist"
    return "image"


def _type_meta(path: str) -> tuple[str, str]:
    """Returns (icon_svg, accent_color) based on file type."""
    t = _file_media_type(path)
    if t == "video":
        return _I_VIDEO, PALETTE.accent_text
    if t == "audio":
        return _I_AUDIO, PALETTE.accent_alt
    if t == "pdf":
        return _I_PDF, PALETTE.warning_text
    if t == "playlist":
        return _I_PLAYLIST, PALETTE.success
    return _I_IMAGE, PALETTE.success


def _rounded_pixmap(src: QPixmap, w: int, h: int, radius: int = 10) -> QPixmap:
    """Scale + crop to w×h with rounded corners."""
    scaled = src.scaled(
        w,
        h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    # Center-crop
    x = (scaled.width() - w) // 2
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


# ── Clickable frame (URL field) ───────────────────────────────────────────────


class _ClickableFrame(QFrame):
    """QFrame emitting clicked() when clicked anywhere."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# Types supporting direct playback

_PLAYABLE_TYPES = frozenset({"video", "audio", "image"})


def _is_playable(path: str) -> bool:
    """Return True for types supporting direct playback/viewing."""
    return _file_media_type(path) in _PLAYABLE_TYPES


# Clickable thumbnail with a play overlay


class _ThumbArea(QWidget):
    """
    Thumbnail area showing a play overlay on hover for playable media.
    Emit clicked() on a left-button press.
    """

    clicked = Signal()

    _OVERLAY_SIZE = 34
    _OVERLAY_BG = qss_rgba(PALETTE.black, 0.71)

    def __init__(self, w: int, h: int, playable: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(w, h)
        self._playable = playable

        # Image label (fills the entire area)
        self._lbl = QLabel(self)
        self._lbl.setFixedSize(w, h)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        if playable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            # Play overlay (centered, does not capture events)
            oz = self._OVERLAY_SIZE
            self._overlay = QLabel(self)
            self._overlay.setFixedSize(oz, oz)
            self._overlay.move((w - oz) // 2, (h - oz) // 2)
            self._overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._overlay.setStyleSheet(f"background:{self._OVERLAY_BG};border-radius:{oz // 2}px;")
            self._overlay.setPixmap(make_icon(_I_PLAY, 18, _WHITE).pixmap(18, 18))
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
    """Individual card with thumbnail, name, playlist button, and click-to-play."""

    add_to_destination = Signal(str, str, str)  # path, title, orig_name
    play_requested = Signal(str, str)  # path, title

    def __init__(
        self,
        path: str,
        title: str,
        lang: LanguageManager,
        parent: QWidget | None = None,
        orig_name: str = "",
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._title = title
        self._orig_name = orig_name or Path(path).name
        self._has_custom_thumbnail = False
        self._type_badge: QLabel | None = None
        self._playlist_btn: QPushButton | None = None
        self.setObjectName("WifiCard")
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._apply_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 10)
        root.setSpacing(0)

        # Thumbnail area (clickable for playable media, inert otherwise)
        playable = _is_playable(path)
        self._thumb_area = _ThumbArea(_CARD_W, _THUMB_H, playable=playable, parent=self)
        self._thumb_lbl = self._thumb_area.label
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_placeholder()
        if playable:
            self._thumb_area.clicked.connect(
                lambda: self.play_requested.emit(self._path, self._title)
            )
        root.addWidget(self._thumb_area)

        # Type badge (video / audio / image)
        _mtype = _file_media_type(path)
        if _mtype in ("video", "audio", "image"):
            _, _badge_color = _type_meta(path)
            # Translate text through tr.
            video = self.tr("video")
            audio = self.tr("audio")
            image = self.tr("image")
            _badge_text = {"video": video, "audio": audio, "image": image}[_mtype]
            self._type_badge = QLabel(_badge_text, self._thumb_area)
            self._style_type_badge(_badge_color)
            self._type_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._type_badge.adjustSize()
            self._type_badge.move(6, 6)
            self._type_badge.raise_()

        # Name and button
        bot = QHBoxLayout()
        bot.setContentsMargins(8, 7, 6, 0)
        bot.setSpacing(4)

        short = title if len(title) <= 22 else title[:20] + "…"
        self._name_lbl = QLabel(short)
        self._name_lbl.setToolTip(title)
        self._name_lbl.setStyleSheet(
            f"background:transparent;color:{_TEXT2};font-size:10px;font-weight:500;border:none;"
        )
        self._name_lbl.setWordWrap(False)
        name = self._name_lbl

        tip = self.tr("Add to…")
        pl_btn = QPushButton()
        self._playlist_btn = pl_btn
        pl_btn.setIcon(make_icon(_I_PLUS, 11, _MUTED))
        pl_btn.setIconSize(QSize(11, 11))
        pl_btn.setFixedSize(24, 24)
        pl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pl_btn.setToolTip(tip)
        pl_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:1px solid {_BORDER2};"
            "border-radius:5px;}"
            f"QPushButton:hover{{background:{PALETTE.accent_muted};border-color:{_ACCENT};}}"
        )
        pl_btn.clicked.connect(
            lambda: self.add_to_destination.emit(
                self._path,
                self._title,
                self._orig_name,
            )
        )

        bot.addWidget(name, stretch=1)
        bot.addWidget(pl_btn)
        root.addLayout(bot)

    # ── Thumbnail helpers ──────────────────────────────────────────────────

    def _set_placeholder(self) -> None:
        """Dark placeholder background with a type icon."""
        svg, color = _type_meta(self._path)
        ph = QPixmap(_CARD_W, _THUMB_H)
        ph.fill(QColor(str(_SURFACE2)))
        # Round only the top corners.
        rounded = QPixmap(_CARD_W, _THUMB_H)
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, _CARD_W, _THUMB_H + 12, 12, 12)
        p.setClipPath(path)
        p.drawPixmap(0, 0, ph)
        p.end()
        # Overlay a centered icon.
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
        self._has_custom_thumbnail = True
        thumb = _rounded_pixmap_top(pixmap, _CARD_W, _THUMB_H, radius=12)
        self._thumb_lbl.setPixmap(thumb)

    def update_title(self, title: str) -> None:
        """Update the displayed title and the title used when choosing a destination."""
        if not title:
            return
        self._title = title
        short = title if len(title) <= 22 else title[:20] + "…"
        self._name_lbl.setText(short)
        self._name_lbl.setToolTip(title)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"QFrame#WifiCard{{background:{_SURFACE};border-radius:12px;"
            f"border:1px solid {_BORDER};}}"
            f"QFrame#WifiCard:hover{{border-color:{_BORDER2};}}"
        )

    def _style_type_badge(self, color: str) -> None:
        if self._type_badge is None:
            return
        self._type_badge.setStyleSheet(
            f"background:{qss_rgba(PALETTE.bg0, 0.78)};border-radius:4px;"
            f"color:{color};font-size:7px;font-weight:700;"
            "padding:2px 6px;letter-spacing:0.5px;border:none;"
        )

    def apply_theme(self) -> None:
        self._apply_style()
        if not self._has_custom_thumbnail:
            self._set_placeholder()
        if self._type_badge is not None:
            _, badge_color = _type_meta(self._path)
            self._style_type_badge(badge_color)
            self._type_badge.adjustSize()
        self._name_lbl.setStyleSheet(
            f"background:transparent;color:{_TEXT2};font-size:10px;font-weight:500;border:none;"
        )
        if self._playlist_btn is not None:
            self._playlist_btn.setIcon(make_icon(_I_PLUS, 11, _MUTED))
            self._playlist_btn.setStyleSheet(
                f"QPushButton{{background:transparent;border:1px solid {_BORDER2};"
                "border-radius:5px;}"
                f"QPushButton:hover{{background:{PALETTE.accent_muted};border-color:{_ACCENT};}}"
            )
        self.update()


def _rounded_pixmap_top(src: QPixmap, w: int, h: int, radius: int = 12) -> QPixmap:
    """Scale and crop to w×h, rounding only the top corners."""
    scaled = src.scaled(
        w,
        h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = (scaled.width() - w) // 2
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
    media_received = Signal(str, str)
    request_add_to_destination = Signal(str, str, str)
    request_add_all_to_destination = Signal(list)
    request_play = Signal(str, str)  # path, title

    def __init__(
        self,
        lang: LanguageManager,
        *,
        notifications: NotificationCenter,
        document_conversion_service: DocumentConversionService,
        profile_media_store: ProfileMediaStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        qr_generation_session_factory: QrGenerationSessionFactory,
        wifi_receive_server_factory: Callable[[QObject], WifiReceiveServer],
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang
        self._notifications = notifications
        self._document_conversion_service = document_conversion_service
        self._profile_media_store = profile_media_store
        self._jwpub_import_thread_factory = jwpub_import_thread_factory
        self._qr_generation_session = qr_generation_session_factory.create(
            parent=self,
        )
        self._server = wifi_receive_server_factory(self)
        self._session_url = ""
        self._received_files: list[dict] = []
        self._cards: list[_MediaCard] = []
        self._thumb_service = media_info_service_factory(self)
        self._wifi_tmp_files: set[str] = set()  # temporary files created by PDF/JWL expansion

        self._server.server_started.connect(self._on_server_started)
        self._server.server_stopped.connect(self._on_server_stopped)
        self._server.file_received.connect(self._on_file_received)
        self._server.error_occurred.connect(self._on_error)
        self._server.inactivity_stopped.connect(self._on_inactivity_stopped)
        self._thumb_service.info_ready.connect(self._on_thumb_ready)
        self._qr_generation_session.ready.connect(self._on_qr_done)
        self._qr_generation_session.failed.connect(self._on_qr_failed)

        self._build_ui()

    # ═══════════════════════════════════════════════════════════════════════
    # BUILD UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_header())

        # Main stack: fills everything below the header.
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{_BG};")
        self._stack.addWidget(self._make_starting_page())  # 0
        self._stack.addWidget(self._make_active_page())  # 1
        self._stack.addWidget(self._make_idle_page())  # 2
        self._stack.setCurrentIndex(2)
        root.addWidget(self._stack, stretch=1)

    # ── Header ────────────────────────────────────────────────────────────

    def _make_header(self) -> QFrame:
        hdr = QFrame()
        self._header_frame = hdr
        hdr.setStyleSheet(f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}")
        hdr.setFixedHeight(54)
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)

        ico = QLabel()
        self._header_icon_lbl = ico
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

    # Page 0: starting

    def _make_starting_page(self) -> QWidget:
        w = QWidget()
        self._starting_page = w
        w.setStyleSheet(f"background:{_BG};")
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._starting_lbl = QLabel(self.tr("Starting server…"))
        self._starting_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._starting_lbl.setStyleSheet(f"color:{_MUTED};font-size:13px;background:transparent;")
        lay.addWidget(self._starting_lbl)
        return w

    # Page 1: active

    def _make_active_page(self) -> QWidget:
        """Zona superior (QR + URL + stop) + zona inferior (grid de cards)."""
        w = QWidget()
        self._active_page = w
        w.setStyleSheet(f"background:{_BG};")
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Server top bar
        bar = QFrame()
        self._server_bar = bar
        bar.setStyleSheet(f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(20, 12, 20, 12)
        bar_lay.setSpacing(16)

        # QR
        qr_frame = QFrame()
        self._qr_frame = qr_frame
        qr_frame.setStyleSheet(f"QFrame{{background:{_WHITE};border-radius:10px;padding:6px;}}")
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

        # Right-side info
        info = QVBoxLayout()
        info.setSpacing(8)
        info.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Instructions
        self._instruction_lbl = QLabel(
            self.tr(
                "Scan the QR code with your phone.\nBoth devices must be on the same Wi-Fi network."
            )
        )
        self._instruction_lbl.setWordWrap(True)
        self._instruction_lbl.setStyleSheet(
            f"background:transparent;color:{_TEXT2};font-size:12px;line-height:1.5;"
        )
        info.addWidget(self._instruction_lbl)

        # Copyable URL: the entire frame is clickable (click to copy).
        self._url_frame = _ClickableFrame()
        self._url_frame.setObjectName("UrlFrame")
        self._url_frame.setToolTip(self.tr("Copy link"))
        self._url_frame.setStyleSheet(
            f"QFrame#UrlFrame{{background:{_BG};border-radius:8px;border:1px solid {_BORDER};}}"
        )
        url_lay = QHBoxLayout(self._url_frame)
        url_lay.setContentsMargins(10, 7, 6, 7)
        url_lay.setSpacing(6)
        self._url_lbl = QLabel("—")
        self._url_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_ACCENT};"
            "font-size:10px;font-family:monospace;"
        )
        self._url_lbl.setWordWrap(True)
        # Disable text selection so it does not conflict with click-to-copy.
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
        # Both the entire frame and the button call _copy_url.
        self._url_frame.clicked.connect(self._copy_url)
        self._copy_btn.clicked.connect(self._copy_url)
        url_lay.addWidget(self._url_lbl, stretch=1)
        url_lay.addWidget(self._copy_btn)
        info.addWidget(self._url_frame)

        # Dica inatividade
        self._inact_lbl = QLabel(
            self.tr("The server stops automatically after 15 minutes outside this screen.")
        )
        self._inact_lbl.setWordWrap(True)
        self._inact_lbl.setStyleSheet(f"background:transparent;color:{_MUTED2};font-size:9px;")
        info.addWidget(self._inact_lbl)

        info.addStretch()

        # Stop button
        self._stop_btn = QPushButton()
        self._stop_btn.setIcon(make_icon(_I_STOP, 12, _ERR))
        self._stop_btn.setIconSize(QSize(12, 12))
        self._stop_btn.setText(self.tr("  Stop server"))
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setStyleSheet(
            f"QPushButton{{background:{_ERR_SURFACE};border:1px solid {_ERR_BORDER};"
            f"border-radius:7px;color:{_ERR_TEXT};font-size:11px;font-weight:600;padding:0 12px;}}"
            f"QPushButton:hover{{background:{_ERR_HOVER};border-color:{_ERR};color:{_ERR};}}"
        )
        self._stop_btn.clicked.connect(lambda: self._server.stop())
        info.addWidget(self._stop_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        bar_lay.addLayout(info, stretch=1)
        root.addWidget(bar)

        # Media section header
        media_hdr = QWidget()
        self._media_header = media_hdr
        media_hdr.setStyleSheet(f"background:{_BG};")
        mh_lay = QHBoxLayout(media_hdr)
        mh_lay.setContentsMargins(20, 12, 20, 8)
        mh_lay.setSpacing(8)

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
        self._send_all_btn.setText(self.tr("  Add all to…"))
        self._send_all_btn.setFixedHeight(26)
        self._send_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_all_btn.setEnabled(False)
        self._send_all_btn.setStyleSheet(
            f"QPushButton{{background:{_BORDER};border:1px solid {_BORDER2};"
            f"border-radius:6px;color:{_MUTED2};font-size:10px;font-weight:600;"
            f"padding:0 10px;}}"
            f"QPushButton:enabled{{color:{_MUTED};}}"
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
        self._media_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{_BG};border:none;}}"
            f"QScrollBar:vertical{{background:{_BG};width:4px;border-radius:2px;margin:0;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;min-height:20px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        # Wrapper containing the centered placeholder AND card grid
        self._content_stack = QStackedWidget()
        self._content_stack.setStyleSheet(f"background:{_BG};")

        # Stack page 0: centered placeholder
        ph_page = QWidget()
        self._placeholder_page = ph_page
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
        self._content_stack.addWidget(ph_page)  # index 0

        # Stack page 1: card grid
        self._grid_container = QWidget()
        self._grid_container.setStyleSheet(f"background:{_BG};")
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setContentsMargins(20, 0, 20, 20)
        self._grid_layout.setSpacing(12)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._content_stack.addWidget(self._grid_container)  # index 1

        self._content_stack.setCurrentIndex(0)  # start on the placeholder
        scroll.setWidget(self._content_stack)
        root.addWidget(scroll, stretch=1)
        return w

    # Page 2: idle

    def _make_idle_page(self) -> QWidget:
        """Centered full-screen view with the server stopped."""
        w = QWidget()
        self._idle_page = w
        w.setStyleSheet(f"background:{_BG};")
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setContentsMargins(40, 0, 40, 0)
        lay.setSpacing(0)

        # Large icon
        ico = QLabel()
        self._idle_icon_lbl = ico
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

        self._idle_subtitle = QLabel(
            self.tr(
                "Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."
            )
        )
        self._idle_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_subtitle.setWordWrap(True)
        self._idle_subtitle.setStyleSheet(
            f"background:transparent;font-size:12px;color:{_MUTED};max-width:340px;"
        )
        lay.addWidget(self._idle_subtitle)

        lay.addSpacing(28)

        self._start_btn = QPushButton()
        self._start_btn.setIcon(make_icon(_I_PLAY, 16, _WHITE))
        self._start_btn.setIconSize(QSize(16, 16))
        self._start_btn.setText(self.tr("  Start server"))
        self._start_btn.setFixedHeight(44)
        self._start_btn.setMinimumWidth(180)
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.setStyleSheet(
            f"QPushButton{{background:{_ACCENT};border:none;border-radius:12px;"
            f"color:{_WHITE};font-size:13px;font-weight:700;padding:0 28px;}}"
            f"QPushButton:hover{{background:{_ACCENT_HOVER};}}"
            f"QPushButton:pressed{{background:{_ACCENT_PRESSED};}}"
        )
        self._start_btn.clicked.connect(self._start_server)
        lay.addWidget(self._start_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        return w

    def apply_theme(self) -> None:
        if hasattr(self, "_header_frame"):
            self._header_frame.setStyleSheet(
                f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}"
            )
            self._header_icon_lbl.setPixmap(make_icon(_I_WIFI, 18, _MUTED).pixmap(18, 18))
            self._header_title.setStyleSheet(
                f"background:transparent;font-size:14px;font-weight:700;color:{_TEXT};"
            )
        if hasattr(self, "_stack"):
            self._stack.setStyleSheet(f"background:{_BG};")
            self._starting_page.setStyleSheet(f"background:{_BG};")
            self._starting_lbl.setStyleSheet(
                f"color:{_MUTED};font-size:13px;background:transparent;"
            )
        if hasattr(self, "_active_page"):
            self._active_page.setStyleSheet(f"background:{_BG};")
            self._server_bar.setStyleSheet(
                f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER};}}"
            )
            self._qr_frame.setStyleSheet(
                f"QFrame{{background:{_WHITE};border-radius:10px;padding:6px;}}"
            )
            self._instruction_lbl.setStyleSheet(
                f"background:transparent;color:{_TEXT2};font-size:12px;line-height:1.5;"
            )
            self._url_frame.setStyleSheet(
                f"QFrame#UrlFrame{{background:{_BG};border-radius:8px;border:1px solid {_BORDER};}}"
            )
            self._url_lbl.setStyleSheet(
                f"background:transparent;border:none;color:{_ACCENT};"
                "font-size:10px;font-family:monospace;"
            )
            self._copy_btn.setIcon(make_icon(_I_COPY, 13, _MUTED))
            self._copy_btn.setStyleSheet(
                "QPushButton{background:transparent;border:none;border-radius:5px;}"
            )
            self._inact_lbl.setStyleSheet(f"background:transparent;color:{_MUTED2};font-size:9px;")
            self._stop_btn.setIcon(make_icon(_I_STOP, 12, _ERR))
            self._stop_btn.setStyleSheet(
                f"QPushButton{{background:{_ERR_SURFACE};border:1px solid {_ERR_BORDER};"
                f"border-radius:7px;color:{_ERR_TEXT};font-size:11px;font-weight:600;padding:0 12px;}}"
                f"QPushButton:hover{{background:{_ERR_HOVER};border-color:{_ERR};color:{_ERR};}}"
            )
            self._media_header.setStyleSheet(f"background:{_BG};")
            self._section_lbl.setStyleSheet(
                f"background:transparent;font-size:11px;font-weight:600;color:{_MUTED};"
            )
            self._count_badge.setStyleSheet(
                f"background:{_BORDER2};color:{_MUTED};font-size:9px;font-weight:600;"
                "border-radius:7px;padding:1px 7px;"
            )
            self._send_all_btn.setIcon(make_icon(_I_LIST, 12, _MUTED))
            self._send_all_btn.setStyleSheet(
                f"QPushButton{{background:{_BORDER};border:1px solid {_BORDER2};"
                f"border-radius:6px;color:{_MUTED2};font-size:10px;font-weight:600;"
                f"padding:0 10px;}}"
                f"QPushButton:enabled{{color:{_MUTED};}}"
                f"QPushButton:enabled:hover{{background:{_BORDER2};color:{_TEXT2};"
                f"border-color:{_ACCENT};}}"
            )
            self._media_scroll.setStyleSheet(
                f"QScrollArea{{background:{_BG};border:none;}}"
                f"QScrollBar:vertical{{background:{_BG};width:4px;border-radius:2px;margin:0;}}"
                f"QScrollBar::handle:vertical{{background:{_BORDER};border-radius:2px;min-height:20px;}}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            )
            self._content_stack.setStyleSheet(f"background:{_BG};")
            self._placeholder_page.setStyleSheet(f"background:{_BG};")
            self._placeholder.setStyleSheet(
                f"color:{_MUTED2};font-size:12px;background:transparent;padding:32px 0;"
            )
            self._grid_container.setStyleSheet(f"background:{_BG};")
        if hasattr(self, "_idle_page"):
            self._idle_page.setStyleSheet(f"background:{_BG};")
            self._idle_icon_lbl.setPixmap(make_icon(_I_WIFI, 56, _BORDER2).pixmap(56, 56))
            self._idle_title.setStyleSheet(
                f"background:transparent;font-size:18px;font-weight:700;color:{_TEXT};"
            )
            self._idle_subtitle.setStyleSheet(
                f"background:transparent;font-size:12px;color:{_MUTED};max-width:340px;"
            )
            self._start_btn.setIcon(make_icon(_I_PLAY, 16, _WHITE))
            self._start_btn.setStyleSheet(
                f"QPushButton{{background:{_ACCENT};border:none;border-radius:12px;"
                f"color:{_WHITE};font-size:13px;font-weight:700;padding:0 28px;}}"
                f"QPushButton:hover{{background:{_ACCENT_HOVER};}}"
                f"QPushButton:pressed{{background:{_ACCENT_PRESSED};}}"
            )
        for card in self._cards:
            card.apply_theme()
        self.update()

    # ═══════════════════════════════════════════════════════════════════════
    # SERVIDOR
    # ═══════════════════════════════════════════════════════════════════════

    def _build_html_labels(self) -> dict[str, str]:
        return {
            "lang": str(getattr(self._lang, "current_code", "en")),
            "title": self.tr("Send Media"),
            "subtitle": self.tr("Select or drag photos, videos or audio files"),
            "btn_label": self.tr("Send media"),
            "drop_hint": self.tr("Drag files here"),
            "success": self.tr("✓ File sent!"),
            "error": self.tr("Upload error"),
            "no_port": self.tr(
                "No network port is available in the configured range."
            ),
        }

    def _build_html_theme(self) -> dict[str, str]:
        return {
            "bg": PALETTE.bg0,
            "surface": PALETTE.surface,
            "surface2": PALETTE.surface_hover_strong,
            "border": PALETTE.border,
            "border2": PALETTE.border_muted,
            "accent": PALETTE.accent,
            "accent_hover": PALETTE.accent_selection,
            "accent_soft": qss_rgba(PALETTE.accent, 0.12),
            "accent_subtle": qss_rgba(PALETTE.accent, 0.03),
            "accent_subtle_hover": qss_rgba(PALETTE.accent, 0.09),
            "text": PALETTE.text_primary,
            "text_on_accent": PALETTE.text_on_accent,
            "muted": PALETTE.text_muted,
            "ok": PALETTE.success,
            "err": PALETTE.danger,
        }

    def _start_server(self) -> None:
        self._stack.setCurrentIndex(0)
        self._server.start(self._build_html_labels(), self._build_html_theme())

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
        self._idle_subtitle.setText(
            self.tr(
                "Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."
            )
        )
        self._stack.setCurrentIndex(2)

    @Slot(str, str)
    def _on_file_received(self, path: str, orig_name: str) -> None:
        ext = Path(path).suffix.lower()

        if ext in PDF_EXTS:
            self._expand_pdf(path, orig_name)
            return

        if ext in PLAYLIST_EXTS:
            self._expand_jwlplaylist(path, orig_name)
            return

        if ext in JWPUB_EXTS:
            self._expand_jwpub(path, orig_name)
            return

        title = Path(orig_name).stem or Path(path).stem
        self._received_files.append({"path": path, "title": title, "orig_name": orig_name})
        self._add_card(path, title, orig_name=orig_name)
        short = orig_name if len(orig_name) <= 34 else orig_name[:32] + "…"
        self._notifications.success(short)
        self.media_received.emit(path, orig_name)

    # PDF expansion

    def _expand_pdf(self, path: str, orig_name: str) -> None:
        """Convert PDF pages to images and add each as a card."""
        pdf_stem = Path(orig_name).stem or Path(path).stem

        pages = self._document_conversion_service.cached_pdf_pages(path)
        if pages:
            self._on_pdf_pages_ready(pages, pdf_stem, orig_name)
            return

        thread = self._document_conversion_service.create_pdf_thread(
            path,
            parent=self,
        )
        thread.pages_ready.connect(
            lambda pages, stem, n=orig_name: self._on_pdf_pages_ready(pages, stem, n)
        )
        thread.conversion_failed.connect(
            lambda err, n=orig_name: self._notifications.error(
                self.tr("Could not convert {name}.\n{error}").format(
                    name=Path(n).name,
                    error=err,
                )
            )
        )
        # Keep the reference alive while running.
        if not hasattr(self, "_pdf_threads"):
            self._pdf_threads: list = []
        self._pdf_threads.append(thread)
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t) if t in self._pdf_threads else None
        )
        thread.start()

    def _on_pdf_pages_ready(self, pages: list, pdf_stem: str, orig_name: str) -> None:
        if not pages:
            return
        n = len(pages)
        for i, page_path in enumerate(pages):
            title = tr_document_page_title(pdf_stem, i + 1)
            self._received_files.append(
                {"path": page_path, "title": title, "orig_name": title + ".jpg"}
            )
            self._add_card(page_path, title, orig_name=title + ".jpg")
        short_name = orig_name if len(orig_name) <= 28 else orig_name[:26] + "…"
        self._notifications.success(
            self.tr("{name}: %n page(s) converted", "", n).format(name=short_name)
        )

    # JWPUB expansion

    def _expand_jwpub(self, path: str, orig_name: str) -> None:
        """
        Parse a .jwpub received via Wi-Fi and add each media item as a card.
        Uses an injected worker factory so signals reach the main thread reliably.
        Images are copied to data/images (persistent). Videos resolved via API.
        """
        stem = Path(orig_name).stem or Path(path).stem
        self._notifications.information(f"{stem}…")
        lang = jw_media_language_context(self._lang).api_code

        thread = self._jwpub_import_thread_factory.create(
            path,
            lang=lang,
            parent=self,
        )

        if not hasattr(self, "_jwpub_threads"):
            self._jwpub_threads: list = []
        self._jwpub_threads.append(thread)
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t) if t in self._jwpub_threads else None
        )

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str):
            added = 0
            for raw in items:
                item_path = raw.get("url", "")
                item_title = raw.get("title", file_stem)
                if not item_path:
                    continue
                self._received_files.append(
                    {
                        "path": item_path,
                        "title": item_title,
                        "orig_name": item_title,
                    }
                )
                self._add_card(item_path, item_title, orig_name=item_title)
                added += 1
            if added:
                self._notifications.success(
                    self.tr("{name}: %n item(s) added", "", added).format(
                        name=file_stem
                    )
                )
            else:
                self._notifications.warning(
                    self.tr("No media found in {name}.").replace("{name}", file_stem)
                )

        @thread.failed.connect
        def _on_fail(err: str):
            self._notifications.error(
                self.tr("Could not open {name}.\n{error}").format(
                    name=stem,
                    error=err,
                )
            )

        thread.start()

    # JWL playlist expansion

    def _expand_jwlplaylist(self, path: str, orig_name: str) -> None:
        """Read .jwlplaylist and add each item as an individual card."""
        import zipfile
        from ..core.playlists.jwl_files import read_jwlplaylist_document

        fallback_lang = jw_media_language_context(self._lang).fallback_code

        try:
            document = read_jwlplaylist_document(
                path,
                fallback_lang_code=fallback_lang,
            )
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            self._notifications.error(
                self.tr("Could not read playlist {name}.\n{error}").format(
                    name=Path(orig_name).name,
                    error=exc,
                )
            )
            return

        def _save_embedded(
            data: bytes,
            filename: str,
            identifier: str,
            default_suffix: str,
        ) -> str:
            """Write embedded media to data/embedded/ for persistence."""
            saved_path = self._profile_media_store.save_embedded(
                data,
                filename,
                identifier=identifier,
                default_suffix=default_suffix,
            )
            self._wifi_tmp_files.add(saved_path)
            return saved_path

        result = playlist_items_from_jwl_document_items(
            document.items,
            source_name=orig_name,
            save_embedded=_save_embedded,
        )

        for item in result.items:
            item_path = str(item.get("url") or "")
            title = str(item.get("title") or Path(item_path).stem or self.tr("Item"))
            orig_item_name = str(item.get("original_filename") or title)
            entry = {
                "path": item_path,
                "title": title,
                "orig_name": orig_item_name,
                "type": item.get("type", "video"),
                "key_symbol": item.get("key_symbol"),
                "track": item.get("track"),
                "issue_tag": item.get("issue_tag"),
                "doc_id": item.get("doc_id"),
                "meps_language": item.get("meps_language", 0),
            }
            self._received_files.append(entry)
            self._add_card(item_path, title, orig_name=orig_item_name)

        pl_name = document.name or Path(orig_name).stem
        if result.items:
            self._notifications.success(
                self.tr(
                    "{name}: %n item(s) imported",
                    "",
                    len(result.items),
                ).format(name=pl_name)
            )
        if result.skipped_titles:
            self._notifications.warning(
                self.tr(
                    "%n item(s) could not be resolved.",
                    "",
                    len(result.skipped_titles),
                )
            )

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._idle_subtitle.setText(
            self.tr("Could not start the server.\n{error}").format(error=msg)
        )
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
        """Calculate columns from the available width."""
        w = self._grid_container.width() or self.width()
        usable = max(w - 40, _CARD_W)  # 40 = margens
        return max(1, usable // (_CARD_W + 12))

    def _add_card(self, path: str, title: str, orig_name: str = "") -> None:
        n = len(self._cards)
        # Remove the placeholder when the first media arrives.
        if n == 0:
            self._content_stack.setCurrentIndex(1)  # mostra grid

        card = _MediaCard(path, title, self._lang, self._grid_container, orig_name=orig_name)
        card.add_to_destination.connect(self._on_card_add_to_destination)
        card.play_requested.connect(self.request_play)
        self._cards.append(card)

        # Grid position
        cols = self._cols()
        row = n // cols
        col = n % cols
        self._grid_layout.addWidget(card, row, col)

        # Request a thumbnail only for direct media types.
        mtype = _file_media_type(path)
        if mtype not in ("pdf", "playlist"):
            self._thumb_service.request(path, mtype)

        # Badge and button
        cnt = len(self._cards)
        self._count_badge.setText(str(cnt))
        self._count_badge.show()
        self._send_all_btn.setEnabled(True)

    def _on_send_all(self) -> None:
        items = [dict(entry) for entry in self._received_files]
        if items:
            self.request_add_all_to_destination.emit(items)

    def _on_card_add_to_destination(self, path: str, title: str, orig_name: str) -> None:
        """Emit the signal only; remove the card after dialog confirmation."""
        self.request_add_to_destination.emit(path, title, orig_name)

    def _rebuild_grid(self) -> None:
        """Reposition all cards in the grid after removal."""
        cols = self._cols()
        for i, card in enumerate(self._cards):
            self._grid_layout.removeWidget(card)
            self._grid_layout.addWidget(card, i // cols, i % cols)

    # Public API: called by main_window after dialog confirmation.

    def received_entry(self, path: str) -> dict:
        """Return a copy of the received-media entry for a local path."""
        entry = next(
            (entry for entry in self._received_files if entry.get("path") == path),
            None,
        )
        return dict(entry) if entry is not None else {}

    def preserve_temp_file(self, path: str) -> None:
        """Transfer a generated temp file to playlist ownership."""
        self._wifi_tmp_files.discard(path)

    def remove_received_file(self, path: str) -> None:
        """Remove one card. Called by main_window after user confirmation."""
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
            if path in self._wifi_tmp_files:
                self._profile_media_store.remove_file(path)
                self._wifi_tmp_files.discard(path)
            cnt = len(self._cards)
            if cnt == 0:
                self._content_stack.setCurrentIndex(0)  # mostra placeholder
                self._count_badge.hide()
                self._send_all_btn.setEnabled(False)
            else:
                self._count_badge.setText(str(cnt))

    def clear_all_received(self) -> None:
        """Remove all cards after confirming 'send all' or stopping the server."""
        for card in self._cards:
            self._grid_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._cards.clear()
        self._received_files.clear()
        self._content_stack.setCurrentIndex(0)  # mostra placeholder
        self._count_badge.hide()
        self._send_all_btn.setEnabled(False)
        for tmp in list(self._wifi_tmp_files):
            self._profile_media_store.remove_file(tmp)
        self._wifi_tmp_files.clear()

    @Slot(str, QPixmap, str)
    def _on_thumb_ready(self, path: str, pixmap: QPixmap, title: str) -> None:
        for card in self._cards:
            if card._path == path:
                card.set_thumbnail(pixmap)
                # Update the card title with the title resolved from metadata.
                if title and title != card._title:
                    card.update_title(title)
                break
        # Update _received_files so sending all uses the correct title.
        if title:
            for entry in self._received_files:
                if entry["path"] == path and title != entry["title"]:
                    entry["title"] = title
                    break

    # ═══════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _set_qr_placeholder(self) -> None:
        ph = QPixmap(_QR_SIZE, _QR_SIZE)
        ph.fill(Qt.GlobalColor.white)
        self._qr_lbl.setPixmap(ph)

    def _copy_url(self) -> None:
        if not self._session_url:
            return
        QApplication.clipboard().setText(self._session_url)
        self._copy_btn.setIcon(make_icon(_I_CHECK, 13, _OK))
        # Subtle flash: briefly darken only the link text, then restore it.
        self._url_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_ACCENT_PRESSED};"
            "font-size:10px;font-family:monospace;"
        )
        QTimer.singleShot(
            350,
            lambda: self._url_lbl.setStyleSheet(
                f"background:transparent;border:none;color:{_ACCENT};"
                "font-size:10px;font-family:monospace;"
            ),
        )
        QTimer.singleShot(1800, lambda: self._copy_btn.setIcon(make_icon(_I_COPY, 13, _MUTED)))

    # ── QR ────────────────────────────────────────────────────────────────

    def _start_qr_generation(self, url: str) -> None:
        self._set_qr_placeholder()
        self._qr_generation_session.start(url)

    def _cancel_qr_generation(self) -> None:
        self._qr_generation_session.cancel()

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

    @Slot()
    def _on_qr_failed(self) -> None:
        self._qr_lbl.setStyleSheet(
            f"background:{_WHITE};border:none;font-size:8px;"
            f"font-family:monospace;color:{_BG};padding:6px;border-radius:6px;"
        )
        self._qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_lbl.setWordWrap(True)
        self._qr_lbl.setText(self._session_url)

    # Reflow the grid on resize.

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
        # Tell the server the window is visible: suspend the inactivity
        # timer and reset it to allow a full 15 minutes on return.
        self._server.set_window_visible(True)
        if self._server.is_running:
            self._url_lbl.setText(self._session_url)
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(2)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        # Window no longer visible: start the inactivity timer now.
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
        self._instruction_lbl.setText(
            self.tr(
                "Scan the QR code with your phone.\nBoth devices must be on the same Wi-Fi network."
            )
        )
        self._inact_lbl.setText(
            self.tr("The server stops automatically after 15 minutes outside this screen.")
        )
        self._stop_btn.setText(self.tr("  Stop server"))
        self._server.update_upload_page(
            self._build_html_labels(),
            self._build_html_theme(),
        )
        self._idle_title.setText(self.tr("Receive media via Wi-Fi"))
        self._idle_subtitle.setText(
            self.tr(
                "Connect to the same Wi-Fi and open the link on your phone to send photos, videos or audio."
            )
        )
        self._start_btn.setText(self.tr("  Start server"))
        self._copy_btn.setToolTip(self.tr("Copy link"))
        self._section_lbl.setText(self.tr("Received media"))
        self._send_all_btn.setText(self.tr("  Add all to…"))
        self._placeholder.setText(self.tr("Files sent from your phone will appear here."))

    def cleanup(self) -> None:
        """Stop the server and helper threads before destroying the window."""
        try:
            self._server.stop(wait=True)
        except Exception:  # noqa: BLE001 - background server shutdown boundary
            log_ignored_exception(__name__, "Could not stop Wi-Fi receive server")
        self._qr_generation_session.close()
        for attr in ("_pdf_threads", "_jwpub_threads"):
            for thread in list(getattr(self, attr, [])):
                stop_owned_qthread(
                    thread,
                    wait_ms=3_000,
                    label="Wi-Fi helper",
                )
            getattr(self, attr, []).clear()
