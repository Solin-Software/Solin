"""
updates.py
=================
Serviço assíncrono de verificação de atualizações do Solin.

Fluxo:
  1. UpdateWorker roda em QThread separado, alguns segundos após o app abrir.
  2. Faz GET em UPDATE_CHECK_URL e solicita changelog localizado acumulado.
  3. Compara a versão atual (APP_VERSION) com setup/patch disponíveis.
  4. Prioridade: patch > setup (patch é menor e silencioso).
  5. Patch só é oferecido se current >= patch.min_version.
  6. Emite `update_available` com um UpdateInfo descrevendo o que fazer.

Formato esperado da API (GET /v1/version):
  {
    "setup": {
      "version":  "1.1.0.0",
      "url":      "https://releases.solinav.com/SolinSetup_1.1.0.0.exe"
    },
    "patch": {
      "version":     "1.0.1.0",
      "url":         "https://releases.solinav.com/SolinPatch_1.0.1.0.exe",
      "min_version": "1.0.0.0"
    },
    "changelog": {
      "requested_language": "T",
      "entries": [
        {"version": "1.1.0.0", "language": "T", "markdown": "- Mudança"}
      ]
    }
  }

Segurança:
  - HTTPS + verificação de certificado pelo adaptador HTTP central.
  - Timeout agressivo para não atrasar a inicialização.
  - Nenhum dado do usuário é enviado.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

from solin.core.foundation.constants import APP_PLATFORM, APP_VERSION, UPDATE_CHECK_URL
from solin.core.network.http import HttpError, get_json
from solin.core.remote.update_policy import UpdateInfo, evaluate_update

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S: int = 10
InstallIdProvider = Callable[[], str]
LanguageCodeProvider = Callable[[], str]


# ── Worker ─────────────────────────────────────────────────────────────────────

class UpdateWorker(QObject):
    """
    Roda em QThread separado.
    Não acessa widgets — apenas emite signals.
    """
    update_available = Signal(object)  # UpdateInfo
    no_update        = Signal()
    fetch_failed     = Signal(str)     # mensagem de erro (log silencioso)

    def __init__(
        self,
        install_id_provider: InstallIdProvider,
        language_code_provider: LanguageCodeProvider,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._install_id_provider = install_id_provider
        self._language_code_provider = language_code_provider

    def run(self) -> None:
        try:
            params = {
                "v":        APP_VERSION,
                "id":       self._install_id_provider(),
                "platform": APP_PLATFORM,
                "lang":     self._language_code_provider(),
                "include":  "changelog",
            }
            payload = get_json(
                UPDATE_CHECK_URL,
                params=params,
                timeout=FETCH_TIMEOUT_S,
                headers={
                    "Accept":     "application/json",
                    "User-Agent": f"Solin/{APP_VERSION}",
                },
            )
        except HttpError as exc:
            log.debug("[Update] fetch failed: %s", exc)
            self.fetch_failed.emit(str(exc))
            return

        info = evaluate_update(payload, current_version=APP_VERSION)
        if info:
            log.debug("[Update] available: %s %s", info.kind.value, info.version)
            self.update_available.emit(info)
        else:
            log.debug("[Update] no update")
            self.no_update.emit()

# ── Controlador público ────────────────────────────────────────────────────────

class UpdateService(QObject):
    """
    Fachada pública. Gerencia ciclo de vida da thread.

    Uso típico (em MainWindow.__init__):
        self._update_svc = UpdateService(
            install_id_provider,
            language_code_provider,
            self,
        )
        self._update_svc.update_available.connect(self._on_update_available)
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._update_svc.check)
    """
    update_available = Signal(object)  # UpdateInfo

    def __init__(
        self,
        install_id_provider: InstallIdProvider,
        language_code_provider: LanguageCodeProvider,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._install_id_provider = install_id_provider
        self._language_code_provider = language_code_provider
        self._thread: QThread | None = None
        self._worker: UpdateWorker | None = None
        self._running = False
        self._stopping = False
        self._delete_when_stopped = False

    def check(self) -> None:
        """Inicia verificação assíncrona. Ignora chamadas duplicadas."""
        if self._stopping or self._running:
            return
        self._running = True

        self._thread = QThread(self)
        self._worker = UpdateWorker(
            self._install_id_provider,
            self._language_code_provider,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.update_available.connect(self._on_result)
        self._worker.no_update.connect(self._thread.quit)
        self._worker.fetch_failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)

        self._thread.start()

    def stop(self, wait_ms: int = 0, delete_when_stopped: bool = False) -> None:
        """
        Para o ciclo de vida da thread de forma segura.

        Se o worker estiver bloqueado em rede, ele termina quando o timeout da
        requisição voltar. Nesse caso o serviço é desanexado da janela antiga.
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

    def _on_result(self, info: UpdateInfo) -> None:
        self._thread.quit()
        if not self._stopping:
            self.update_available.emit(info)

    def _cleanup(self) -> None:
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
        self._running = False
        if self._delete_when_stopped:
            self.deleteLater()
