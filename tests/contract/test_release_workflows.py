"""Cross-workflow guarantees required before a distribution can be published."""
from pathlib import Path
import re
import subprocess

import pytest
import yaml

from tests._paths import REPO_ROOT


def workflow(name: str) -> dict:
    return yaml.load((REPO_ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_publication_requires_all_platforms_and_quality():
    release = workflow("release.yml")
    assert set(release["jobs"]["publish"]["needs"]) == {"quality", "windows", "linux", "macos"}
    assert release["jobs"]["publish"]["concurrency"]["cancel-in-progress"] == "false"
    for platform in ("windows", "linux", "macos"):
        assert release["jobs"][platform]["with"]["release"] == "true"
        build = workflow(f"build-solin-{platform}.yml")
        assert "workflow_call" in build["on"]
        assert "workflow_dispatch" in build["on"]
        assert build["permissions"]["contents"] == "read"
    mac = workflow("build-solin-macos.yml")["jobs"]["build"]
    assert mac["strategy"]["fail-fast"] == "false"
    assert {row["architecture"] for row in mac["strategy"]["matrix"]["include"]} == {"arm64", "x86_64"}


def test_quality_gate_waits_for_every_platform():
    quality = workflow("quality.yml")["jobs"]
    assert quality["gate"]["name"] == "Quality Gate"
    assert quality["gate"]["if"] == "always()"
    assert set(quality["gate"]["needs"]) == {"changes", "static", "platforms"}
    assert quality["platforms"]["needs"] == "changes"
    assert quality["platforms"]["if"] == (
        "needs.changes.outputs.application_required == 'true'"
    )
    assert "git diff --no-renames --name-only" in quality["changes"]["steps"][1]["run"]
    assert len(quality["platforms"]["strategy"]["matrix"]["include"]) == 4


def test_all_external_actions_are_pinned_to_commits():
    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"uses: ([^\s#]+)", path.read_text()):
            assert action.startswith("./") or re.search(r"@[a-f0-9]{40}$", action), action


def test_linux_smoke_rejects_clean_early_exit():
    text = (REPO_ROOT / ".github/workflows/build-solin-linux.yml").read_text()
    assert 'exit "$status"' not in text
    block = re.search(r'if \[\[ "\$status" -ne 124.*?fi', text, re.DOTALL)
    assert block is not None
    bash = Path("C:/Program Files/Git/bin/bash.exe")
    if not bash.exists():
        import shutil
        executable = shutil.which("bash")
        if executable is None:
            pytest.skip("bash is not installed")
    else:
        executable = str(bash)
    assert subprocess.run([executable, "-c", "status=0; " + block.group()], check=False).returncode == 1


def test_windows_distribution_contains_only_the_full_installer():
    text = (REPO_ROOT / ".github/workflows/build-solin-windows.yml").read_text()
    upload = text.split("- name: Upload Windows packages", 1)[1]
    assert "dist/Solin-${{ env.APP_VERSION }}-windows-x86_64.exe" in upload
    assert "steps.standalone.outputs.path" not in upload
    assert "Package standalone zip" not in text
    assert "SOLIN_SIGNING_CERTIFICATE" not in text
    assert "sign_windows_artifacts" not in text
    assert "&& '' || '-unsigned-diagnostic'" not in text


def test_platform_builders_share_pinned_packaging_tools():
    requirements = (REPO_ROOT / "requirements-build.txt").read_text().splitlines()
    assert requirements == ["Nuitka==4.1", "ordered-set==4.1.0", "zstandard==0.25.0"]
    for platform in ("windows", "linux", "macos"):
        text = (REPO_ROOT / f".github/workflows/build-solin-{platform}.yml").read_text()
        assert "requirements-build.txt" in text
        assert "Nuitka==4.1 ordered-set==4.1.0" not in text
