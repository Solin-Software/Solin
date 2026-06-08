"""
profile_settings.py — Solin
============================
Wrapper fino sobre QSettings que sempre usa o org do perfil ativo.

Uso nos módulos:
    from app.core.profiles import settings as _ps
    ...
    self._prefs = _ps.prefs()          # → QSettings("<org>_{slug}", "ProjectionPrefs")
    s = _ps.prefs("App")               # → QSettings("<org>_{slug}", "App")

A variável _ORG é atualizada automaticamente pelo ProfileManager.set_active().
"""
from __future__ import annotations
from PySide6.QtCore import QSettings

from app.core.foundation.constants import QSETTINGS_ORG_NAME, QSETTINGS_PREFS_APP

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
