from __future__ import annotations

import json

from solin.ui.qml.loader import _compiled_qml_module_is_complete


def _write_module(module_dir, *, include_bytecode: bool = True) -> None:
    module_dir.mkdir(parents=True)
    (module_dir / "qml-manifest.json").write_text(
        json.dumps({"schema": 1, "sources": {"Startup.qml": "digest"}}),
        encoding="utf-8",
    )
    (module_dir / "qmldir").write_text(
        "module Solin\nStartup 1.0 q_startup.qml\n",
        encoding="utf-8",
    )
    if include_bytecode:
        (module_dir / "q_startup.qmlc").write_bytes(b"compiled")


def test_packaged_qml_module_accepts_manifested_bytecode_without_sources(tmp_path) -> None:
    module_dir = tmp_path / "Solin"
    _write_module(module_dir)

    assert _compiled_qml_module_is_complete(module_dir) is True


def test_packaged_qml_module_rejects_a_manifest_with_missing_bytecode(tmp_path) -> None:
    module_dir = tmp_path / "Solin"
    _write_module(module_dir, include_bytecode=False)

    assert _compiled_qml_module_is_complete(module_dir) is False
