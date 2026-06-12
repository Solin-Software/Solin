"""
CacheMediaWidget — gerenciador de mídias baixadas via Cache ON.

Princípios de design:
  1. LAZY: scan e thumbs SÓ começam quando activate() é chamado
     (primeira expansão da seção). O app não faz nada ao iniciar.
  2. Extração de thumbnails centralizada via MediaThumbService:
       - Áudio: bytes brutos (ID3v2/MP4/FLAC/OGG) sem player → fallback QMediaMetaData
       - Vídeo: QMediaMetaData (CoverArtImage) → fallback frame no seek 5%
       - Imagem: QPixmap direto, sem player
  3. Scan do diretório em thread separada — não bloqueia a UI.
  4. Threads com cleanup explícito — sem "destroyed while running".
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QGridLayout, QDialog,
)
from PySide6.QtCore import (
    Qt, Signal, QSize, QThread, QObject, QTimer, QEvent,
)
from PySide6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QFontMetrics,
)

from ..core.foundation import paths as _paths
from ..core.foundation.exception_logging import log_ignored_exception
from ..core.media.cache import MediaCacheManager
from ..core.foundation.constants import (
    VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS,
)
from ..core.i18n.manager import LanguageManager
from ..styles.icons import (
    make_icon,
    ICON_TRASH, ICON_VIDEO, ICON_MUSIC, ICON_IMAGE,
    ICON_CLOUD_DONE,
)
from .media_info_extractor import MediaInfoService


# ── Constantes visuais ─────────────────────────────────────────────────────────

_CARD_W   = 168
_CARD_H   = 148
_THUMB_W  = 168
_THUMB_H  = 94
_GRID_GAP = 12


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def _crop_center(px: QPixmap, w: int, h: int) -> QPixmap:
    if px.isNull():
        return px
    sw, sh = px.width(), px.height()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    return px.copy(x, y, min(w, sw), min(h, sh))


def _scale_to_thumb(px: QPixmap) -> QPixmap:
    """Escala e corta centralizado para _THUMB_W × _THUMB_H."""
    scaled = px.scaled(
        _THUMB_W, _THUMB_H,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    return _crop_center(scaled, _THUMB_W, _THUMB_H)


# ── Worker de scan ─────────────────────────────────────────────────────────────

class _CacheScanWorker(QObject):
    """Escaneia _paths.MEDIA_CACHE_DIR — não carrega thumbs, apenas lista arquivos."""
    finished = Signal(list)

    def run(self):
        result: list[dict] = []
        cache_dir = _paths.MEDIA_CACHE_DIR
        if not os.path.isdir(cache_dir):
            self.finished.emit(result)
            return
        for fname in sorted(os.listdir(cache_dir)):
            if QThread.currentThread().isInterruptionRequested():
                return
            # Ignora markers e temporários
            if fname.endswith(".done") or fname.endswith(".tmp"):
                continue
            fpath = os.path.join(cache_dir, fname)
            if not os.path.isfile(fpath):
                continue
            # Ignora downloads incompletos (.tmp existe mas não .done)
            # Aceita arquivo se: tem .done OU simplesmente é um arquivo de mídia completo
            # (downloads mais antigos podem não ter marker .done)
            tmp = fpath + ".tmp"
            if os.path.isfile(tmp):
                continue  # ainda baixando
            ext = Path(fname).suffix.lower()
            if ext in VIDEO_EXTS:
                mtype = "video"
            elif ext in AUDIO_EXTS:
                mtype = "audio"
            elif ext in IMAGE_EXTS:
                mtype = "image"
            else:
                continue
            result.append({
                "path":  fpath,
                "name":  fname,
                "ext":   ext,
                "type":  mtype,
                "size":  os.path.getsize(fpath),
            })
        self.finished.emit(result)



# ── Pill de filtro ─────────────────────────────────────────────────────────────

class _FilterPill(QPushButton):
    def __init__(self, label: str, count: int, parent=None):
        super().__init__(parent)
        self._base_label = label
        self._count      = count
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        self._refresh_text()
        self._apply_style()
        self.toggled.connect(lambda _: self._apply_style())

    def _refresh_text(self):
        self.setText(f"{self._base_label}  {self._count}")

    def update_label(self, label: str):
        self._base_label = label
        self._refresh_text()

    def update_count(self, count: int):
        self._count = count
        self._refresh_text()

    def _apply_style(self):
        if self.isChecked():
            self.setStyleSheet(
                "QPushButton{background:#1f3a5f;border:1px solid #388bfd;"
                "border-radius:15px;color:#388bfd;"
                "font-size:12px;font-weight:600;padding:0 14px;}"
            )
        else:
            self.setStyleSheet(
                "QPushButton{background:#21262d;border:1px solid #30363d;"
                "border-radius:15px;color:#8b949e;"
                "font-size:12px;padding:0 14px;}"
                "QPushButton:hover{border-color:#484f58;color:#c9d1d9;}"
            )


# ── Card individual ────────────────────────────────────────────────────────────

class _MediaCard(QFrame):
    play_requested    = Signal(str)
    selection_changed = Signal()

    _SEL_BORDER   = "#388bfd"
    _UNSEL_BORDER = "#21262d"
    _HOVER_BORDER = "#30363d"
    _SEL_OVERLAY  = QColor(56, 139, 253, 40)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self._info     = info
        self._selected = False
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setObjectName("CacheMediaCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._apply_border()
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._thumb_lbl = QLabel()
        self._thumb_lbl.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setStyleSheet("background:#161b22;border:none;border-radius:0;")
        self._set_placeholder()
        lay.addWidget(self._thumb_lbl)

        info_w = QWidget()
        info_w.setFixedHeight(_CARD_H - _THUMB_H)
        info_w.setStyleSheet("background:transparent;")
        il = QVBoxLayout(info_w)
        il.setContentsMargins(8, 5, 8, 5)
        il.setSpacing(1)

        name = self._info["name"]
        self._name_lbl = QLabel()
        self._name_lbl.setStyleSheet(
            "color:#e6edf3;font-size:11px;font-weight:600;background:transparent;"
        )
        fm = QFontMetrics(self._name_lbl.font())
        self._name_lbl.setText(fm.elidedText(name, Qt.TextElideMode.ElideMiddle, _CARD_W - 20))
        self._name_lbl.setToolTip(name)

        self._size_lbl = QLabel(_format_size(self._info["size"]))
        self._size_lbl.setStyleSheet("color:#8b949e;font-size:10px;background:transparent;")

        il.addWidget(self._name_lbl)
        il.addWidget(self._size_lbl)
        il.addStretch()
        lay.addWidget(info_w)

    def _set_placeholder(self):
        svg_map = {"video": ICON_VIDEO, "audio": ICON_MUSIC, "image": ICON_IMAGE}
        icon_px = make_icon(svg_map.get(self._info["type"], ICON_VIDEO), 32, "#484f58").pixmap(32, 32)
        bg = QPixmap(_THUMB_W, _THUMB_H)
        bg.fill(QColor("#161b22"))
        p = QPainter(bg)
        p.drawPixmap((_THUMB_W - 32) // 2, (_THUMB_H - 32) // 2, icon_px)
        p.end()
        self._thumb_lbl.setPixmap(bg)

    def set_thumb(self, px: QPixmap):
        if px and not px.isNull():
            self._thumb_lbl.setPixmap(_scale_to_thumb(px))
            
    def set_title(self, title: str):
        if not title:
            return
        fm = QFontMetrics(self._name_lbl.font())
        elided = fm.elidedText(title, Qt.TextElideMode.ElideMiddle, _CARD_W - 20)
        self._name_lbl.setText(elided)
        self._name_lbl.setToolTip(title)

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, value: bool):
        if self._selected == value:
            return
        self._selected = value
        self._apply_border()
        self.update()
        self.selection_changed.emit()

    def toggle_selected(self):
        self.set_selected(not self._selected)

    def _apply_border(self):
        c = self._SEL_BORDER if self._selected else self._UNSEL_BORDER
        self.setStyleSheet(
            f"#CacheMediaCard{{background:#161b22;border:2px solid {c};border-radius:10px;}}"
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._selected:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(self._SEL_OVERLAY)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 9, 9)
            p.setBrush(QColor("#388bfd"))
            p.drawEllipse(self.width() - 26, 8, 18, 18)
            p.setPen(QPen(QColor("white"), 2,
                         Qt.PenStyle.SolidLine,
                         Qt.PenCapStyle.RoundCap,
                         Qt.PenJoinStyle.RoundJoin))
            p.drawLine(self.width() - 21, 17, self.width() - 18, 20)
            p.drawLine(self.width() - 18, 20, self.width() - 12, 14)
            p.end()

    def enterEvent(self, event):
        if not self._selected:
            self.setStyleSheet(
                f"#CacheMediaCard{{background:#1c2128;border:2px solid "
                f"{self._HOVER_BORDER};border-radius:10px;}}"
            )

    def leaveEvent(self, event):
        self._apply_border()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_selected()
        elif event.button() == Qt.MouseButton.RightButton:
            self.play_requested.emit(self._info["path"])

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._info["path"])


# ── Widget principal ────────────────────────────────────────────────────────────

class CacheMediaWidget(QWidget):
    """
    Gerenciador de mídias em cache.

    IMPORTANTE: scan e thumbs NÃO começam no __init__.
    Chamar activate() na primeira expansão para iniciar o carregamento.
    """
    play_requested = Signal(str)

    def __init__(self, lang: LanguageManager, parent=None):
        super().__init__(parent)
        self._lang      = lang
        self._all_items: list[dict]       = []
        self._cards:     list[_MediaCard] = []
        self._pending_render_items: list[dict] = []
        self._filter    = "all"
        self._activated = False
        self._scanning  = False
        self._scan_thread = None
        self._scan_worker = None
        self._thumb_service = MediaInfoService(self)
        self._thumb_service.info_ready.connect(self._on_thumb_ready)
        self._build_ui()

    def activate(self):
        """Chamado na primeira expansão — inicia o scan."""
        if not self._activated:
            self._activated = True
            self._scan()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self._size_lbl = QLabel("")
        self._size_lbl.setObjectName("SectionSubtitle")
        hdr.addWidget(self._size_lbl)
        hdr.addStretch()
        self._refresh_btn = QPushButton()
        self._refresh_btn.setIcon(make_icon(ICON_CLOUD_DONE, 14, "#8b949e"))
        self._refresh_btn.setIconSize(QSize(14, 14))
        self._refresh_btn.setFixedSize(28, 28)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setToolTip(self.tr("Refresh"))
        self._refresh_btn.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;border-radius:6px;}"
            "QPushButton:hover{background:#2d333b;border-color:#484f58;}"
        )
        self._refresh_btn.clicked.connect(self._scan)
        hdr.addWidget(self._refresh_btn)
        root.addLayout(hdr)

        # Filter pills
        pr = QHBoxLayout()
        pr.setSpacing(6)
        self._pill_all   = _FilterPill(self.tr("All"),   0)
        self._pill_video = _FilterPill(self.tr("Videos"), 0)
        self._pill_audio = _FilterPill(self.tr("Audio"), 0)
        self._pill_image = _FilterPill(self.tr("Images"), 0)
        self._pill_all.setChecked(True)
        for pill, ftype in (
            (self._pill_all,   "all"),
            (self._pill_video, "video"),
            (self._pill_audio, "audio"),
            (self._pill_image, "image"),
        ):
            pill.toggled.connect(
                lambda checked, t=ftype, p=pill: self._on_pill(checked, t, p)
            )
        pr.addWidget(self._pill_all)
        pr.addWidget(self._pill_video)
        pr.addWidget(self._pill_audio)
        pr.addWidget(self._pill_image)
        pr.addStretch()
        root.addLayout(pr)

        # Selection bar
        self._sel_bar = QFrame()
        self._sel_bar.setStyleSheet(
            "QFrame{background:#1f3a5f;border:1px solid #388bfd;border-radius:8px;}"
        )
        self._sel_bar.setFixedHeight(44)
        sl = QHBoxLayout(self._sel_bar)
        sl.setContentsMargins(12, 0, 12, 0)
        sl.setSpacing(8)
        self._sel_count_lbl = QLabel("")
        self._sel_count_lbl.setStyleSheet(
            "color:#388bfd;font-size:12px;font-weight:600;background:transparent;"
        )
        sl.addWidget(self._sel_count_lbl)
        sl.addStretch()
        for attr, key, style, slot in (
            ("_sel_all_btn", self.tr("Select all"),
             "QPushButton{background:transparent;border:1px solid #388bfd;"
             "border-radius:6px;color:#388bfd;font-size:11px;padding:4px 10px;}"
             "QPushButton:hover{background:#233d6b;}",
             self._select_all_visible),
            ("_desel_btn", self.tr("Deselect all"),
             "QPushButton{background:transparent;border:1px solid #30363d;"
             "border-radius:6px;color:#8b949e;font-size:11px;padding:4px 10px;}"
             "QPushButton:hover{background:#21262d;color:#c9d1d9;}",
             self._deselect_all),
        ):
            btn = QPushButton(key)  # key is already tr()-translated string from tuple
            btn.setStyleSheet(style)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            sl.addWidget(btn)
            setattr(self, attr, btn)
        self._del_btn = QPushButton(self.tr("Delete selected"))
        self._del_btn.setIcon(make_icon(ICON_TRASH, 13, "#f85149"))
        self._del_btn.setIconSize(QSize(13, 13))
        self._del_btn.setStyleSheet(
            "QPushButton{background:#3d1a1a;border:1px solid #f85149;"
            "border-radius:6px;color:#f85149;font-size:11px;padding:4px 12px;}"
            "QPushButton:hover{background:#5a2020;}"
        )
        self._del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_btn.clicked.connect(self._delete_selected)
        sl.addWidget(self._del_btn)
        self._sel_bar.setVisible(False)
        root.addWidget(self._sel_bar)

        # Scroll + grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(200)
        self._scroll.setMaximumHeight(520)
        self._grid_container = QWidget()
        self._grid_container.setStyleSheet("background:transparent;")
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(_GRID_GAP)
        self._grid_layout.setContentsMargins(0, 4, 0, 4)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_container)
        self._scroll.setVisible(False)
        root.addWidget(self._scroll)

        self._empty_lbl = QLabel(self.tr("No cached media found."))
        self._empty_lbl.setObjectName("SectionSubtitle")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setVisible(False)
        root.addWidget(self._empty_lbl)

        self._loading_lbl = QLabel(self.tr("Loading cached media…"))
        self._loading_lbl.setObjectName("LoadingLabel")
        self._loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_lbl.setVisible(True)
        root.addWidget(self._loading_lbl)

    # ── Scan ──────────────────────────────────────────────────────────────

    def _scan(self):
        if self._scanning:
            return
        self._scanning = True
        self._loading_lbl.setVisible(True)
        self._empty_lbl.setVisible(False)
        self._scroll.setVisible(False)
        self._sel_bar.setVisible(False)

        # Atributos de instância — evita GC destruir worker antes de terminar
        self._scan_thread = QThread(self)
        self._scan_worker = _CacheScanWorker()
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.start()

    def _on_scan_done(self, items: list[dict]):
        self._scanning  = False
        self._all_items = items
        self._loading_lbl.setVisible(False)
        self._update_pills()
        self._render_grid()
        self._update_size_label()

    # ── Pills ─────────────────────────────────────────────────────────────

    def _update_pills(self):
        counts = {"video": 0, "audio": 0, "image": 0}
        for it in self._all_items:
            counts[it["type"]] = counts.get(it["type"], 0) + 1
        self._pill_all.update_count(len(self._all_items))
        self._pill_video.update_count(counts["video"])
        self._pill_audio.update_count(counts["audio"])
        self._pill_image.update_count(counts["image"])
        self._pill_video.setVisible(counts["video"] > 0)
        self._pill_audio.setVisible(counts["audio"] > 0)
        self._pill_image.setVisible(counts["image"] > 0)
        if self._filter != "all" and counts.get(self._filter, 0) == 0:
            self._pill_all.blockSignals(True)
            self._pill_all.setChecked(True)
            self._pill_all.blockSignals(False)
            self._pill_all._apply_style()
            self._filter = "all"

    def _on_pill(self, checked: bool, ftype: str, pill: _FilterPill):
        if not checked:
            return
        for p in (self._pill_all, self._pill_video, self._pill_audio, self._pill_image):
            if p is not pill and p.isChecked():
                p.blockSignals(True)
                p.setChecked(False)
                p.blockSignals(False)
                p._apply_style()
        self._filter = ftype
        self._render_grid()

    def _update_size_label(self):
        total = sum(it["size"] for it in self._all_items)
        count = len(self._all_items)
        self._size_lbl.setText(
            self.tr("{count} file(s)  ·  {size}").replace("{count}", str(count)).replace("{size}", str(_format_size(total)))
            if count else ""
        )

    # ── Grid ──────────────────────────────────────────────────────────────

    def _visible_items(self) -> list[dict]:
        if self._filter == "all":
            return self._all_items
        return [it for it in self._all_items if it["type"] == self._filter]

    def _render_grid(self):
        # Limpa cards existentes
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        # Limpa grid layout
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item and item.widget():
                item.widget().setParent(None)

        self._pending_render_items = self._visible_items()
        if not self._pending_render_items:
            self._scroll.setVisible(False)
            self._empty_lbl.setVisible(True)
            self._sel_bar.setVisible(False)
            return

        self._empty_lbl.setVisible(False)
        self._scroll.setVisible(True)

        # Inicia renderização em chunks
        self._render_next_chunk()

    def _render_next_chunk(self):
        if not self._pending_render_items:
            self._on_selection_changed()
            return

        cols = max(2, min(5, (max(self.width(), 360) - 20) // (_CARD_W + _GRID_GAP)))
        chunk_size = 24
        chunk = self._pending_render_items[:chunk_size]
        self._pending_render_items = self._pending_render_items[chunk_size:]

        start_idx = len(self._cards)

        for i, info in enumerate(chunk):
            idx = start_idx + i
            card = _MediaCard(info)
            card.play_requested.connect(self.play_requested.emit)
            card.selection_changed.connect(self._on_selection_changed)
            self._grid_layout.addWidget(card, *divmod(idx, cols))
            self._cards.append(card)

            if info["type"] == "image":
                # Imagens: carregamento direto sem serviço
                QTimer.singleShot(
                    0, lambda c=card, path=info["path"]: c.set_thumb(QPixmap(path))
                )
            else:
                # Áudio e vídeo: MediaThumbService (cover art → frame fallback)
                self._thumb_service.request(info["path"], info["type"])

        if self._pending_render_items:
            # Agenda o próximo lote deixando o UI Thread respirar
            QTimer.singleShot(0, self._render_next_chunk)
        else:
            self._on_selection_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cards:
            QTimer.singleShot(0, self._render_grid)

    # ── Thumb loading ─────────────────────────────────────────────────────

    def _on_thumb_ready(self, path: str, pixmap: QPixmap, _title: str = ""):
        """Recebe thumbnail do MediaInfoService e aplica ao card correspondente."""
        for card in self._cards:
            if card._info["path"] == path:
                card.set_thumb(pixmap)
                if _title:
                    card.set_title(_title)
                break

    # ── Selection ─────────────────────────────────────────────────────────

    def _selected_cards(self) -> list[_MediaCard]:
        return [c for c in self._cards if c.is_selected]

    def _on_selection_changed(self):
        sel = self._selected_cards()
        n   = len(sel)
        self._sel_bar.setVisible(n > 0)
        if n > 0:
            self._sel_count_lbl.setText(
                self.tr("{count} selected").replace("{count}", str(n))
            )

    def _select_all_visible(self):
        for c in self._cards:
            c.set_selected(True)

    def _deselect_all(self):
        for c in self._cards:
            c.set_selected(False)

    # ── Delete ────────────────────────────────────────────────────────────

    def _delete_selected(self):
        sel = self._selected_cards()
        if not sel:
            return
        paths = [c._info["path"] for c in sel]
        if _ConfirmDeleteDialog(len(paths), self._lang, self).exec() != QDialog.DialogCode.Accepted:
            return
        for path in paths:
            try:
                MediaCacheManager.instance().remove_cached_file(path)
            except OSError:
                log_ignored_exception(__name__, "Could not remove cached media file")
        self._scan()

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._refresh_btn.setToolTip(self.tr("Refresh"))
        self._pill_all.update_label(self.tr("All"))
        self._pill_video.update_label(self.tr("Videos"))
        self._pill_audio.update_label(self.tr("Audio"))
        self._pill_image.update_label(self.tr("Images"))
        self._sel_all_btn.setText(self.tr("Select all"))
        self._desel_btn.setText(self.tr("Deselect all"))
        self._del_btn.setText(self.tr("Delete selected"))
        self._empty_lbl.setText(self.tr("No cached media found."))
        self._loading_lbl.setText(self.tr("Loading cached media…"))
        self._update_size_label()

    def refresh_language(self) -> None:
        """Alias de compatibilidade → retranslateUi()."""
        self.retranslateUi()

    def cleanup(self):
        """Chamar ao fechar o app."""
        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            thread.wait(2_000)
            if thread.isRunning():
                thread.setParent(None)
                thread.finished.connect(thread.deleteLater)
        self._scan_thread = None
        self._scan_worker = None
        self._thumb_service.clear()


# ── Confirm Delete Dialog ───────────────────────────────────────────────────────

class _ConfirmDeleteDialog(QDialog):
    def __init__(self, count: int, lang: LanguageManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Confirm deletion"))
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet(
            "QDialog{background:#161b22;border:1px solid #30363d;border-radius:12px;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(16)

        tr = QHBoxLayout()
        il = QLabel()
        il.setPixmap(make_icon(ICON_TRASH, 20, "#f85149").pixmap(20, 20))
        il.setFixedSize(26, 26)
        il.setAlignment(Qt.AlignmentFlag.AlignCenter)
        il.setStyleSheet("background:transparent;")
        tr.addWidget(il)
        tl = QLabel(self.tr("Confirm deletion"))
        tl.setStyleSheet(
            "font-size:15px;font-weight:700;color:#e6edf3;background:transparent;"
        )
        tr.addWidget(tl)
        tr.addStretch()
        lay.addLayout(tr)

        ml = QLabel(self.tr("Delete {count} file(s) from this computer?").replace("{count}", str(count)))
        ml.setWordWrap(True)
        ml.setStyleSheet("color:#8b949e;font-size:13px;background:transparent;")
        lay.addWidget(ml)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:#30363d;max-height:1px;border:none;")
        lay.addWidget(sep)

        br = QHBoxLayout()
        br.addStretch()
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setMinimumHeight(38)
        cancel.setMinimumWidth(100)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            "QPushButton{background:#21262d;border:1px solid #30363d;"
            "border-radius:8px;color:#c9d1d9;font-size:13px;padding:0 16px;}"
            "QPushButton:hover{background:#2d333b;border-color:#484f58;}"
        )
        cancel.clicked.connect(self.reject)
        confirm = QPushButton(self.tr("Delete"))
        confirm.setMinimumHeight(38)
        confirm.setMinimumWidth(120)
        confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm.setStyleSheet(
            "QPushButton{background:#3d1a1a;border:1px solid #f85149;"
            "border-radius:8px;color:#f85149;font-size:13px;font-weight:600;"
            "padding:0 16px;}"
            "QPushButton:hover{background:#5a2020;}"
        )
        confirm.clicked.connect(self.accept)
        br.addWidget(cancel)
        br.addWidget(confirm)
        lay.addLayout(br)
