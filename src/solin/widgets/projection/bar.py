"""Projection bar and related preview controls."""

from __future__ import annotations

import os
import random as _random
from collections.abc import Callable

from PySide6.QtCore import (
    QDateTime,
    QEvent,
    QObject,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QImage,
    QPixmap,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from solin.core.foundation.constants import ORDER_OFF, ORDER_NEXT, ORDER_RANDOM
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.time_utils import ceil_remaining_seconds
from solin.core.media.playback import MediaController
from solin.core.media.settings import ProjectionPlaybackSettingsStore
from solin.styles.icons import (
    ICON_ADD_TO_PLAYLIST,
    ICON_CAST,
    ICON_CHEVRON_DOWN,
    ICON_CLOSE,
    ICON_IMAGE,
    ICON_MORE_VERT,
    ICON_MUSIC,
    ICON_NAV_TIMER,
    ICON_OBS,
    ICON_PANEL_RIGHT,
    ICON_PAUSE,
    ICON_PLAY,
    ICON_SCREEN,
    ICON_SEND_TO_PLAYLIST,
    ICON_SET_AS_IDLE,
    ICON_SKIP_NEXT,
    ICON_SKIP_PREV,
    ICON_VIDEO,
    ICON_VOLUME_HIGH,
    ICON_VOLUME_LOW,
    ICON_VOLUME_MUTE,
    make_icon,
)
from solin.widgets.circular_timer import CircularTimerWidget
from solin.ui.media_info import MediaInfoQueue
from solin.widgets.playlist.panel import PlaylistPanel
from .audio import ProjectionAudioMixin
from .playlist import ProjectionPlaylistMixin
from .preview import ImagePreviewWidget
from solin.widgets.songs_widget import BufferedSlider


# Keep ProjectionBar decoupled from PlaylistPanel internals while preserving timing.
_ANIM_MS = 220

def _icon_btn(svg: str, size: int = 30, icon_px: int = 15,
              color: str = "#c9d1d9", tooltip: str = "") -> QPushButton:
    btn = QPushButton()
    btn.setFixedSize(size, size)
    btn.setIcon(make_icon(svg, icon_px, color))
    btn.setIconSize(QSize(icon_px, icon_px))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    r = size // 2
    btn.setStyleSheet(
        f"QPushButton{{border:none;border-radius:{r}px;"
        "background:transparent;padding:0;}"
        f"QPushButton:hover{{background:rgba(255,255,255,0.08);border-radius:{r}px;}}"
        "QPushButton:pressed{background:rgba(255,255,255,0.13);}"
    )
    return btn


_MENU_STYLE = """
QMenu {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 6px 4px;
    color: #c9d1d9;
    font-size: 12px;
}
QMenu::item { padding: 6px 20px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: #21262d; color: #e6edf3; }
QMenu::item:checked  { color: #388bfd; font-weight: 600; }
QMenu::separator     { height: 1px; background: #30363d; margin: 4px 8px; }
QMenu::indicator     { width: 0; }
"""

# Projection bar (bottom-right projection control)

class ProjectionBar(ProjectionAudioMixin, ProjectionPlaylistMixin, QFrame):
    """
    Barra inferior de projeção (48 px) com overlay expansível.

    Novidades:
      - Ícones SVG (sem emojis)
      - Suporte a playlist com modos: desligado / próximo / aleatório
      - Menu de opções de vídeo (velocidade, loop, ordem)
      - Persisted playback preferences through a typed settings store
    """
    stop_requested      = Signal()
    seek_requested      = Signal(int)
    toggle_requested    = Signal()
    volume_changed      = Signal(float)
    timer_updated       = Signal(int, int)
    timer_blink         = Signal(bool)
    play_next_requested = Signal(str, str, str)  # url, title, media_type — avanço automático
    playlist_navigate   = Signal(int)        # índice absoluto — navegação manual prev/next
    add_to_playlist_requested = Signal(str, str, object)  # url, title, jw_metadata_dict
    send_to_temp_playlist_requested = Signal(list)  # lista de itens da playlist atual
    monitor_manager_requested = Signal(object)   # QWidget (the button) for popup positioning
    obs_scene_toggle_requested = Signal()    # usuário quer alternar entre cena de mídia e cena anterior
    set_as_idle_requested      = Signal(str) # path — usuário quer definir mídia como idle screen
    expanded_changed           = Signal(bool)

    _BAR_H = 48

    def __init__(
        self,
        media_ctrl: MediaController,
        *,
        playback_settings: ProjectionPlaybackSettingsStore,
        profile_paths: ProfilePaths,
        media_cache_dir: str | os.PathLike[str],
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        lang_manager=None,
        container: QWidget = None,
        parent=None,
    ):
        super().__init__(parent)
        self.media      = media_ctrl
        self._playback_settings = playback_settings
        self._profile_paths = profile_paths
        self._media_cache_dir = media_cache_dir
        self.lang       = lang_manager
        self._container = container
        self._expanded  = False
        self._mode      = None  # 'video' | 'image' | 'timer' | None
        self._image_pixmap: QPixmap | None = None
        self._image_file_path: str = ""   # caminho do arquivo salvo para imagens sem URL
        self._is_audio: bool = False
        self._audio_cover_pixmap: QPixmap | None = None
        self._is_live_tab: bool = False   # True quando projetando aba ao vivo do browser

        # ── Timer state ──────────────────────────────────────────────────
        self._timer_target: QDateTime | None = None
        self._timer_total_secs: int = 0
        self._timer_tick = QTimer(self)
        # Sub-second cadence so the displayed countdown always reflects the
        # current second within ~200 ms of its boundary — a 1 s timer drifts
        # against the wall clock and would occasionally freeze or skip a second.
        self._timer_tick.setInterval(200)
        self._timer_tick.timeout.connect(self._on_timer_tick)
        self._timer_blink_timer = QTimer(self)
        self._timer_blink_timer.setInterval(400)
        self._timer_blink_timer.timeout.connect(self._on_blink_tick)
        self._blink_count = 0
        self._blink_on = False

        # ── Waveform animation ────────────────────────────────────────────
        self._wave_timer = QTimer(self)
        self._wave_timer.setInterval(50)  # 20 fps
        self._wave_timer.timeout.connect(self._on_wave_tick)
        # Estado independente por barra: altura atual, alvo, velocidade de lerp
        _B = [22, 50, 76, 96, 76, 50, 22]   # alturas de repouso
        self._wave_cur   = [float(h) for h in _B]
        self._wave_target= [float(h) for h in _B]
        self._wave_speed = [0.07, 0.09, 0.11, 0.08, 0.10, 0.07, 0.09]  # lerp por barra

        # ── Playlist state ───────────────────────────────────────────────
        self._playlist: list[dict] = []         # [{"url":..., "title":...}]
        self._playlist_index: int = 0
        self._played_indices: set = set()        # para ordem aleatória sem loop
        self._screen_count: int = 0              # armazenado para refresh de idioma
        self._is_from_saved_playlist: bool = False  # True quando reproduzindo de playlist salva

        # ── Persisted playback preferences ───────────────────────────────
        self._loop: bool = self._playback_settings.loop_enabled()
        self._playback_order: str = self._playback_settings.playback_order()
        self._speed: float = self._playback_settings.speed()
        self._volume: float = self._playback_settings.volume()

        # ── Song Announcement Mode state machine ─────────────────────────
        # States: "off" | "gate" | "ready"
        #   off   → normal video playback
        #   gate  → first 3.5s of media time: playing muted, controls locked
        #   ready → paused unmuted, play enabled, slider locked
        self._announce_state: str = "off"
        self._announce_gate_ms: int = 3500
        self._announce_timer = QTimer(self)
        self._announce_timer.setInterval(40)
        self._announce_timer.timeout.connect(self._on_announce_gate_expired)

        # ── Thumbnail queue para o painel de playlist ─────────────────────
        self._thumb_queue = media_info_queue_factory(self)
        self._panel_populated = False
        # Timer one-shot: captura thumbnail da mídia atual ao vivo (uma vez por faixa)
        self._live_thumb_captured: bool = False
        self._live_thumb_timer = QTimer(self)
        self._live_thumb_timer.setSingleShot(True)
        self._live_thumb_timer.timeout.connect(self._do_live_thumb_capture)

        # ── OBS scene toggle state (modo imagem) ──────────────────────────
        # True  → OBS está (ou deveria estar) na cena de mídia
        # False → OBS está (ou deveria estar) na cena anterior/idle
        self._obs_scene_is_media: bool = True
        # Habilitado somente quando OBS conectado + media_window_scene configurada
        self._obs_btn_available: bool = False

        self.setObjectName("StatusBar")
        self.setFixedHeight(self._BAR_H)
        # Cursor is ArrowCursor in inactive state; PointingHandCursor when media is active
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._build_bar_ui()
        self._build_overlay()
        self._connect_media()

        # Aplica volume salvo
        self.vol_slider.setValue(int(self._volume * 100))
        self.media.set_volume(self._volume)

    # ── Barra (sempre visível) ────────────────────────────────────────────

    def _build_bar_ui(self):
        bar_lay = QHBoxLayout(self)
        # Deixamos o topo e a base em 0 para o Qt centralizar verticalmente de forma automática.
        # Colocamos 9px na esquerda e direita para igualar com a folga natural de 9px do topo/base.
        bar_lay.setContentsMargins(9, 0, 9, 0) 
        bar_lay.setSpacing(8)

        # ── Estado inativo ────────────────────────────────────────────────
        self.inactive_widget = QWidget()
        self.inactive_widget.setStyleSheet("background: transparent;")
        inact_lay = QHBoxLayout(self.inactive_widget)
        inact_lay.setContentsMargins(0, 0, 0, 0)
        inact_lay.setSpacing(8)

        # Monitor icon — static, non-interactive
        self.monitor_icon = QLabel()
        self.monitor_icon.setFixedSize(28, 28)
        self.monitor_icon.setPixmap(make_icon(ICON_SCREEN, 15, "#484f58").pixmap(15, 15))
        self.monitor_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.monitor_icon.setStyleSheet("background: transparent; border: none;")

        self.screen_count_label = QLabel()
        self.screen_count_label.setObjectName("StatusLabel")
        self.screen_count_label.setStyleSheet("color: #484f58; font-size: 11px; background: transparent;")
        inact_lay.addWidget(self.monitor_icon)
        inact_lay.addWidget(self.screen_count_label)
        bar_lay.addWidget(self.inactive_widget)

        # ── Estado ativo ──────────────────────────────────────────────────
        self.active_widget = QWidget()
        self.active_widget.setStyleSheet("background: transparent;")
        self.active_widget.setVisible(False)
        act_lay = QHBoxLayout(self.active_widget)
        act_lay.setContentsMargins(0, 0, 0, 0)
        act_lay.setSpacing(8)

        # Thumb / ícone de modo
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(28, 28)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet(
            "background: rgba(88,166,255,0.10); border-radius: 6px;"
            " border: 1px solid rgba(88,166,255,0.20);"
        )

        # Título
        self.proj_title = QLabel()
        self.proj_title.setObjectName("StatusLabel")
        self.proj_title.setStyleSheet(
            "background: transparent; color: #e6edf3; font-weight: 600; font-size: 12px;"
            " letter-spacing: 0.1px;"
        )
        self.proj_title.setMaximumWidth(175)
        self.proj_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # Badge de reprodução offline — ponto de status verde (10 px).
        # Padrão de design universal (Slack, Spotify, Discord) para indicadores
        # de status a tamanhos pequenos: ponto sólido colorido + tooltip descritivo.
        self._offline_badge = QLabel()
        self._offline_badge.setFixedSize(10, 10)
        self._offline_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_badge.setToolTip(
            self.tr("Playing offline")
        )
        self._offline_badge.setVisible(False)
        # Ponto verde sólido. QToolTip override garante que o tooltip não herda
        # o background verde do widget pai.
        self._offline_badge.setStyleSheet(
            "QLabel {"
            "  background: #3fb950;"
            "  border-radius: 5px;"
            "  border: 1.5px solid rgba(0,0,0,0.30);"
            "}"
            "QToolTip {"
            "  background: #161b22;"
            "  color: #c9d1d9;"
            "  border: 1px solid #30363d;"
            "  border-radius: 6px;"
            "  padding: 4px 8px;"
            "  font-size: 12px;"
            "}"
        )

        # Seek slider
        self.seek_slider = BufferedSlider()
        self.seek_slider.setMinimumWidth(100)
        self.seek_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Tempo
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("StatusLabel")
        self.time_label.setStyleSheet(
            "background: transparent; font-size: 11px; color: #8b949e;"
        )
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

        # Play/Pause — mesmo tamanho e estilo dos demais botoes
        self.play_btn = _icon_btn(ICON_PAUSE, 30, 15, "#c9d1d9",
                                  self.tr("Pause/Resume"))
        self.play_btn.clicked.connect(self._on_play_btn_clicked)

        # Prev / Next playlist navigation
        self.prev_btn = _icon_btn(ICON_SKIP_PREV, 30, 15, "#6e7681", self.tr("Previous"))
        self.next_btn = _icon_btn(ICON_SKIP_NEXT, 30, 15, "#6e7681", self.tr("Next"))
        self.prev_btn.clicked.connect(self._on_prev_clicked)
        self.next_btn.clicked.connect(self._on_next_clicked)
        self.prev_btn.setVisible(False)
        self.next_btn.setVisible(False)

        # Volume
        self.vol_btn = _icon_btn(ICON_VOLUME_HIGH, 30, 15, "#8b949e",
                                 self.tr("Volume"))
        self.vol_btn.clicked.connect(self._toggle_mute)
        self._muted = False
        self._pre_mute_vol = self._volume

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(70)
        self.vol_slider.setStyleSheet(
            "QSlider::groove:horizontal{height:3px;background:#3d444d;border-radius:2px;}"
            "QSlider::handle:horizontal{width:10px;height:10px;margin:-4px 0;"
            "background:#c9d1d9;border-radius:5px;}"
            "QSlider::sub-page:horizontal{background:#58a6ff;border-radius:2px;}"
        )
        self.vol_slider.valueChanged.connect(self._on_volume_slider)

        # Mais opções (só vídeo)
        self.more_btn = _icon_btn(ICON_MORE_VERT, 30, 15, "#8b949e",
                                  self.tr("Playback options"))
        self.more_btn.setVisible(False)
        self.more_btn.clicked.connect(self._show_more_menu)

        # OBS scene toggle — só imagem, só quando OBS conectado + cena de mídia configurada
        # Alterna entre a cena de mídia (projetor visível) e a cena anterior/idle (projetor oculto)
        self.obs_scene_btn = _icon_btn(ICON_OBS, 30, 14, "#8b949e",
                                       self.tr("Toggle OBS scene"))
        self.obs_scene_btn.setVisible(False)
        self.obs_scene_btn.clicked.connect(self._on_obs_scene_btn_clicked)
        # Tooltip dinâmico — atualizado em _refresh_obs_scene_btn

        # Timer countdown (só modo timer)
        self.timer_countdown_label = QLabel("00:00")
        self.timer_countdown_label.setStyleSheet(
            "background: transparent; color: #3b82f6; font-size: 18px;"
            " font-weight: 700; letter-spacing: 1px; min-width: 90px;"
        )
        self.timer_countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timer_countdown_label.setVisible(False)

        # Fechar / parar — circular, transparente, vermelho só no hover
        self.close_btn = _icon_btn(ICON_CLOSE, 30, 13, "#8b949e",
                                   self.tr("Stop projection"))
        self.close_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:15px;"
            "background:transparent;padding:0;}"
            "QPushButton:hover{background:rgba(248,81,73,0.18);}"
            "QPushButton:pressed{background:rgba(248,81,73,0.30);}"
        )
        # Muda cor do ícone para vermelho no hover via evento
        self.close_btn.installEventFilter(self)
        self.close_btn.clicked.connect(self.stop_requested)

        act_lay.addWidget(self.thumb_label)
        act_lay.addWidget(self.proj_title)
        act_lay.addWidget(self._offline_badge)
        act_lay.addSpacing(4)
        act_lay.addWidget(self.play_btn)
        act_lay.addWidget(self.seek_slider, stretch=1)
        act_lay.addWidget(self.time_label)
        act_lay.addWidget(self.vol_btn)
        act_lay.addWidget(self.vol_slider)
        act_lay.addWidget(self.timer_countdown_label, stretch=1)
        act_lay.addWidget(self.more_btn)
        # Espaçador só visível no modo imagem — ancora prev/next à direita em todos os modos
        self._fill_spacer = QWidget()
        self._fill_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._fill_spacer.setVisible(False)
        act_lay.addWidget(self._fill_spacer, stretch=1)
        act_lay.addWidget(self.obs_scene_btn)
        act_lay.addWidget(self.prev_btn)
        act_lay.addWidget(self.next_btn)
        act_lay.addWidget(self.close_btn)

        bar_lay.addWidget(self.active_widget, stretch=1)

        self.mousePressEvent = self._on_bar_clicked

    # ── Overlay ───────────────────────────────────────────────────────────

    def _build_overlay(self):
        parent = self._container if self._container else self
        self.overlay = QWidget(parent)
        self.overlay.setVisible(False)
        self.overlay.setStyleSheet("background: #0d1117;")
        self.overlay.raise_()

        ov_lay = QVBoxLayout(self.overlay)
        ov_lay.setContentsMargins(0, 0, 0, 0)
        ov_lay.setSpacing(0)

        # ── Topo ─────────────────────────────────────────────────────────
        ov_top = QWidget()
        ov_top.setFixedHeight(42)
        ov_top.setStyleSheet("background: #161b22; border-bottom: 1px solid #30363d;")
        ov_top_lay = QHBoxLayout(ov_top)
        ov_top_lay.setContentsMargins(12, 0, 12, 0)
        ov_top_lay.setSpacing(8)

        self.minimize_btn = _icon_btn(ICON_CHEVRON_DOWN, 28, 14, "#8b949e",
                                      self.tr("Minimize"))
        self.minimize_btn.clicked.connect(self._collapse)

        self.ov_title = QLabel()
        self.ov_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ov_title.setStyleSheet(
            "background: transparent; color: #e6edf3; font-size: 13px; font-weight: 600;"
        )

        # Botão de toggle do painel de playlist (só aparece com > 1 item)
        self.ov_panel_btn = _icon_btn(ICON_PANEL_RIGHT, 28, 14, "#8b949e",
                                      self.tr("Show playlist"))
        self.ov_panel_btn.setVisible(False)
        self.ov_panel_btn.clicked.connect(self._toggle_playlist_panel)

        self.ov_close_btn = _icon_btn(ICON_CLOSE, 28, 13, "#8b949e",
                                      self.tr("Stop projection"))
        self.ov_close_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:14px;"
            "background:transparent;padding:0;}"
            "QPushButton:hover{background:rgba(248,81,73,0.18);}"
            "QPushButton:pressed{background:rgba(248,81,73,0.30);}"
        )
        self.ov_close_btn.clicked.connect(self.stop_requested)
        self.ov_close_btn.installEventFilter(self)

        # Botão "Adicionar à Playlist"
        self.ov_add_playlist_btn = _icon_btn(
            ICON_ADD_TO_PLAYLIST, 28, 13, "#8b949e",
            self.tr("Add to Playlist"),
        )
        self.ov_add_playlist_btn.setVisible(False)
        self.ov_add_playlist_btn.clicked.connect(self._on_add_to_playlist_clicked)

        # Botão "Enviar para playlist temporária" (só aparece se não for de playlist salva)
        self.ov_send_temp_btn = _icon_btn(
            ICON_SEND_TO_PLAYLIST, 28, 14, "#8b949e",
            self.tr("Open as temporary playlist"),
        )
        self.ov_send_temp_btn.setVisible(False)
        self.ov_send_temp_btn.clicked.connect(self._on_send_to_temp_playlist)

        # Botão "Definir como Idle Screen"
        # Visível apenas para vídeo (não-áudio) e imagem; nunca para timer ou aba ao vivo.
        self.ov_set_idle_btn = _icon_btn(
            ICON_SET_AS_IDLE, 28, 14, "#8b949e",
            self.tr("Set as idle screen"),
        )
        self.ov_set_idle_btn.setVisible(False)
        self.ov_set_idle_btn.clicked.connect(self._on_set_as_idle_clicked)

        ov_top_lay.addWidget(self.minimize_btn)
        ov_top_lay.addWidget(self.ov_title, stretch=1)
        ov_top_lay.addWidget(self.ov_send_temp_btn)
        ov_top_lay.addWidget(self.ov_add_playlist_btn)
        ov_top_lay.addWidget(self.ov_set_idle_btn)
        ov_top_lay.addWidget(self.ov_panel_btn)
        ov_top_lay.addWidget(self.ov_close_btn)
        ov_lay.addWidget(ov_top)

        # ── Body: preview + painel lateral ───────────────────────────────
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Stack de conteúdo (preview / timer)
        self.overlay_stack = QStackedWidget()
        self.overlay_stack.setStyleSheet("background: transparent;")

        self.preview_content = ImagePreviewWidget()
        self.preview_content.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_content.apply_transform.connect(self._on_image_apply_transform)
        self.preview_content.reset_transform.connect(self._on_image_reset_transform)
        self.overlay_stack.addWidget(self.preview_content)   # index 0

        self.circular_timer = CircularTimerWidget()
        self.overlay_stack.addWidget(self.circular_timer)    # index 1

        # Painel de playlist (inicia fechado = largura 0)
        self.playlist_panel = PlaylistPanel(lang=self.lang)
        self._thumb_queue.info_ready.connect(self._on_thumbnail_ready)
        self.playlist_panel.item_clicked.connect(self._on_panel_item_clicked)

        body_lay.addWidget(self.overlay_stack, stretch=1)
        body_lay.addWidget(self.playlist_panel)

        ov_lay.addWidget(body, stretch=1)

    def _update_overlay_geometry(self):
        if not self._container:
            return
        c = self._container
        self.overlay.setGeometry(0, 0, c.width(), c.height() - self._BAR_H)

    # ── Event filter: botão fechar muda ícone no hover ────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        _close_btns = (
            getattr(self, "close_btn", None),
            getattr(self, "ov_close_btn", None),
        )
        if obj in _close_btns:
            if event.type() == QEvent.Type.Enter:
                obj.setIcon(make_icon(ICON_CLOSE, 13, "#f85149"))
            elif event.type() == QEvent.Type.Leave:
                obj.setIcon(make_icon(ICON_CLOSE, 13, "#8b949e"))
        return super().eventFilter(obj, event)

    # ── Expand / Collapse ─────────────────────────────────────────────────

    def _on_bar_clicked(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._mode is not None:
            self._toggle_expand()

    def _toggle_expand(self):
        self._collapse() if self._expanded else self._expand()

    def _expand(self):
        if self._expanded:
            return
        self._expanded = True
        self._update_overlay_geometry()
        self.overlay.raise_()
        self.overlay.setVisible(True)
        self.expanded_changed.emit(True)
        if self._mode == 'image' and self._image_pixmap:
            QTimer.singleShot(10, self._redraw_preview_image)
        elif self._mode == 'video' and self._is_audio:
            QTimer.singleShot(10, self._refresh_audio_cover_in_overlay)

    def _collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self.overlay.setVisible(False)
        self._stop_wave_animation()
        self.expanded_changed.emit(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._expanded:
            self._update_overlay_geometry()

    def _redraw_preview_image(self):
        if not self._image_pixmap:
            return
        # ImagePreviewWidget handles its own scaling + zoom in paintEvent
        self.preview_content.setPixmap(self._image_pixmap)

    def _on_video_frame(self, frame):
        # MP3 não tem frames de vídeo — ignora para não sobrescrever a capa
        if not self._expanded or self._mode != 'video' or self._is_audio:
            return
        img = frame.toImage()
        if img.isNull():
            return
        pixmap = QPixmap.fromImage(img)
        self.preview_content.setPixmap(pixmap)

    # ── Conexões com MediaController ─────────────────────────────────────

    def _connect_media(self):
        self.seek_slider.sliderMoved.connect(self.seek_requested)
        self.media.state_changed.connect(self._on_state_changed)
        self.media.duration_changed.connect(self._on_duration_changed)
        self.media.position_changed.connect(self._on_position_changed)
        self.media.media_ended.connect(self._on_media_ended)
        self.media.buffer_progress.connect(self._on_buffer_progress)
        self.media.frame_ready.connect(self._on_video_frame)
        self.media.playback_source_changed.connect(self._on_playback_source_changed)

    @Slot(bool)
    def _on_playback_source_changed(self, is_offline: bool):
        """
        Mostra/oculta o badge de offline dependendo da fonte de reprodução.
        is_offline=True  → arquivo local (cache) — mostra badge verde sutil
        is_offline=False → stream HTTP ou inativo — oculta badge
        """
        self._offline_badge.setVisible(is_offline and self._mode is not None)

    # ── API pública ───────────────────────────────────────────────────────

    def is_expanded(self) -> bool:
        return self._expanded

    def expand_overlay(self) -> None:
        self._expand()

    def collapse_overlay(self) -> None:
        self._collapse()

    def is_video_mode(self) -> bool:
        return self._mode == "video"

    def is_audio_mode(self) -> bool:
        return self._is_audio

    def is_visual_media_active(self) -> bool:
        return self._mode == "image" or (self._mode == "video" and not self._is_audio)

    def set_projected_title(self, title: str) -> None:
        if not title:
            return
        short = (title[:22] + "…") if len(title) > 22 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)

    def hide_add_to_playlist_action(self) -> None:
        self.ov_add_playlist_btn.setVisible(False)

    def is_obs_scene_media(self) -> bool:
        return self._obs_scene_is_media

    def activate_video(self, title: str, keep_expanded: bool = False, is_audio: bool = False):
        self._cancel_announcement_mode()
        self._mode = 'video'
        self._is_audio = is_audio
        self._image_pixmap = None
        self._stop_wave_animation()   # para animação da faixa anterior (se houver)
        # _live_thumb_captured sempre reseta ao trocar de faixa.
        # keep_expanded=True mantém overlay aberto mas é uma nova mídia — nova captura.
        self._live_thumb_captured = False
        # Sempre reseta a capa de áudio ao trocar de faixa — a nova mídia pode não ter capa.
        self._audio_cover_pixmap = None
        self._stop_timer_internals()

        # Disable interactive image mode
        self.preview_content.set_image_mode(False)

        # Ícone correto: música para áudio, vídeo para vídeo
        _thumb_icon = ICON_MUSIC if is_audio else ICON_VIDEO
        self.thumb_label.setPixmap(make_icon(_thumb_icon, 18, "#3fb950").pixmap(18, 18))

        short = (title[:22] + "…") if len(title) > 22 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)

        self.seek_slider.reset()
        self.time_label.setText("0:00 / 0:00")
        self.play_btn.setIcon(make_icon(ICON_PAUSE, 15, "#c9d1d9"))
        self.play_btn.setVisible(True)
        self.seek_slider.setVisible(True)
        self.time_label.setVisible(True)
        self.vol_btn.setVisible(True)
        self.vol_slider.setVisible(True)
        self.more_btn.setVisible(True)
        self.timer_countdown_label.setVisible(False)
        self.obs_scene_btn.setVisible(False)
        self._fill_spacer.setVisible(False)
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.overlay_stack.setCurrentIndex(0)
        # Bar is clickable to expand when active
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Aplica velocidade salva
        self.media.set_playback_rate(self._speed)

        if self._expanded:
            if keep_expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
                # Se é áudio e overlay está aberto, atualiza a capa (ou fallback)
                if is_audio:
                    QTimer.singleShot(10, self._refresh_audio_cover_in_overlay)
            else:
                self._collapse()

        # Mostra botão de adicionar à playlist no overlay
        self.ov_add_playlist_btn.setVisible(True)
        # Idle btn: visível apenas para vídeo local (não para áudio nem URLs remotas)
        self._is_live_tab = False
        self._refresh_idle_btn_visibility()

        # Atualiza botões de navegação
        self._update_nav_buttons()

        # Agenda captura one-shot de thumbnail ao vivo, se o item ainda não tem miniatura
        self._live_thumb_timer.stop()
        self._schedule_live_thumb()

    def activate_image(self, title: str = "", image_data: bytes = b"", keep_expanded: bool = False):
        self._mode = 'image'
        default_label = self.tr("Projected image")
        label = title if title else default_label
        short = (label[:28] + "…") if len(label) > 28 else label
        self.proj_title.setText(short)
        self.proj_title.setToolTip(label)
        self.ov_title.setText(label)

        # Enable interactive image mode on the preview widget
        self.preview_content.set_image_mode(True)
        # Snap-reset projection windows transform for the new image.
        # Uses the instant variant — no lerp animation when switching images.
        self.image_reset_transform_instant.emit()

        if image_data:
            # ── Salva bytes como arquivo para poder referenciar na playlist ──
            import uuid as _uuid
            self._profile_paths.images_dir.mkdir(parents=True, exist_ok=True)
            _img_path = self._profile_paths.images_dir / f"{_uuid.uuid4().hex}.png"
            try:
                with _img_path.open("wb") as _f:
                    _f.write(image_data)
                self._image_file_path = os.fspath(_img_path)
            except OSError:
                self._image_file_path = ""

            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            self._image_pixmap = pixmap
            # Fresh load — resets zoom/offset
            self.preview_content.set_image_pixmap_fresh(pixmap)
            thumb = pixmap.scaled(
                32, 32,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            if thumb.width() > 32 or thumb.height() > 32:
                x = (thumb.width() - 32) // 2
                y = (thumb.height() - 32) // 2
                thumb = thumb.copy(x, y, 32, 32)
            self.thumb_label.setPixmap(thumb)
        else:
            self._image_pixmap = None
            self._image_file_path = ""
            self.thumb_label.setPixmap(make_icon(ICON_IMAGE, 18, "#3fb950").pixmap(18, 18))

        self.seek_slider.setVisible(False)
        self.time_label.setVisible(False)
        self.play_btn.setVisible(False)
        self.vol_btn.setVisible(False)
        self.vol_slider.setVisible(False)
        self.more_btn.setVisible(False)
        self.timer_countdown_label.setVisible(False)
        self._fill_spacer.setVisible(True)
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.overlay_stack.setCurrentIndex(0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Botão OBS: visível se disponível; ao entrar no modo imagem, assume cena de mídia ativa
        self._obs_scene_is_media = True
        self._refresh_obs_scene_btn()
        self.obs_scene_btn.setVisible(self._obs_btn_available)

        if self._expanded:
            if keep_expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
                QTimer.singleShot(10, self._redraw_preview_image)
            else:
                self._collapse()

        # Atualiza botões de navegação
        self._update_nav_buttons()

        # Mostra botão de adicionar à playlist no overlay (imagem)
        self.ov_add_playlist_btn.setVisible(True)
        # Mostra botão de idle screen para imagens (exceto aba ao vivo — tratado em set_live_tab_mode)
        self._is_live_tab = False
        self.ov_set_idle_btn.setVisible(True)

    def update_tab_live_preview(self, frame):
        """Atualiza o overlay com o frame ao vivo da aba projetada (bug 2).

        Chamado no ritmo entregue pelo motor nativo da aba ao vivo.
        Só renderiza se o overlay estiver expandido — sem custo quando minimizado.
        """
        if not self._expanded or self.overlay_stack.currentIndex() != 0:
            return
        if isinstance(frame, QImage):
            pixmap = QPixmap.fromImage(frame)
        else:
            pixmap = frame
        self.preview_content.setPixmap(pixmap)

    def activate_live_stream(self, title: str, keep_expanded: bool = False):
        self._cancel_announcement_mode()
        self._mode = 'live_stream'
        self._is_audio = False
        self._is_live_tab = True
        self._image_pixmap = None
        self._image_file_path = ""
        self._audio_cover_pixmap = None
        self._stop_wave_animation()
        self._stop_timer_internals()
        self.preview_content.set_image_mode(False)

        short = (title[:24] + "…") if len(title) > 24 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)
        self.thumb_label.setPixmap(make_icon(ICON_CAST, 18, "#58a6ff").pixmap(18, 18))

        self.seek_slider.reset()
        self.seek_slider.setVisible(False)
        self.time_label.setText(self.tr("LIVE"))
        self.time_label.setVisible(True)
        self.play_btn.setVisible(False)
        self.vol_btn.setVisible(False)
        self.vol_slider.setVisible(False)
        self.more_btn.setVisible(False)
        self.obs_scene_btn.setVisible(False)
        self.timer_countdown_label.setVisible(False)
        self._fill_spacer.setVisible(True)
        self.prev_btn.setVisible(False)
        self.next_btn.setVisible(False)
        self.ov_panel_btn.setVisible(False)
        self.ov_add_playlist_btn.setVisible(False)
        self.ov_send_temp_btn.setVisible(False)
        self.ov_set_idle_btn.setVisible(False)
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.overlay_stack.setCurrentIndex(0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        if self._expanded:
            if keep_expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
            else:
                self._collapse()

    def activate_timer(self, target_dt: QDateTime):
        self._stop_timer_internals()
        self._mode = 'timer'
        self._timer_target = target_dt
        remaining = ceil_remaining_seconds(target_dt)
        self._timer_total_secs = max(1, remaining)

        self.circular_timer.update_data(remaining, self._timer_total_secs)
        self.overlay_stack.setCurrentIndex(1)
        self.timer_updated.emit(remaining, self._timer_total_secs)

        self.thumb_label.setPixmap(make_icon(ICON_NAV_TIMER, 18, "#3b82f6").pixmap(18, 18))
        target_str = target_dt.time().toString("HH:mm")
        prefix = self.tr("Timer →")
        short_title = f"{prefix} {target_str}"
        self.proj_title.setText(short_title)
        self.ov_title.setText(short_title)
        self._update_timer_bar_label(remaining)

        self.seek_slider.setVisible(False)
        self.time_label.setVisible(False)
        self.play_btn.setVisible(False)
        self.vol_btn.setVisible(False)
        self.vol_slider.setVisible(False)
        self.more_btn.setVisible(False)
        self.obs_scene_btn.setVisible(False)
        self.timer_countdown_label.setVisible(True)
        self.ov_add_playlist_btn.setVisible(False)   # cronômetro não pode ser adicionado à playlist
        self.ov_set_idle_btn.setVisible(False)        # cronômetro não pode ser idle
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._expanded:
            self._collapse()
        self._timer_tick.start()

    # ── Song Announcement Mode ────────────────────────────────────────────

    def begin_announcement_mode(self) -> None:
        """
        Enters GATE state: video plays muted while the conductor
        announces the song. Controls are locked — only the close button works.
        Called by MainWindow right before media_ctrl.play_url().
        """
        self._announce_state = "gate"
        if hasattr(self.media, "set_local_switch_deferred"):
            self.media.set_local_switch_deferred(True)
        # Mute audio immediately (before play_url starts streaming)
        self.media.audio_output.setVolume(0.0)
        # Lock play button and seek slider — close button remains active
        self.play_btn.setEnabled(False)
        self.seek_slider.setEnabled(False)
        # Polls media position; this is media-time, not wall-clock time.
        self._announce_timer.start()

    def _on_announce_gate_expired(self) -> None:
        """
        Gate target reached in media time → pause, then unmute after a delay.

        The delay is intentional: Qt's audio pipeline may still be processing
        the last decoded frame when pause() is called. Restoring the volume
        immediately causes that residual frame to play back audibly (a chirp).
        Waiting ~80 ms lets the pipeline fully settle into the paused/silent
        state before the volume is brought back up.
        """
        if self._announce_state != "gate":
            return
        if self.media.position < self._announce_gate_ms:
            return
        self._announce_timer.stop()
        self._announce_state = "ready"
        self.media.pause()
        if hasattr(self.media, "set_local_switch_deferred"):
            self.media.set_local_switch_deferred(False)
        # Delay unmute so the audio pipeline drains before volume is restored
        actual_vol = 0.0 if self._muted else self._volume
        QTimer.singleShot(
            80,
            lambda: self.media.audio_output.setVolume(actual_vol)
                    if self._announce_state == "ready" else None,
        )
        # Only play button is re-enabled; slider remains locked
        self.play_btn.setEnabled(True)

    def _on_play_btn_clicked(self) -> None:
        """
        Central dispatcher for the play/pause button.
        In READY state: seek to 0, release slider lock, start playback.
        Otherwise: emit toggle_requested as usual.
        """
        if self._announce_state == "ready":
            self._announce_state = "off"
            if hasattr(self.media, "set_local_switch_deferred"):
                self.media.set_local_switch_deferred(False)
            # Unlock seek slider before seeking (setPosition fires positionChanged)
            self.seek_slider.setEnabled(True)
            self.media.seek(0)
            self.media.play()
            return
        self.toggle_requested.emit()

    def _cancel_announcement_mode(self) -> None:
        """Aborts announcement mode and restores controls unconditionally."""
        if self._announce_state == "off":
            return
        self._announce_timer.stop()
        self._announce_state = "off"
        if hasattr(self.media, "set_local_switch_deferred"):
            self.media.set_local_switch_deferred(False)
        # Restore volume
        actual_vol = 0.0 if self._muted else self._volume
        self.media.audio_output.setVolume(actual_vol)
        # Restore controls
        self.play_btn.setEnabled(True)
        self.seek_slider.setEnabled(True)

    def deactivate(self):
        self._cancel_announcement_mode()
        self._mode = None
        self._image_pixmap = None
        self._image_file_path = ""
        self._is_audio = False
        self._audio_cover_pixmap = None
        self._playlist = []
        self._playlist_index = 0
        self._played_indices = set()
        self._stop_timer_internals()
        if self._expanded:
            self._collapse()
        # Fecha e reseta o painel
        if self.playlist_panel.is_open():
            self.playlist_panel.close_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, "#8b949e"))
        self._live_thumb_timer.stop()
        self._thumb_queue.clear()
        self._panel_populated = False
        self.preview_content.set_image_mode(False)
        self.preview_content.setPixmap(QPixmap())
        self.preview_content.setText("")
        self.overlay_stack.setCurrentIndex(0)
        self.timer_countdown_label.setVisible(False)
        self.more_btn.setVisible(False)
        self.prev_btn.setVisible(False)
        self.next_btn.setVisible(False)
        self.obs_scene_btn.setVisible(False)
        self.ov_panel_btn.setVisible(False)
        self.ov_add_playlist_btn.setVisible(False)
        self.ov_set_idle_btn.setVisible(False)
        self._is_live_tab = False
        self.inactive_widget.setVisible(True)
        self.active_widget.setVisible(False)
        self._offline_badge.setVisible(False)
        self.seek_slider.reset()
        # No clickable expand action when inactive — use arrow cursor
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ── OBS scene toggle (modo imagem) ────────────────────────────────────

    def set_obs_btn_available(self, available: bool):
        """
        Habilita/desabilita o botão OBS de acordo com conexão e configuração.
        Chamado pela MainWindow quando o estado do OBS muda.
        Só mostra o botão se disponível E modo image estiver ativo.
        """
        self._obs_btn_available = available
        # Visibilidade real: só no modo imagem
        self.obs_scene_btn.setVisible(available and self._mode == 'image')

    def set_obs_scene_is_media(self, is_media: bool):
        """
        Atualiza o estado visual do botão para refletir se a cena de mídia
        está ativa ou não. Chamado pela MainWindow após cada troca de cena.
        """
        self._obs_scene_is_media = is_media
        self._refresh_obs_scene_btn()

    def _refresh_obs_scene_btn(self):
        """Atualiza cor e tooltip do botão para refletir o estado atual."""
        if self._obs_scene_is_media:
            # Cena de mídia ativa → ícone destacado (azul), tooltip indica "ocultar"
            self.obs_scene_btn.setIcon(make_icon(ICON_OBS, 14, "#8b949e"))
            self.obs_scene_btn.setToolTip(self.tr("Hide media from OBS"))
        else:
            # Cena anterior ativa → ícone neutro, tooltip indica "mostrar"
            self.obs_scene_btn.setIcon(make_icon(ICON_OBS, 14, "#484f58"))
            self.obs_scene_btn.setToolTip(self.tr("Show media in OBS"))

    def _on_obs_scene_btn_clicked(self):
        """Solicita à MainWindow que alterne entre cena de mídia e cena anterior/idle."""
        self.obs_scene_toggle_requested.emit()

    def set_screen_count(self, n: int):
        self._screen_count = n
        label = self.tr("secondary screen") if n == 1 else self.tr("secondary screens")
        self.screen_count_label.setText(f"{n} {label}")

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        """Atualiza todos os textos do widget global quando o idioma muda."""
        self.play_btn.setToolTip(self.tr("Pause/Resume"))
        self.vol_btn.setToolTip(self.tr("Volume"))
        self.more_btn.setToolTip(self.tr("Playback options"))
        self.close_btn.setToolTip(self.tr("Stop projection"))
        self.prev_btn.setToolTip(self.tr("Previous"))
        self.next_btn.setToolTip(self.tr("Next"))
        self.minimize_btn.setToolTip(self.tr("Minimize"))
        self.ov_close_btn.setToolTip(self.tr("Stop projection"))
        self.ov_panel_btn.setToolTip(self.tr("Show playlist"))
        self.ov_send_temp_btn.setToolTip(self.tr("Open as temporary playlist"))
        self.ov_add_playlist_btn.setToolTip(self.tr("Add to Playlist"))
        self.ov_set_idle_btn.setToolTip(self.tr("Set as idle screen"))
        self.set_screen_count(self._screen_count)
        self._offline_badge.setToolTip(self.tr("Playing offline"))
        self.obs_scene_btn.setToolTip(self.tr("Hide media from OBS"))
        self.playlist_panel.refresh_language(self.lang)
        if hasattr(self, "_monitor_popup") and self._monitor_popup is not None:
            self._monitor_popup.retranslateUi()

    def refresh_language(self) -> None:
        """Alias de compatibilidade → retranslateUi()."""
        self.retranslateUi()

    # ── Image zoom/pan → projector ────────────────────────────────────────

    # Signals forwarded to MainWindow so it can apply the transform to all
    # projection windows without ProjectionBar knowing about them directly.
    image_apply_transform = Signal(float, float, float)  # zoom, norm_x, norm_y
    image_reset_transform = Signal()           # animated reset (user pressed Reset btn)
    image_reset_transform_instant = Signal()   # snap reset (image switch — no animation)

    @Slot(float, float, float)
    def _on_image_apply_transform(self, zoom: float, norm_x: float, norm_y: float):
        """Received from ImagePreviewWidget — forward to MainWindow."""
        self.image_apply_transform.emit(zoom, norm_x, norm_y)

    @Slot()
    def _on_image_reset_transform(self):
        """Received from ImagePreviewWidget — forward animated reset to MainWindow."""
        self.image_reset_transform.emit()

    # ── Menu de opções de vídeo ───────────────────────────────────────────

    def _show_more_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLE)

        # ── Velocidade ──────────────────────────────────────────────────
        speed_menu = menu.addMenu("  " + self.tr("Speed"))
        speed_menu.setStyleSheet(_MENU_STYLE)
        speed_group = QActionGroup(speed_menu)
        speed_group.setExclusive(True)
        for label, val in [("0.5×", 0.5), ("0.75×", 0.75), ("1×", 1.0),
                            ("1.25×", 1.25), ("1.5×", 1.5), ("2×", 2.0)]:
            act = QAction(label, speed_group)
            act.setCheckable(True)
            act.setChecked(abs(self._speed - val) < 0.01)
            act.triggered.connect(lambda checked, v=val: self._set_speed(v))
            speed_menu.addAction(act)

        menu.addSeparator()

        # ── Loop ────────────────────────────────────────────────────────
        loop_act = QAction("  " + self.tr("Loop"), menu)
        loop_act.setCheckable(True)
        loop_act.setChecked(self._loop)
        loop_act.triggered.connect(self._toggle_loop)
        menu.addAction(loop_act)

        menu.addSeparator()

        # ── Ordem de reprodução ─────────────────────────────────────────
        order_menu = menu.addMenu("  " + self.tr("Playback Order"))
        order_menu.setStyleSheet(_MENU_STYLE)
        order_group = QActionGroup(order_menu)
        order_group.setExclusive(True)
        for label, val in [
            (self.tr("Off"),    ORDER_OFF),
            (self.tr("Next"),   ORDER_NEXT),
            (self.tr("Random"), ORDER_RANDOM),
        ]:
            act = QAction("  " + label, order_group)
            act.setCheckable(True)
            act.setChecked(self._playback_order == val)
            act.triggered.connect(lambda checked, v=val: self._set_playback_order(v))
            order_menu.addAction(act)

        menu.exec(self.more_btn.mapToGlobal(
            self.more_btn.rect().topLeft() - QSize(0, menu.sizeHint().height()).toSize() if False
            else self.more_btn.rect().bottomLeft()
        ))

    def _set_speed(self, rate: float):
        self._speed = rate
        self._playback_settings.set_speed(rate)
        self.media.set_playback_rate(rate)

    def _toggle_loop(self):
        self._loop = not self._loop
        self._playback_settings.set_loop_enabled(self._loop)

    def _set_playback_order(self, order: str):
        self._playback_order = order
        self._playback_settings.set_playback_order(order)
        # Reinicia rastreamento de aleatório ao mudar de modo
        self._played_indices = {self._playlist_index}

    # ── Volume ────────────────────────────────────────────────────────────

    def _on_volume_slider(self, value: int):
        vol = value / 100.0
        self._volume = vol
        self._playback_settings.set_volume(vol)
        self.volume_changed.emit(vol)
        # Atualiza ícone
        if value == 0:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_MUTE, 15, "#8b949e"))
        elif value < 50:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_LOW, 15, "#8b949e"))
        else:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_HIGH, 15, "#8b949e"))

    def _toggle_mute(self):
        if self._muted:
            self._muted = False
            self.vol_slider.setValue(int(self._pre_mute_vol * 100))
        else:
            self._pre_mute_vol = self._volume
            self._muted = True
            self.vol_slider.setValue(0)

    # ── Timer mode ────────────────────────────────────────────────────────

    def _update_timer_bar_label(self, remaining: int):
        rem = max(0, remaining)
        h_p = rem // 3600
        m_p = (rem % 3600) // 60
        s_p = rem % 60
        text = f"{h_p:02d}:{m_p:02d}:{s_p:02d}" if h_p else f"{m_p:02d}:{s_p:02d}"
        self.timer_countdown_label.setText(text)

    def _on_timer_tick(self):
        if not self._timer_target:
            return
        remaining = ceil_remaining_seconds(self._timer_target)
        window = self.window()
        if window is None or not window.isMinimized():
            self.circular_timer.update_data(remaining, self._timer_total_secs)
            self._update_timer_bar_label(remaining)
        self.timer_updated.emit(remaining, self._timer_total_secs)
        if remaining <= 0:
            self._timer_tick.stop()
            self._start_blink_sequence()

    def _start_blink_sequence(self):
        self._blink_count = 0
        self._blink_on = False
        self._timer_blink_timer.start()
        QTimer.singleShot(5000, self._auto_close_timer)

    def _on_blink_tick(self):
        self._blink_on = not self._blink_on
        self.circular_timer.set_blink(self._blink_on)
        self.timer_blink.emit(self._blink_on)
        color = "#ef4444" if self._blink_on else "#3b82f6"
        self.timer_countdown_label.setStyleSheet(
            f"background: transparent; color: {color}; font-size: 18px;"
            " font-weight: 700; letter-spacing: 1px; min-width: 90px;"
        )

    def _auto_close_timer(self):
        self._timer_blink_timer.stop()
        self.circular_timer.set_blink(False)
        self.stop_requested.emit()

    def _stop_timer_internals(self):
        self._timer_tick.stop()
        self._timer_blink_timer.stop()
        self._timer_target = None
        self._blink_on = False
        self._blink_count = 0

    # ── Callbacks de mídia ────────────────────────────────────────────────

    @Slot(QMediaPlayer.PlaybackState)
    def _on_state_changed(self, state):
        playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        icon = ICON_PAUSE if playing else ICON_PLAY
        self.play_btn.setIcon(make_icon(icon, 15, "#c9d1d9"))

    @Slot(int)
    def _on_duration_changed(self, duration: int):
        self.seek_slider.setRange(0, duration)
        self._update_time_label(self.media.position, duration)
        # Persiste duração no item da playlist para uso no export .jwlplaylist.
        # Roda sempre que há playlist ativa — independente de ser salva ou temp.
        # (BaseDurationTicks = duration_ms × 10000 ticks de 100ns)
        if duration > 0:
            idx = self._playlist_index
            if 0 <= idx < len(self._playlist):
                item_id = self._playlist[idx].get("id", "")
                if item_id:
                    edit = getattr(self.playlist_widget, "_edit_view", None)
                    if edit is not None:
                        edit.notify_duration(item_id, duration)

    @Slot(int)
    def _on_position_changed(self, position: int):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self._update_time_label(position, self.media.duration)
        if self._announce_state == "gate" and position >= self._announce_gate_ms:
            self._on_announce_gate_expired()

    def _on_media_ended(self):
        """Lógica de avanço automático com base na playlist e ordem de reprodução."""
        if self._mode != 'video':
            return

        order = self._playback_order
        n = len(self._playlist)

        if n == 0:
            # Sem playlist — comportamento legado
            if self._loop:
                self.play_next_requested.emit("__replay__", "", "video")
            else:
                self.stop_requested.emit()
            return

        if order == ORDER_OFF:
            # Reproduz apenas o item atual
            if self._loop:
                self.media.replay()
            else:
                self.stop_requested.emit()

        elif order == ORDER_NEXT:
            next_idx = self._playlist_index + 1
            if next_idx >= n:
                if self._loop:
                    if n == 1:
                        self.media.replay()
                        return
                    next_idx = 0
                else:
                    self.stop_requested.emit()
                    return
            self._playlist_index = next_idx
            self._played_indices.add(next_idx)
            self._update_nav_buttons()
            item = self._playlist[next_idx]
            self.play_next_requested.emit(item["url"], item["title"],
                                          item.get("type", "video"))

        elif order == ORDER_RANDOM:
            self._played_indices.add(self._playlist_index)
            remaining = [i for i in range(n) if i not in self._played_indices]
            if not remaining:
                if self._loop:
                    if n == 1:
                        self.media.replay()
                        return
                    self._played_indices = set()
                    remaining = list(range(n))
                else:
                    self.stop_requested.emit()
                    return
            next_idx = _random.choice(remaining)
            self._playlist_index = next_idx
            self._played_indices.add(next_idx)
            self._update_nav_buttons()
            item = self._playlist[next_idx]
            self.play_next_requested.emit(item["url"], item["title"],
                                          item.get("type", "video"))

    @Slot(int, int)
    def _on_buffer_progress(self, downloaded: int, total: int):
        if total > 0:
            self.seek_slider.setBufferedRatio(downloaded / total)
        else:
            self.seek_slider.setBufferedRatio(0.0)

    def _update_time_label(self, pos: int, dur: int):
        self.time_label.setText(f"{self._fmt(pos)} / {self._fmt(dur)}")

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60}:{s % 60:02d}"

