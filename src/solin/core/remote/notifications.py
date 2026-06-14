"""
notifications.py
=======================
Serviço assíncrono de notificações do Solin.

Fluxo completo:
  1. NotificationWorker é iniciado em QThread separado ~1,5 s após o app abrir.
  2. Faz GET na NOTIFICATION_API_URL com timeout curto (não trava a UI).
  3. Valida o schema mínimo do payload.
  4. Filtra notificações cujo ID já está no histórico local do perfil.
  5. Resolve o conteúdo localizado usando o api_code do idioma ativo,
     com fallback para "E" (inglês) se o código não estiver disponível.
  6. Emite o signal `notifications_ready` com a lista já processada.
  7. A MainWindow conecta esse signal e enfileira os dialogs (não-modais).

Segurança:
  - HTTPS + verificação de certificado pelo adaptador HTTP central.
  - Timeout agressivo (FETCH_TIMEOUT_S) para não atrasar a inicialização.
  - Nenhum dado sensível é enviado ao servidor.
  - IDs marcados como vistos ANTES de emitir o signal — evita re-exibição
    em caso de crash entre a emissão e a interação do usuário.
  - Parsing totalmente defensivo; qualquer exceção é silenciada e logada.

Formato esperado da API:
  {
    "version": 1,
    "notifications": [
      {
        "id": "2025_001",          // str único e imutável
        "type": "info",            // "info" | "warning" | "error"
        "content": {
          "E": {"title": "...", "detail": "..."},  // English (fallback)
          "T": {"title": "...", "detail": "..."},  // Português
          "S": {"title": "...", "detail": "..."}   // Español
          // … outros api_codes conforme necessário
        },
        "action": {                // OPCIONAL
          "url": "https://...",
          "label": "Saiba mais"    // texto do botão (opcional; usa padrão se ausente)
        }
      }
    ]
  }
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from solin.core.i18n.manager import LanguageManager
    
from solin.core.foundation.constants import NOTIFICATION_API_URL
from solin.core.network.http import HttpError, get_json
from solin.core.remote.notification_settings import NotificationSettingsStore
from solin.core.profiles.settings import ProfileSettings

log = logging.getLogger(__name__)

# ── Configuração ───────────────────────────────────────────────────────────────
FETCH_TIMEOUT_S: int = 8        # timeout total da requisição HTTP
FALLBACK_LANG: str = "E"        # api_code de fallback (English)

# Schema mínimo obrigatório em cada notificação
_REQUIRED_FIELDS = {"id", "type", "content"}
_VALID_TYPES = {"info", "warning", "error"}


# ── Modelo de dados ────────────────────────────────────────────────────────────

class Notification:
    """Notificação já resolvida e pronta para exibir."""
    __slots__ = ("id", "notif_type", "title", "detail", "action_url", "action_label")

    def __init__(
        self,
        notif_id: str,
        notif_type: str,
        title: str,
        detail: str,
        action_url: str = "",
        action_label: str = "",
    ):
        self.id = notif_id
        self.notif_type = notif_type   # "info" | "warning" | "error"
        self.title = title
        self.detail = detail
        self.action_url = action_url   # vazio = sem botão de ação
        self.action_label = action_label


# ── Armazenamento de IDs vistos ────────────────────────────────────────────────

def _load_seen_ids(profile_settings: ProfileSettings) -> set[str]:
    """Carrega a lista de IDs já exibidos do store tipado."""
    return NotificationSettingsStore.for_profile_settings(profile_settings).seen_ids()


def mark_seen(notif_id: str, profile_settings: ProfileSettings) -> None:
    """Marca uma notificação como exibida."""
    NotificationSettingsStore.for_profile_settings(profile_settings).mark_seen(notif_id)


def reset_seen_ids(profile_settings: ProfileSettings) -> None:
    """Utilitário de diagnóstico: limpa o histórico de IDs vistos."""
    NotificationSettingsStore.for_profile_settings(profile_settings).reset_seen_ids()


# ── Worker assíncrono ──────────────────────────────────────────────────────────

class NotificationWorker(QObject):
    """
    Roda em QThread separado. Faz a requisição HTTP e emite o resultado.
    Não acessa widgets — apenas emite signals.
    """
    notifications_ready = Signal(list)   # list[Notification]
    fetch_failed = Signal(str)           # mensagem de erro (para log silencioso)

    def __init__(
        self,
        api_code: str,
        profile_settings: ProfileSettings,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        # api_code do idioma ativo (ex: "T" para Português, "E" para English)
        self._api_code = api_code
        self._profile_settings = profile_settings

    def run(self) -> None:
        """Chamado pela thread. Faz fetch, processa, emite resultado."""
        try:
            from solin.core.foundation.identity import get_install_id
            from solin.core.foundation.constants import APP_PLATFORM, APP_VERSION

            # Enviamos id + v para o servidor poder upsert AppInstance
            # (contribui para métricas de instâncias ativas sem precisar de
            #  uma chamada extra dedicada). Ambos são opcionais pelo servidor.
            params = {
                "id":       get_install_id(),
                "v":        APP_VERSION,
                "platform": APP_PLATFORM,
            }
            payload = get_json(
                NOTIFICATION_API_URL,
                params=params,
                timeout=FETCH_TIMEOUT_S,
                headers={
                    "Accept":     "application/json",
                    "User-Agent": f"Solin/{APP_VERSION}",
                },
            )
        except HttpError as exc:
            log.debug("[Notifications] fetch failed: %s", exc)
            self.fetch_failed.emit(str(exc))
            return

        notifications = self._process(payload)
        self.notifications_ready.emit(notifications)

    def _process(self, payload: dict) -> list[Notification]:
        """
        Valida o payload, filtra vistas e resolve conteúdo localizado.
        Retorna lista de Notification prontas para exibir.
        """
        if not isinstance(payload, dict):
            return []

        raw_list = payload.get("notifications")
        if not isinstance(raw_list, list):
            return []

        seen = _load_seen_ids(self._profile_settings)
        result: list[Notification] = []

        for item in raw_list:
            if not isinstance(item, dict):
                continue

            # Validação de schema mínimo
            if not _REQUIRED_FIELDS.issubset(item.keys()):
                log.debug("[Notifications] item missing required fields: %s", item)
                continue

            notif_id = str(item["id"]).strip()
            if not notif_id:
                continue

            # Já exibida? Pula.
            if notif_id in seen:
                continue

            notif_type = str(item.get("type", "info")).lower()
            if notif_type not in _VALID_TYPES:
                notif_type = "info"

            content = item.get("content", {})
            if not isinstance(content, dict):
                continue

            # Resolve localização: tenta api_code ativo → fallback "E"
            localized = content.get(self._api_code) or content.get(FALLBACK_LANG)
            if not isinstance(localized, dict):
                log.debug("[Notifications] no content for code '%s' or fallback: %s",
                          self._api_code, notif_id)
                continue

            title = str(localized.get("title", "")).strip()
            detail = str(localized.get("detail", "")).strip()
            if not title:
                continue

            # Ação opcional
            action_url = ""
            action_label = ""
            action = item.get("action")
            if isinstance(action, dict):
                action_url = str(action.get("url", "")).strip()
                action_label = str(action.get("label", "")).strip()

            # Marca como vista AGORA — antes de emitir — para evitar re-exibição em crash
            mark_seen(notif_id, self._profile_settings)

            result.append(Notification(
                notif_id=notif_id,
                notif_type=notif_type,
                title=title,
                detail=detail,
                action_url=action_url,
                action_label=action_label,
            ))

        return result


# ── Controlador público ────────────────────────────────────────────────────────

class NotificationService(QObject):
    """
    Fachada pública. Gerencia o ciclo de vida da thread e expõe um signal limpo.

    Uso típico:
        self._notif_service = NotificationService(
            lang_manager,
            profile_settings,
            self,
        )
        self._notif_service.notifications_ready.connect(self._on_notifications)
        QTimer.singleShot(1500, self._notif_service.check)
    """
    notifications_ready = Signal(list)   # list[Notification]

    def __init__(
        self,
        lang_manager: "LanguageManager",
        profile_settings: ProfileSettings,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._lang = lang_manager
        self._profile_settings = profile_settings
        self._thread: QThread | None = None
        self._worker: NotificationWorker | None = None
        self._running = False
        self._stopping = False
        self._delete_when_stopped = False

    def check(self) -> None:
        """Inicia a verificação assíncrona. Ignora chamadas duplicadas."""
        if self._stopping or self._running:
            return
        self._running = True

        api_code = self._lang.api_code  # ex: "T", "E", "S"

        self._thread = QThread(self)
        self._worker = NotificationWorker(api_code, self._profile_settings)
        self._worker.moveToThread(self._thread)

        # Conecta sinais
        self._thread.started.connect(self._worker.run)
        self._worker.notifications_ready.connect(self._on_ready)
        self._worker.fetch_failed.connect(self._on_failed)
        self._worker.notifications_ready.connect(self._thread.quit)
        self._worker.fetch_failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)

        self._thread.start()

    def stop(self, wait_ms: int = 0, delete_when_stopped: bool = False) -> None:
        """
        Para o ciclo de vida da thread de forma segura.

        A requisição HTTP em andamento pode estar bloqueada fora do event loop
        do Qt. Se ela não terminar dentro do prazo, o serviço é desanexado do
        pai para que a janela possa morrer sem destruir um QThread ativo.
        """
        self._stopping = True
        self._delete_when_stopped = self._delete_when_stopped or delete_when_stopped

        if self._thread and self._thread.isRunning():
            self._thread.quit()
            if wait_ms > 0:
                self._thread.wait(wait_ms)

        if self._thread and self._thread.isRunning() and self._delete_when_stopped:
            self.setParent(None)
        elif not self._running and self._delete_when_stopped:
            self.deleteLater()

    def shutdown(self) -> None:
        """Compatibilidade para callers que esperam shutdown()."""
        self.stop(wait_ms=3000, delete_when_stopped=True)

    # ── Slots privados ─────────────────────────────────────────────────────────

    def _on_ready(self, notifications: list) -> None:
        if self._stopping:
            return
        if notifications:
            log.debug("[Notifications] %d new notification(s)", len(notifications))
            self.notifications_ready.emit(notifications)

    def _on_failed(self, msg: str) -> None:
        log.debug("[Notifications] silent failure: %s", msg)
        self._running = False

    def _cleanup(self) -> None:
        """Libera recursos da thread após conclusão."""
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
        self._running = False
        if self._delete_when_stopped:
            self.deleteLater()
