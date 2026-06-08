"""
Solin Dev Data Tool
===================

Small GUI utility for local development.

Actions:
  - Delete every known SolinDev QSettings registry tree and dev data/cache dir.
  - Create a clean legacy, pre-profile state to exercise ProfileManager.migrate_legacy().

Windows QSettings live under:
    HKEY_CURRENT_USER\\Software\\SolinDev
    HKEY_CURRENT_USER\\Software\\SolinDev_<profile>

Dev files are resolved with the same QStandardPaths policy used by app/core/foundation/paths.py.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QSettings, QStandardPaths, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


DEV_ORG = "SolinDev"
DEV_APP = "SolinDev"
DEV_PROFILE_PREFIX = "SolinDev_"

@dataclass(frozen=True)
class DevPaths:
    data_dir: Path
    data_root: Path
    cache_dir: Path
    cache_root: Path


def _require_windows_registry() -> bool:
    return sys.platform == "win32"


def _open_software_key(access: int):
    import winreg

    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software", 0, access)


def _enum_subkeys(key) -> Iterable[str]:
    import winreg

    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1


def _delete_tree(parent, subkey_name: str) -> None:
    import winreg

    with winreg.OpenKey(parent, subkey_name, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        for child in list(_enum_subkeys(key)):
            _delete_tree(key, child)
    winreg.DeleteKey(parent, subkey_name)


def discover_dev_registry_orgs() -> list[str]:
    """Return known SolinDev org keys under HKCU\\Software."""
    if not _require_windows_registry():
        return []

    import winreg

    try:
        with _open_software_key(winreg.KEY_READ) as software:
            orgs = [
                org for org in _enum_subkeys(software)
                if org == DEV_ORG or org.startswith(DEV_PROFILE_PREFIX)
            ]
    except FileNotFoundError:
        return []
    return sorted(orgs, key=str.lower)


def delete_dev_registry() -> list[str]:
    """Delete all SolinDev QSettings org keys found in the current user registry."""
    if not _require_windows_registry():
        return ["Registry cleanup skipped: this tool only deletes registry keys on Windows."]

    import winreg

    deleted: list[str] = []
    orgs = discover_dev_registry_orgs()
    if not orgs:
        return ["No SolinDev registry keys found."]

    with _open_software_key(winreg.KEY_READ | winreg.KEY_WRITE) as software:
        for org in orgs:
            _delete_tree(software, org)
            deleted.append(f"Deleted HKCU\\Software\\{org}")
    return deleted


def dev_paths() -> DevPaths:
    """Resolve the same dev locations that app/core/foundation/paths.py resolves."""
    loc = QStandardPaths.StandardLocation
    data_dir = Path(QStandardPaths.writableLocation(loc.AppDataLocation))
    cache_dir = Path(QStandardPaths.writableLocation(loc.CacheLocation))

    data_root = data_dir.parent if data_dir.name == DEV_APP and data_dir.parent.name == DEV_ORG else data_dir

    # Windows CacheLocation is normally %LOCALAPPDATA%/SolinDev/SolinDev/cache.
    if cache_dir.name.lower() == "cache" and cache_dir.parent.name == DEV_APP:
        cache_root = cache_dir.parent.parent
    elif cache_dir.name == DEV_APP and cache_dir.parent.name == DEV_ORG:
        cache_root = cache_dir.parent
    else:
        cache_root = cache_dir

    return DevPaths(
        data_dir=data_dir,
        data_root=data_root,
        cache_dir=cache_dir,
        cache_root=cache_root,
    )


def _is_safe_dev_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return DEV_ORG.lower() in parts and ("appdata" in parts or sys.platform != "win32")


def delete_path_tree(path: Path) -> str:
    if not _is_safe_dev_path(path):
        return f"Skipped unsafe path: {path}"
    if not path.exists():
        return f"Not found: {path}"
    shutil.rmtree(path)
    return f"Deleted {path}"


def delete_dev_files() -> list[str]:
    paths = dev_paths()
    messages: list[str] = []
    for root in (paths.data_root, paths.cache_root):
        try:
            messages.append(delete_path_tree(root))
        except Exception as exc:
            messages.append(f"Failed to delete {root}: {exc}")
    return messages


def clear_dev_data() -> list[str]:
    messages = []
    messages.extend(delete_dev_registry())
    messages.extend(delete_dev_files())
    return messages


def _write_legacy_settings() -> list[str]:
    messages: list[str] = []

    app_settings = QSettings(DEV_ORG, "App")
    app_settings.setValue("language", "pt_BR")
    app_settings.setValue("media_language_code", "T")
    app_settings.setValue("legacy_migration_fixture", True)
    app_settings.sync()
    messages.append("Created legacy QSettings: SolinDev/App")

    projection_settings = QSettings(DEV_ORG, "ProjectionPrefs")
    projection_settings.setValue("legacy_migration_fixture", True)
    projection_settings.setValue("volume", 0.75)
    projection_settings.setValue("obs/enabled", False)
    projection_settings.setValue("obs/port", 4455)
    projection_settings.setValue("obs/default_scene", "Legacy Idle Scene")
    projection_settings.setValue("obs/media_window_scene", "Legacy Media Scene")
    projection_settings.sync()
    messages.append("Created legacy QSettings: SolinDev/ProjectionPrefs")

    return messages


def _write_legacy_files() -> list[str]:
    paths = dev_paths()
    data_dir = paths.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    playlist = {
        "id": f"legacy-{uuid.uuid4()}",
        "name": "Legacy migration fixture",
        "items": [],
    }
    playlists_file = data_dir / "playlists.json"
    playlists_file.write_text(
        json.dumps({"playlists": [playlist]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    images_dir = data_dir / "images"
    embedded_dir = data_dir / "embedded"
    images_dir.mkdir(parents=True, exist_ok=True)
    embedded_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "legacy_migration_marker.txt").write_text(
        "Legacy image folder marker created by qsettings_inspector.py\n",
        encoding="utf-8",
    )
    (embedded_dir / "legacy_migration_marker.txt").write_text(
        "Legacy embedded folder marker created by qsettings_inspector.py\n",
        encoding="utf-8",
    )

    profiles_file = data_dir / "profiles.json"
    if profiles_file.exists():
        profiles_file.unlink()

    return [
        f"Created legacy playlists file: {playlists_file}",
        f"Created legacy images dir: {images_dir}",
        f"Created legacy embedded dir: {embedded_dir}",
        "Confirmed profiles.json is absent.",
    ]


def create_legacy_fixture() -> list[str]:
    messages = clear_dev_data()
    messages.extend(_write_legacy_settings())
    messages.extend(_write_legacy_files())
    return messages


class DevDataTool(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Solin Dev Data Tool")
        self.setMinimumSize(720, 560)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QWidget {
                background: #0d1117;
                color: #e6edf3;
                font-family: Segoe UI;
                font-size: 13px;
            }
            QLabel#Title {
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#Muted {
                color: #8b949e;
            }
            QFrame#Panel {
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 8px;
            }
            QPushButton {
                background: #21262d;
                color: #e6edf3;
                border: 1px solid #30363d;
                border-radius: 7px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #2d333b;
                border-color: #484f58;
            }
            QPushButton#Danger {
                background: #3a1618;
                color: #ffb3b8;
                border-color: #7d242a;
            }
            QPushButton#Danger:hover {
                background: #4c1d21;
                border-color: #a33138;
            }
            QPushButton#Primary {
                background: #1f3a5f;
                color: #cae8ff;
                border-color: #388bfd;
            }
            QPushButton#Primary:hover {
                background: #294f7f;
                border-color: #58a6ff;
            }
            QTextEdit {
                background: #0d1117;
                color: #c9d1d9;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas;
                font-size: 12px;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 22)
        root.setSpacing(14)

        title = QLabel("Solin Dev Data Tool")
        title.setObjectName("Title")
        root.addWidget(title)

        subtitle = QLabel(
            "Limpa o ambiente SolinDev ou cria um estado legado sem perfil para testar a migração."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.paths_label = QLabel()
        self.paths_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.paths_label.setObjectName("Muted")
        self.paths_label.setWordWrap(True)
        root.addWidget(self._panel("Caminhos detectados", self.paths_label))

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        refresh_btn = QPushButton("Atualizar")
        refresh_btn.clicked.connect(self.refresh)
        button_row.addWidget(refresh_btn)

        delete_btn = QPushButton("Deletar tudo do dev")
        delete_btn.setObjectName("Danger")
        delete_btn.clicked.connect(self.delete_dev_clicked)
        button_row.addWidget(delete_btn)

        legacy_btn = QPushButton("Criar dados legados sem perfil")
        legacy_btn.setObjectName("Primary")
        legacy_btn.clicked.connect(self.create_legacy_clicked)
        button_row.addWidget(legacy_btn)

        root.addLayout(button_row)

        self.status = QTextEdit()
        self.status.setReadOnly(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.status, 1)

    def _panel(self, title: str, content: QWidget) -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        label = QLabel(title)
        label.setStyleSheet("font-weight: 700; background: transparent;")
        lay.addWidget(label)
        lay.addWidget(content)
        return panel

    def _append_log(self, lines: Iterable[str]) -> None:
        stamp = time.strftime("%H:%M:%S")
        current = self.status.toPlainText().rstrip()
        block = "\n".join(f"[{stamp}] {line}" for line in lines)
        self.status.setPlainText(f"{current}\n{block}".strip())
        self.status.moveCursor(self.status.textCursor().MoveOperation.End)

    def refresh(self) -> None:
        paths = dev_paths()
        orgs = discover_dev_registry_orgs()
        self.paths_label.setText(
            "\n".join([
                f"Data dir:  {paths.data_dir}",
                f"Data root: {paths.data_root}",
                f"Cache dir: {paths.cache_dir}",
                f"Cache root: {paths.cache_root}",
            ])
        )

        lines = [
            "Estado atual:",
            f"Registry: {', '.join(orgs) if orgs else 'nenhuma chave SolinDev encontrada'}",
            f"Data root exists: {paths.data_root.exists()}",
            f"Cache root exists: {paths.cache_root.exists()}",
        ]
        self.status.setPlainText("\n".join(lines))

    def delete_dev_clicked(self) -> None:
        if self._confirm(
            "Deletar tudo do SolinDev?",
            "Isso remove QSettings, perfis, playlists, cache e dados dev. Feche o SolinDev antes de continuar.",
        ):
            messages = clear_dev_data()
            self.refresh()
            self._append_log(["Ação: deletar tudo do dev", *messages])

    def create_legacy_clicked(self) -> None:
        if self._confirm(
            "Criar estado legado sem perfil?",
            "Isso limpa o SolinDev e recria dados no formato antigo: QSettings em SolinDev/*, "
            "playlists.json na raiz de dados e nenhum profiles.json.",
        ):
            messages = create_legacy_fixture()
            self.refresh()
            self._append_log(["Ação: criar dados legados sem perfil", *messages])

    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(text)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName(DEV_ORG)
    app.setApplicationName(DEV_APP)

    win = DevDataTool()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
