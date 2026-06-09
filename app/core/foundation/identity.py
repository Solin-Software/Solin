"""
device_id.py
============
Gera e persiste um UUID anônimo para identificar esta instalação do Solin.

Propósito:
    Permite correlacionar verificações de atualização consecutivas do mesmo
    dispositivo, possibilitando métricas de versão precisas no servidor:
      • Distribuição de versões em uso
      • Upgrade confirmado (mesmo ID, nova versão)
      • Contagem de instâncias ativas sem inflação por troca de IP

Privacidade:
    - É um UUID aleatório gerado com uuid4() na primeira execução.
    - NÃO contém nome de usuário, e-mail, HWID, MAC address ou qualquer
      dado de identificação pessoal.
    - Pode ser redefinido limpando as configurações do Solin (QSettings).

IMPORTANTE — escopo GLOBAL (não por perfil):
    O install_id identifica a *instalação/dispositivo*, não o perfil de usuário.
    Deliberadamente usa QSettings("<base>", "App") fixo, sem isolamento por perfil,
    para que todos os perfis na mesma máquina compartilhem o mesmo ID.
    Em dev, <base> = "SolinDev"; em producao, "Solin", evitando misturar
    telemetria/configuracao de teste com a instalacao real.

Uso:
    from app.core.foundation.identity import get_install_id
    install_id = get_install_id()  # str, 32 hex chars sem separadores
"""
from __future__ import annotations

import uuid
import logging

from PySide6.QtCore import QSettings

from .constants import QSETTINGS_APP_APP, QSETTINGS_ORG_NAME
from .settings_keys import SettingsKey

log = logging.getLogger(__name__)

# ── GLOBAL: fixo, independente do perfil ativo ────────────────────────────────
_GLOBAL_ORG = QSETTINGS_ORG_NAME
_GLOBAL_APP = QSETTINGS_APP_APP
_KEY        = SettingsKey.INSTALL_ID


def get_install_id() -> str:
    """
    Retorna o install_id persistido. Gera um novo se ainda não existir.
    Thread-safe via QSettings (serializado pelo Qt internamente).

    Usa sempre QSettings("<base>", "App") — namespace global, não por perfil.
    """
    prefs    = QSettings(_GLOBAL_ORG, _GLOBAL_APP)
    stored = prefs.value(_KEY, "", str)
    existing = stored if isinstance(stored, str) else ""

    if existing and len(existing) >= 32:
        return existing

    # Primeira execução: gera e persiste um UUID aleatório.
    new_id = uuid.uuid4().hex   # 32 chars hex sem separadores
    prefs.setValue(_KEY, new_id)
    prefs.sync()
    log.debug("[DeviceID] novo install_id gerado: %s…", new_id[:8])
    return new_id
