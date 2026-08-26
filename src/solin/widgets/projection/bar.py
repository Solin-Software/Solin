"""Projection bar and related preview controls."""

from __future__ import annotations

import os
import random
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
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from solin.core.foundation.constants import ORDER_OFF, ORDER_NEXT, ORDER_RANDOM
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.time_utils import ceil_remaining_seconds
from solin.core.media.playback import MediaController
from solin.core.media.profile_store import ProfileMediaStore
from solin.core.media.settings import ProjectionPlaybackSettingsStore
from solin.core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from solin.core.projection.image_framing import (
    IDENTITY_IMAGE_TRANSFORM,
    ImageTransform,
)
from solin.core.rendering.fonts import FontManager
from solin.core.timer.models import MediaCountdownPresentation
from solin.core.timer.render import format_fixed_countdown
from solin.projection.yearly_text import YearlyTextWidget
from solin.styles.icons import (
    ICON_ADD_TO_PLAYLIST,
    ICON_CAST,
    ICON_CHEVRON_DOWN,
    ICON_CLOSE,
    ICON_FULLSCREEN,
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
from solin.styles.theme import PALETTE, qss_rgba, tooltip_stylesheet
from solin.ui.themed_tooltip import install_themed_tooltip
from solin.ui.incremental_load import IncrementalLoadHandle
from solin.widgets.circular_timer import CircularTimerWidget
from solin.ui.media_info import MediaInfoQueue
from solin.widgets.common.themed_slider import ThemedHorizontalSlider
from solin.widgets.playlist.panel import PlaylistPanel
from .audio import ProjectionAudioMixin
from .controls import (
    SPEED_CHOICES,
    icon_button as _icon_btn,
    projection_menu_style,
)
from .fullscreen import FullscreenVideoOverlay
from .native_surface import NativeVideoSurface
from .playlist import ProjectionPlaylistMixin, playback_order_has_pending_item
from .preview import ImagePreviewWidget
from solin.widgets.common.buffered_slider import BufferedSlider


# Keep ProjectionBar decoupled from PlaylistPanel internals while preserving timing.
_ANIM_MS = 220


class _ThemedVideoPreview(QWidget):
    """Present video without letting the backend own the letterbox area."""

    _DEFAULT_ASPECT_RATIO = 16 / 9

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {PALETTE.bg0};")
        self._aspect_ratio = self._DEFAULT_ASPECT_RATIO
        self._native_output_active = False
        self._native_surface: NativeVideoSurface | None = None
        self._video_widget = QVideoWidget(self)
        # The child always has the video's exact aspect ratio, so the multimedia
        # backend has no letterbox pixels of its own to paint black.
        self._video_widget.setAspectRatioMode(
            Qt.AspectRatioMode.IgnoreAspectRatio
        )
        self._video_widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )

    def set_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            if not self._native_output_active:
                self.clear_frame()
            return
        viewport = frame.surfaceFormat().viewport()
        frame_size = viewport.size() if viewport.isValid() else frame.size()
        width = frame_size.width()
        height = frame_size.height()
        if frame.rotation().value in (90, 270):
            width, height = height, width
        self.set_video_size(QSize(width, height))
        if self._native_output_active:
            return
        self._video_widget.videoSink().setVideoFrame(frame)

    @Slot(QSize)
    def set_video_size(self, size: QSize) -> None:
        """Fit both presenters without materializing the decoded frame."""

        width = size.width()
        height = size.height()
        if width <= 0 or height <= 0:
            return
        aspect_ratio = width / height
        self._aspect_ratio = aspect_ratio
        self._apply_video_geometry()

    def clear_frame(self) -> None:
        self._video_widget.videoSink().setVideoFrame(QVideoFrame())

    def set_native_output_active(self, active: bool) -> bool:
        active = bool(active)
        if active == self._native_output_active:
            return False
        self._native_output_active = active
        if active:
            surface = self._ensure_native_surface()
            self.clear_frame()
            self._video_widget.hide()
            surface.show()
            surface.raise_()
        else:
            if self._native_surface is not None:
                self._native_surface.hide()
            self._video_widget.show()
        return True

    @property
    def native_output_active(self) -> bool:
        return self._native_output_active

    @property
    def native_surface(self) -> NativeVideoSurface | None:
        return self._native_surface

    def _ensure_native_surface(self) -> NativeVideoSurface:
        if self._native_surface is None:
            self._native_surface = NativeVideoSurface(self)
            self._native_surface.set_input_target(self)
            self._apply_video_geometry()
        return self._native_surface

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background: {PALETTE.bg0};")
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_video_geometry()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_video_geometry()

    def _apply_video_geometry(self) -> None:
        available_width = max(0, self.width())
        available_height = max(0, self.height())
        if available_width == 0 or available_height == 0:
            self._video_widget.setGeometry(0, 0, 0, 0)
            if self._native_surface is not None:
                self._native_surface.setGeometry(0, 0, 0, 0)
            return
        target_width = available_width
        target_height = round(target_width / self._aspect_ratio)
        if target_height > available_height:
            target_height = available_height
            target_width = round(target_height * self._aspect_ratio)
        target_geometry = (
            (available_width - target_width) // 2,
            (available_height - target_height) // 2,
            target_width,
            target_height,
        )
        self._video_widget.setGeometry(*target_geometry)
        if self._native_surface is not None:
            self._native_surface.setGeometry(*target_geometry)


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
    play_next_requested = Signal(object)  # complete media item for automatic advance
    playlist_navigate   = Signal(int)        # índice absoluto — navegação manual prev/next
    add_to_destination_requested = Signal(str, str, object)  # url, title, metadata
    send_to_temp_playlist_requested = Signal(list)  # lista de itens da playlist atual
    source_duration_discovered = Signal(str, int)  # playlist item id, original duration ms
    monitor_manager_requested = Signal(object)   # QWidget (the button) for popup positioning
    obs_scene_toggle_requested = Signal()    # usuário quer alternar entre cena de mídia e cena anterior
    set_as_idle_requested      = Signal(str) # path — usuário quer definir mídia como idle screen
    expanded_changed           = Signal(bool)
    video_output_target_changed = Signal()

    _BAR_H = 48

    def __init__(
        self,
        media_ctrl: MediaController,
        *,
        playback_settings: ProjectionPlaybackSettingsStore,
        playback_protection,
        profile_paths: ProfilePaths,
        profile_media_store: ProfileMediaStore,
        media_cache_dir: str | os.PathLike[str],
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        font_manager: FontManager,
        yearly_text_provider: Callable[[], tuple[str, str, str]],
        projection_aspect_ratio_provider: Callable[[], ProjectionAspectRatio] | None = None,
        lang_manager=None,
        container: QWidget = None,
        parent=None,
    ):
        super().__init__(parent)
        self.media      = media_ctrl
        self._playback_settings = playback_settings
        self._playback_protection = playback_protection
        self._profile_paths = profile_paths
        self._profile_media_store = profile_media_store
        self._media_cache_dir = media_cache_dir
        self._projection_aspect_ratio_provider = (
            projection_aspect_ratio_provider
            or (lambda: DEFAULT_PROJECTION_ASPECT_RATIO)
        )
        self._font_manager = font_manager
        self._yearly_text_provider = yearly_text_provider
        self.lang       = lang_manager
        self._container = container
        self._expanded  = False
        self._mode      = None  # 'video' | 'image' | 'timer' | None
        self._image_pixmap: QPixmap | None = None
        self._image_file_path: str = ""   # caminho do arquivo salvo para imagens sem URL
        self._is_audio: bool = False
        self._video_preview_route_requested = False
        self._audio_cover_pixmap: QPixmap | None = None
        self._is_live_tab: bool = False   # True quando projetando aba ao vivo do browser
        self._fullscreen_overlay: FullscreenVideoOverlay | None = None
        self._fullscreen_preparation_scheduled = False
        self._last_buffer_progress: tuple[int, int] = (0, 0)
        self._playback_recovering: bool = False

        # ── Timer state ──────────────────────────────────────────────────
        self._timer_target: QDateTime | None = None
        self._timer_total_secs: int = 0
        self._timer_presentation: MediaCountdownPresentation | None = None
        self._yearly_timer_text: tuple[str, str, str] = ("", "", "")
        self.yearly_timer: YearlyTextWidget | None = None
        self._timer_tick = QTimer(self)
        # Sub-second cadence so the displayed countdown always reflects the
        # current second within ~200 ms of its boundary — a 1 s timer drifts
        # against the wall clock and would occasionally freeze or skip a second.
        self._timer_tick.setInterval(200)
        self._timer_tick.timeout.connect(self._on_timer_tick)
        self._timer_blink_timer = QTimer(self)
        self._timer_blink_timer.setInterval(400)
        self._timer_blink_timer.timeout.connect(self._on_blink_tick)
        self._timer_auto_close = QTimer(self)
        self._timer_auto_close.setSingleShot(True)
        self._timer_auto_close.setInterval(5000)
        self._timer_auto_close.timeout.connect(self._auto_close_timer)
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
        self._image_match_projection_aspect: bool = (
            self._playback_settings.image_match_projection_aspect()
        )
        self._image_constrain_to_frame: bool = (
            self._playback_settings.image_constrain_to_frame()
        )

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

        # ── Automatic Zoom share playback gate ───────────────────────────
        # Visual videos remain paused and user playback controls stay locked
        # until the asynchronous share-start attempt has been resolved.
        self._auto_share_playback_waiting = False

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
        # The shell is not interactive. Each visible state owns its cursor so
        # native child-window materialization cannot replace the active hint.
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._build_bar_ui()
        from solin.bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("projection_bar_shell_constructed")
        self.overlay: QWidget | None = None
        self._overlay_ready = False
        self.overlay_preparation_handle = IncrementalLoadHandle(
            (
                self._build_overlay,
                self._build_overlay_preview,
                self._build_overlay_timer,
                self._finish_overlay,
            ),
            self,
        )
        self._connect_media()
        self._playback_protection.enabledChanged.connect(
            self._sync_protected_media_controls
        )
        self._playback_protection.lockedChanged.connect(
            self._sync_protected_media_controls
        )
        if self._container is not None:
            self._container.installEventFilter(self)

        # Aplica volume salvo
        self.vol_slider.setValue(int(self._volume * 100))
        self.media.set_volume(self._volume)

    # ── Barra (sempre visível) ────────────────────────────────────────────

    def _offline_badge_stylesheet(self) -> str:
        return (
            "QLabel {"
            f"  background: {PALETTE.success};"
            "  border-radius: 5px;"
            f"  border: 1.5px solid {qss_rgba(PALETTE.bg0, 0.60)};"
            "}"
            f"{tooltip_stylesheet()}"
        )

    def _close_button_stylesheet(self) -> str:
        return (
            "QPushButton{border:none;border-radius:15px;"
            "background:transparent;padding:0;}"
            f"QPushButton:hover{{background:{qss_rgba(PALETTE.danger, 0.18)};}}"
            f"QPushButton:pressed{{background:{qss_rgba(PALETTE.danger, 0.30)};}}"
        )

    def _circular_button_stylesheet(self, button: QWidget) -> str:
        radius = max(1, button.width() // 2)
        return (
            f"QPushButton{{border:none;border-radius:{radius}px;"
            "background:transparent;padding:0;}"
            f"QPushButton:hover{{background:{PALETTE.surface_hover};border-radius:{radius}px;}}"
            f"QPushButton:pressed{{background:{PALETTE.surface_hover_strong};}}"
        )

    def _apply_icon_button_styles(self) -> None:
        for button in (
            self.play_btn,
            self.prev_btn,
            self.next_btn,
            self.vol_btn,
            self.more_btn,
            self.obs_scene_btn,
            self.minimize_btn,
            self.ov_panel_btn,
            self.ov_add_destination_btn,
            self.ov_send_temp_btn,
            self.ov_set_idle_btn,
            self.ov_fullscreen_btn,
        ):
            button.setStyleSheet(self._circular_button_stylesheet(button))

    def _build_bar_ui(self):
        bar_lay = QHBoxLayout(self)
        # Deixamos o topo e a base em 0 para o Qt centralizar verticalmente de forma automática.
        # Colocamos 9px na esquerda e direita para igualar com a folga natural de 9px do topo/base.
        bar_lay.setContentsMargins(9, 0, 9, 0) 
        bar_lay.setSpacing(8)

        # ── Estado inativo ────────────────────────────────────────────────
        self.inactive_widget = QWidget()
        self.inactive_widget.setCursor(Qt.CursorShape.ArrowCursor)
        self.inactive_widget.setStyleSheet("background: transparent;")
        inact_lay = QHBoxLayout(self.inactive_widget)
        inact_lay.setContentsMargins(0, 0, 0, 0)
        inact_lay.setSpacing(8)

        # Monitor icon — static, non-interactive
        self.monitor_icon = QLabel()
        self.monitor_icon.setFixedSize(28, 28)
        self.monitor_icon.setPixmap(make_icon(ICON_SCREEN, 15, PALETTE.text_dim).pixmap(15, 15))
        self.monitor_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.monitor_icon.setStyleSheet("background: transparent; border: none;")

        self.screen_count_label = QLabel()
        self.screen_count_label.setObjectName("StatusLabel")
        self.screen_count_label.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px; background: transparent;"
        )
        inact_lay.addWidget(self.monitor_icon)
        inact_lay.addWidget(self.screen_count_label)
        bar_lay.addWidget(self.inactive_widget)

        # ── Estado ativo ──────────────────────────────────────────────────
        self.active_widget = QWidget()
        self.active_widget.setCursor(Qt.CursorShape.PointingHandCursor)
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
            f"background: {qss_rgba(PALETTE.accent_hover, 0.10)}; border-radius: 6px;"
            f" border: 1px solid {qss_rgba(PALETTE.accent_hover, 0.20)};"
        )

        # Título
        self.proj_title = QLabel()
        self.proj_title.setObjectName("StatusLabel")
        self.proj_title.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary}; font-weight: 600; font-size: 12px;"
            " letter-spacing: 0.1px;"
        )
        self.proj_title.setMaximumWidth(175)
        self.proj_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        install_themed_tooltip(self.proj_title)

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
        install_themed_tooltip(self._offline_badge)
        # Ponto verde sólido. QToolTip override garante que o tooltip não herda
        # o background verde do widget pai.
        self._offline_badge.setStyleSheet(self._offline_badge_stylesheet())

        # Seek slider
        self.seek_slider = BufferedSlider()
        self.seek_slider.setMinimumWidth(100)
        self.seek_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Tempo
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("StatusLabel")
        self.time_label.setStyleSheet(
            f"background: transparent; font-size: 11px; color: {PALETTE.text_muted};"
        )
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

        # Play/Pause — mesmo tamanho e estilo dos demais botoes
        self.play_btn = _icon_btn(ICON_PAUSE, 30, 15, PALETTE.text_secondary,
                                  self.tr("Pause/Resume"))
        self.play_btn.clicked.connect(self._on_play_btn_clicked)

        # Prev / Next playlist navigation
        self.prev_btn = _icon_btn(ICON_SKIP_PREV, 30, 15, PALETTE.text_faint, self.tr("Previous"))
        self.next_btn = _icon_btn(ICON_SKIP_NEXT, 30, 15, PALETTE.text_faint, self.tr("Next"))
        self.prev_btn.clicked.connect(self._on_prev_clicked)
        self.next_btn.clicked.connect(self._on_next_clicked)
        self.prev_btn.setVisible(False)
        self.next_btn.setVisible(False)

        # Volume
        self.vol_btn = _icon_btn(ICON_VOLUME_HIGH, 30, 15, PALETTE.text_muted,
                                 self.tr("Volume"))
        self.vol_btn.clicked.connect(self._toggle_mute)
        self._muted = False
        self._pre_mute_vol = self._volume

        self.vol_slider = ThemedHorizontalSlider(track_height=3, handle_diameter=10)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(70)
        self.vol_slider.valueChanged.connect(self._on_volume_slider)

        # Mais opções (só vídeo)
        self.more_btn = _icon_btn(ICON_MORE_VERT, 30, 15, PALETTE.text_muted,
                                  self.tr("Playback options"))
        self.more_btn.setVisible(False)
        self.more_btn.clicked.connect(self._show_more_menu)

        # OBS scene toggle — só imagem, só quando OBS conectado + cena de mídia configurada
        # Alterna entre a cena de mídia (projetor visível) e a cena anterior/idle (projetor oculto)
        self.obs_scene_btn = _icon_btn(ICON_OBS, 30, 14, PALETTE.text_muted,
                                       self.tr("Toggle OBS scene"))
        self.obs_scene_btn.setVisible(False)
        self.obs_scene_btn.clicked.connect(self._on_obs_scene_btn_clicked)
        # Tooltip dinâmico — atualizado em _refresh_obs_scene_btn

        # Timer countdown (só modo timer)
        self.timer_countdown_label = QLabel("00:00")
        self.timer_countdown_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.accent}; font-size: 18px;"
            " font-weight: 700; letter-spacing: 1px; min-width: 90px;"
        )
        self.timer_countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timer_countdown_label.setVisible(False)

        # Fechar / parar — circular, transparente, vermelho só no hover
        self.close_btn = _icon_btn(ICON_CLOSE, 30, 13, PALETTE.text_muted,
                                   self.tr("Stop projection"))
        self.close_btn.setStyleSheet(self._close_button_stylesheet())
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
        if self.overlay is not None:
            return
        from solin.bootstrap.startup_timeline import startup_timeline

        startup = startup_timeline()
        parent = self._container if self._container else self
        self.overlay = QWidget(parent)
        self.overlay.setVisible(False)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.overlay.setAutoFillBackground(True)
        self.overlay.setStyleSheet(f"background: {PALETTE.bg0};")
        self.overlay.raise_()

        self._overlay_layout = QVBoxLayout(self.overlay)
        self._overlay_layout.setContentsMargins(0, 0, 0, 0)
        self._overlay_layout.setSpacing(0)

        # ── Topo ─────────────────────────────────────────────────────────
        ov_top = QWidget()
        self._overlay_header = ov_top
        ov_top.setFixedHeight(42)
        ov_top.setStyleSheet(
            f"background: {PALETTE.surface}; border-bottom: 1px solid {PALETTE.border};"
        )
        ov_top_lay = QHBoxLayout(ov_top)
        ov_top_lay.setContentsMargins(12, 0, 12, 0)
        ov_top_lay.setSpacing(8)

        self.minimize_btn = _icon_btn(ICON_CHEVRON_DOWN, 28, 14, PALETTE.text_muted,
                                      self.tr("Minimize"))
        self.minimize_btn.clicked.connect(self._collapse)

        self.ov_title = QLabel()
        self.ov_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ov_title.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary}; font-size: 13px; font-weight: 600;"
        )

        # Botão de toggle do painel de playlist (só aparece com > 1 item)
        self.ov_panel_btn = _icon_btn(ICON_PANEL_RIGHT, 28, 14, PALETTE.text_muted,
                                      self.tr("Show playlist"))
        self.ov_panel_btn.setVisible(False)
        self.ov_panel_btn.clicked.connect(self._toggle_playlist_panel)

        # Botão "Adicionar à Playlist"
        self.ov_add_destination_btn = _icon_btn(
            ICON_ADD_TO_PLAYLIST, 28, 13, PALETTE.text_muted,
            self.tr("Add to…"),
        )
        self.ov_add_destination_btn.setVisible(False)
        self.ov_add_destination_btn.clicked.connect(self._on_add_to_destination_clicked)

        # Botão "Enviar para playlist temporária" (só aparece se não for de playlist salva)
        self.ov_send_temp_btn = _icon_btn(
            ICON_SEND_TO_PLAYLIST, 28, 14, PALETTE.text_muted,
            self.tr("Open as temporary playlist"),
        )
        self.ov_send_temp_btn.setVisible(False)
        self.ov_send_temp_btn.clicked.connect(self._on_send_to_temp_playlist)

        # Botão "Definir como Idle Screen"
        # Visível apenas para vídeo (não-áudio) e imagem; nunca para timer ou aba ao vivo.
        self.ov_set_idle_btn = _icon_btn(
            ICON_SET_AS_IDLE, 28, 14, PALETTE.text_muted,
            self.tr("Set as idle screen"),
        )
        self.ov_set_idle_btn.setVisible(False)
        self.ov_set_idle_btn.clicked.connect(self._on_set_as_idle_clicked)

        self.ov_fullscreen_btn = _icon_btn(
            ICON_FULLSCREEN, 28, 14, PALETTE.text_muted,
            self.tr("Fullscreen"),
        )
        self.ov_fullscreen_btn.setVisible(False)
        self.ov_fullscreen_btn.clicked.connect(self.enter_app_fullscreen)

        ov_top_lay.addWidget(self.minimize_btn)
        ov_top_lay.addWidget(self.ov_title, stretch=1)
        ov_top_lay.addWidget(self.ov_send_temp_btn)
        ov_top_lay.addWidget(self.ov_add_destination_btn)
        ov_top_lay.addWidget(self.ov_set_idle_btn)
        ov_top_lay.addWidget(self.ov_fullscreen_btn)
        ov_top_lay.addWidget(self.ov_panel_btn)
        self._overlay_layout.addWidget(ov_top)
        startup.mark("projection_overlay_header_constructed")

        # ── Body: preview + painel lateral ───────────────────────────────
        body = QWidget()
        self._overlay_body = body
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body.setStyleSheet(f"background: {PALETTE.bg0};")
        self._overlay_body_layout = QHBoxLayout(body)
        self._overlay_body_layout.setContentsMargins(0, 0, 0, 0)
        self._overlay_body_layout.setSpacing(0)

        # Stack de conteúdo (preview / timer)
        self.overlay_stack = QStackedWidget()
        self.overlay_stack.setStyleSheet("background: transparent;")

    def _build_overlay_preview(self) -> None:
        from solin.bootstrap.startup_timeline import startup_timeline

        self.preview_host = QWidget()
        preview_layout = QStackedLayout(self.preview_host)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.preview_content = ImagePreviewWidget()
        self.preview_content.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_content.apply_transform.connect(self._on_image_apply_transform)
        self.preview_content.reset_transform.connect(self._on_image_reset_transform)
        self.preview_content.match_projection_aspect_changed.connect(
            self._on_image_match_projection_aspect_changed
        )
        self.preview_content.constrain_to_frame_changed.connect(
            self._on_image_constrain_to_frame_changed
        )
        self.preview_content.installEventFilter(self)
        self.video_preview = _ThemedVideoPreview()
        self.video_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.media.video_sink.videoSizeChanged.connect(
            self._sync_video_preview_size
        )
        self._sync_video_preview_size()
        self.video_preview.setVisible(False)
        self.video_preview.installEventFilter(self)
        preview_layout.addWidget(self.preview_content)
        preview_layout.addWidget(self.video_preview)
        self.overlay_stack.addWidget(self.preview_host)   # index 0
        startup_timeline().mark("projection_overlay_preview_constructed")

    @Slot()
    def _sync_video_preview_size(self) -> None:
        preview = getattr(self, "video_preview", None)
        if preview is None:
            return
        preview.set_video_size(self.media.video_sink.videoSize())

    def _build_overlay_timer(self) -> None:
        from solin.bootstrap.startup_timeline import startup_timeline

        self.circular_timer = CircularTimerWidget()
        self.overlay_stack.addWidget(self.circular_timer)    # index 1
        startup_timeline().mark("projection_overlay_timer_constructed")

    def _finish_overlay(self) -> None:
        from solin.bootstrap.startup_timeline import startup_timeline

        # Painel de playlist (inicia fechado = largura 0)
        self.playlist_panel = PlaylistPanel(lang=self.lang)
        self._thumb_queue.info_ready.connect(self._on_thumbnail_ready)
        self.playlist_panel.item_clicked.connect(self._on_panel_item_clicked)
        startup_timeline().mark("projection_overlay_playlist_constructed")

        self._overlay_body_layout.addWidget(self.overlay_stack, stretch=1)
        self._overlay_body_layout.addWidget(self.playlist_panel)

        self._overlay_layout.addWidget(self._overlay_body, stretch=1)
        self._overlay_ready = True
        startup_timeline().mark("projection_bar_overlay_constructed")

    def _ensure_overlay_ready(self) -> None:
        if self._overlay_ready:
            return
        self.overlay_preparation_handle.complete_now()

    def _update_overlay_geometry(self):
        if not self._container or self.overlay is None:
            return
        c = self._container
        self.overlay.setGeometry(0, 0, c.width(), max(0, c.height() - self._BAR_H))

    # ── Event filter: botão fechar muda ícone no hover ────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._container and event.type() == QEvent.Type.Resize:
            if self._expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
        if (
            obj
            in (
                getattr(self, "preview_content", None),
                getattr(self, "video_preview", None),
            )
            and event.type() == QEvent.Type.MouseButtonDblClick
            and getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton
        ):
            if self._is_app_fullscreen_available():
                self.enter_app_fullscreen()
                return True
        _close_btns = (
            getattr(self, "close_btn", None),
        )
        if obj in _close_btns:
            if event.type() == QEvent.Type.Enter:
                obj.setIcon(make_icon(ICON_CLOSE, 13, PALETTE.danger))
            elif event.type() == QEvent.Type.Leave:
                obj.setIcon(make_icon(ICON_CLOSE, 13, PALETTE.text_muted))
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
        self._ensure_overlay_ready()
        self._expanded = True
        self.expanded_changed.emit(True)
        self._update_overlay_geometry()
        self.overlay.raise_()
        self.overlay.setVisible(True)
        self._sync_preview_surface()
        self._present_current_video_frame()
        if self._mode == 'image' and self._image_pixmap:
            QTimer.singleShot(10, self._redraw_preview_image)
        elif self._mode == 'video' and self._is_audio:
            QTimer.singleShot(10, self._refresh_audio_cover_in_overlay)

    def _collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self._sync_preview_surface()
        self.overlay.setVisible(False)
        self._stop_wave_animation()
        self.expanded_changed.emit(False)

    def _video_preview_desired(self) -> bool:
        return (
            getattr(self, "_expanded", False)
            and getattr(self, "_mode", None) == "video"
            and not getattr(self, "_is_audio", False)
            and hasattr(self, "video_preview")
        )

    def _sync_preview_surface(self) -> None:
        video_visible = self._video_preview_desired()
        video_preview = getattr(self, "video_preview", None)
        preview_content = getattr(self, "preview_content", None)
        if video_preview is not None:
            video_preview.setVisible(video_visible)
            if not video_visible:
                video_preview.clear_frame()
        if preview_content is not None:
            preview_content.setVisible(not video_visible)
        if video_visible != getattr(
            self,
            "_video_preview_route_requested",
            False,
        ):
            self._video_preview_route_requested = video_visible
            self.video_output_target_changed.emit()

    @property
    def native_video_output_requested(self) -> bool:
        return self.app_fullscreen_active() or getattr(
            self,
            "_video_preview_route_requested",
            False,
        )

    @property
    def native_video_output_surface(self) -> NativeVideoSurface | None:
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None and overlay.is_active():
            return overlay.native_video_surface
        preview = getattr(self, "video_preview", None)
        return preview.native_surface if preview is not None else None

    @property
    def python_video_frame_delivery_required(self) -> bool:
        """Whether an active Qt-only surface still needs decoded video frames."""

        if self._mode != "video" or self._is_audio:
            return False
        if self.app_fullscreen_active():
            overlay = getattr(self, "_fullscreen_overlay", None)
            return overlay is None or not overlay.native_output_active
        if not self._video_preview_desired():
            return False
        preview = getattr(self, "video_preview", None)
        return preview is None or not preview.native_output_active

    def set_native_video_output_active(self, active: bool) -> None:
        fullscreen_active = self.app_fullscreen_active()
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_native_output_active(bool(active) and fullscreen_active)

        preview = getattr(self, "video_preview", None)
        if preview is None:
            return
        effective = (
            bool(active)
            and not fullscreen_active
            and getattr(self, "_video_preview_route_requested", False)
        )
        changed = preview.set_native_output_active(effective)
        if changed and not effective and not fullscreen_active:
            self._present_current_video_frame()

    def _present_current_video_frame(self) -> None:
        if not self._video_preview_desired():
            return
        frame = self.media.video_sink.videoFrame()
        if frame.isValid():
            self.video_preview.set_frame(frame)

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
        if self._mode != 'video' or self._is_audio:
            return
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None and overlay.is_active():
            overlay.set_frame(frame)
        if not self._expanded:
            return
        if self._video_preview_desired():
            self.video_preview.set_frame(frame)

    # ── Conexões com MediaController ─────────────────────────────────────

    def _connect_media(self):
        self.seek_slider.sliderMoved.connect(self.seek_requested)
        self.media.state_changed.connect(self._on_state_changed)
        self.media.duration_changed.connect(self._on_duration_changed)
        self.media.source_duration_changed.connect(self._on_source_duration_changed)
        self.media.position_changed.connect(self._on_position_changed)
        self.media.media_ended.connect(self._on_media_ended)
        self.media.buffer_progress.connect(self._on_buffer_progress)
        self.media.frame_ready.connect(self._on_video_frame)
        self.media.playback_source_changed.connect(self._on_playback_source_changed)
        self.media.playback_recovery_changed.connect(
            self._on_playback_recovery_changed
        )

    def _sync_protected_media_controls(self) -> None:
        seek_enabled = (
            self._announce_state == "off"
            and not self._auto_share_playback_waiting
            and not self._playback_protection.locked
        )
        self.seek_slider.setEnabled(seek_enabled)
        self.seek_slider.setToolTip(
            self.tr("Pause playback to seek.")
            if self._playback_protection.locked
            else ""
        )
        self._sync_fullscreen_media_controls()
        self._update_nav_buttons()

    @Slot(bool)
    def _on_playback_source_changed(self, is_offline: bool):
        """
        Mostra/oculta o badge de offline dependendo da fonte de reprodução.
        is_offline=True  → arquivo local (cache) — mostra badge verde sutil
        is_offline=False → stream HTTP ou inativo — oculta badge
        """
        self._offline_badge.setVisible(is_offline and self._mode is not None)

    @Slot(bool)
    def _on_playback_recovery_changed(self, recovering: bool):
        self._playback_recovering = bool(recovering) and self._mode == "video"
        self.seek_slider.setReconnectActive(
            self._playback_recovering
        )
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_reconnect_active(self._playback_recovering)

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

    def current_media_title(self) -> str:
        return (self.proj_title.text() or "").strip()

    @property
    def projection_mode(self) -> str | None:
        return self._mode

    @property
    def volume_percent(self) -> int:
        return max(0, min(100, int(round(self._volume * 100))))

    def set_volume_percent(self, value: int) -> None:
        self.vol_slider.setValue(max(0, min(100, int(value))))

    def is_visual_media_active(self) -> bool:
        return self._mode == "image" or (self._mode == "video" and not self._is_audio)

    def app_fullscreen_active(self) -> bool:
        overlay = getattr(self, "_fullscreen_overlay", None)
        return bool(overlay is not None and overlay.is_active())

    def enter_app_fullscreen(self) -> None:
        if not self._is_app_fullscreen_available():
            return
        self._fullscreen_preparation_scheduled = False
        overlay = self._ensure_fullscreen_overlay()
        preview = getattr(self, "video_preview", None)
        overlay.set_native_output_active(
            bool(preview is not None and preview.native_output_active)
        )
        self._hydrate_fullscreen_overlay(overlay)
        overlay.show_fullscreen()

    def exit_app_fullscreen(self) -> None:
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.hide_fullscreen()

    def _is_app_fullscreen_available(self) -> bool:
        return self._mode == "video" and not self._is_audio

    def _ensure_fullscreen_overlay(self) -> FullscreenVideoOverlay:
        if getattr(self, "_fullscreen_overlay", None) is not None:
            return self._fullscreen_overlay
        overlay = FullscreenVideoOverlay(
            source_widget=self._container or self,
            translate=self.tr,
            parent=self,
        )
        overlay.exit_requested.connect(self.exit_app_fullscreen)
        overlay.seek_requested.connect(self._on_fullscreen_seek_requested)
        overlay.toggle_requested.connect(self._on_play_btn_clicked)
        overlay.volume_changed.connect(self._on_fullscreen_volume_changed)
        overlay.stop_requested.connect(self.stop_requested.emit)
        overlay.previous_requested.connect(self._on_prev_clicked)
        overlay.next_requested.connect(self._on_next_clicked)
        overlay.speed_selected.connect(self._set_speed)
        overlay.loop_toggled.connect(self._toggle_loop)
        overlay.playback_order_selected.connect(self._set_playback_order)
        overlay.visibility_changed.connect(
            self._on_fullscreen_visibility_changed
        )
        self._fullscreen_overlay = overlay
        return overlay

    def _schedule_fullscreen_preparation(self) -> None:
        if (
            not self._is_app_fullscreen_available()
            or self._fullscreen_preparation_scheduled
        ):
            return
        self._fullscreen_preparation_scheduled = True
        QTimer.singleShot(0, self._prepare_fullscreen_when_idle)

    def _prepare_fullscreen_when_idle(self) -> None:
        self._fullscreen_preparation_scheduled = False
        if not self._is_app_fullscreen_available():
            return
        self._ensure_fullscreen_overlay().prepare()

    @Slot(bool)
    def _on_fullscreen_visibility_changed(self, _visible: bool) -> None:
        self.video_output_target_changed.emit()

    def _hydrate_fullscreen_overlay(self, overlay: FullscreenVideoOverlay) -> None:
        title = self.ov_title.text() or self.proj_title.toolTip() or self.proj_title.text()
        overlay.set_title(title)
        overlay.set_volume(self._volume)
        overlay.set_speed(self._speed)
        overlay.set_loop_enabled(self._loop)
        overlay.set_playback_order(self._playback_order)
        overlay.set_playback_options_active(self._playback_options_indicator_active())
        overlay.set_duration(self.media.duration)
        overlay.set_position(self.media.position, self.media.duration)
        overlay.set_buffer_progress(*self._last_buffer_progress)
        overlay.set_reconnect_active(self._playback_recovering)
        overlay.set_playback_state(self.media.player.playbackState())
        self._sync_fullscreen_media_controls(overlay)
        self._sync_app_fullscreen_navigation()

    def _exit_app_fullscreen(self, *, clear_frame: bool = False) -> None:
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.hide_fullscreen(clear_frame=clear_frame)

    def _reset_app_fullscreen(self) -> None:
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.reset()

    def _sync_app_fullscreen_availability(self) -> None:
        available = self._is_app_fullscreen_available()
        self.ov_fullscreen_btn.setVisible(available)
        if not available:
            self._exit_app_fullscreen(clear_frame=True)

    def _sync_app_fullscreen_navigation(self) -> None:
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is None:
            return
        item_count = len(self._playlist)
        navigation_unlocked = not self._playback_protection.locked
        overlay.set_navigation(
            show=item_count > 1,
            can_previous=navigation_unlocked and self._playlist_index > 0,
            can_next=(
                navigation_unlocked
                and self._playlist_index < item_count - 1
            ),
        )
        overlay.set_playback_options_active(self._playback_options_indicator_active())

    def _on_fullscreen_volume_changed(self, volume: float) -> None:
        self.vol_slider.setValue(int(round(max(0.0, min(1.0, volume)) * 100)))

    def _on_fullscreen_seek_requested(self, value: int) -> None:
        if self._announce_state != "off" or self._auto_share_playback_waiting:
            return
        self.seek_requested.emit(value)

    def _sync_fullscreen_media_controls(
        self,
        overlay: FullscreenVideoOverlay | None = None,
    ) -> None:
        overlay = overlay or getattr(self, "_fullscreen_overlay", None)
        if overlay is None:
            return
        overlay.set_play_enabled(self.play_btn.isEnabled())
        overlay.set_seek_enabled(self.seek_slider.isEnabled())

    def set_projected_title(self, title: str) -> None:
        if not title:
            return
        self._ensure_overlay_ready()
        short = (title[:22] + "…") if len(title) > 22 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_title(title)

    def _enter_mode(self, mode: str | None) -> None:
        """End mode-specific timer work before changing projected content."""
        self._stop_timer_internals()
        if mode != "video":
            self.cancel_auto_share_playback_wait()
        self._mode = mode
        self._sync_preview_surface()

    def hide_add_to_destination_action(self) -> None:
        self._ensure_overlay_ready()
        self.ov_add_destination_btn.setVisible(False)

    def is_obs_scene_media(self) -> bool:
        return self._obs_scene_is_media

    def activate_video(self, title: str, keep_expanded: bool = False, is_audio: bool = False):
        self._ensure_overlay_ready()
        self.cancel_auto_share_playback_wait()
        self._cancel_announcement_mode()
        self._is_audio = is_audio
        self._enter_mode("video")
        self._image_pixmap = None
        self._stop_wave_animation()   # para animação da faixa anterior (se houver)
        self._last_buffer_progress = (0, 0)
        self._playback_recovering = False
        # _live_thumb_captured sempre reseta ao trocar de faixa.
        # keep_expanded=True mantém overlay aberto mas é uma nova mídia — nova captura.
        self._live_thumb_captured = False
        # Sempre reseta a capa de áudio ao trocar de faixa — a nova mídia pode não ter capa.
        self._audio_cover_pixmap = None
        # Disable interactive image mode
        self.preview_content.set_image_mode(False)

        # Ícone correto: música para áudio, vídeo para vídeo
        _thumb_icon = ICON_MUSIC if is_audio else ICON_VIDEO
        self.thumb_label.setPixmap(make_icon(_thumb_icon, 18, PALETTE.success).pixmap(18, 18))

        short = (title[:22] + "…") if len(title) > 22 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)

        self.seek_slider.reset()
        self.time_label.setText("0:00 / 0:00")
        self.play_btn.setIcon(make_icon(ICON_PAUSE, 15, PALETTE.text_secondary))
        self.play_btn.setVisible(True)
        self.seek_slider.setVisible(True)
        self.time_label.setVisible(True)
        self.vol_btn.setVisible(True)
        self.vol_slider.setVisible(True)
        self.more_btn.setVisible(True)
        self._sync_protected_media_controls()
        self.timer_countdown_label.setVisible(False)
        self.obs_scene_btn.setVisible(False)
        self._fill_spacer.setVisible(False)
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.overlay_stack.setCurrentIndex(0)
        self._sync_app_fullscreen_availability()

        # Aplica velocidade salva
        self.media.set_playback_rate(self._speed)

        if self.app_fullscreen_active() and self._fullscreen_overlay is not None:
            self._fullscreen_overlay.clear_frame()
            self._hydrate_fullscreen_overlay(self._fullscreen_overlay)

        if self._expanded:
            if keep_expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
                # Se é áudio e overlay está aberto, atualiza a capa (ou fallback)
                if is_audio:
                    QTimer.singleShot(10, self._refresh_audio_cover_in_overlay)
            else:
                self._collapse()
        self._sync_preview_surface()

        # Mostra botão de adicionar à playlist no overlay
        self.ov_add_destination_btn.setVisible(True)
        # Idle btn: visível apenas para vídeo local (não para áudio nem URLs remotas)
        self._is_live_tab = False
        self._refresh_idle_btn_visibility()

        # Atualiza botões de navegação
        self._update_nav_buttons()

        # Agenda captura one-shot de thumbnail ao vivo, se o item ainda não tem miniatura
        self._live_thumb_timer.stop()
        self._schedule_live_thumb()
        self._schedule_fullscreen_preparation()

    def activate_image(
        self,
        title: str = "",
        image_data: bytes = b"",
        keep_expanded: bool = False,
        initial_transform: ImageTransform | None = None,
        *,
        persist_operator_copy: bool = True,
        allow_add_to_destination: bool = True,
        allow_set_as_idle: bool = True,
    ):
        self._ensure_overlay_ready()
        self._enter_mode("image")
        self._last_buffer_progress = (0, 0)
        self._playback_recovering = False
        self._sync_app_fullscreen_availability()
        default_label = self.tr("Projected image")
        label = title if title else default_label
        short = (label[:28] + "…") if len(label) > 28 else label
        self.proj_title.setText(short)
        self.proj_title.setToolTip(label)
        self.ov_title.setText(label)

        # Enable interactive image mode on the preview widget
        self.preview_content.set_image_mode(True)

        if image_data:
            if persist_operator_copy:
                try:
                    self._image_file_path = (
                        self._profile_media_store.save_projected_image(image_data)
                    )
                except OSError:
                    self._image_file_path = ""
            else:
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
            self.thumb_label.setPixmap(make_icon(ICON_IMAGE, 18, PALETTE.success).pixmap(18, 18))
            self.preview_content.set_image_pixmap_fresh(QPixmap())

        self._configure_image_preview_framing(reset=True)
        if initial_transform is not None:
            self.preview_content.set_current_transform(initial_transform)

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
        self.ov_add_destination_btn.setVisible(allow_add_to_destination)
        # Mostra botão de idle screen para imagens (exceto aba ao vivo — tratado em set_live_tab_mode)
        self._is_live_tab = False
        self.ov_set_idle_btn.setVisible(allow_set_as_idle)

    def set_projected_image_transform(
        self,
        transform: ImageTransform,
    ) -> ImageTransform:
        """Synchronize the active preview without reloading its image."""

        self._configure_image_preview_framing(reset=False)
        return self.preview_content.set_current_transform(transform)

    def update_tab_live_preview(self, frame):
        """Atualiza o overlay com o frame ao vivo da aba projetada (bug 2).

        Chamado no ritmo entregue pelo motor nativo da aba ao vivo.
        Só renderiza se o overlay estiver expandido — sem custo quando minimizado.
        """
        if (
            not self._expanded
            or self.overlay_stack.currentIndex() != 0
        ):
            return
        if isinstance(frame, QImage):
            pixmap = QPixmap.fromImage(frame)
        else:
            pixmap = frame
        self.preview_content.setPixmap(pixmap)

    def activate_live_stream(self, title: str, keep_expanded: bool = False):
        self._ensure_overlay_ready()
        self._cancel_announcement_mode()
        self._enter_mode("live_stream")
        self._last_buffer_progress = (0, 0)
        self._playback_recovering = False
        self._sync_app_fullscreen_availability()
        self._is_audio = False
        self._is_live_tab = True
        self._image_pixmap = None
        self._image_file_path = ""
        self._audio_cover_pixmap = None
        self._stop_wave_animation()
        self.preview_content.set_image_mode(False)

        short = (title[:24] + "…") if len(title) > 24 else title
        self.proj_title.setText(short)
        self.proj_title.setToolTip(title)
        self.ov_title.setText(title)
        self.thumb_label.setPixmap(make_icon(ICON_CAST, 18, PALETTE.accent_hover).pixmap(18, 18))

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
        self.ov_add_destination_btn.setVisible(False)
        self.ov_send_temp_btn.setVisible(False)
        self.ov_set_idle_btn.setVisible(False)
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        self.overlay_stack.setCurrentIndex(0)
        if self._expanded:
            if keep_expanded:
                self._update_overlay_geometry()
                self.overlay.raise_()
            else:
                self._collapse()

    def set_yearly_text(self, quote: str, reference: str, api_code: str = "") -> None:
        self._yearly_timer_text = (quote, reference, api_code)
        if self.yearly_timer is not None:
            self.yearly_timer.set_text(quote, reference, api_code)

    def _ensure_yearly_timer(self) -> YearlyTextWidget:
        if self.yearly_timer is None:
            self.yearly_timer = YearlyTextWidget(self._font_manager)
            self.yearly_timer.set_text(*self._yearly_timer_text)
            self.overlay_stack.addWidget(self.yearly_timer)
        return self.yearly_timer

    def _refresh_yearly_timer_text(self) -> None:
        self.set_yearly_text(*self._yearly_text_provider())

    def activate_timer(
        self,
        target_dt: QDateTime,
        presentation: MediaCountdownPresentation,
    ) -> None:
        self._ensure_overlay_ready()
        self._enter_mode("timer")
        self._timer_presentation = presentation
        self._last_buffer_progress = (0, 0)
        self._playback_recovering = False
        self._sync_app_fullscreen_availability()
        self._timer_target = target_dt
        remaining = ceil_remaining_seconds(target_dt)
        self._timer_total_secs = max(1, remaining)

        if presentation is MediaCountdownPresentation.YEARLY_TEXT:
            self._refresh_yearly_timer_text()
        self._update_timer_preview(remaining)
        self.timer_updated.emit(remaining, self._timer_total_secs)

        self.thumb_label.setPixmap(make_icon(ICON_NAV_TIMER, 18, PALETTE.accent).pixmap(18, 18))
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
        self.ov_add_destination_btn.setVisible(False)
        self.ov_set_idle_btn.setVisible(False)        # cronômetro não pode ser idle
        self.inactive_widget.setVisible(False)
        self.active_widget.setVisible(True)
        if self._expanded:
            self._collapse()
        self._timer_tick.start()

    # ── Automatic share playback gate ─────────────────────────────────────

    def begin_auto_share_playback_wait(self) -> None:
        """Lock a paused visual video until Zoom share startup is resolved."""
        self._auto_share_playback_waiting = True
        self.play_btn.setEnabled(False)
        self._sync_protected_media_controls()

    def resolve_auto_share_playback_wait(self, success: bool) -> None:
        """Release the share gate, resuming only successful regular videos."""
        if not self._auto_share_playback_waiting:
            return
        self._auto_share_playback_waiting = False

        if self._announce_state == "gate":
            # Announcement mode has precedence. Its muted media-time gate must
            # run before it reaches the user-controlled READY state.
            self.play_btn.setEnabled(False)
            self._announce_timer.start()
            self.media.play()
        else:
            self.play_btn.setEnabled(True)
            if success:
                self.media.play()

        self._sync_protected_media_controls()

    def cancel_auto_share_playback_wait(self) -> None:
        """Discard a pending gate without changing playback state."""
        if not self._auto_share_playback_waiting:
            return
        self._auto_share_playback_waiting = False
        self.play_btn.setEnabled(self._announce_state != "gate")
        self._sync_protected_media_controls()

    # ── Song Announcement Mode ────────────────────────────────────────────

    def begin_announcement_mode(self) -> None:
        """
        Enters GATE state: video plays muted while the conductor
        announces the song. Controls are locked — only the close button works.
        Called by MainWindow right before media_ctrl.start_playback().
        """
        self._announce_state = "gate"
        if hasattr(self.media, "set_local_switch_deferred"):
            self.media.set_local_switch_deferred(True)
        # Mute audio immediately (before the playback request starts streaming)
        self.media.audio_output.setVolume(0.0)
        # Lock play button and seek slider — close button remains active
        self.play_btn.setEnabled(False)
        self._sync_protected_media_controls()
        self._sync_fullscreen_media_controls()
        # Poll media time only while playback can advance. Automatic sharing
        # starts this timer when its paused startup gate is resolved.
        if not self._auto_share_playback_waiting:
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
        self.play_btn.setEnabled(not self._auto_share_playback_waiting)
        self._sync_protected_media_controls()
        self._sync_fullscreen_media_controls()

    def _on_play_btn_clicked(self) -> None:
        """
        Central dispatcher for the play/pause button.
        In READY state: seek to 0, release slider lock, start playback.
        Otherwise: emit toggle_requested as usual.
        """
        if self._auto_share_playback_waiting:
            return
        if self._announce_state == "ready":
            self._announce_state = "off"
            if hasattr(self.media, "set_local_switch_deferred"):
                self.media.set_local_switch_deferred(False)
            # Unlock seek slider before seeking (setPosition fires positionChanged)
            self.media.seek(0)
            self.media.play()
            self._sync_protected_media_controls()
            self._sync_fullscreen_media_controls()
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
        self.play_btn.setEnabled(not self._auto_share_playback_waiting)
        self._sync_protected_media_controls()
        self._sync_fullscreen_media_controls()

    def deactivate(self):
        self._cancel_announcement_mode()
        self._reset_app_fullscreen()
        self._enter_mode(None)
        self._image_pixmap = None
        self._image_file_path = ""
        self._is_audio = False
        self._audio_cover_pixmap = None
        self._playlist = []
        self._playlist_index = 0
        self._played_indices = set()
        self._last_buffer_progress = (0, 0)
        self._playback_recovering = False
        if self._expanded:
            self._collapse()
        # Fecha e reseta o painel
        if self.playlist_panel.is_open():
            self.playlist_panel.close_panel()
            self.ov_panel_btn.setIcon(make_icon(ICON_PANEL_RIGHT, 14, PALETTE.text_muted))
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
        self.ov_add_destination_btn.setVisible(False)
        self.ov_fullscreen_btn.setVisible(False)
        self.ov_set_idle_btn.setVisible(False)
        self._is_live_tab = False
        self.inactive_widget.setVisible(True)
        self.active_widget.setVisible(False)
        self._offline_badge.setVisible(False)
        self.seek_slider.reset()

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
            self.obs_scene_btn.setIcon(make_icon(ICON_OBS, 14, PALETTE.text_muted))
            self.obs_scene_btn.setToolTip(self.tr("Hide media from OBS"))
        else:
            # Cena anterior ativa → ícone neutro, tooltip indica "mostrar"
            self.obs_scene_btn.setIcon(make_icon(ICON_OBS, 14, PALETTE.text_dim))
            self.obs_scene_btn.setToolTip(self.tr("Show media in OBS"))

    def _on_obs_scene_btn_clicked(self):
        """Solicita à MainWindow que alterne entre cena de mídia e cena anterior/idle."""
        self.obs_scene_toggle_requested.emit()

    def set_screen_count(self, n: int):
        self._screen_count = n
        label = self.tr("secondary screen") if n == 1 else self.tr("secondary screens")
        self.screen_count_label.setText(f"{n} {label}")

    def apply_theme(self) -> None:
        self.monitor_icon.setPixmap(
            make_icon(ICON_SCREEN, 15, PALETTE.text_dim).pixmap(15, 15)
        )
        self.screen_count_label.setStyleSheet(
            f"color: {PALETTE.text_dim}; font-size: 11px; background: transparent;"
        )
        self.thumb_label.setStyleSheet(
            f"background: {qss_rgba(PALETTE.accent_hover, 0.10)}; border-radius: 6px;"
            f" border: 1px solid {qss_rgba(PALETTE.accent_hover, 0.20)};"
        )
        self.proj_title.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary}; font-weight: 600; font-size: 12px;"
            " letter-spacing: 0.1px;"
        )
        self._offline_badge.setStyleSheet(self._offline_badge_stylesheet())
        self.time_label.setStyleSheet(
            f"background: transparent; font-size: 11px; color: {PALETTE.text_muted};"
        )
        self.vol_slider.apply_theme()
        timer_color = PALETTE.danger if self._blink_on else PALETTE.accent
        self.timer_countdown_label.setStyleSheet(
            f"background: transparent; color: {timer_color}; font-size: 18px;"
            " font-weight: 700; letter-spacing: 1px; min-width: 90px;"
        )
        self.close_btn.setStyleSheet(self._close_button_stylesheet())
        if self.overlay is None:
            return
        self._apply_icon_button_styles()
        self.overlay.setStyleSheet(f"background: {PALETTE.bg0};")
        self._overlay_header.setStyleSheet(
            f"background: {PALETTE.surface}; border-bottom: 1px solid {PALETTE.border};"
        )
        self.ov_title.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_primary}; font-size: 13px; font-weight: 600;"
        )
        self._overlay_body.setStyleSheet(f"background: {PALETTE.bg0};")
        self.preview_content.apply_theme()
        self.video_preview.apply_theme()
        self.seek_slider.update()
        self._on_state_changed(self.media.player.playbackState())
        self._on_volume_slider(self.vol_slider.value())
        self._update_nav_buttons()
        self._refresh_playback_options_indicator()
        self._refresh_obs_scene_btn()
        self.playlist_panel.apply_theme()
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None and hasattr(overlay, "apply_theme"):
            overlay.apply_theme()

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
        if self.overlay is None:
            self.set_screen_count(self._screen_count)
            self._offline_badge.setToolTip(self.tr("Playing offline"))
            self.obs_scene_btn.setToolTip(self.tr("Hide media from OBS"))
            return
        self.minimize_btn.setToolTip(self.tr("Minimize"))
        self.ov_panel_btn.setToolTip(self.tr("Show playlist"))
        self.ov_send_temp_btn.setToolTip(self.tr("Open as temporary playlist"))
        self.ov_add_destination_btn.setToolTip(self.tr("Add to…"))
        self.ov_set_idle_btn.setToolTip(self.tr("Set as idle screen"))
        self.ov_fullscreen_btn.setToolTip(self.tr("Fullscreen"))
        self.set_screen_count(self._screen_count)
        self._offline_badge.setToolTip(self.tr("Playing offline"))
        self.obs_scene_btn.setToolTip(self.tr("Hide media from OBS"))
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.retranslateUi()
        self.playlist_panel.refresh_language(self.lang)
        if hasattr(self, "_monitor_popup") and self._monitor_popup is not None:
            self._monitor_popup.retranslateUi()

    # ── Image zoom/pan → projector ────────────────────────────────────────

    # Signals forwarded to MainWindow so it can apply the transform to all
    # projection windows without ProjectionBar knowing about them directly.
    image_apply_transform = Signal(float, float, float)  # zoom, norm_x, norm_y
    image_apply_transform_instant = Signal(float, float, float)
    image_reset_transform = Signal()           # animated reset (user pressed Reset btn)

    def _resolve_projection_aspect_ratio(self) -> ProjectionAspectRatio:
        try:
            ratio = self._projection_aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - defensive UI provider boundary
            return DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            return DEFAULT_PROJECTION_ASPECT_RATIO
        return ratio

    def _configure_image_preview_framing(
        self,
        *,
        reset: bool,
    ) -> ImageTransform:
        ratio = self._resolve_projection_aspect_ratio()
        return self.preview_content.configure_framing(
            match_projection_aspect=self._image_match_projection_aspect,
            constrain_to_frame=self._image_constrain_to_frame,
            aspect_ratio=ratio.value,
            aspect_ratio_label=ratio.label,
            reset=reset,
        )

    def _emit_image_transform(
        self,
        transform: ImageTransform,
        *,
        instant: bool = False,
    ) -> None:
        if instant:
            self.image_apply_transform_instant.emit(
                transform.zoom,
                transform.norm_x,
                transform.norm_y,
            )
            return
        if transform == IDENTITY_IMAGE_TRANSFORM:
            self.image_reset_transform.emit()
        else:
            self.image_apply_transform.emit(
                transform.zoom,
                transform.norm_x,
                transform.norm_y,
            )

    @Slot(bool)
    def _on_image_match_projection_aspect_changed(self, enabled: bool) -> None:
        self._image_match_projection_aspect = bool(enabled)
        self._playback_settings.set_image_match_projection_aspect(
            self._image_match_projection_aspect
        )
        transform = self._configure_image_preview_framing(reset=True)
        self._emit_image_transform(transform)

    @Slot(bool)
    def _on_image_constrain_to_frame_changed(self, enabled: bool) -> None:
        self._image_constrain_to_frame = bool(enabled)
        self._playback_settings.set_image_constrain_to_frame(
            self._image_constrain_to_frame
        )
        transform = self._configure_image_preview_framing(reset=False)
        self._emit_image_transform(transform)

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
        menu.setStyleSheet(projection_menu_style())

        # ── Velocidade ──────────────────────────────────────────────────
        speed_menu = menu.addMenu("  " + self.tr("Speed"))
        speed_menu.setStyleSheet(projection_menu_style())
        speed_group = QActionGroup(speed_menu)
        speed_group.setExclusive(True)
        for label, val in SPEED_CHOICES:
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
        order_menu.setStyleSheet(projection_menu_style())
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

        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def _set_speed(self, rate: float):
        self._speed = rate
        self._playback_settings.set_speed(rate)
        self.media.set_playback_rate(rate)
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_speed(rate)

    def _toggle_loop(self):
        self._loop = not self._loop
        self._playback_settings.set_loop_enabled(self._loop)
        self._refresh_playback_options_indicator()
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_loop_enabled(self._loop)

    def _set_playback_order(self, order: str):
        self._playback_order = order
        self._playback_settings.set_playback_order(order)
        # Reinicia rastreamento de aleatório ao mudar de modo
        self._played_indices = {self._playlist_index}
        self._refresh_playback_options_indicator()
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_playback_order(order)

    def _playback_options_indicator_active(self) -> bool:
        if getattr(self, "_mode", None) != "video":
            return False
        if bool(getattr(self, "_loop", False)):
            return True
        return playback_order_has_pending_item(
            order=getattr(self, "_playback_order", ORDER_OFF),
            item_count=len(getattr(self, "_playlist", ())),
            current_index=getattr(self, "_playlist_index", 0),
            played_indices=set(getattr(self, "_played_indices", ())),
        )

    def _refresh_playback_options_indicator(self) -> None:
        active = self._playback_options_indicator_active()
        color = PALETTE.warning if active else PALETTE.text_muted
        more_btn = getattr(self, "more_btn", None)
        if more_btn is not None:
            more_btn.setIcon(make_icon(ICON_MORE_VERT, 15, color))
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_playback_options_active(active)

    # ── Volume ────────────────────────────────────────────────────────────

    def _on_volume_slider(self, value: int):
        vol = value / 100.0
        self._volume = vol
        self._playback_settings.set_volume(vol)
        self.volume_changed.emit(vol)
        # Atualiza ícone
        if value == 0:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_MUTE, 15, PALETTE.text_muted))
        elif value < 50:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_LOW, 15, PALETTE.text_muted))
        else:
            self.vol_btn.setIcon(make_icon(ICON_VOLUME_HIGH, 15, PALETTE.text_muted))
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_volume(vol)

    def _toggle_mute(self):
        if self._muted:
            self._muted = False
            self.vol_slider.setValue(int(self._pre_mute_vol * 100))
        else:
            self._pre_mute_vol = self._volume
            self._muted = True
            self.vol_slider.setValue(0)

    # ── Timer mode ────────────────────────────────────────────────────────

    def _update_timer_preview(self, remaining: int) -> None:
        if self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT:
            preview = self._ensure_yearly_timer()
            preview.set_countdown(remaining, self._timer_total_secs)
            self.overlay_stack.setCurrentWidget(preview)
            return

        self.circular_timer.update_data(remaining, self._timer_total_secs)
        self.overlay_stack.setCurrentWidget(self.circular_timer)

    def _set_timer_preview_blink(self, on: bool) -> None:
        if self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT:
            self._ensure_yearly_timer().set_countdown_blink(on)
        else:
            self.circular_timer.set_blink(on)

    def _update_timer_bar_label(self, remaining: int):
        if self._timer_presentation is MediaCountdownPresentation.YEARLY_TEXT:
            text = format_fixed_countdown(remaining, self._timer_total_secs)
        else:
            remaining = max(0, remaining)
            hours, remainder = divmod(remaining, 3600)
            minutes, seconds = divmod(remainder, 60)
            text = (
                f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                if hours
                else f"{minutes:02d}:{seconds:02d}"
            )
        self.timer_countdown_label.setText(text)

    def _on_timer_tick(self):
        if not self._timer_target:
            return
        remaining = ceil_remaining_seconds(self._timer_target)
        window = self.window()
        if window is None or not window.isMinimized():
            self._update_timer_preview(remaining)
            self._update_timer_bar_label(remaining)
        self.timer_updated.emit(remaining, self._timer_total_secs)
        if remaining <= 0:
            self._timer_tick.stop()
            self._start_blink_sequence()

    def _start_blink_sequence(self):
        self._blink_count = 0
        self._blink_on = False
        self._timer_blink_timer.start()
        self._timer_auto_close.start()

    def _on_blink_tick(self):
        self._blink_on = not self._blink_on
        self._set_timer_preview_blink(self._blink_on)
        self.timer_blink.emit(self._blink_on)
        color = PALETTE.danger if self._blink_on else PALETTE.accent
        self.timer_countdown_label.setStyleSheet(
            f"background: transparent; color: {color}; font-size: 18px;"
            " font-weight: 700; letter-spacing: 1px; min-width: 90px;"
        )

    def _auto_close_timer(self):
        if self._mode != "timer":
            return
        self._timer_blink_timer.stop()
        self._set_timer_preview_blink(False)
        self.stop_requested.emit()

    def _stop_timer_internals(self):
        self._timer_tick.stop()
        self._timer_blink_timer.stop()
        self._timer_auto_close.stop()
        self._timer_target = None
        self._timer_presentation = None
        self._blink_on = False
        self._blink_count = 0
        self.circular_timer.set_blink(False)
        if self.yearly_timer is not None:
            self.yearly_timer.clear_countdown()

    # ── Callbacks de mídia ────────────────────────────────────────────────

    @Slot(QMediaPlayer.PlaybackState)
    def _on_state_changed(self, state):
        playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        icon = ICON_PAUSE if playing else ICON_PLAY
        self.play_btn.setIcon(make_icon(icon, 15, PALETTE.text_secondary))
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_playback_state(state)
        self._sync_protected_media_controls()

    @Slot(int)
    def _on_duration_changed(self, duration: int):
        self.seek_slider.setRange(0, duration)
        self._update_time_label(self.media.position, duration)
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_duration(duration)
            overlay.set_position(self.media.position, duration)

    @Slot(int)
    def _on_source_duration_changed(self, duration: int):
        if duration <= 0:
            return
        item = self.current_playlist_item()
        if not item:
            return
        item_id = str(item.get("id") or "")
        if item_id:
            # Publish the original source duration. Persistence belongs to the
            # playlist owner, not to the projection UI.
            self.source_duration_discovered.emit(item_id, duration)

    @Slot(int)
    def _on_position_changed(self, position: int):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self._update_time_label(position, self.media.duration)
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_position(position, self.media.duration)
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
                self.play_next_requested.emit(
                    {"url": "__replay__", "title": "", "type": "video"}
                )
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
            self.play_next_requested.emit(dict(item))

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
            next_idx = random.choice(remaining)
            self._playlist_index = next_idx
            self._played_indices.add(next_idx)
            self._update_nav_buttons()
            item = self._playlist[next_idx]
            self.play_next_requested.emit(dict(item))

    @Slot(int, int)
    def _on_buffer_progress(self, downloaded: int, total: int):
        self._last_buffer_progress = (int(downloaded), int(total))
        if total > 0:
            self.seek_slider.setBufferedRatio(downloaded / total)
        else:
            self.seek_slider.setBufferedRatio(0.0)
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            overlay.set_buffer_progress(downloaded, total)

    def _update_time_label(self, pos: int, dur: int):
        self.time_label.setText(f"{self._fmt(pos)} / {self._fmt(dur)}")

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60}:{s % 60:02d}"
