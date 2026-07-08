"""
notifications.py
=======================
Serviço assíncrono de notificações do Solin.

Fluxo completo:
  1. NotificationWorker é iniciado em QThread separado ~1,5 s após o app abrir.
  2. Faz GET na NOTIFICATION_API_URL com timeout curto (não trava a UI).
  3. Valida o schema mínimo do payload.
  4. Filtra notificações cujo ID já está no histórico local do perfil.
  5. Solicita o conteúdo no api_code do idioma ativo; o servidor resolve
     idioma solicitado, inglês e primeiro conteúdo disponível, nessa ordem.
     O cliente mantém a mesma resolução defensiva para backends antigos.
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
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from solin.core.i18n.manager import LanguageManager

from solin.core.foundation.constants import (
    APP_PLATFORM,
    APP_VERSION,
    NOTIFICATION_API_URL,
)
from solin.core.network.http import HttpError, get_json
from solin.core.remote.notification_policy import Notification, resolve_remote_notifications
from solin.core.remote.notification_settings import NotificationSettingsStore

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S: int = 8        # timeout total da requisição HTTP
InstallIdProvider = Callable[[], str]


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
        settings: NotificationSettingsStore,
        install_id_provider: InstallIdProvider,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        # api_code do idioma ativo (ex: "T" para Português, "E" para English)
        self._api_code = api_code
        self._settings = settings
        self._install_id_provider = install_id_provider

    def run(self) -> None:
        """Chamado pela thread. Faz fetch, processa, emite resultado."""
        try:
            # Enviamos id + v para o servidor poder upsert AppInstance
            # (contribui para métricas de instâncias ativas sem precisar de
            #  uma chamada extra dedicada). Ambos são opcionais pelo servidor.
            params = {
                "id":       self._install_id_provider(),
                "v":        APP_VERSION,
                "platform": APP_PLATFORM,
                "lang":     self._api_code,
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

    def _process(self, payload: object) -> list[Notification]:
        """
        Valida o payload, filtra vistas e resolve conteúdo localizado.
        Retorna lista de Notification prontas para exibir.
        """
        result = resolve_remote_notifications(
            payload,
            api_code=self._api_code,
            seen_ids=self._settings.seen_ids(),
        )
        for notification_id in result.mark_seen_ids:
            self._settings.mark_seen(notification_id)
        return list(result.notifications)


# ── Controlador público ────────────────────────────────────────────────────────

class NotificationService(QObject):
    """
    Fachada pública. Gerencia o ciclo de vida da thread e expõe um signal limpo.

    Uso típico:
        self._notif_service = NotificationService(
            lang_manager,
            notification_settings_store,
            install_id_provider,
            self,
        )
        self._notif_service.notifications_ready.connect(self._on_notifications)
        QTimer.singleShot(1500, self._notif_service.check)
    """
    notifications_ready = Signal(list)   # list[Notification]

    def __init__(
        self,
        lang_manager: "LanguageManager",
        notification_settings: NotificationSettingsStore,
        install_id_provider: InstallIdProvider,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._lang = lang_manager
        self._notification_settings = notification_settings
        self._install_id_provider = install_id_provider
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
        self._worker = NotificationWorker(
            api_code,
            self._notification_settings,
            self._install_id_provider,
        )
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
