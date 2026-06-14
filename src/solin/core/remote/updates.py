"""
updates.py
=================
Serviço assíncrono de verificação de atualizações do Solin.

Fluxo:
  1. UpdateWorker roda em QThread separado, alguns segundos após o app abrir.
  2. Faz GET em UPDATE_CHECK_URL — endpoint JSON leve (< 1 KB).
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
    }
  }

Segurança:
  - HTTPS + verificação de certificado pelo adaptador HTTP central.
  - Timeout agressivo para não atrasar a inicialização.
  - Nenhum dado do usuário é enviado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from solin.core.foundation.constants import APP_PLATFORM, APP_VERSION, UPDATE_CHECK_URL
from solin.core.network.http import HttpError, get_json

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S: int = 10


# ── Tipos ──────────────────────────────────────────────────────────────────────

class UpdateKind(Enum):
    SETUP = "setup"   # instalação completa — abre browser
    PATCH = "patch"   # patch silencioso   — baixa e aplica


@dataclass(frozen=True)
class UpdateInfo:
    kind:    UpdateKind
    version: str   # versão disponível
    url:     str   # URL de download / página


# ── Comparação de versão ───────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    """'1.2.3.4' → (1, 2, 3, 4). Tolera 2–4 partes; preenche com 0."""
    parts = v.strip().split(".")
    result = []
    for p in parts[:4]:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    while len(result) < 4:
        result.append(0)
    return tuple(result)


def _is_newer(candidate: str, current: str) -> bool:
    """Retorna True se candidate > current."""
    return _parse_version(candidate) > _parse_version(current)


def _meets_min(current: str, min_version: str) -> bool:
    """Retorna True se current >= min_version."""
    return _parse_version(current) >= _parse_version(min_version)


# ── Worker ─────────────────────────────────────────────────────────────────────

class UpdateWorker(QObject):
    """
    Roda em QThread separado.
    Não acessa widgets — apenas emite signals.
    """
    update_available = Signal(object)  # UpdateInfo
    no_update        = Signal()
    fetch_failed     = Signal(str)     # mensagem de erro (log silencioso)

    def run(self) -> None:
        try:
            from solin.core.foundation.identity import get_install_id

            params = {
                "v":        APP_VERSION,
                "id":       get_install_id(),
                "platform": APP_PLATFORM,
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

        info = self._evaluate(payload)
        if info:
            log.debug("[Update] available: %s %s", info.kind.value, info.version)
            self.update_available.emit(info)
        else:
            log.debug("[Update] no update")
            self.no_update.emit()

    def _evaluate(self, payload: dict) -> UpdateInfo | None:
        if not isinstance(payload, dict):
            return None

        current = APP_VERSION

        # ── Tenta patch primeiro (menor, silencioso) ───────────────────────────
        patch_data = payload.get("patch")
        if isinstance(patch_data, dict):
            p_ver     = str(patch_data.get("version", "")).strip()
            p_url     = str(patch_data.get("url", "")).strip()
            p_min_ver = str(patch_data.get("min_version", "0.0.0.0")).strip()

            if (
                p_ver and p_url
                and _is_newer(p_ver, current)
                and _meets_min(current, p_min_ver)
            ):
                return UpdateInfo(kind=UpdateKind.PATCH, version=p_ver, url=p_url)

        # ── Fallback: setup completo ───────────────────────────────────────────
        setup_data = payload.get("setup")
        if isinstance(setup_data, dict):
            s_ver = str(setup_data.get("version", "")).strip()
            s_url = str(setup_data.get("url", "")).strip()

            if s_ver and s_url and _is_newer(s_ver, current):
                return UpdateInfo(kind=UpdateKind.SETUP, version=s_ver, url=s_url)

        return None


# ── Controlador público ────────────────────────────────────────────────────────

class UpdateService(QObject):
    """
    Fachada pública. Gerencia ciclo de vida da thread.

    Uso típico (em MainWindow.__init__):
        self._update_svc = UpdateService(self)
        self._update_svc.update_available.connect(self._on_update_available)
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._update_svc.check)
    """
    update_available = Signal(object)  # UpdateInfo

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
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
        self._worker = UpdateWorker()
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
