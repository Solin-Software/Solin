from __future__ import annotations

from pathlib import Path

from tests._paths import REPO_ROOT


def _read(relative_path: str) -> str:
    return (REPO_ROOT / Path(relative_path)).read_text(encoding="utf-8")


def test_windows_builds_attach_to_existing_terminal_without_spawning_one():
    for relative_path in (
        "scripts/build_solin.bat",
        ".github/workflows/build-solin-windows.yml",
    ):
        text = _read(relative_path)

        assert "--windows-console-mode=attach" in text
        assert "--windows-console-mode=disable" not in text
        assert "--windows-console-mode=force" not in text


def test_macos_bundle_keeps_finder_launch_gui_only():
    text = _read(".github/workflows/build-solin-macos.yml")

    assert "--macos-create-app-bundle" in text
    assert "--macos-app-console-mode=disable" in text
    assert "--macos-app-console-mode=force" not in text
