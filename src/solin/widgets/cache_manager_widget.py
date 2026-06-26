"""
CacheManagerWidget — Gerenciador visual de mídias em cache.

Funcionalidades:
  • Lista todas as mídias em cache/media/ com thumbnails reais
  • Título extraído de metadados (mutagen) com fallback para nome do arquivo
  • Filtra por tipo: todos / vídeos / áudios / imagens
  • Seleção individual e múltipla; "selecionar todos" aparece após 1ª seleção
  • Reprodução prévia antes de excluir
  • Exclusão segura com confirmação

Decisões técnicas importantes:
  • Cards NUNCA são destruídos ao trocar filtro — usa setVisible(bool).
    Isso evita o crash "Internal C++ object already deleted" que ocorre quando
    um layout Qt chama setParent(None) e libera o ponteiro C++ enquanto o Python
    ainda guarda a referência ao objeto.
  • O grid usa QGridLayout com colunas recalculadas em resizeEvent.
  • Scan + metadados de título rodam em QThread separada.
  • Thumbnails via MediaThumbService (centralizado em thumbnail_extractor.py):
      áudio → bytes brutos (zero player) → fallback QMediaMetaData
      vídeo → QMediaMetaData CoverArt → fallback frame seek 5%
      imagem → QPixmap direto
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, QObject, QSize, QTimer, QEvent
from PySide6.QtGui import QPixmap, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QStackedWidget, QMessageBox,
    QButtonGroup, QGridLayout,
)

from ..core.media.cache import MediaCacheManager
from ..core.media.cache_listing import CachedMediaItem
from ..core.i18n.manager import LanguageManager
from ..styles.icons import make_icon, ICON_MUSIC, ICON_VIDEO, ICON_IMAGE
from ..styles.theme import PALETTE, palette_token, qss_rgba
from ..ui.media_info import MediaInfoService

if TYPE_CHECKING:
    from ..core.media.cache_scan import CacheScanSession, CacheScanSessionFactory


# ── Constantes visuais ────────────────────────────────────────────────────────

_CARD_W       = 160
_CARD_H       = 152
_THUMB_W      = 148
_THUMB_H      = 88
_GRID_SPACING = 12
_ACCENT       = palette_token("accent")
_ACCENT_MUTED = palette_token("accent_muted")
_ACCENT_TEXT  = palette_token("accent_text")
_ACCENT_HOVER = palette_token("accent_muted_hover")
_WHITE        = palette_token("white")
_BG           = palette_token("bg0")
_BG_CARD_HOVER = palette_token("surface_hover_strong")
_BG_CARD      = palette_token("surface_card")
_BG_CARD_SEL  = palette_token("accent_tint")
_SURFACE      = palette_token("surface")
_SURFACE_2    = palette_token("bg2")
_SURFACE_3    = palette_token("bg3")
_SURFACE_BAR  = palette_token("surface_hover")
_BORDER_NORM  = palette_token("border_muted")
_BORDER       = palette_token("border")
_BORDER_SEL   = palette_token("accent")
_TEXT         = palette_token("text_primary")
_TEXT_BODY    = palette_token("text_secondary")
_TEXT_MUTED   = palette_token("text_muted")
_TEXT_DIM     = palette_token("text_dim")
_TEXT_FAINT   = palette_token("text_faint")
_DANGER       = palette_token("danger")
_DANGER_TEXT  = palette_token("danger_text")
_DANGER_BG    = palette_token("danger_surface")
_DANGER_BORDER = palette_token("danger_border")
_DANGER_HOVER = palette_token("danger_surface_hover")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n < 1024:        return f"{n} B"
    if n < 1024 ** 2:   return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:   return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _rounded_pixmap(pixmap: QPixmap, w: int, h: int, radius: int = 6) -> QPixmap:
    scaled = pixmap.scaled(w, h,
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    if scaled.width() > w or scaled.height() > h:
        x = (scaled.width() - w) // 2
        y = (scaled.height() - h) // 2
        scaled = scaled.copy(x, y, w, h)
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, scaled)
    p.end()
    return out

# ── SVG Icons locais ──────────────────────────────────────────────────────────

_ICON_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2.5" stroke-linecap="round"'
    ' stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
)
_ICON_TRASH = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    '<path d="M10 11v6"/><path d="M14 11v6"/>'
    '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>'
    '</svg>'
)
_ICON_PLAY_CIRCLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/>'
    '<polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/>'
    '</svg>'
)
_ICON_STORAGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polygon points="12 2 2 7 12 12 22 7 12 2"/>'
    '<polyline points="2 17 12 22 22 17"/>'
    '<polyline points="2 12 12 17 22 12"/>'
    '</svg>'
)
_ICON_SELECT_ALL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>'
    '<rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>'
    '</svg>'
)
_ICON_DESELECT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2" stroke-dasharray="5 3"/>'
    '<line x1="9" y1="15" x2="15" y2="9"/>'
    '</svg>'
)


# ── Card individual ───────────────────────────────────────────────────────────

class MediaCard(QFrame):
    """
    Card de uma mídia no grid.

    REGRA FUNDAMENTAL: cards nunca são destruídos ao trocar filtro.
    O CacheManagerWidget usa setVisible() para mostrar/ocultar.
    Só _do_delete() pode chamar deleteLater() nos cards excluídos.
    """
    selection_changed = Signal(str, bool)           # path, selected
    play_requested    = Signal(str, str, str, str)  # path, media_type, original_url, display_title

    def __init__(self, item: CachedMediaItem, lang: LanguageManager, parent=None):
        super().__init__(parent)
        self._item     = item
        self._lang     = lang
        self._selected = False
        self._display_title = item.display_title
        self._has_custom_thumbnail = False
        self._play_btn: QPushButton | None = None
        self.setObjectName("MediaCard")
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._apply_style()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 8)
        lay.setSpacing(4)

        # ── Área do thumbnail ─────────────────────────────────────────────
        thumb_area = QWidget()
        thumb_area.setFixedSize(_THUMB_W, _THUMB_H)
        thumb_area.setStyleSheet("background:transparent;")

        self._thumb_lbl = QLabel(thumb_area)
        self._thumb_lbl.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setStyleSheet(
            f"background:{_BG};border-radius:6px;border:1px solid {_BORDER_NORM};"
        )
        icon_map = {"video": ICON_VIDEO, "audio": ICON_MUSIC, "image": ICON_IMAGE}
        self._thumb_lbl.setPixmap(
            make_icon(icon_map.get(self._item.media_type, ICON_VIDEO), 28, _BORDER).pixmap(28, 28)
        )

        # Check badge (canto superior direito)
        self._check_badge = QLabel(thumb_area)
        self._check_badge.setFixedSize(22, 22)
        self._check_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._check_badge.setPixmap(make_icon(_ICON_CHECK, 12, _WHITE).pixmap(12, 12))
        self._check_badge.move(_THUMB_W - 28, 4)
        self._check_badge.setStyleSheet(f"background:{_ACCENT};border-radius:11px;")
        self._check_badge.setVisible(False)

        # Botão play
        if self._item.media_type in ("video", "audio", "image"):
            play_btn = QPushButton(thumb_area)
            self._play_btn = play_btn
            play_btn.setFixedSize(36, 36)
            play_btn.setIcon(make_icon(_ICON_PLAY_CIRCLE, 22, _WHITE))
            play_btn.setIconSize(QSize(22, 22))
            play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            play_btn.setToolTip(self.tr("Play"))
            play_btn.move((_THUMB_W - 36) // 2, (_THUMB_H - 36) // 2)
            play_btn.setStyleSheet(
                f"QPushButton{{background:{qss_rgba(PALETTE.black, 0.55)};border-radius:18px;"
                f"border:1.5px solid {qss_rgba(str(_WHITE), 0.25)};}}"
                f"QPushButton:hover{{background:{qss_rgba(str(_ACCENT), 0.85)};border-color:{_ACCENT};}}"
            )
            play_btn.clicked.connect(
                lambda _=False: self._emit_play_requested()
            )

        lay.addWidget(thumb_area)

        # ── Título ────────────────────────────────────────────────────────
        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_TEXT_BODY};font-size:10px;font-weight:500;"
        )
        self._title_lbl.setFixedWidth(_THUMB_W - 4)
        elided = self._title_lbl.fontMetrics().elidedText(
            self._display_title, Qt.TextElideMode.ElideRight, _THUMB_W - 8
        )
        self._title_lbl.setText(elided)
        
        tt = self._display_title
        if self._display_title != self._item.filename:
            tt = f"{self._display_title}\n{self._item.filename}"
        self._title_lbl.setToolTip(tt)
        
        self._size_lbl = QLabel(_fmt_size(self._item.size))
        self._size_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_TEXT_DIM};font-size:9px;"
        )

        lay.addWidget(self._title_lbl)
        lay.addWidget(self._size_lbl)

    # ── API pública ───────────────────────────────────────────────────────

    def set_thumbnail(self, pixmap: QPixmap):
        self._has_custom_thumbnail = True
        self._thumb_lbl.setPixmap(pixmap)
    
    def set_title(self, title: str):
        if not title:
            return
        self._display_title = title
        elided = self._title_lbl.fontMetrics().elidedText(
            title, Qt.TextElideMode.ElideRight, _THUMB_W - 8
        )
        self._title_lbl.setText(elided)
        
        tt = title
        if title != self._item.filename:
            tt = f"{title}\n{self._item.filename}"
        self._title_lbl.setToolTip(tt)

    @property
    def display_title(self) -> str:
        return self._display_title

    def set_selected(self, selected: bool, emit: bool = False):
        if self._selected == selected:
            return
        self._selected = selected
        self._check_badge.setVisible(selected)
        self._apply_style()
        if emit:
            self.selection_changed.emit(self._item.path, selected)

    def is_selected(self) -> bool:
        return self._selected

    @property
    def path(self)       -> str: return self._item.path
    @property
    def media_type(self) -> str: return self._item.media_type
    @property
    def file_size(self)  -> int: return self._item.size

    def _emit_play_requested(self) -> None:
        self.play_requested.emit(
            self._item.path,
            self._item.media_type,
            self._item.original_url,
            self._display_title,
        )

    def _apply_style(self):
        if self._selected:
            self.setStyleSheet(
                f"QFrame#MediaCard{{background:{_BG_CARD_SEL};border-radius:10px;"
                f"border:1.5px solid {_BORDER_SEL};}}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#MediaCard{{background:{_BG_CARD};border-radius:10px;"
                f"border:1.5px solid {_BORDER_NORM};}}"
                f"QFrame#MediaCard:hover{{background:{_BG_CARD_HOVER};border-color:{_BORDER};}}"
            )

    def apply_theme(self) -> None:
        self._thumb_lbl.setStyleSheet(
            f"background:{_BG};border-radius:6px;border:1px solid {_BORDER_NORM};"
        )
        if not self._has_custom_thumbnail:
            icon_map = {"video": ICON_VIDEO, "audio": ICON_MUSIC, "image": ICON_IMAGE}
            self._thumb_lbl.setPixmap(
                make_icon(
                    icon_map.get(self._item.media_type, ICON_VIDEO),
                    28,
                    _BORDER,
                ).pixmap(28, 28)
            )
        self._check_badge.setPixmap(make_icon(_ICON_CHECK, 12, _WHITE).pixmap(12, 12))
        self._check_badge.setStyleSheet(f"background:{_ACCENT};border-radius:11px;")
        if self._play_btn is not None:
            self._play_btn.setIcon(make_icon(_ICON_PLAY_CIRCLE, 22, _WHITE))
            self._play_btn.setStyleSheet(
                f"QPushButton{{background:{qss_rgba(PALETTE.black, 0.55)};border-radius:18px;"
                f"border:1.5px solid {qss_rgba(str(_WHITE), 0.25)};}}"
                f"QPushButton:hover{{background:{qss_rgba(str(_ACCENT), 0.85)};border-color:{_ACCENT};}}"
            )
        self._title_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_TEXT_BODY};font-size:10px;font-weight:500;"
        )
        self._size_lbl.setStyleSheet(
            f"background:transparent;border:none;color:{_TEXT_DIM};font-size:9px;"
        )
        self._apply_style()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_selected(not self._selected, emit=True)
        super().mousePressEvent(event)


# ── Chip de filtro ────────────────────────────────────────────────────────────

class _Chip(QPushButton):
    def __init__(self, label: str, key: str, parent=None):
        super().__init__(label, parent)
        self.filter_key = key
        self.setCheckable(True)
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh()
        self.toggled.connect(lambda _: self._refresh())

    def _refresh(self):
        if self.isChecked():
            self.setStyleSheet(
                f"QPushButton{{background:{_ACCENT};border-radius:15px;border:none;"
                f"color:{_WHITE};font-size:11px;font-weight:600;padding:0 14px;}}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton{{background:{_SURFACE_2};border-radius:15px;border:1px solid {_BORDER};"
                f"color:{_TEXT_MUTED};font-size:11px;padding:0 14px;}}"
                f"QPushButton:hover{{background:{_SURFACE_3};color:{_TEXT_BODY};border-color:{_TEXT_DIM};}}"
            )

    def apply_theme(self) -> None:
        self._refresh()


# ── Widget principal ──────────────────────────────────────────────────────────

class CacheManagerWidget(QWidget):
    """
    Tela de gerenciamento de cache.
    Acessível pelo nav lateral (índice 7) e via botão em Configurações.
    """
    play_media_requested = Signal(str, str, str, str)  # path, media_type, original_url, display_title

    def __init__(
        self,
        lang: LanguageManager,
        cache_manager: MediaCacheManager,
        *,
        cache_scan_session_factory: CacheScanSessionFactory,
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._lang         = lang
        self._cache_manager = cache_manager
        self._all_cards:   list[MediaCard] = []
        self._vis_cards:   list[MediaCard] = []
        self._selected:    set[str]        = set()
        self._cur_filter:  str             = "all"
        self._type_counts: dict[str, int]  = {}
        self._cache_scan_session_factory = cache_scan_session_factory
        self._scan_session: CacheScanSession | None = None
        self._thumb_service = media_info_service_factory(self)
        self._thumb_service.info_ready.connect(self._on_thumb_ready)
        self._build_ui()

    # ── Build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_header())
        root.addWidget(self._make_toolbar())
        root.addWidget(self._make_stack(), stretch=1)
        root.addWidget(self._make_action_bar())

    def _make_header(self) -> QFrame:
        hdr = QFrame()
        self._header_frame = hdr
        hdr.setStyleSheet(f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER_NORM};}}")
        hdr.setFixedHeight(54)
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)

        icon_lbl = QLabel()
        self._header_icon_lbl = icon_lbl
        icon_lbl.setPixmap(make_icon(_ICON_STORAGE, 18, _TEXT_MUTED).pixmap(18, 18))
        icon_lbl.setStyleSheet("background:transparent;")

        self._title_lbl = QLabel(self.tr("Media Manager"))
        self._title_lbl.setObjectName("SectionTitle")
        self._title_lbl.setStyleSheet(
            f"background:transparent;font-size:14px;font-weight:700;color:{_TEXT};"
        )

        self._size_badge = QLabel()
        self._size_badge.setStyleSheet(
            "background:transparent;border:none;"
            f"color:{_TEXT_FAINT};font-size:10px;padding:2px 0;"
        )
        self._size_badge.setVisible(False)

        lay.addWidget(icon_lbl)
        lay.addWidget(self._title_lbl)
        lay.addStretch()
        lay.addWidget(self._size_badge)
        return hdr

    def _make_toolbar(self) -> QFrame:
        tb = QFrame()
        self._toolbar_frame = tb
        tb.setStyleSheet(f"QFrame{{background:{_BG};border-bottom:1px solid {_BG_CARD_HOVER};}}")
        tb.setFixedHeight(52)
        lay = QHBoxLayout(tb)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(6)

        self._chip_all   = _Chip(self.tr("All"),   "all")
        self._chip_video = _Chip(self.tr("Videos"), "video")
        self._chip_audio = _Chip(self.tr("Audio"), "audio")
        self._chip_image = _Chip(self.tr("Images"), "image")
        self._chip_all.setChecked(True)

        # Grupo exclusivo via QButtonGroup para garantir single-select
        self._chip_grp = QButtonGroup(self)
        self._chip_grp.setExclusive(True)
        for chip in (self._chip_all, self._chip_video, self._chip_audio, self._chip_image):
            self._chip_grp.addButton(chip)
            lay.addWidget(chip)

        # buttonToggled é disparado UMA vez pelo botão que FICOU ativo
        self._chip_grp.buttonToggled.connect(self._on_chip_toggled)

        lay.addStretch()
        return tb

    def _make_stack(self) -> QStackedWidget:
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{_BG};")

        # 0 — Loading
        load_w = QWidget()
        self._loading_page = load_w
        load_w.setStyleSheet(f"background:{_BG};")
        ll = QVBoxLayout(load_w)
        ll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_lbl = QLabel(self.tr("Loading cached media…"))
        self._loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_lbl.setStyleSheet(f"color:{_TEXT_DIM};font-size:13px;")
        ll.addWidget(self._loading_lbl)

        # 1 — Empty
        empty_w = QWidget()
        self._empty_page = empty_w
        empty_w.setStyleSheet(f"background:{_BG};")
        el = QVBoxLayout(empty_w)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.setSpacing(10)
        ei = QLabel()
        self._empty_icon_lbl = ei
        ei.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ei.setPixmap(make_icon(_ICON_STORAGE, 40, _BORDER_NORM).pixmap(40, 40))
        ei.setStyleSheet("background:transparent;")
        self._empty_lbl = QLabel(self.tr("No cached media found."))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet(f"color:{_TEXT_DIM};font-size:13px;")
        el.addWidget(ei)
        el.addWidget(self._empty_lbl)

        # 2 — Grid scroll
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea{{background:{_BG};border:none;}}"
            f"QScrollBar:vertical{{width:6px;background:{_BG};border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{_BORDER_NORM};border-radius:3px;min-height:20px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self._grid_host = QWidget()
        self._grid_host.setStyleSheet(f"background:{_BG};")
        self._grid_lay = QGridLayout(self._grid_host)
        self._grid_lay.setContentsMargins(16, 16, 16, 16)
        self._grid_lay.setSpacing(_GRID_SPACING)
        self._grid_lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_host)

        self._stack.addWidget(load_w)         # 0
        self._stack.addWidget(empty_w)        # 1
        self._stack.addWidget(self._scroll)   # 2
        return self._stack

    def _make_action_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("CacheActionBar")
        bar.setFixedHeight(64)
        bar.setStyleSheet(
            "QFrame#CacheActionBar{"
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {_SURFACE_BAR},stop:1 {_SURFACE});"
            f"border-top:1px solid {_BORDER_NORM};}}"
        )
        bar.setVisible(False)
        self._action_bar = bar

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(12)

        # Coluna de info: contador de seleção
        info_col = QVBoxLayout()
        info_col.setSpacing(2)

        self._sel_count_lbl = QLabel()
        self._sel_count_lbl.setStyleSheet(
            "background:transparent;border:none;"
            f"color:{_TEXT};font-size:12px;font-weight:600;"
        )

        self._sel_size_lbl = QLabel()
        self._sel_size_lbl.setStyleSheet(
            "background:transparent;border:none;"
            f"color:{_TEXT_FAINT};font-size:10px;"
        )

        info_col.addWidget(self._sel_count_lbl)
        info_col.addWidget(self._sel_size_lbl)
        lay.addLayout(info_col)

        lay.addStretch()

        self._sel_all_btn = QPushButton()
        self._sel_all_btn.setIcon(make_icon(_ICON_SELECT_ALL, 14, _TEXT_MUTED))
        self._sel_all_btn.setIconSize(QSize(14, 14))
        self._sel_all_btn.setFixedHeight(34)
        self._sel_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sel_all_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:1px solid {_BORDER};"
            f"border-radius:6px;color:{_TEXT_MUTED};font-size:10px;padding:0 12px;}}"
            f"QPushButton:hover{{background:{_SURFACE_2};color:{_TEXT_BODY};border-color:{_TEXT_DIM};}}"
        )
        self._sel_all_btn.setVisible(False)
        self._sel_all_btn.clicked.connect(self._toggle_select_all)

        self._del_btn = QPushButton()
        self._del_btn.setFixedHeight(34)
        self._del_btn.setIcon(make_icon(_ICON_TRASH, 14, _DANGER_TEXT))
        self._del_btn.setIconSize(QSize(14, 14))
        self._del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_btn.setStyleSheet(
            f"QPushButton{{background:{_DANGER_BG};border:1px solid {_DANGER_BORDER};"
            f"border-radius:6px;color:{_DANGER_TEXT};font-size:11px;"
            "font-weight:600;padding:0 16px;}"
            f"QPushButton:hover{{background:{_DANGER_HOVER};border-color:{_DANGER};color:{_DANGER};}}"
            f"QPushButton:pressed{{background:{_DANGER_BG};}}"
        )
        self._del_btn.clicked.connect(self._confirm_delete)

        lay.addWidget(self._sel_all_btn)
        lay.addWidget(self._del_btn)
        return bar

    def apply_theme(self) -> None:
        if hasattr(self, "_header_frame"):
            self._header_frame.setStyleSheet(
                f"QFrame{{background:{_SURFACE};border-bottom:1px solid {_BORDER_NORM};}}"
            )
            self._header_icon_lbl.setPixmap(
                make_icon(_ICON_STORAGE, 18, _TEXT_MUTED).pixmap(18, 18)
            )
            self._title_lbl.setStyleSheet(
                f"background:transparent;font-size:14px;font-weight:700;color:{_TEXT};"
            )
            self._size_badge.setStyleSheet(
                "background:transparent;border:none;"
                f"color:{_TEXT_FAINT};font-size:10px;padding:2px 0;"
            )
        if hasattr(self, "_toolbar_frame"):
            self._toolbar_frame.setStyleSheet(
                f"QFrame{{background:{_BG};border-bottom:1px solid {_BG_CARD_HOVER};}}"
            )
            for chip in (
                self._chip_all,
                self._chip_video,
                self._chip_audio,
                self._chip_image,
            ):
                chip.apply_theme()
        if hasattr(self, "_stack"):
            self._stack.setStyleSheet(f"background:{_BG};")
            self._loading_page.setStyleSheet(f"background:{_BG};")
            self._loading_lbl.setStyleSheet(f"color:{_TEXT_DIM};font-size:13px;")
            self._empty_page.setStyleSheet(f"background:{_BG};")
            self._empty_icon_lbl.setPixmap(
                make_icon(_ICON_STORAGE, 40, _BORDER_NORM).pixmap(40, 40)
            )
            self._empty_lbl.setStyleSheet(f"color:{_TEXT_DIM};font-size:13px;")
            self._scroll.setStyleSheet(
                f"QScrollArea{{background:{_BG};border:none;}}"
                f"QScrollBar:vertical{{width:6px;background:{_BG};border-radius:3px;}}"
                f"QScrollBar::handle:vertical{{background:{_BORDER_NORM};border-radius:3px;min-height:20px;}}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            )
            self._grid_host.setStyleSheet(f"background:{_BG};")
        if hasattr(self, "_action_bar"):
            self._action_bar.setStyleSheet(
                "QFrame#CacheActionBar{"
                "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 {_SURFACE_BAR},stop:1 {_SURFACE});"
                f"border-top:1px solid {_BORDER_NORM};}}"
            )
            self._sel_count_lbl.setStyleSheet(
                "background:transparent;border:none;"
                f"color:{_TEXT};font-size:12px;font-weight:600;"
            )
            self._sel_size_lbl.setStyleSheet(
                "background:transparent;border:none;"
                f"color:{_TEXT_FAINT};font-size:10px;"
            )
            self._sel_all_btn.setStyleSheet(
                f"QPushButton{{background:transparent;border:1px solid {_BORDER};"
                f"border-radius:6px;color:{_TEXT_MUTED};font-size:10px;padding:0 12px;}}"
                f"QPushButton:hover{{background:{_SURFACE_2};color:{_TEXT_BODY};border-color:{_TEXT_DIM};}}"
            )
            self._del_btn.setIcon(make_icon(_ICON_TRASH, 14, _DANGER_TEXT))
            self._del_btn.setStyleSheet(
                f"QPushButton{{background:{_DANGER_BG};border:1px solid {_DANGER_BORDER};"
                f"border-radius:6px;color:{_DANGER_TEXT};font-size:11px;"
                "font-weight:600;padding:0 16px;}"
                f"QPushButton:hover{{background:{_DANGER_HOVER};border-color:{_DANGER};color:{_DANGER};}}"
                f"QPushButton:pressed{{background:{_DANGER_BG};}}"
            )
            self._refresh_action_bar()
        for card in self._all_cards:
            card.apply_theme()
        self.update()

    # ── Scan ──────────────────────────────────────────────────────────────

    def refresh(self):
        self._cancel_scan()
        self._thumb_service.clear()

        # Destrói cards antigos com segurança
        for card in self._all_cards:
            card.setVisible(False)
            card.setParent(None)
            card.deleteLater()
        self._all_cards.clear()
        self._vis_cards.clear()
        self._selected.clear()
        self._type_counts.clear()

        # Esvazia o grid sem destruir (os widgets já foram desanexados acima)
        while self._grid_lay.count():
            self._grid_lay.takeAt(0)

        self._action_bar.setVisible(False)
        self._sel_all_btn.setVisible(False)
        self._size_badge.setVisible(False)
        self._stack.setCurrentIndex(0)

        session = self._cache_scan_session_factory.create(
            self._cache_manager.media_cache_dir,
            parent=self,
        )
        session.results_ready.connect(self._on_results)
        session.finished.connect(
            lambda current=session: self._clear_scan_session(current)
        )
        session.finished.connect(session.deleteLater)
        self._scan_session = session
        session.start()

    def _cancel_scan(self):
        session = self._scan_session
        self._scan_session = None
        if session is not None:
            session.cancel()

    def _clear_scan_session(self, session: CacheScanSession) -> None:
        if self._scan_session is session:
            self._scan_session = None

    def cleanup(self):
        """Para threads/serviços internos antes da janela ser destruída."""
        self._cancel_scan()
        self._thumb_service.clear()

    # ── Slots do worker ───────────────────────────────────────────────────

    def _on_results(self, items: list):
        if not items:
            self._update_chip_labels()
            self._stack.setCurrentIndex(1)
            return

        total_size = 0
        for item in items:
            # parent=self._grid_host para manter vivo enquanto o host existir
            card = MediaCard(item, self._lang, self._grid_host)
            card.selection_changed.connect(self._on_card_sel)
            card.play_requested.connect(self.play_media_requested)
            card.setVisible(False)  # _apply_filter cuida de mostrar
            self._all_cards.append(card)
            self._type_counts[item.media_type] = self._type_counts.get(item.media_type, 0) + 1
            total_size += item.size
            # Solicita thumbnail via serviço centralizado
            self._thumb_service.request(item.path, item.media_type)

        n = len(items)
        self._size_badge.setText(
            self.tr("{size} · {count} file(s)")
            .replace("{size}", str(_fmt_size(total_size)))
            .replace("{count}", str(n))
        )
        self._size_badge.setVisible(True)
        self._update_chip_labels()
        self._apply_filter(self._cur_filter)
        self._stack.setCurrentIndex(2)

    def _on_thumb_ready(self, path: str, pixmap: QPixmap, title: str = ""):
        """Recebe thumbnail e título do MediaInfoService e aplica ao card correspondente."""
        thumb = None
        if not pixmap.isNull():
            thumb = _rounded_pixmap(pixmap, _THUMB_W, _THUMB_H)
            
        for card in self._all_cards:
            if card.path == path:
                if thumb:
                    card.set_thumbnail(thumb)
                if title:
                    card.set_title(title)
                break

    # ── Filtros ───────────────────────────────────────────────────────────

    def _on_chip_toggled(self, button, checked: bool):
        if not checked:
            return
        if hasattr(button, "filter_key"):
            self._cur_filter = button.filter_key
            self._apply_filter(button.filter_key)

    def _apply_filter(self, key: str):
        """
        Mostra/oculta cards SEM destruí-los.
        Remove do grid e re-adiciona apenas os visíveis — sem setParent(None).
        """
        self._vis_cards = [
            c for c in self._all_cards
            if key == "all" or c.media_type == key
        ]

        # Oculta todos sem desanexar
        for c in self._all_cards:
            c.setVisible(False)

        # Remove itens do grid (takeAt não destrói o widget quando não chama setParent)
        while self._grid_lay.count():
            self._grid_lay.takeAt(0)

        if not self._vis_cards:
            self._stack.setCurrentIndex(1)
            return

        self._stack.setCurrentIndex(2)
        self._relayout_grid()

    def _relayout_grid(self):
        """Redistribui _vis_cards no QGridLayout com colunas calculadas pela largura."""
        vp_w = self._scroll.viewport().width()
        avail = (vp_w if vp_w > 0 else self._scroll.width() - 20) or 600
        cols = max(1, (avail - 32 + _GRID_SPACING) // (_CARD_W + _GRID_SPACING))

        for idx, card in enumerate(self._vis_cards):
            self._grid_lay.addWidget(card, idx // cols, idx % cols)
            card.setVisible(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._vis_cards:
            QTimer.singleShot(0, self._relayout_grid)

    def _update_chip_labels(self):
        v = self._type_counts.get("video", 0)
        a = self._type_counts.get("audio", 0)
        i = self._type_counts.get("image", 0)
        total = v + a + i + self._type_counts.get("other", 0)

        self._chip_all.setText(f'{self.tr("All")}  {total}')
        self._chip_video.setVisible(v > 0)
        self._chip_audio.setVisible(a > 0)
        self._chip_image.setVisible(i > 0)
        if v: self._chip_video.setText(f'{self.tr("Videos")}  {v}')
        if a: self._chip_audio.setText(f'{self.tr("Audio")}  {a}')
        if i: self._chip_image.setText(f'{self.tr("Images")}  {i}')

    # ── Seleção ───────────────────────────────────────────────────────────

    def _on_card_sel(self, path: str, selected: bool):
        if selected:
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self._refresh_action_bar()

    def _refresh_action_bar(self):
        n = len(self._selected)
        self._action_bar.setVisible(n > 0)
        self._sel_all_btn.setVisible(n > 0)
        if n == 0:
            return

        all_sel = bool(self._vis_cards) and all(c.is_selected() for c in self._vis_cards)
        if all_sel:
            self._sel_all_btn.setText(self.tr("Deselect all"))
            self._sel_all_btn.setIcon(make_icon(_ICON_DESELECT, 14, _TEXT_MUTED))
        else:
            self._sel_all_btn.setText(self.tr("Select all"))
            self._sel_all_btn.setIcon(make_icon(_ICON_SELECT_ALL, 14, _TEXT_MUTED))

        total_sz = sum(c.file_size for c in self._all_cards if c.path in self._selected)
        one  = n == 1
        word = self.tr("file selected") if one else self.tr("files selected")
        self._sel_count_lbl.setText(f"{n} {word}")
        self._sel_size_lbl.setText(_fmt_size(total_sz))
        self._del_btn.setText("  " + self.tr("Delete {n}").replace("{n}", str(n)))

    def _toggle_select_all(self):
        all_sel = bool(self._vis_cards) and all(c.is_selected() for c in self._vis_cards)
        if all_sel:
            self._deselect_all()
        else:
            for c in self._vis_cards:
                c.set_selected(True)
                self._selected.add(c.path)
            self._refresh_action_bar()

    def _deselect_all(self):
        for c in self._all_cards:
            c.set_selected(False)
        self._selected.clear()
        self._refresh_action_bar()

    # ── Delete ────────────────────────────────────────────────────────────

    def _confirm_delete(self):
        n = len(self._selected)
        if n == 0:
            return
        word = self.tr("file") if n == 1 else self.tr("files")
        dlg = QMessageBox(self)
        dlg.setWindowTitle(self.tr("Confirm deletion"))
        dlg.setText(self.tr("Delete {n} {word} from this computer?").replace("{n}", str(n)).replace("{word}", str(word)))
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dlg.setIcon(QMessageBox.Icon.Warning)
        dlg.setStyleSheet(
            f"QMessageBox{{background:{_SURFACE};}}"
            f"QLabel{{color:{_TEXT};background:transparent;}}"
            f"QPushButton{{background:{_SURFACE_2};border:1px solid {_BORDER};border-radius:6px;"
            f"color:{_TEXT_BODY};padding:5px 16px;min-width:60px;}}"
            f"QPushButton:hover{{background:{_SURFACE_3};}}"
        )
        if dlg.exec() != QMessageBox.StandardButton.Yes:
            return
        self._do_delete()

    def _do_delete(self):
        deleted, failed = [], []
        for path in list(self._selected):
            try:
                self._cache_manager.remove_cached_file(path)
                deleted.append(path)
            except OSError:
                failed.append(path)

        deleted_set = set(deleted)

        # Destrói cards excluídos de forma segura
        survivors = []
        for card in self._all_cards:
            if card.path in deleted_set:
                # Remove do grid antes de desanexar
                idx = self._grid_lay.indexOf(card)
                if idx >= 0:
                    self._grid_lay.takeAt(idx)
                card.setVisible(False)
                card.setParent(None)
                card.deleteLater()
            else:
                survivors.append(card)
        self._all_cards = survivors

        self._type_counts.clear()
        for c in self._all_cards:
            self._type_counts[c.media_type] = self._type_counts.get(c.media_type, 0) + 1

        self._selected.clear()

        total_sz = sum(c.file_size for c in self._all_cards)
        total_n  = len(self._all_cards)
        if total_n > 0:
            self._size_badge.setText(
                self.tr("{size} · {count} file(s)")
                .replace("{size}", str(_fmt_size(total_sz)))
                .replace("{count}", str(total_n))
            )
        else:
            self._size_badge.setVisible(False)

        self._update_chip_labels()
        self._apply_filter(self._cur_filter)
        self._refresh_action_bar()

        if failed:
            # Usa o título exibido no card em vez do nome de arquivo bruto
            path_to_title = {c.path: c.display_title for c in self._all_cards}
            names = "\n".join(
                f"  • {path_to_title.get(p, Path(p).name)}" for p in failed
            )
            QMessageBox.warning(
                self,
                self.tr("Delete error"),
                f'{self.tr("Could not delete the following files:")}\n\n{names}',
            )

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._title_lbl.setText(self.tr("Media Manager"))
        self._loading_lbl.setText(self.tr("Loading cached media…"))
        self._empty_lbl.setText(self.tr("No cached media found."))
        self._update_chip_labels()
        self._refresh_action_bar()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(80, self.refresh)

    def hideEvent(self, event):
        self._cancel_scan()
        self._thumb_service.clear()
        super().hideEvent(event)
