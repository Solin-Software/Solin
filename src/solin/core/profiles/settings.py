"""
profile_settings.py — Solin
============================
Wrapper fino sobre QSettings que sempre usa o org do perfil ativo.

Uso nos módulos:
    from solin.core.foundation.constants import QSETTINGS_APP_APP
    from solin.core.profiles import settings as _ps
    ...
    self._prefs = _ps.prefs()          # → QSettings("<org>_{slug}", "ProjectionPrefs")
    s = _ps.prefs(QSETTINGS_APP_APP)    # → QSettings("<org>_{slug}", "App")

A variável _ORG é atualizada automaticamente pelo ProfileManager.set_active().
"""
from __future__ import annotations
from PySide6.QtCore import QSettings

from solin.core.foundation.constants import QSETTINGS_ORG_NAME, QSETTINGS_PREFS_APP
from solin.core.foundation.settings_store import ProfileAppSettingsStore

# Org padrão (fallback / antes de qualquer perfil ser ativado).
_ORG: str = QSETTINGS_ORG_NAME


def set_org(org: str) -> None:
    """Chamado pelo ProfileManager ao ativar um perfil."""
    global _ORG
    _ORG = org


def current_org() -> str:
    return _ORG


def prefs(app_name: str = QSETTINGS_PREFS_APP) -> QSettings:
    """
    Retorna um novo QSettings com o org do perfil ativo.
    Cada chamada cria uma nova instância — adequado para uso em __init__
    e em funções que não guardam estado.
    """
    return QSettings(_ORG, app_name)


def app_settings() -> ProfileAppSettingsStore:
    return ProfileAppSettingsStore.for_organization(_ORG)
