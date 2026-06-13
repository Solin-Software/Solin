from __future__ import annotations

import json

import pytest

from solin.core.storage.json_files import read_json_file, write_json_atomic


def test_write_json_atomic_writes_payload_and_removes_temp_files(tmp_path) -> None:
    path = tmp_path / "profiles.json"

    write_json_atomic(path, {"profiles": [{"id": "main"}]}, sort_keys=True)

    assert read_json_file(path) == {"profiles": [{"id": "main"}]}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_json_atomic_can_preserve_trailing_newline(tmp_path) -> None:
    path = tmp_path / "meeting_trees.json"

    write_json_atomic(path, {"trees": {}}, trailing_newline=True)

    assert path.read_text(encoding="utf-8").endswith("\n")


def test_write_json_atomic_removes_temp_file_when_serialization_fails(tmp_path) -> None:
    path = tmp_path / "broken.json"

    with pytest.raises(TypeError):
        write_json_atomic(path, {"bad": object()})

    assert not path.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_read_json_file_propagates_json_decode_errors(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{invalid", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        read_json_file(path)
