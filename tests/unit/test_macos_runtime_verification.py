from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.bootstrap import macos_runtime_verification as verification
from tests._paths import REPO_ROOT


@pytest.mark.parametrize("arguments", [
    ["--verify-macos-runtime=extra"],
    ["--verify-macos-runtime", "--create-profile"],
    ["--scene-engine-sidecar", "--verify-macos-runtime"],
])
def test_invalid_verification_exits_before_gui_and_application_imports(arguments, tmp_path):
    code = (
        "import runpy,sys; sys.modules['PySide6']=None; "
        "sys.modules['solin.bootstrap.application']=None; "
        f"sys.argv=['Solin', *{arguments!r}]; "
        f"runpy.run_path({str(REPO_ROOT / 'main.py')!r},run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["error"] == "ValueError"
    assert list(tmp_path.iterdir()) == []


def test_verification_rejects_unsupported_platform_before_native_imports(monkeypatch, capsys):
    monkeypatch.setattr(verification, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setitem(sys.modules, "pylibobs.macos_camera", None)
    assert verification.main(["--verify-macos-runtime"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "RuntimeError"


@pytest.mark.parametrize("failure", [None, "camera", "surface", "backend"])
def test_verification_checks_loaders_and_closes_the_surface_on_failure(monkeypatch, capsys, failure):
    monkeypatch.setattr(verification, "sys", SimpleNamespace(platform="darwin", stderr=sys.stderr))
    camera = Mock(side_effect=OSError("Missing camera helper") if failure == "camera" else None)
    monkeypatch.setitem(sys.modules, "pylibobs.macos_camera", SimpleNamespace(_native=camera))
    app = Mock()
    app.platformName.return_value = "offscreen" if failure == "backend" else "cocoa"
    application = Mock(return_value=app)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", SimpleNamespace(QApplication=application))

    class Surface:
        def __init__(self):
            self.resize = Mock()
            self.close = Mock()

        @property
        def surface_endpoint(self):
            if failure == "surface":
                raise OSError("Missing surface helper")
            return "private-surface-endpoint"

    surface = Surface()
    create_surface = Mock(return_value=surface)
    monkeypatch.setitem(sys.modules, "solin.widgets.projection.native_surface",
                        SimpleNamespace(NativeVideoSurface=create_surface))
    code = verification.main(["--verify-macos-runtime"])
    result = json.loads(capsys.readouterr().out)
    assert code == (0 if failure is None else 1)
    assert result["ok"] is (failure is None)
    if failure in ("camera", "backend"):
        create_surface.assert_not_called()
    else:
        surface.close.assert_called_once()
    if failure == "camera":
        application.assert_not_called()
