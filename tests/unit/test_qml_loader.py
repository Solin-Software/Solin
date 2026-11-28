from __future__ import annotations

import pytest
from PySide6.QtQuickWidgets import QQuickWidget

from solin.bootstrap import qml_runtime_verification
from solin.ui.qml import loader


@pytest.mark.parametrize(
    ("source", "archive", "error"),
    [
        (False, False, "No Solin QML module sources or resource archive"),
        (True, True, "Both QML sources and a resource archive"),
        (False, True, "Could not register Solin QML resource archive"),
    ],
)
def test_qml_loader_rejects_invalid_deployments(tmp_path, source, archive, error):
    qml_root = tmp_path / "qml"
    qml_root.mkdir()
    if source:
        module = qml_root / "Solin"
        module.mkdir()
        (module / "qmldir").write_text("module Solin", encoding="utf-8")
    if archive:
        (qml_root / "solin-qml.rcc").write_bytes(b"invalid RCC archive")
    widget = QQuickWidget()

    with pytest.raises(RuntimeError, match=error):
        loader._configure_import_root(widget.engine(), qml_root)


def test_synchronous_load_of_unknown_type_reports_failure():
    widget = QQuickWidget()
    handle = loader.QmlLoadHandle(
        widget, loader.QML_MODULE_URI, "MissingQmlType", asynchronous=False,
    )
    errors: list[str] = []
    handle.failed.connect(errors.append)

    handle.start()

    assert handle.state is loader.QmlLoadState.FAILED
    assert widget.rootObject() is None
    assert len(errors) == 1 and "MissingQmlType" in errors[0]
    assert handle.failure_message == errors[0]


def test_runtime_qualification_reports_a_missing_deployment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loader, "__file__", str(tmp_path / "ui/qml/loader.py"))

    assert qml_runtime_verification.main(["--verify-qml-runtime"]) == 1

    assert "No Solin QML module sources or resource archive" in capsys.readouterr().err
