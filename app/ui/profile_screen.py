"""
profile_screen.py — Solin
==========================
Tela de perfis do Solin. Cobre três cenários:

  1. Primeiro uso (sem configs)      → Onboarding completo (nome → idiomas → OBS)
  2. Dados legados (sem perfis)      → Migração: pede nome, migra dados
  3. Múltiplos perfis configurados   → Seletor estilo Netflix

Emite o sinal `profile_ready(profile_id)` quando um perfil está selecionado
e pronto para iniciar o MainWindow.
"""
from __future__ import annotations

from PySide6.QtCore import (
    Qt, Signal, QTimer,
    QEvent, QT_TRANSLATE_NOOP,
)
from PySide6.QtGui import QKeyEvent, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFrame, QScrollArea,
    QStackedWidget, QMenu, QDialog, QMessageBox,
)

from ..core.foundation.constants import QSETTINGS_APP_APP, QSETTINGS_PREFS_APP
from ..core.foundation.settings_keys import SettingsKey
from ..core.ui.helpers import fade_in as _fade_in
from ..core.profiles.manager import get as _get_pm
from ..widgets.common import NoScrollComboBox as _NoScrollComboBox
from ..styles.icons import (
    make_icon,
    ICON_EDIT, ICON_TRASH,
    ICON_NAV_BROWSER, ICON_BOOK,
    ICON_CLAPPERBOARD, ICON_PLUG, ICON_OBS,
    ICON_PACKAGE,
)

from .profile_obs_setup import ProfileOBSSetupMixin
from .profile_widgets import (
    AddProfileCard,
    ProfileCard,
    StepProgress,
    _ACCENT,
    _BG,
    _BORDER,
    _DIM,
    _FlowLayout,
    _MUTED,
    _OBSToggle,
    _ProfileNameDialog,
    _RED,
    _SCROLLBAR_SS,
    _SURF,
    _TEXT,
    _btn,
    _field,
    _lbl,
    _obs_combo_style,
    _obs_field_style,
    _section_card,
)

# ══════════════════════════════════════════════════════════════════════════════
# ProfileScreen — widget raiz
# ══════════════════════════════════════════════════════════════════════════════

class ProfileScreen(ProfileOBSSetupMixin, QWidget):
    """
    Tela de gerenciamento de perfis exibida ANTES do MainWindow.

    Signals
    -------
    profile_ready(str)
        Emitido com o profile_id quando o perfil está pronto para abrir
        o MainWindow.
    """

    profile_ready = Signal(str)

    def __init__(self, lang_manager=None, parent=None):
        super().__init__(parent)
        self._lang = lang_manager
        self._pm   = _get_pm()
        self._creating_additional_profile = False
        self._ob_cancel_buttons: list[QPushButton] = []
        self._tr_labels: list[tuple[QLabel, str]] = []
        self._tr_buttons: list[tuple[QPushButton, str]] = []
        self._tr_placeholders: list[tuple[QLineEdit, str]] = []
        self._tr_sections: list[tuple[QFrame, str, str]] = []
        self._onboard_headers: list[tuple[QLabel, str, QLabel, str]] = []
        self._ob_skip_buttons: list[QPushButton] = []
        self._init_ob_obs_setup()

        self.setStyleSheet(f"background: {_BG}; color: {_TEXT};")
        self.setMinimumSize(800, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        # ── Páginas ───────────────────────────────────────────────────────
        self._page_selector  = self._build_selector_page()
        self._page_onboard   = self._build_onboard_pages()   # QStackedWidget interno
        self._page_migration = self._build_migration_page()

        self._stack.addWidget(self._page_selector)   # 0
        self._stack.addWidget(self._page_onboard)    # 1
        self._stack.addWidget(self._page_migration)  # 2

        self._reset_onboarding_defaults()
        if self._lang:
            self._lang.language_changed.connect(lambda _code: self.retranslateUi())

    def _tr_label(self, label: QLabel, source: str) -> QLabel:
        label.setText(self.tr(source))
        self._tr_labels.append((label, source))
        return label

    def _tr_button(self, button: QPushButton, source: str) -> QPushButton:
        button.setText(self.tr(source))
        self._tr_buttons.append((button, source))
        return button

    def _tr_placeholder(self, field: QLineEdit, source: str) -> QLineEdit:
        field.setPlaceholderText(self.tr(source))
        self._tr_placeholders.append((field, source))
        return field

    def _register_section(self, card: QFrame, title_source: str, desc_source: str) -> QFrame:
        self._tr_sections.append((card, title_source, desc_source))
        return card

    # ── Seletor de perfil (Netflix) ────────────────────────────────────────

    def _build_selector_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {_BG};")
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(72)
        header.setStyleSheet(f"background: {_SURF}; border-bottom: 1px solid #21262d;")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(32, 0, 32, 0)
        logo = QLabel("Solin")
        logo.setStyleSheet("color: #e6edf3; font-size: 22px; font-weight: 700; background: transparent;")
        hlay.addWidget(logo)
        hlay.addStretch()
        lay.addWidget(header)

        # Body
        body = QWidget()
        blay = QVBoxLayout(body)
        blay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        blay.setContentsMargins(40, 60, 40, 40)
        blay.setSpacing(0)

        title = _lbl("", 28, _TEXT, 700, Qt.AlignmentFlag.AlignHCenter)
        self._selector_title = self._tr_label(
            title,
            QT_TRANSLATE_NOOP("ProfileScreen", "Who is using Solin?"),
        )
        blay.addWidget(title)
        blay.addSpacing(40)

        # Grid de cards
        self._selector_grid = QWidget()
        self._grid_lay = _FlowLayout(self._selector_grid, h_spacing=18, v_spacing=18)
        blay.addWidget(self._selector_grid, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(body, 1)

        return page

    def _populate_selector(self) -> None:
        """Preenche o grid com os perfis atuais."""
        # Limpa
        while self._grid_lay.count():
            item = self._grid_lay.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for p in self._pm.profiles:
            card = ProfileCard(p)
            card.clicked.connect(self._on_profile_selected)
            card.context_requested.connect(self._show_profile_context_menu)
            self._grid_lay.addWidget(card)

        add_card = AddProfileCard(self.tr("New Profile"))
        self._selector_add_card = add_card
        add_card.clicked.connect(self._on_new_profile_clicked)
        self._grid_lay.addWidget(add_card)

    # ── Onboarding ─────────────────────────────────────────────────────────

    def _reset_onboarding_defaults(self) -> None:
        iface_code = self._lang.current_code if self._lang else "en"
        self._ob_iface_selected_code = iface_code
        self._ob_media_selected_code = self._interface_api_code(iface_code) or "E"
        self._ob_media_touched = False
        if hasattr(self, "_ob_name_error"):
            self._ob_name_error.hide()
        if hasattr(self, "_ob_iface_btn"):
            self._refresh_ob_language_buttons()
        if hasattr(self, "_ob_obs_toggle") and self._ob_obs_toggle.is_checked:
            self._ob_obs_toggle.set_checked(False)
            self._ob_obs_fields.hide()
        self._ob_obs_teardown()

    def _set_onboarding_cancel_visible(self, visible: bool) -> None:
        for btn in self._ob_cancel_buttons:
            btn.setVisible(visible)

    def _cancel_onboarding_profile_creation(self) -> None:
        if not self._pm.has_profiles():
            return
        self._creating_additional_profile = False
        self._set_onboarding_cancel_visible(False)
        self._reset_onboarding_defaults()
        self._populate_selector()
        self._stack.setCurrentIndex(0)

    def _build_onboard_pages(self) -> QStackedWidget:
        """Retorna um QStackedWidget com as 3 etapas do onboarding."""
        stack = QStackedWidget()
        stack.setStyleSheet(f"background: {_BG};")

        self._ob_steps = []
        self._ob_dots  = None    # criado abaixo

        step0 = self._build_ob_step0()
        step1 = self._build_ob_step1()
        step2 = self._build_ob_step2()
        stack.addWidget(step0)  # 0 — nome do perfil
        stack.addWidget(step1)  # 1 — idiomas
        stack.addWidget(step2)  # 2 — OBS

        self._ob_stack = stack
        return stack

    # ── Onboard shell ──────────────────────────────────────────────────────

    def _onboard_shell(self, step: int, title: str, subtitle: str,
                       body: QWidget, show_skip: bool = True) -> QWidget:
        """Frame padrão para cada etapa do onboarding — estilo premium."""
        page = QWidget()
        page.setStyleSheet(f"background: {_BG};")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ─────────────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(56)
        header.setStyleSheet(
            f"background: {_SURF}; border-bottom: 1px solid #21262d;"
        )
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(32, 0, 32, 0)
        logo = QLabel("Solin")
        logo.setStyleSheet(
            "color: #e6edf3; font-size: 17px; font-weight: 700; "
            "background: transparent;"
        )
        hlay.addWidget(logo)
        hlay.addStretch(1)

        # Step progress dots — centrado no header
        dots = StepProgress(3)
        dots.set_step(step)
        hlay.addWidget(dots)
        hlay.addStretch(1)

        if not hasattr(self, "_ob_dot_widgets"):
            self._ob_dot_widgets = []
        self._ob_dot_widgets.append(dots)

        # Step counter — sem pill, integrado ao fundo do header
        step_counter = QLabel(f"{step + 1} / 3")
        step_counter.setStyleSheet(
            f"color: {_MUTED}; font-size: 11px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        hlay.addWidget(step_counter)
        lay.addWidget(header)

        # ── Content ────────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setStyleSheet(_SCROLLBAR_SS)

        inner = QWidget()
        inner.setStyleSheet(f"background: {_BG};")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(56, 44, 56, 40)
        ilay.setSpacing(0)
        ilay.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Title
        t = _lbl(
            self.tr(title), 26, _TEXT, 700,
            Qt.AlignmentFlag.AlignHCenter,
        )
        ilay.addWidget(t)
        ilay.addSpacing(8)

        # Subtitle
        s = _lbl(
            self.tr(subtitle), 13, _MUTED, 400,
            Qt.AlignmentFlag.AlignHCenter,
        )
        ilay.addWidget(s)
        ilay.addSpacing(32)
        self._onboard_headers.append((t, title, s, subtitle))

        ilay.addWidget(body)

        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        # ── Footer ─────────────────────────────────────────────────────
        footer = QWidget()
        footer.setFixedHeight(56)
        footer.setStyleSheet(
            f"background: {_SURF}; border-top: 1px solid #21262d;"
        )
        flay = QHBoxLayout(footer)
        flay.setContentsMargins(32, 0, 32, 0)

        cancel_btn = _btn(self.tr("Cancel"), primary=False)
        self._tr_buttons.append((cancel_btn, "Cancel"))
        cancel_btn.clicked.connect(self._cancel_onboarding_profile_creation)
        cancel_btn.hide()
        self._ob_cancel_buttons.append(cancel_btn)
        flay.addWidget(cancel_btn)
        flay.addStretch()

        if show_skip:
            skip_btn = QPushButton(self.tr("Skip setup"))
            skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            skip_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {_DIM};
                    border: none;
                    font-size: 12px;
                    padding: 0 8px;
                }}
                QPushButton:hover {{ color: {_MUTED}; }}
            """)
            skip_btn.clicked.connect(self._on_skip_setup)
            self._ob_skip_buttons.append(skip_btn)
            flay.addWidget(skip_btn)

        lay.addWidget(footer)
        return page

    # ── Step 0 — Profile name ──────────────────────────────────────────────

    def _build_ob_step0(self) -> QWidget:
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(14)

        self._ob_name_label = self._tr_label(
            _lbl("", 12, _MUTED, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Profile name"),
        )
        blay.addWidget(self._ob_name_label)
        self._ob_name_field = _field()
        self._tr_placeholder(
            self._ob_name_field,
            QT_TRANSLATE_NOOP("ProfileScreen", "Example: Congregation A, Name, \u2026"),
        )
        self._ob_name_field.setText(self.tr("Profile 1"))
        self._ob_name_field.selectAll()
        blay.addWidget(self._ob_name_field)

        self._ob_name_error = _lbl("", 12, _RED)
        self._ob_name_error.hide()
        blay.addWidget(self._ob_name_error)

        blay.addSpacing(8)

        cont_btn = _btn("")
        self._tr_button(
            cont_btn, QT_TRANSLATE_NOOP("ProfileScreen", "Continue \u2192")
        )
        cont_btn.clicked.connect(self._ob_step0_next)
        blay.addWidget(cont_btn)

        self._ob_name_field.returnPressed.connect(self._ob_step0_next)

        page = self._onboard_shell(
            0,
            QT_TRANSLATE_NOOP("ProfileScreen", "Welcome to Solin"),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Give this profile a name. Each profile keeps its own settings and "
                "playlists \u2014 great for managing different setups.",
            ),
            body,
            show_skip=False,
        )
        return page

    # ── Step 1 — Languages ─────────────────────────────────────────────────

    def _build_ob_step1(self) -> QWidget:
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(16)

        # Interface language
        iface_title = QT_TRANSLATE_NOOP("ProfileScreen", "Interface language")
        iface_desc = QT_TRANSLATE_NOOP(
            "ProfileScreen",
            "Controls menus, buttons, and all text displayed throughout the app.",
        )
        iface_card = _section_card(
            self.tr(iface_title),
            ICON_NAV_BROWSER,
            self.tr(iface_desc),
        )
        self._register_section(iface_card, iface_title, iface_desc)
        self._ob_iface_selected_code = (
            self._lang.current_code if self._lang else "en"
        )
        self._ob_iface_lang = self._build_lang_selector(interface=True)
        iface_card.layout().addWidget(self._ob_iface_lang)
        blay.addWidget(iface_card)

        # JW media language
        media_title = QT_TRANSLATE_NOOP("ProfileScreen", "Content language")
        media_desc = QT_TRANSLATE_NOOP(
            "ProfileScreen",
            "Language for songs, videos, and other media "
            "downloaded from JW.org."
        )
        media_card = _section_card(
            self.tr(media_title),
            ICON_BOOK,
            self.tr(media_desc),
        )
        self._register_section(media_card, media_title, media_desc)
        self._ob_media_selected_code = (
            self._interface_api_code(self._ob_iface_selected_code) or "E"
        )
        self._ob_media_lang = self._build_lang_selector(interface=False)
        media_card.layout().addWidget(self._ob_media_lang)
        blay.addWidget(media_card)

        blay.addSpacing(8)

        cont_btn = _btn("")
        self._tr_button(
            cont_btn, QT_TRANSLATE_NOOP("ProfileScreen", "Continue \u2192")
        )
        cont_btn.clicked.connect(self._ob_step1_next)
        blay.addWidget(cont_btn)

        page = self._onboard_shell(
            1,
            QT_TRANSLATE_NOOP("ProfileScreen", "Language preferences"),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Choose the language for the interface and for media content "
                "from JW.org.",
            ),
            body,
            show_skip=True,
        )
        return page

    def _build_lang_selector(self, interface: bool) -> QPushButton:
        btn = QPushButton()
        btn.setFixedHeight(44)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: #13161c;
                color: {_TEXT};
                border: 1.5px solid #21262d;
                border-radius: 10px;
                font-size: 13px;
                text-align: left;
                padding: 0 14px;
            }}
            QPushButton:hover {{ border-color: {_ACCENT}; background: #181d27; }}
            QPushButton:pressed {{ background: #0d1520; }}
        """)
        if interface:
            self._ob_iface_btn = btn
            btn.clicked.connect(self._open_ob_interface_picker)
        else:
            self._ob_media_btn = btn
            btn.clicked.connect(self._open_ob_media_picker)
            svc = self._lang.jw_lang_service if self._lang else None
            if svc:
                svc.languages_ready.connect(lambda _: self._refresh_ob_media_button())
                svc.fetch_if_needed()
        self._refresh_ob_language_buttons()
        return btn

    def _refresh_ob_language_buttons(self) -> None:
        if hasattr(self, "_ob_iface_btn"):
            code = getattr(self, "_ob_iface_selected_code", "pt_BR")
            name = self._interface_language_name(code)
            self._ob_iface_btn.setText(f"  {name}")#    ({code})")
        if hasattr(self, "_ob_media_btn"):
            self._refresh_ob_media_button()

    def _refresh_ob_media_button(self) -> None:
        if not hasattr(self, "_ob_media_btn"):
            return
        code = getattr(self, "_ob_media_selected_code", "T")
        name = self._media_language_name(code)
        self._ob_media_btn.setText(f"  {name}")#    ({code})")

    def _interface_language_items(self) -> list[tuple[str, str, str]]:
        if self._lang:
            return [
                (code, name, code)
                for code, name in sorted(self._lang.available_languages(), key=lambda x: x[1])
            ]
        return [
            ("pt_BR", "Português (Brasil)", "pt_BR"),
            ("en", "English", "en"),
        ]

    def _interface_language_name(self, code: str) -> str:
        for item_code, name, _ in self._interface_language_items():
            if item_code == code:
                return name
        return code

    def _interface_api_code(self, code: str) -> str:
        if self._lang:
            meta = getattr(self._lang, "languages", {}).get(code, {}).get("meta", {})
            return meta.get("api_code", "")
        return "T" if code == "pt_BR" else "E"

    def _media_language_items(self) -> list[tuple[str, str, str]]:
        svc = self._lang.jw_lang_service if self._lang else None
        if svc and svc.has_data:
            out: list[tuple[str, str, str]] = []
            for lang in sorted(
                svc.languages,
                key=lambda x: x.get("vernacular") or x.get("name") or x.get("code", ""),
            ):
                code = lang.get("code", "")
                if not code:
                    continue
                primary = lang.get("vernacular") or lang.get("name") or code
                secondary = lang.get("name") or code
                if lang.get("isRTL"):
                    secondary += "  [RTL]"
                out.append((code, primary, secondary))
            return out
        return [
            ("T", "Português", "T"),
            ("E", "English", "E"),
            ("S", "Español", "S"),
            ("I", "Italiano", "I"),
        ]

    def _media_language_name(self, code: str) -> str:
        svc = self._lang.jw_lang_service if self._lang else None
        if svc:
            lang = svc.get_language(code)
            if lang:
                return lang.get("vernacular") or lang.get("name") or code
        for item_code, primary, _ in self._media_language_items():
            if item_code == code:
                return primary
        return code

    def _open_ob_interface_picker(self) -> None:
        if not self._lang:
            return
        from ..widgets.settings_widget import create_interface_language_picker

        current = getattr(self, "_ob_iface_selected_code", "pt_BR")
        dlg = create_interface_language_picker(self._lang, parent=self, current_code=current)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._ob_iface_selected_code = dlg.chosen_code
            self._lang.set_language(dlg.chosen_code)
            if not getattr(self, "_ob_media_touched", False):
                api_code = self._interface_api_code(dlg.chosen_code)
                if api_code:
                    self._ob_media_selected_code = api_code
            self.retranslateUi()
            self._refresh_ob_language_buttons()

    def _open_ob_media_picker(self) -> None:
        svc = self._lang.jw_lang_service if self._lang else None
        if not svc:
            return
        from ..widgets.settings_widget import create_jw_language_picker

        current = getattr(self, "_ob_media_selected_code", "T")
        dlg = create_jw_language_picker(svc, current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._ob_media_touched = True
            self._ob_media_selected_code = dlg.chosen_code
            self._refresh_ob_language_buttons()

    # ── Step 2 — OBS Studio ────────────────────────────────────────────────

    def _build_ob_step2(self) -> QWidget:
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(14)

        # ── Intro ──────────────────────────────────────────────────────
        intro_source = QT_TRANSLATE_NOOP(
            "ProfileScreen",
            "Solin connects to OBS Studio via WebSocket to automatically "
            "switch scenes during media playback \u2014 songs, videos, and "
            "images are sent seamlessly to your live output."
        )
        intro = _lbl(self.tr(intro_source), 13, _MUTED)
        self._tr_labels.append((intro, intro_source))
        blay.addWidget(intro)

        # ── Card 1: Create your scenes ─────────────────────────────────
        scene_body = QWidget()
        scene_body.setStyleSheet("background: transparent;")
        s_lay = QVBoxLayout(scene_body)
        s_lay.setContentsMargins(0, 0, 0, 0)
        s_lay.setSpacing(6)

        scene_steps = [
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Create a main scene with your camera. For USB webcams, "
                "add a Video Capture Device source."
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "For IP cameras, add a Media Source and enter the RTSP "
                "stream URL of your camera."
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Create additional scenes for different framings "
                "(speaker, reader, stage) using Source \u2192 Scene, then "
                "adjust the crop and transform for each view."
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Create a separate media scene for Solin's projection. "
                "With the external monitor connected, add a Display Capture "
                "source and select the monitor where songs, videos, and "
                "images will appear."
            ),
        ]
        for i, text in enumerate(scene_steps, 1):
            row = QHBoxLayout()
            row.setSpacing(10)
            num = QLabel(str(i))
            num.setFixedSize(24, 24)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(
                "background: #1a2d4d; color: #79c0ff; border-radius: 12px; "
                "font-size: 11px; font-weight: 700; border: none;"
            )
            row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            step_lbl = self._tr_label(_lbl("", 12, _TEXT), text)
            row.addWidget(step_lbl, 1)
            s_lay.addLayout(row)

        # RTSP code example
        rtsp_frame = QFrame()
        rtsp_frame.setObjectName("RtspFrame")
        rtsp_frame.setStyleSheet(
            "QFrame#RtspFrame {"
            "  background: #13161c;"
            "  border: 1px solid #21262d;"
            "  border-radius: 10px;"
            "}"
        )
        rtsp_lay = QVBoxLayout(rtsp_frame)
        rtsp_lay.setContentsMargins(16, 12, 16, 12)
        rtsp_lay.setSpacing(6)

        rtsp_hint = QLabel(self.tr("RTSP URL example:"))
        rtsp_hint.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; font-weight: 600; "
            "letter-spacing: 0.5px; background: transparent; border: none;"
        )
        self._tr_labels.append((rtsp_hint, "RTSP URL example:"))
        rtsp_lay.addWidget(rtsp_hint)

        rtsp_url = QLabel(
            "rtsp://user:password@192.168.0.120:554/cam/"
            "realmonitor?channel=2&subtype=1"
        )
        rtsp_url.setWordWrap(True)
        rtsp_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        rtsp_url.setStyleSheet(
            f"color: {_ACCENT}; "
            "font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace; "
            "font-size: 11.5px; background: transparent; border: none;"
        )
        rtsp_lay.addWidget(rtsp_url)
        s_lay.addWidget(rtsp_frame)

        scene_title = QT_TRANSLATE_NOOP("ProfileScreen", "Set up your scenes")
        scene_desc = QT_TRANSLATE_NOOP(
            "ProfileScreen",
            "Configure your camera sources and create scene layouts "
            "in OBS before connecting."
        )
        scene_card = _section_card(
            self.tr(scene_title),
            ICON_CLAPPERBOARD,
            self.tr(scene_desc),
            scene_body,
        )
        self._register_section(scene_card, scene_title, scene_desc)
        blay.addWidget(scene_card)

        # ── Card 2: Enable WebSocket ───────────────────────────────────
        ws_body = QWidget()
        ws_body.setStyleSheet("background: transparent;")
        w_lay = QVBoxLayout(ws_body)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.setSpacing(6)

        ws_steps = [
            QT_TRANSLATE_NOOP("ProfileScreen", "Open OBS Studio"),
            QT_TRANSLATE_NOOP(
                "ProfileScreen", "Go to Tools \u2192 WebSocket Server Settings"
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen", "Check \u201cEnable WebSocket server\u201d"
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Confirm the port (default: 4455) and set a password if desired",
            ),
            QT_TRANSLATE_NOOP(
                "ProfileScreen", "Click Apply and restart OBS if prompted"
            ),
        ]
        for i, text in enumerate(ws_steps, 1):
            row = QHBoxLayout()
            row.setSpacing(10)
            num = QLabel(str(i))
            num.setFixedSize(24, 24)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(
                "background: #2a1a4d; color: #a371f7; border-radius: 12px; "
                "font-size: 11px; font-weight: 700; border: none;"
            )
            row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            step_lbl = self._tr_label(_lbl("", 12, _TEXT), text)
            row.addWidget(step_lbl, 1)
            w_lay.addLayout(row)

        ws_title = QT_TRANSLATE_NOOP("ProfileScreen", "Enable the WebSocket server")
        ws_desc = QT_TRANSLATE_NOOP(
            "ProfileScreen", "Solin communicates with OBS through this protocol."
        )
        ws_card = _section_card(
            self.tr(ws_title),
            ICON_PLUG,
            self.tr(ws_desc),
            ws_body,
        )
        self._register_section(ws_card, ws_title, ws_desc)
        blay.addWidget(ws_card)

        # ── Card 3: Connect Solin ──────────────────────────────────────
        conn_body = QWidget()
        conn_body.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(conn_body)
        c_lay.setContentsMargins(0, 4, 0, 0)
        c_lay.setSpacing(10)

        # Enable toggle row
        tog_row = QHBoxLayout()
        self._ob_obs_enable_lbl = self._tr_label(
            _lbl("", 13, _TEXT, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Enable OBS integration"),
        )
        tog_row.addWidget(self._ob_obs_enable_lbl)
        tog_row.addStretch()
        self._ob_obs_toggle = _OBSToggle(False)
        self._ob_obs_toggle.toggled.connect(self._on_ob_obs_toggled)
        tog_row.addWidget(self._ob_obs_toggle)
        c_lay.addLayout(tog_row)

        # ── Collapsible fields ─────────────────────────────────────────
        self._ob_obs_fields = QWidget()
        self._ob_obs_fields.setStyleSheet("background: transparent;")
        f_lay = QVBoxLayout(self._ob_obs_fields)
        f_lay.setContentsMargins(0, 0, 0, 0)
        f_lay.setSpacing(8)

        # Port
        f_lay.addWidget(self._tr_label(
            _lbl("", 12, _MUTED, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "WebSocket port"),
        ))
        self._ob_obs_port = _field("4455")
        self._ob_obs_port.setStyleSheet(_obs_field_style())
        self._ob_obs_port.setText("4455")
        self._ob_obs_port.textChanged.connect(self._ob_obs_field_changed)
        f_lay.addWidget(self._ob_obs_port)

        # Password
        f_lay.addWidget(self._tr_label(
            _lbl("", 12, _MUTED, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Password (optional)"),
        ))
        self._ob_obs_pwd = _field("")
        self._ob_obs_pwd.setStyleSheet(_obs_field_style())
        self._ob_obs_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._tr_placeholder(
            self._ob_obs_pwd,
            QT_TRANSLATE_NOOP("ProfileScreen", "Leave blank if no password is set"),
        )
        self._ob_obs_pwd.textChanged.connect(self._ob_obs_field_changed)
        f_lay.addWidget(self._ob_obs_pwd)

        # Connection status indicator
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._ob_obs_status_dot = QLabel("\u25cf")
        self._ob_obs_status_dot.setFixedWidth(14)
        self._ob_obs_status_dot.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; background: transparent; border: none;"
        )
        self._ob_obs_status_lbl = _lbl("", 12, _MUTED)
        self._ob_obs_status_lbl.setText(self.tr("Disconnected"))
        status_row.addWidget(self._ob_obs_status_dot)
        status_row.addWidget(self._ob_obs_status_lbl, 1)
        f_lay.addLayout(status_row)

        # ── Scene selectors (visible only when connected) ───────────────
        self._ob_obs_scenes_frame = QFrame()
        self._ob_obs_scenes_frame.setStyleSheet("background: transparent; border: none;")
        self._ob_obs_scenes_frame.setVisible(False)
        scenes_lay = QVBoxLayout(self._ob_obs_scenes_frame)
        scenes_lay.setContentsMargins(0, 4, 0, 0)
        scenes_lay.setSpacing(8)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #21262d; border: none;")
        scenes_lay.addWidget(sep)

        # Default / idle scene
        self._ob_obs_default_lbl = self._tr_label(
            _lbl("", 12, _TEXT, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Default scene (idle)"),
        )
        scenes_lay.addWidget(self._ob_obs_default_lbl)
        self._ob_obs_default_hint = self._tr_label(
            _lbl("", 11, _DIM),
            QT_TRANSLATE_NOOP(
                "ProfileScreen", "Scene shown when nothing is being projected."
            ),
        )
        scenes_lay.addWidget(self._ob_obs_default_hint)
        self._ob_obs_default_combo = _NoScrollComboBox()
        self._ob_obs_default_combo.setStyleSheet(_obs_combo_style())
        self._ob_obs_default_combo.currentTextChanged.connect(self._ob_obs_save_scenes)
        scenes_lay.addWidget(self._ob_obs_default_combo)

        # Media window scene
        self._ob_obs_media_lbl = self._tr_label(
            _lbl("", 12, _TEXT, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Media window scene"),
        )
        self._ob_obs_media_lbl.setContentsMargins(0, 4, 0, 0)
        scenes_lay.addWidget(self._ob_obs_media_lbl)
        self._ob_obs_media_hint = self._tr_label(
            _lbl("", 11, _DIM),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Scene that captures the projection monitor. "
                "Activated when content is displayed.",
            ),
        )
        self._ob_obs_media_hint.setWordWrap(True)
        scenes_lay.addWidget(self._ob_obs_media_hint)
        self._ob_obs_media_combo = _NoScrollComboBox()
        self._ob_obs_media_combo.setStyleSheet(_obs_combo_style())
        self._ob_obs_media_combo.currentTextChanged.connect(self._ob_obs_save_scenes)
        scenes_lay.addWidget(self._ob_obs_media_combo)

        f_lay.addWidget(self._ob_obs_scenes_frame)

        self._ob_obs_fields.hide()
        c_lay.addWidget(self._ob_obs_fields)

        conn_title = QT_TRANSLATE_NOOP("ProfileScreen", "Connect Solin to OBS")
        conn_desc = QT_TRANSLATE_NOOP(
            "ProfileScreen", "Enter the same port and password you configured in OBS."
        )
        conn_card = _section_card(
            self.tr(conn_title),
            ICON_OBS,
            self.tr(conn_desc),
            conn_body,
        )
        self._register_section(conn_card, conn_title, conn_desc)
        blay.addWidget(conn_card)

        blay.addSpacing(10)

        finish_btn = _btn("")
        self._tr_button(
            finish_btn, QT_TRANSLATE_NOOP("ProfileScreen", "Complete setup")
        )
        finish_btn.clicked.connect(self._ob_step2_finish)
        blay.addWidget(finish_btn)

        page = self._onboard_shell(
            2,
            QT_TRANSLATE_NOOP("ProfileScreen", "OBS Studio"),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Connect to OBS for automatic scene switching during "
                "presentations.",
            ),
            body,
            show_skip=True,
        )
        return page

    def _build_migration_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet(f"background: {_BG};")
        lay  = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setContentsMargins(60, 60, 60, 60)
        lay.setSpacing(0)

        # Card central
        card = QFrame()
        card.setMaximumWidth(480)
        card.setStyleSheet(f"""
            QFrame {{
                background: {_SURF};
                border: 1px solid #21262d;
                border-radius: 14px;
            }}
        """)
        clay = QVBoxLayout(card)
        clay.setContentsMargins(36, 36, 36, 36)
        clay.setSpacing(16)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(52, 52)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            "background: #13161c; border: 1px solid #21262d; "
            "border-radius: 14px;"
        )
        icon_lbl.setPixmap(make_icon(ICON_PACKAGE, size=24, color=_ACCENT).pixmap(24, 24))
        _wrap = QHBoxLayout()
        _wrap.addStretch()
        _wrap.addWidget(icon_lbl)
        _wrap.addStretch()
        clay.addLayout(_wrap)

        clay.addWidget(self._tr_label(
            _lbl("", 18, _TEXT, 700, Qt.AlignmentFlag.AlignHCenter),
            QT_TRANSLATE_NOOP("ProfileScreen", "Existing settings found"),
        ))
        clay.addSpacing(4)
        clay.addWidget(self._tr_label(
            _lbl("", 13, _MUTED, 400, Qt.AlignmentFlag.AlignHCenter),
            QT_TRANSLATE_NOOP(
                "ProfileScreen",
                "Solin found settings and playlists from a previous version. "
                "Name this profile to continue with your data:",
            ),
        ))

        clay.addSpacing(8)
        clay.addWidget(self._tr_label(
            _lbl("", 12, _MUTED, 500),
            QT_TRANSLATE_NOOP("ProfileScreen", "Profile name"),
        ))
        self._mig_name_field = _field()
        self._tr_placeholder(
            self._mig_name_field,
            QT_TRANSLATE_NOOP("ProfileScreen", "Example: Central Congregation"),
        )
        # self._mig_name_field.setText(self.tr("My Profile"))
        clay.addWidget(self._mig_name_field)

        self._mig_error = _lbl("", 12, _RED)
        self._mig_error.hide()
        clay.addWidget(self._mig_error)

        clay.addSpacing(4)
        confirm_btn = _btn("")
        self._tr_button(
            confirm_btn,
            QT_TRANSLATE_NOOP("ProfileScreen", "Confirm and migrate data"),
        )
        confirm_btn.clicked.connect(self._on_migrate_confirm)
        clay.addWidget(confirm_btn)

        self._mig_name_field.returnPressed.connect(self._on_migrate_confirm)

        lay.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        return page

    # ── Ponto de entrada público ───────────────────────────────────────────

    def start(self) -> None:
        """
        Decide qual tela mostrar baseado no estado do sistema.
        Deve ser chamado após ProfileManager.init().
        """
        pm = self._pm
        if not pm.has_profiles():
            if pm.has_legacy_settings():
                # Dados legados existem → migração
                self._stack.setCurrentIndex(2)
                _fade_in(self)
            else:
                # Primeira vez — onboarding
                self._creating_additional_profile = False
                self._set_onboarding_cancel_visible(False)
                self._reset_onboarding_defaults()
                self._ob_name_field.setText(self.tr("Profile 1"))
                self._ob_name_field.selectAll()
                self._ob_stack.setCurrentIndex(0)
                self._stack.setCurrentIndex(1)
                _fade_in(self)
                QTimer.singleShot(100, lambda: self._ob_name_field.setFocus())
        elif len(pm.profiles) == 1:
            # Apenas 1 perfil → entra direto (sem mostrar seletor)
            self._activate_and_emit(pm.profiles[0].id)
        else:
            # Múltiplos perfis → seletor
            self._populate_selector()
            self._stack.setCurrentIndex(0)
            _fade_in(self)

    # ── Handlers ──────────────────────────────────────────────────────────

    def _on_profile_selected(self, profile_id: str) -> None:
        self._activate_and_emit(profile_id)

    def _show_profile_context_menu(self, profile_id: str, global_pos) -> None:
        profile = self._pm.get_profile(profile_id)
        if not profile:
            return

        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {_SURF};
                color: {_TEXT};
                border: 1px solid {_BORDER};
                border-radius: 8px;
                padding: 6px;
                font-size: 13px;
            }}
            QMenu::item {{
                min-width: 150px;
                padding: 8px 14px 8px 10px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background: #21262d;
                color: {_TEXT};
            }}
            QMenu::separator {{
                height: 1px;
                background: {_BORDER};
                margin: 6px 8px;
            }}
        """)

        rename_action = QAction(make_icon(ICON_EDIT, 15, _MUTED), self.tr("Rename"), menu)
        delete_action = QAction(make_icon(ICON_TRASH, 15, _RED), self.tr("Delete"), menu)
        delete_action.setEnabled(len(self._pm.profiles) > 1)
        menu.addAction(rename_action)
        menu.addSeparator()
        menu.addAction(delete_action)

        action = menu.exec(global_pos)
        if action is rename_action:
            self._rename_profile(profile_id)
        elif action is delete_action:
            self._delete_profile(profile_id)

    def _rename_profile(self, profile_id: str) -> None:
        profile = self._pm.get_profile(profile_id)
        if not profile:
            return
        dlg = _ProfileNameDialog(
            current=profile.name,
            parent=self,
            title="Rename profile",
            label="Profile name",
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name = dlg.result_name()
            if new_name:
                self._pm.rename_profile(profile_id, new_name)
                self._populate_selector()

    def _delete_profile(self, profile_id: str) -> None:
        profile = self._pm.get_profile(profile_id)
        if not profile or len(self._pm.profiles) <= 1:
            return

        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Delete profile"))
        box.setText(self.tr('Delete "{name}"?').replace("{name}", profile.name))
        box.setInformativeText(
            self.tr(
                "This will delete playlists, images, received media, browser cache, "
                "and settings for this profile. This action cannot be undone."
            )
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet(f"""
            QMessageBox {{ background: {_SURF}; }}
            QLabel {{ color: {_TEXT}; font-size: 13px; background: transparent; }}
            QPushButton {{
                background: {_BG}; color: {_TEXT};
                border: 1px solid {_BORDER}; border-radius: 7px;
                padding: 7px 18px; min-width: 82px;
            }}
            QPushButton:hover {{ background: #21262d; }}
        """)
        if box.exec() == QMessageBox.StandardButton.Yes:
            if self._pm.delete_profile(profile_id):
                self._populate_selector()

    def _on_new_profile_clicked(self) -> None:
        self.start_new_profile()

    def start_new_profile(self) -> None:
        """Abre o onboarding para criar um perfil adicional."""
        if not self._pm.has_profiles():
            self.start()
            return
        existing = len(self._pm.profiles)
        self._populate_selector()
        self._creating_additional_profile = True
        self._set_onboarding_cancel_visible(True)
        self._reset_onboarding_defaults()
        self._ob_pending_name = ""
        self._ob_name_field.setText(
            self.tr("Profile {n}").replace("{n}", str(existing + 1))
        )
        self._ob_name_field.selectAll()
        self._ob_stack.setCurrentIndex(0)
        self._sync_ob_dots(0)
        self._stack.setCurrentIndex(1)
        QTimer.singleShot(100, lambda: self._ob_name_field.setFocus())

    def _on_migrate_confirm(self) -> None:
        name = self._mig_name_field.text().strip()
        if not name:
            self._mig_error.setText(self.tr("Please enter a profile name."))
            self._mig_error.show()
            return
        self._mig_error.hide()
        profile = self._pm.migrate_legacy(name)
        self._activate_and_emit(profile.id)

    def _ob_step0_next(self) -> None:
        name = self._ob_name_field.text().strip()
        if not name:
            self._ob_name_error.setText(self.tr("The name cannot be empty."))
            self._ob_name_error.show()
            return
        self._ob_name_error.hide()
        # Cria o perfil temporariamente (será salvo ao finalizar)
        self._ob_pending_name = name
        self._ob_stack.setCurrentIndex(1)
        self._sync_ob_dots(1)

    def _ob_step1_next(self) -> None:
        self._ob_stack.setCurrentIndex(2)
        self._sync_ob_dots(2)

    def _ob_step2_finish(self) -> None:
        self._finish_onboarding()

    def _on_skip_setup(self) -> None:
        """Confirma pulo e finaliza onboarding."""
        from PySide6.QtWidgets import QMessageBox
        dlg = QMessageBox(self)
        dlg.setWindowTitle(self.tr("Skip setup"))
        dlg.setText(self.tr("Do you want to skip initial setup?"))
        dlg.setInformativeText(
            self.tr("You can configure languages and OBS later in Settings.")
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        dlg.setStyleSheet(f"""
            QMessageBox {{ background: {_SURF}; }}
            QMessageBox QLabel {{
                color: {_TEXT};
                background: transparent;
                border: none;
                font-size: 13px;
            }}
            QPushButton {{
                background: {_SURF}; color: {_TEXT};
                border: 1px solid {_BORDER}; border-radius: 6px;
                padding: 6px 18px; min-width: 80px;
            }}
            QPushButton:hover {{ background: #21262d; }}
        """)
        if dlg.exec() == QMessageBox.StandardButton.Yes:
            self._finish_onboarding(skip=True)

    def _sync_ob_dots(self, step: int) -> None:
        for dots in getattr(self, "_ob_dot_widgets", []):
            dots.set_step(step)

    def _finish_onboarding(self, skip: bool = False) -> None:
        name = getattr(self, "_ob_pending_name", self.tr("My Profile"))
        profile = self._pm.create_profile(name)
        self._pm.set_active(profile.id)

        iface_code = getattr(self, "_ob_iface_selected_code", self._lang.current_code if self._lang else "en")
        if self._lang:
            self._lang.set_language(iface_code)
        s = self._pm.prefs(QSETTINGS_APP_APP)
        s.setValue(SettingsKey.APP_LANGUAGE, iface_code)
        s.sync()

        media_code = getattr(self, "_ob_media_selected_code", "") or self._interface_api_code(iface_code)
        if media_code:
            s2 = self._pm.prefs(QSETTINGS_APP_APP)
            s2.setValue(SettingsKey.MEDIA_LANGUAGE_CODE, media_code)
            s2.sync()

        # Save OBS config only when integration was enabled and not skipped
        if not skip and self._ob_obs_toggle.is_checked:
            port_text = self._ob_obs_port.text().strip()
            try:
                port = int(port_text) if port_text else 4455
            except ValueError:
                port = 4455
            pwd = self._ob_obs_pwd.text()

            default_scene = self._ob_obs_default_combo.currentText()
            media_scene   = self._ob_obs_media_combo.currentText()

            s3 = self._pm.prefs(QSETTINGS_PREFS_APP)
            s3.setValue(SettingsKey.OBS_ENABLED, True)
            s3.setValue(SettingsKey.OBS_PORT, port)
            s3.setValue(SettingsKey.OBS_PASSWORD, pwd)
            s3.setValue(SettingsKey.OBS_DEFAULT_SCENE, default_scene)
            s3.setValue(SettingsKey.OBS_MEDIA_WINDOW_SCENE, media_scene)
            s3.sync()

        # Clean up the temporary OBS service before handing off
        self._ob_obs_teardown()

        self.profile_ready.emit(profile.id)

    def _activate_and_emit(self, profile_id: str) -> None:
        self._pm.set_active(profile_id)

        # Restaura idioma do perfil se houver
        s = self._pm.prefs(QSETTINGS_APP_APP)
        saved_lang = s.value(SettingsKey.APP_LANGUAGE, "", str)
        if saved_lang and self._lang:
            self._lang.set_language(saved_lang)

        self.profile_ready.emit(profile_id)

    def retranslateUi(self) -> None:
        self.setWindowTitle(self.tr("Solin"))
        for label, source in self._tr_labels:
            label.setText(self.tr(source))
        for button, source in self._tr_buttons:
            button.setText(self.tr(source))
        for field, source in self._tr_placeholders:
            field.setPlaceholderText(self.tr(source))
        for card, title_source, desc_source in self._tr_sections:
            title_lbl = getattr(card, "_title_lbl", None)
            desc_lbl = getattr(card, "_desc_lbl", None)
            if title_lbl is not None:
                title_lbl.setText(self.tr(title_source))
            if desc_lbl is not None:
                desc_lbl.setText(self.tr(desc_source))
        for title_lbl, title_source, subtitle_lbl, subtitle_source in self._onboard_headers:
            title_lbl.setText(self.tr(title_source))
            subtitle_lbl.setText(self.tr(subtitle_source))
        for btn in self._ob_skip_buttons:
            btn.setText(self.tr("Skip setup"))
        if hasattr(self, "_selector_add_card"):
            self._selector_add_card.set_label(self.tr("New Profile"))
        self._refresh_ob_language_buttons()
        self._refresh_ob_obs_status()
        if hasattr(self, "_ob_obs_default_combo") and hasattr(self, "_ob_obs_media_combo"):
            scenes = []
            if self._ob_obs_svc is not None:
                scenes = self._ob_obs_svc.scenes
            elif self._ob_obs_default_combo.count() > 0:
                scenes = [
                    self._ob_obs_default_combo.itemText(i)
                    for i in range(1, self._ob_obs_default_combo.count())
                ]
            if scenes:
                self._ob_obs_populate_combos(scenes)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    # ── OBS toggle helper ──────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._stack.currentIndex() == 1 and self._creating_additional_profile:
                self._cancel_onboarding_profile_creation()
                return
        super().keyPressEvent(event)
