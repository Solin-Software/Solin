from __future__ import annotations

from pathlib import Path

from tests._paths import REPO_ROOT


_SRC_ROOT = REPO_ROOT / "src" / "solin"
_QML_HOST = _SRC_ROOT / "ui" / "qml" / "host.py"
_QML_LOADER = _SRC_ROOT / "ui" / "qml" / "loader.py"
_BOOTSTRAP_APPLICATION = _SRC_ROOT / "bootstrap" / "application.py"


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in _SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_qml_types_load_through_the_shared_host_or_loader():
    allowed = {_QML_HOST, _QML_LOADER}
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in _python_sources()
        if path not in allowed and "load_qml_type(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_qquickwidget_surface_setup_stays_centralized():
    allowed = {_QML_HOST, _BOOTSTRAP_APPLICATION}
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in _python_sources()
        if path not in allowed and "QSurfaceFormat()" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_qml_image_providers_register_through_the_shared_host():
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in _python_sources()
        if path != _QML_HOST and ".addImageProvider(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
