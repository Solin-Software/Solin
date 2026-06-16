"""
notifications.py
======================
Dialog não-modal de notificação remota para o Solin.

Características:
  - Não bloqueia a janela principal (show(), não exec()).
  - Não usa WindowStaysOnTopHint para não irritar o usuário.
  - Fila automática: se houver múltiplas notificações, exibe uma por vez
    e avança para a próxima ao fechar.
  - Tipo info / warning / error com cores e ícones distintos.
  - Botão de ação opcional — abre URL no browser padrão do sistema.
    Com URL: exibe "Sim" (abre link) + "Não" (descarta).
    Sem URL: exibe apenas "OK".
  - Posicionada centralmente sobre a janela pai (não no canto, para
    notificações que merecem atenção).
  - Pequena animação de fade-in para não assustar o usuário.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QSizePolicy,
    QWidget,
    QGraphicsOpacityEffect,
    QScrollArea,
)
from PySide6.QtCore import Qt, QUrl, QPropertyAnimation, QEasingCurve, QTimer, QObject, QByteArray
from PySide6.QtGui import QDesktopServices, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

if TYPE_CHECKING:
    from solin.core.i18n.manager import LanguageManager
    from solin.core.remote.notification_policy import Notification

# ── Paleta interna (espelha theme.py sem import circular) ──────────────────────
_C = {
    "bg0": "#0d1117",
    "bg1": "#161b22",
    "bg2": "#21262d",
    "bg3": "#2d333b",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#388bfd",
    "info": "#388bfd",
    "warning": "#d29922",
    "error": "#f85149",
    "success": "#3fb950",
}

# Ícone SVG inline para cada tipo de notificação
_ICONS: dict[str, str] = {
    "info": """
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             xmlns="http://www.w3.org/2000/svg">
          <circle cx="12" cy="12" r="10" stroke="#388bfd" stroke-width="2"/>
          <line x1="12" y1="8" x2="12" y2="8" stroke="#388bfd"
                stroke-width="2.5" stroke-linecap="round"/>
          <line x1="12" y1="11" x2="12" y2="16" stroke="#388bfd"
                stroke-width="2" stroke-linecap="round"/>
        </svg>""",
    "warning": """
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             xmlns="http://www.w3.org/2000/svg">
          <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"
                stroke="#d29922" stroke-width="2" fill="none"/>
          <line x1="12" y1="9" x2="12" y2="13" stroke="#d29922"
                stroke-width="2" stroke-linecap="round"/>
          <line x1="12" y1="17" x2="12" y2="17" stroke="#d29922"
                stroke-width="2.5" stroke-linecap="round"/>
        </svg>""",
    "error": """
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             xmlns="http://www.w3.org/2000/svg">
          <circle cx="12" cy="12" r="10" stroke="#f85149" stroke-width="2"/>
          <line x1="15" y1="9" x2="9" y2="15" stroke="#f85149"
                stroke-width="2" stroke-linecap="round"/>
          <line x1="9" y1="9" x2="15" y2="15" stroke="#f85149"
                stroke-width="2" stroke-linecap="round"/>
        </svg>""",
}

_ACCENT_COLOR: dict[str, str] = {
    "info": _C["info"],
    "warning": _C["warning"],
    "error": _C["error"],
}


def _darken(hex_color: str, factor: float = 0.78) -> str:
    """Returns a darker version of hex_color (no alpha — safe for Qt stylesheets)."""
    h = hex_color.lstrip("#")
    r = int(int(h[0:2], 16) * factor)
    g = int(int(h[2:4], 16) * factor)
    b = int(int(h[4:6], 16) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def _svg_to_pixmap(svg_str: str, size: int = 22) -> QPixmap:
    """Renders an SVG string into a QPixmap of the given square size."""
    data = QByteArray(svg_str.strip().encode("utf-8"))
    renderer = QSvgRenderer(data)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap


def _make_stylesheet(accent: str) -> str:
    accent_hover = _darken(accent, 0.78)
    return f"""
        QDialog {{
            background-color: {_C["bg1"]};
            border: 1px solid {_C["border"]};
            border-radius: 10px;
        }}
        QLabel {{
            background-color: transparent;
        }}
        QLabel#title {{
            color: {_C["text"]};
            font-size: 14px;
            font-weight: 600;
            background-color: transparent;
        }}
        QLabel#detail {{
            color: {_C["muted"]};
            font-size: 12px;
            background-color: transparent;
        }}
        QFrame#separator {{
            background-color: {_C["border"]};
        }}
        QPushButton {{
            background-color: {_C["bg2"]};
            color: {_C["text"]};
            border: 1px solid {_C["border"]};
            border-radius: 6px;
            padding: 6px 18px;
            font-size: 12px;
            font-weight: 500;
            min-width: 72px;
        }}
        QPushButton:hover {{
            background-color: {_C["bg3"]};
            border-color: {_C["muted"]};
        }}
        QPushButton#action_btn {{
            background-color: {accent};
            color: #ffffff;
            border: none;
        }}
        QPushButton#action_btn:hover {{
            background-color: {accent_hover};
        }}
        QScrollArea {{
            border: none;
            background-color: transparent;
        }}
        QScrollArea > QWidget > QWidget {{
            background-color: transparent;
        }}
        QScrollBar:vertical {{
            background: {_C["bg2"]};
            width: 6px;
            border-radius: 3px;
            margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background: {_C["bg3"]};
            border-radius: 3px;
            min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {_C["muted"]};
        }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
    """


class NotificationDialog(QDialog):
    """
    Dialog não-modal para uma única notificação.
    A fila de múltiplas notificações é gerenciada pelo RemoteNotificationQueue.
    """

    def __init__(
        self,
        notification: "Notification",
        lang: "LanguageManager",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._notif = notification
        self._lang = lang

        # ── Flags de janela ───────────────────────────────────────────────
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowTitle(self.tr("Solin"))
        self.setFixedWidth(400)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        accent = _ACCENT_COLOR.get(notification.notif_type, _C["info"])
        self.setStyleSheet(_make_stylesheet(accent))

        self._build_ui(notification, accent)
        self._fade_in()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_ui(self, n: "Notification", accent: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(0)

        # ── Área rolável: ícone + título + detail ─────────────────────────
        scroll_content = QWidget()
        scroll_content.setObjectName("scroll_content")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 8, 0)  # margem direita p/ scrollbar
        scroll_layout.setSpacing(0)

        # Cabeçalho: ícone + título
        header = QHBoxLayout()
        header.setSpacing(10)
        header.setContentsMargins(0, 0, 0, 0)

        icon_label = QLabel()
        icon_label.setFixedSize(22, 22)
        icon_label.setPixmap(_svg_to_pixmap(_ICONS.get(n.notif_type, _ICONS["info"]), size=22))
        header.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        title_label = QLabel(n.title)
        title_label.setObjectName("title")
        title_label.setWordWrap(True)
        header.addWidget(title_label, 1)

        scroll_layout.addLayout(header)

        if n.detail:
            scroll_layout.addSpacing(10)
            detail_label = QLabel(n.detail)
            detail_label.setObjectName("detail")
            detail_label.setWordWrap(True)
            detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # Garante que o label respeite a largura do scroll
            detail_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            scroll_layout.addWidget(detail_label)

        scroll_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(scroll_content)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Altura máxima antes de ativar scroll (~40% da tela ou 280px)
        scroll.setMaximumHeight(280)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        root.addWidget(scroll)
        root.addSpacing(16)

        # ── Separador ─────────────────────────────────────────────────────
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        root.addWidget(sep)
        root.addSpacing(12)

        # ── Botões (sempre visíveis, fora do scroll) ──────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        if n.action_url:
            # Com URL: botão de ação (Sim / label) + botão de fechar (Não)
            action_label = n.action_label or self.tr("Open Link")
            action_btn = QPushButton(action_label)
            action_btn.setObjectName("action_btn")
            action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            action_btn.clicked.connect(lambda: self._open_url(n.action_url))

            close_btn = QPushButton(self.tr("No, thanks"))
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.clicked.connect(self.close)

            btn_row.addWidget(close_btn)
            btn_row.addWidget(action_btn)
        else:
            # Sem URL: apenas OK
            ok_btn = QPushButton(self.tr("OK"))
            ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ok_btn.clicked.connect(self.close)
            btn_row.addWidget(ok_btn)

        root.addLayout(btn_row)

        # Ajusta o scroll para a altura real do conteúdo (evita scroll quando desnecessário)
        scroll_content.adjustSize()
        natural_h = scroll_content.sizeHint().height()
        scroll.setFixedHeight(min(natural_h, 280))

        self.adjustSize()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))
        self.close()

    def _fade_in(self) -> None:
        """Fade-in suave de 200 ms."""
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(200)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()

    # ── Posicionamento ────────────────────────────────────────────────────

    def show_centered_on_parent(self) -> None:
        """Centraliza sobre a janela pai (ou tela) e exibe."""
        self.show()
        self.adjustSize()

        parent = self.parent()
        if parent and hasattr(parent, "frameGeometry"):
            pg = parent.frameGeometry()
            cx = pg.left() + (pg.width() - self.width()) // 2
            cy = pg.top() + (pg.height() - self.height()) // 2
            self.move(cx, cy)
        else:
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
            if screen:
                sg = screen.availableGeometry()
                self.move(
                    sg.left() + (sg.width() - self.width()) // 2,
                    sg.top() + (sg.height() - self.height()) // 2,
                )


# ── Gerenciador de fila ────────────────────────────────────────────────────────


class RemoteNotificationQueue(QObject):
    """
    Gerencia a exibição sequencial de múltiplas notificações.
    Quando uma dialog é fechada, exibe a próxima da fila.

    Uso:
        queue = RemoteNotificationQueue(lang_manager, parent_window)
        queue.enqueue(list_of_notifications)
    """

    def __init__(
        self,
        lang: "LanguageManager",
        parent: QWidget | None = None,
    ):
        from PySide6.QtCore import QObject

        QObject.__init__(self, parent)
        self._lang = lang
        self._parent = parent
        self._queue: list["Notification"] = []
        self._current: NotificationDialog | None = None

    def enqueue(self, notifications: list["Notification"]) -> None:
        """Adiciona notificações à fila e inicia exibição se ociosa."""
        self._queue.extend(notifications)
        if self._current is None:
            self._show_next()

    def _show_next(self) -> None:
        if not self._queue:
            self._current = None
            return

        notif = self._queue.pop(0)
        dlg = NotificationDialog(notif, self._lang, self._parent)
        dlg.finished.connect(self._on_dialog_closed)
        self._current = dlg

        # Pequeno delay entre notificações encadeadas para evitar sobreposição de animações
        QTimer.singleShot(50, dlg.show_centered_on_parent)

    def _on_dialog_closed(self) -> None:
        self._current = None
        # Aguarda um tick antes de abrir a próxima (UX mais suave)
        if self._queue:
            QTimer.singleShot(300, self._show_next)
