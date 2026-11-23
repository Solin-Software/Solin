from __future__ import annotations

import ast
from pathlib import Path

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
    QSETTINGS_MONITORS_APP,
    QSETTINGS_NOTIFICATIONS_APP,
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
    QSETTINGS_PROFILE_SCOPED_APPS,
    QSETTINGS_TIMER_APP,
)
from solin.core.foundation import settings_store
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.profiles import infrastructure as profile_infrastructure
from solin.core.profiles import models as profile_models
from solin.core.profiles.settings import ProfileSettings


def test_profile_scoped_settings_apps_are_complete():
    assert QSETTINGS_PROFILE_SCOPED_APPS == (
        QSETTINGS_PREFS_APP,
        QSETTINGS_APP_APP,
        QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
        QSETTINGS_TIMER_APP,
        QSETTINGS_MONITORS_APP,
        QSETTINGS_NOTIFICATIONS_APP,
    )
    assert QSETTINGS_GLOBAL_APP not in QSETTINGS_PROFILE_SCOPED_APPS


def test_deleting_profile_clears_every_profile_scoped_settings_app(monkeypatch, tmp_path):
    cleared: list[tuple[str, str]] = []

    class FakeSettings:
        def __init__(self, organization: str, application: str):
            self.organization = organization
            self.application = application

        def clear(self) -> None:
            cleared.append((self.organization, self.application))

        def allKeys(self):
            return []

        def sync(self) -> None:
            pass

        def value(self, _key, default=None, _type=None):
            return default

        def setValue(self, _key, _value) -> None:
            pass

    monkeypatch.setattr(settings_store, "QSettings", FakeSettings)

    registry = profile_infrastructure.JsonProfileRegistry(tmp_path / "profiles.json")
    registry.save(
        [
            profile_models.ProfileInfo("kept", "Kept"),
            profile_models.ProfileInfo("removed", "Removed"),
        ]
    )
    manager = profile_infrastructure.create_local_profile_service(
        tmp_path,
        global_settings=settings_store.GlobalSettingsStore.create(),
        profile_app_settings_for=_profile_app_settings_for,
    )
    manager.set_active("kept")

    assert manager.delete_profile("removed") is True

    profile_org = f"{QSETTINGS_PROFILE_ORG_PREFIX}removed"
    assert cleared == [(profile_org, application) for application in QSETTINGS_PROFILE_SCOPED_APPS]


def _profile_app_settings_for(profile_id: str) -> ProfileAppSettingsStore:
    return ProfileSettings.for_profile_id(profile_id).app_settings()


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_settings_receiver(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        name = node.id.lower().strip("_")
        return (
            name in {"s", "s2", "s3", "gs", "prefs", "settings", "src_s", "dst_s"}
            or name.endswith("prefs")
            or name.endswith("settings")
        )
    if isinstance(node, ast.Attribute):
        name = node.attr.lower().strip("_")
        return name in {"prefs", "settings"} or name.endswith("prefs")
    if isinstance(node, ast.Call):
        return _call_name(node.func) in {"prefs", "QSettings"}
    return False


def _local_settings_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            aliases.add(target.id)
        elif isinstance(value, ast.Attribute) and _call_name(value.value) == "SettingsKey":
            aliases.add(target.id)
        elif isinstance(value, ast.Name) and value.id.startswith("QSETTINGS_"):
            aliases.add(target.id)
    return aliases


def test_qsettings_accesses_use_canonical_keys_and_namespaces():
    paths = [Path("main.py"), *sorted(Path("src/solin").rglob("*.py"))]
    offenders: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        local_aliases = _local_settings_aliases(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            call_name = _call_name(node.func)
            if call_name == "prefs" and node.args:
                namespace = node.args[0]
                if isinstance(namespace, ast.Constant) and isinstance(namespace.value, str):
                    offenders.append(f"{path}:{node.lineno}: literal prefs namespace")
                elif isinstance(namespace, ast.Name) and namespace.id in local_aliases:
                    offenders.append(f"{path}:{node.lineno}: local prefs namespace alias")

            if call_name == "QSettings" and len(node.args) >= 2:
                namespace = node.args[1]
                if isinstance(namespace, ast.Constant) and isinstance(namespace.value, str):
                    offenders.append(f"{path}:{node.lineno}: literal QSettings namespace")
                elif isinstance(namespace, ast.Name) and namespace.id in local_aliases:
                    offenders.append(f"{path}:{node.lineno}: local QSettings namespace alias")

            if not isinstance(node.func, ast.Attribute) or not node.args:
                continue
            if node.func.attr not in {"value", "setValue", "remove"}:
                continue
            if not _is_settings_receiver(node.func.value):
                continue

            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                offenders.append(f"{path}:{node.lineno}: literal settings key")
            elif isinstance(key, ast.Name) and key.id in local_aliases:
                offenders.append(f"{path}:{node.lineno}: local settings key alias")

    assert offenders == []
