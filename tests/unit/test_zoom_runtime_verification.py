from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.bootstrap import zoom_runtime_verification
from tests._paths import REPO_ROOT


def _run_headless(
    arguments: list[str], *, cwd: Path, prelude: str = "",
) -> subprocess.CompletedProcess[str]:
    code = (
        "import runpy, sys; "
        "sys.modules['PySide6'] = None; "
        "sys.modules['solin.bootstrap.application'] = None; "
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r}); "
        + prelude
        + f"sys.argv = ['Solin', *{arguments!r}]; "
        f"runpy.run_path({str(REPO_ROOT / 'main.py')!r}, run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=15, cwd=cwd,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--verify-zoom-runtime=extra"],
        ["--verify-zoom-runtime", "extra"],
        ["--create-profile", "--verify-zoom-runtime"],
        ["--verify-zoom-runtime", "--verify-zoom-runtime"],
        ["--zoom-automation", "--verify-zoom-runtime"],
        ["--camera-discovery", "--verify-zoom-runtime"],
        ["--scene-engine-sidecar", "--verify-zoom-runtime"],
        ["--verify-media-runtime", "--verify-zoom-runtime"],
        ["--verify-http-runtime", "--verify-zoom-runtime"],
    ],
)
def test_invalid_arguments_fail_as_json_before_native_or_qt_import(arguments, tmp_path):
    result = _run_headless(
        arguments, cwd=tmp_path,
        prelude="sys.modules['solin.core.integrations.automation.zoom.controls'] = None; ",
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solin.zoom-runtime.v1", "ok": False, "error": "ValueError",
    }
    assert "Usage: --verify-zoom-runtime" in result.stderr


def test_missing_role_argument_is_rejected_before_verification(monkeypatch, capsys):
    verify = Mock(side_effect=AssertionError("Unexpected native initialization"))
    monkeypatch.setattr(zoom_runtime_verification, "_verify", verify)
    assert zoom_runtime_verification.main([]) == 2
    verify.assert_not_called()
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"


def test_verification_only_initializes_automation(monkeypatch, capsys):
    automation = Mock(return_value=object())
    monkeypatch.setattr(
        zoom_runtime_verification, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr),
    )
    monkeypatch.setitem(
        sys.modules, "solin.core.integrations.automation.zoom.controls",
        SimpleNamespace(Automation=automation),
    )

    assert zoom_runtime_verification.main(["--verify-zoom-runtime"]) == 0

    automation.assert_called_once_with()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"schema": "solin.zoom-runtime.v1", "ok": True}


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unsupported_platform_fails_before_importing_native_runtime(platform, tmp_path):
    result = _run_headless(
        ["--verify-zoom-runtime"], cwd=tmp_path,
        prelude=(
            "from types import SimpleNamespace; "
            "from solin.bootstrap import zoom_runtime_verification as verifier; "
            f"verifier.sys = SimpleNamespace(platform={platform!r}, stderr=sys.stderr); "
            "sys.modules['solin.core.integrations.automation.zoom.controls'] = None; "
        ),
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solin.zoom-runtime.v1", "ok": False, "error": "RuntimeError",
    }
    assert "available only on Windows" in result.stderr


@pytest.mark.parametrize("failure", ["ModuleNotFoundError", "OSError", "RuntimeError"])
def test_native_failure_is_reported_by_headless_process(failure, tmp_path):
    result = _run_headless(
        ["--verify-zoom-runtime"], cwd=tmp_path,
        prelude=(
            "from types import SimpleNamespace; "
            "from solin.bootstrap import zoom_runtime_verification as verifier; "
            "verifier.sys = SimpleNamespace(platform='win32', stderr=sys.stderr); "
            f"exec('def fail():\\n    raise {failure}(\"Native runtime unavailable\")'); "
            "sys.modules['solin.core.integrations.automation.zoom.controls'] = "
            "SimpleNamespace(Automation=fail); "
        ),
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solin.zoom-runtime.v1", "ok": False, "error": failure,
    }
    assert "Native runtime unavailable" in result.stderr


def test_native_import_failure_is_reported_by_headless_process(tmp_path):
    result = _run_headless(
        ["--verify-zoom-runtime"], cwd=tmp_path,
        prelude=(
            "from types import SimpleNamespace; "
            "from solin.bootstrap import zoom_runtime_verification as verifier; "
            "verifier.sys = SimpleNamespace(platform='win32', stderr=sys.stderr); "
            "sys.modules['solin.core.integrations.automation.zoom.controls'] = None; "
        ),
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solin.zoom-runtime.v1", "ok": False, "error": "ModuleNotFoundError",
    }
    assert "solin.core.integrations.automation.zoom.controls" in result.stderr


def test_headless_success_does_not_require_qt_or_a_meeting(tmp_path):
    result = _run_headless(
        ["--verify-zoom-runtime"], cwd=tmp_path,
        prelude=(
            "from types import SimpleNamespace; "
            "from solin.bootstrap import zoom_runtime_verification as verifier; "
            "verifier.sys = SimpleNamespace(platform='win32', stderr=sys.stderr); "
            "sys.modules['solin.core.integrations.automation.zoom.controls'] = "
            "SimpleNamespace(Automation=object); "
        ),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"schema": "solin.zoom-runtime.v1", "ok": True}
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
