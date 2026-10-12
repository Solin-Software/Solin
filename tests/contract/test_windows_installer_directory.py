from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT
from tests.e2e._windows_process import run_windows_process_tree


def _install_delete_section(setup: str) -> str:
    return setup.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]


def test_upgrade_cleanup_is_scoped_to_installer_owned_retired_payloads() -> None:
    setup = (REPO_ROOT / "packaging/windows/installer/setup.iss").read_text(encoding="utf-8")
    entries = [
        line.strip()
        for line in _install_delete_section(setup).splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    assert set(entries) == {
        'Type: filesandordirs; Name: "{app}\\backports"',
        'Type: filesandordirs; Name: "{app}\\native\\media-engine"',
    }


def test_silent_reinstall_preserves_files_in_existing_directory(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("The Inno directory contract requires Windows.")
    compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("The Inno directory contract requires Inno Setup 6.")
    setup = (REPO_ROOT / "packaging/windows/installer/setup.iss").read_text(encoding="utf-8")
    policy = re.search(r"^DirExistsWarning=(.+)$", setup, re.MULTILINE)
    assert policy is not None
    # Include any custom directory validation in the executable probe as well.
    # This reproduces the production silent-navigation behavior, without its
    # app/camera registration, shortcuts or uninstall hooks.
    handler = ""
    if "function NextButtonClick(" in setup:
        handler = setup.split("function NextButtonClick(", 1)[1].split(
            '#include "uninstall.iss"', 1
        )[0]
        handler = "function NextButtonClick(" + handler
    install = tmp_path / "installed"
    install.mkdir()
    sentinel = install / "user-kept.txt"
    sentinel.write_text("user-owned file", encoding="utf-8")
    obsolete_engine = install / "native" / "media-engine"
    for relative in (
        "solin-media-engine.exe",
        "qt-media-bridge/obsolete.pyd",
        "gstreamer/bin/obsolete.dll",
    ):
        path = obsolete_engine / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retired engine payload")
    obsolete_extension = install / "backports" / "zstd" / "obsolete.pyd"
    obsolete_extension.parent.mkdir(parents=True)
    obsolete_extension.write_bytes(b"retired optional extension")
    preserved = {}
    for base, relatives in (
        (
            install,
            (
                "native/virtual-camera/x64/solin-virtual-camera.dll",
                "native/virtual-camera/x86/solin-virtual-camera.dll",
                "native/user-kept.dll",
            ),
        ),
        (
            tmp_path / "localappdata" / "Solin" / "VirtualCamera",
            (
                "versions/pair/x64/solin-virtual-camera.dll",
                "versions/pair/x86/solin-virtual-camera.dll",
            ),
        ),
        (
            tmp_path / "commonpf64" / "Solin" / "VirtualCamera",
            (
                "versions/pair/x64/solin-virtual-camera.dll",
                "versions/pair/x86/solin-virtual-camera.dll",
            ),
        ),
    ):
        for relative in relatives:
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(path.relative_to(tmp_path)).encode("utf-8")
            path.write_bytes(content)
            preserved[path] = content
    payload = tmp_path / "Solin.exe"
    payload.write_bytes(b"private fixture payload")
    recipe = f"""#define MyAppExeName "Solin.exe"
[Setup]
AppName=Private Directory Contract
AppVersion=1
DefaultDirName={{#SourcePath}}\\installed
PrivilegesRequired=lowest
Uninstallable=no
CreateUninstallRegKey=no
DisableDirPage=no
DisableProgramGroupPage=yes
DirExistsWarning={policy.group(1).strip()}
OutputDir=.
OutputBaseFilename=probe
[InstallDelete]
{_install_delete_section(setup)}
[Files]
Source: "Solin.exe"; DestDir: "{{app}}"
[Code]
{handler}
"""
    script = tmp_path / "probe.iss"
    script.write_text(recipe, encoding="utf-8")
    compiled = subprocess.run(
        [str(compiler), "/Q", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = run_windows_process_tree(
        [
            str(tmp_path / "probe.exe"),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={install}",
            f"/LOG={tmp_path / 'setup.log'}",
        ],
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "Solin.exe").read_bytes() == payload.read_bytes()
    assert sentinel.read_text(encoding="utf-8") == "user-owned file"
    assert not obsolete_engine.exists(), "retired media-engine payload survived upgrade"
    assert not obsolete_extension.exists(), "retired Python extension survived upgrade"
    for path, content in preserved.items():
        assert path.read_bytes() == content, f"upgrade cleanup changed {path.relative_to(tmp_path)}"
