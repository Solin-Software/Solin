"""
SermonThemeWidget — painel de controle para projetar o tema do discurso.
Projeta como uma imagem estática: o botão "Projetar" envia o snapshot atual.
Alterações no campo de texto NÃO afetam o que está projetado até o próximo clique.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QTextEdit, QSizePolicy, QGraphicsDropShadowEffect,
    QLineEdit,
)
from PySide6.QtCore import Qt, Signal, QByteArray, QRectF, QRect, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics
from PySide6.QtSvg import QSvgRenderer

from ..core.i18n.manager import LanguageManager
from ..styles.icons import ICON_NAV_SETTINGS, make_icon
from ..styles.theme import PALETTE

# ── SVG background — faithful copy of the reference design ──────────────────
_BG_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 576" width="100%" height="100%">
  <defs>
    <linearGradient id="mainBgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#f8fbf9"/>
      <stop offset="100%" stop-color="#f2f8f5"/>
    </linearGradient>
    <linearGradient id="topRightBgGradient" x1="100%" y1="0%" x2="50%" y2="50%">
      <stop offset="0%" stop-color="#d8e6dd" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="waveBgGradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#cae9d9"/>
      <stop offset="100%" stop-color="#b5dec7"/>
    </linearGradient>
    <linearGradient id="waveFgGradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#b6e1cd"/>
      <stop offset="100%" stop-color="#a3d4bd"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="576" fill="url(#mainBgGrad)"/>
  <rect width="512" height="288" x="512" y="0" fill="url(#topRightBgGradient)"/>
  <path d="M 0,0 L 450,0 C 380,120 200,100 0,160 Z"
        fill="url(#waveBgGradient)" opacity="0.7"/>
  <path d="M 0,0 L 320,0 C 250,80 150,60 0,140 Z"
        fill="url(#waveFgGradient)" opacity="0.9"/>
  <path d="M 0,130 C 40,125 80,105 110,75 C 130,55 150,25 170,10 C 190,-5 205,-10 215,-5"
        fill="none" stroke="#85c3a4" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 0,400 C 100,400 180,470 250,576 L 0,576 Z"
        fill="#def2e6" opacity="0.8"/>
  <rect x="0" y="335" width="48" height="28" fill="#a2d8d3"/>
  <path d="M 0,265 C 35,265 48,290 48,335 L 0,335 Z" fill="#ceeadd"/>
  <path d="M 46,265 C -5,255 -5,195 0,185 C 25,195 40,230 46,265 Z" fill="#99d2ad"/>
  <path d="M 47,268 C 65,230 85,210 105,215 C 85,245 65,270 47,268 Z" fill="#99d2ad"/>
  <path d="M 52,205 C 55,165 80,155 95,163 C 75,190 65,210 52,205 Z" fill="#9ad3cd"/>
  <path d="M 48,335 C 70,300 95,280 105,285 C 85,315 65,340 48,335 Z" fill="#9ad3cd"/>
  <path d="M 0,410 C 35,410 65,395 85,370 C 95,355 105,365 95,380 C 88,390 80,380 85,370"
        fill="none" stroke="#85c3a4" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="85" cy="370" r="6.5" fill="#c7ebdb"/>
</svg>
"""


class SermonThemePreview(QWidget):
    """Live mini-preview inside the control panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme_text = ""
        self._subtitle = "DISCURSO PÚBLICO"
        self._renderer = QSvgRenderer(QByteArray(_BG_SVG.encode()))
        self.setMinimumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_theme(self, text: str):
        self._theme_text = text
        self.update()

    def set_subtitle(self, subtitle: str):
        self._subtitle = subtitle
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w = self.width()
        h = self.height()

        self._renderer.render(p, QRectF(0, 0, w, h))

        if not self._theme_text:
            hint_font = QFont()
            hint_font.setPixelSize(max(11, int(h * 0.08)))
            hint_font.setItalic(True)
            p.setFont(hint_font)
            p.setPen(QColor(90, 130, 90, 110))
            p.drawText(
                QRectF(w * 0.18, 0, w * 0.75, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Digite o tema…",
            )
            p.end()
            return

        text_x = int(w * 0.175)
        text_w = int(w * 0.73)

        # ── Available vertical space ──────────────────────────────────────
        avail_top    = h * 0.08
        avail_bottom = h * 0.92
        avail_h      = avail_bottom - avail_top
        gap          = h * 0.05   # fixed gap between title bottom and subtitle

        # ── Find largest font size where (title + gap + subtitle) fits ────
        max_size = max(13, int(h * 0.20))
        min_size = max(7,  int(h * 0.04))

        title_font = sub_font = None
        title_h = sub_h = 0

        for size in range(max_size, min_size - 1, -1):
            tf = QFont()
            tf.setFamily("Arial")
            tf.setBold(True)
            tf.setPixelSize(size)

            sub_px = max(min_size, int(size * 0.40))
            sf = QFont()
            sf.setFamily("Arial")
            sf.setPixelSize(sub_px)
            sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)

            t_rect = QFontMetrics(tf).boundingRect(
                QRect(0, 0, text_w, 10000),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                self._theme_text,
            )
            t_h = t_rect.height()
            s_h = QFontMetrics(sf).height()

            if t_h + gap + s_h <= avail_h:
                title_font, sub_font = tf, sf
                title_h, sub_h = t_h, s_h
                break

        # Fallback: use minimum size even if it overflows
        if title_font is None:
            title_font = QFont()
            title_font.setFamily("Arial")
            title_font.setBold(True)
            title_font.setPixelSize(min_size)
            sub_px = max(min_size, int(min_size * 0.40))
            sub_font = QFont()
            sub_font.setFamily("Arial")
            sub_font.setPixelSize(sub_px)
            sub_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
            t_rect = QFontMetrics(title_font).boundingRect(
                QRect(0, 0, text_w, 10000),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                self._theme_text,
            )
            title_h = t_rect.height()
            sub_h   = QFontMetrics(sub_font).height()

        # ── Vertically centre the whole block inside the available band ───
        block_h = title_h + gap + sub_h
        start_y = avail_top + (avail_h - block_h) / 2

        # Draw title (top-aligned within its measured rect)
        p.setFont(title_font)
        p.setPen(QColor("#3A4E42"))
        p.drawText(
            QRectF(text_x, start_y, text_w, title_h + 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            self._theme_text,
        )

        # Draw subtitle immediately below title + gap
        sub_y = start_y + title_h + gap
        p.setFont(sub_font)
        p.setPen(QColor("#5a7a62"))
        p.drawText(
            QRectF(text_x, sub_y, text_w, sub_h + 4),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._subtitle,
        )

        p.end()


class SermonThemeWidget(QWidget):
    """
    Control panel for the talk theme feature.

    Clicking 'Project' sends a snapshot — editing afterwards does NOT update
    the projector. The global projection bar handles stopping.
    """

    project_theme_signal = Signal(str, str)  # (theme_text, subtitle)

    def __init__(self, lang: "LanguageManager | None" = None, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(0)

        self.title_label = QLabel()
        self.title_label.setObjectName("SectionTitle")
        outer.addWidget(self.title_label)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("SectionSubtitle")
        self.subtitle_label.setStyleSheet(
            f"padding-bottom: 20px; color: {PALETTE.text_muted}; font-size: 12px; background: transparent;"
        )
        outer.addWidget(self.subtitle_label)

        self.theme_card = QFrame()
        self.theme_card.setObjectName("ThemeCard")
        card_lay = QVBoxLayout(self.theme_card)
        card_lay.setContentsMargins(24, 24, 24, 24)
        card_lay.setSpacing(16)

        self.preview_label = QLabel()
        self.preview_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        card_lay.addWidget(self.preview_label)

        self.preview = SermonThemePreview()
        self.preview.setStyleSheet("border-radius: 10px")
        self._preview_shadow = QGraphicsDropShadowEffect()
        self._preview_shadow.setBlurRadius(14)
        self._preview_shadow.setOffset(0, 3)
        self.preview.setGraphicsEffect(self._preview_shadow)
        card_lay.addWidget(self.preview)

        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        card_lay.addWidget(self.separator)

        self.input_label = QLabel()
        self.input_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        card_lay.addWidget(self.input_label)

        self.theme_input = QTextEdit()
        self.theme_input.setObjectName("ThemeInput")
        self.theme_input.setMaximumHeight(90)
        self.theme_input.setMinimumHeight(60)
        self.theme_input.setStyleSheet(f"""
            QTextEdit#ThemeInput {{
                background: {PALETTE.bg0}; border: 1px solid {PALETTE.border}; border-radius: 8px;
                color: {PALETTE.text_primary}; font-size: 14px; padding: 10px 12px;
                selection-background-color: {PALETTE.accent_selection};
            }}
            QTextEdit#ThemeInput:focus {{ border-color: {PALETTE.accent}; }}
        """)
        self.theme_input.textChanged.connect(self._on_text_changed)
        card_lay.addWidget(self.theme_input)

        self.project_btn = QPushButton()
        self.project_btn.setObjectName("ProjectThemeBtn")
        self.project_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project_btn.setMinimumHeight(40)
        self.project_btn.setStyleSheet(f"""
            QPushButton#ProjectThemeBtn {{
                background: {PALETTE.projection}; border: 1px solid {PALETTE.success}; border-radius: 8px;
                color: {PALETTE.text_on_accent}; font-size: 13px; font-weight: 600; padding: 0 20px;
            }}
            QPushButton#ProjectThemeBtn:hover   {{ background: {PALETTE.success}; }}
            QPushButton#ProjectThemeBtn:pressed  {{ background: {PALETTE.success_pressed}; }}
            QPushButton#ProjectThemeBtn:disabled {{
                background: {PALETTE.bg2}; border-color: {PALETTE.border}; color: {PALETTE.text_dim};
            }}
        """)
        self.project_btn.clicked.connect(self._on_project)

        # Row: project button + settings gear icon
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addWidget(self.project_btn)

        self.settings_toggle_btn = QPushButton()
        self.settings_toggle_btn.setObjectName("SettingsToggleBtn")
        self.settings_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_toggle_btn.setFixedSize(36, 36)
        self.settings_toggle_btn.setIcon(make_icon(ICON_NAV_SETTINGS, size=16, color=PALETTE.text_muted))
        from PySide6.QtCore import QSize
        self.settings_toggle_btn.setIconSize(QSize(16, 16))
        self.settings_toggle_btn.setToolTip("Configurações de projeção")
        self.settings_toggle_btn.setCheckable(True)
        self.settings_toggle_btn.setStyleSheet(f"""
            QPushButton#SettingsToggleBtn {{
                background: transparent;
                border: 1px solid {PALETTE.border};
                border-radius: 8px;
                padding: 0;
            }}
            QPushButton#SettingsToggleBtn:hover   {{ background: {PALETTE.bg2}; border-color: {PALETTE.text_dim}; }}
            QPushButton#SettingsToggleBtn:checked  {{ background: {PALETTE.bg2}; border-color: {PALETTE.accent}; }}
        """)
        self.settings_toggle_btn.clicked.connect(self._toggle_subtitle_panel)
        btn_row.addWidget(self.settings_toggle_btn)
        btn_row.addStretch()
        card_lay.addLayout(btn_row)

        # ── Collapsible subtitle panel ────────────────────────────────────
        self._subtitle_panel_expanded = False
        self.subtitle_panel = QFrame()
        self.subtitle_panel.setObjectName("SubtitlePanel")
        self.subtitle_panel.setStyleSheet(f"""
            QFrame#SubtitlePanel {{
                background: {PALETTE.bg0};
                border: 1px solid {PALETTE.border};
                border-radius: 8px;
            }}
        """)
        self.subtitle_panel.setMaximumHeight(0)
        self.subtitle_panel.setMinimumHeight(0)
        self.subtitle_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        panel_lay = QVBoxLayout(self.subtitle_panel)
        panel_lay.setContentsMargins(14, 12, 14, 12)
        panel_lay.setSpacing(8)

        self.sub_prefix_label = QLabel()
        self.sub_prefix_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        panel_lay.addWidget(self.sub_prefix_label)

        self.subtitle_input = QLineEdit()
        self.subtitle_input.setObjectName("SubtitleInput")
        self.subtitle_input.setStyleSheet(f"""
            QLineEdit#SubtitleInput {{
                background: {PALETTE.surface};
                border: 1px solid {PALETTE.border};
                border-radius: 6px;
                color: {PALETTE.text_primary};
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 1px;
                padding: 6px 10px;
            }}
            QLineEdit#SubtitleInput:focus {{ border-color: {PALETTE.accent}; }}
        """)
        self.subtitle_input.textChanged.connect(self._on_subtitle_changed)
        panel_lay.addWidget(self.subtitle_input)

        card_lay.addWidget(self.subtitle_panel)

        # Animation for expand/collapse
        self._panel_anim = QPropertyAnimation(self.subtitle_panel, b"maximumHeight")
        self._panel_anim.setDuration(200)
        self._panel_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._panel_expanded_height = 90  # approximate

        outer.addWidget(self.theme_card)
        outer.addStretch()

        self.apply_theme()
        self.retranslateUi()
        self._update_project_btn()

    def _on_text_changed(self):
        self.preview.set_theme(self.theme_input.toPlainText())
        self._update_project_btn()

    def _update_project_btn(self):
        self.project_btn.setEnabled(bool(self.theme_input.toPlainText().strip()))

    def _on_subtitle_changed(self, text: str):
        self.preview.set_subtitle(text.upper() if text else self.tr("PUBLIC TALK"))

    def _toggle_subtitle_panel(self, checked: bool):
        self._subtitle_panel_expanded = checked
        self._panel_anim.stop()
        if checked:
            self._panel_anim.setStartValue(0)
            self._panel_anim.setEndValue(self._panel_expanded_height)
        else:
            self._panel_anim.setStartValue(self._panel_expanded_height)
            self._panel_anim.setEndValue(0)
        self._panel_anim.start()

    def _on_project(self):
        text = self.theme_input.toPlainText().strip()
        if text:
            subtitle = self.subtitle_input.text().strip() or self.tr("PUBLIC TALK")
            self.project_theme_signal.emit(text, subtitle)

    def apply_theme(self) -> None:
        self.subtitle_label.setStyleSheet(
            f"padding-bottom: 20px; color: {PALETTE.text_muted}; font-size: 12px; background: transparent;"
        )
        self.theme_card.setStyleSheet(
            f"#ThemeCard {{ background: {PALETTE.surface}; border: 1px solid {PALETTE.border}; border-radius: 16px; }}"
        )
        self.preview_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        self._preview_shadow.setColor(QColor(0, 0, 0, 70))
        self.separator.setStyleSheet(
            f"color: {PALETTE.border}; background: {PALETTE.border}; margin: 4px 0;"
        )
        self.input_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        self.theme_input.setStyleSheet(f"""
            QTextEdit#ThemeInput {{
                background: {PALETTE.bg0}; border: 1px solid {PALETTE.border}; border-radius: 8px;
                color: {PALETTE.text_primary}; font-size: 14px; padding: 10px 12px;
                selection-background-color: {PALETTE.accent_selection};
            }}
            QTextEdit#ThemeInput:focus {{ border-color: {PALETTE.accent}; }}
        """)
        self.project_btn.setStyleSheet(f"""
            QPushButton#ProjectThemeBtn {{
                background: {PALETTE.projection}; border: 1px solid {PALETTE.success}; border-radius: 8px;
                color: {PALETTE.text_on_accent}; font-size: 13px; font-weight: 600; padding: 0 20px;
            }}
            QPushButton#ProjectThemeBtn:hover   {{ background: {PALETTE.success}; }}
            QPushButton#ProjectThemeBtn:pressed  {{ background: {PALETTE.success_pressed}; }}
            QPushButton#ProjectThemeBtn:disabled {{
                background: {PALETTE.bg2}; border-color: {PALETTE.border}; color: {PALETTE.text_dim};
            }}
        """)
        self.settings_toggle_btn.setIcon(
            make_icon(ICON_NAV_SETTINGS, size=16, color=PALETTE.text_muted)
        )
        self.settings_toggle_btn.setStyleSheet(f"""
            QPushButton#SettingsToggleBtn {{
                background: transparent;
                border: 1px solid {PALETTE.border};
                border-radius: 8px;
                padding: 0;
            }}
            QPushButton#SettingsToggleBtn:hover   {{ background: {PALETTE.bg2}; border-color: {PALETTE.text_dim}; }}
            QPushButton#SettingsToggleBtn:checked  {{ background: {PALETTE.bg2}; border-color: {PALETTE.accent}; }}
        """)
        self.subtitle_panel.setStyleSheet(f"""
            QFrame#SubtitlePanel {{
                background: {PALETTE.bg0};
                border: 1px solid {PALETTE.border};
                border-radius: 8px;
            }}
        """)
        self.sub_prefix_label.setStyleSheet(
            f"background: transparent; color: {PALETTE.text_muted}; font-size: 11px; font-weight: 600;"
        )
        self.subtitle_input.setStyleSheet(f"""
            QLineEdit#SubtitleInput {{
                background: {PALETTE.surface};
                border: 1px solid {PALETTE.border};
                border-radius: 6px;
                color: {PALETTE.text_primary};
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 1px;
                padding: 6px 10px;
            }}
            QLineEdit#SubtitleInput:focus {{ border-color: {PALETTE.accent}; }}
        """)

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self.title_label.setText(self.tr("Talk theme"))
        self.subtitle_label.setText(self.tr("Project the talk title on the secondary screen"))
        self.preview_label.setText(self.tr("PREVIEW"))
        self.input_label.setText(self.tr("Talk theme"))
        self.theme_input.setPlaceholderText(self.tr("E.g.: Imitate Jehovah's mercy"))
        self.sub_prefix_label.setText(self.tr("Subtitle:"))

        new_default = self.tr("PUBLIC TALK")
        current_text = self.subtitle_input.text()

        # Atualiza campo se: vazio, ou ainda igual ao default do idioma anterior
        if not current_text or current_text == getattr(self, "_subtitle_default", current_text):
            self.subtitle_input.setText(new_default)

        self._subtitle_default = new_default
        self.subtitle_input.setPlaceholderText(new_default)
        self.preview.set_subtitle(self.subtitle_input.text() or new_default)
        self.project_btn.setText(self.tr("Project Theme"))

