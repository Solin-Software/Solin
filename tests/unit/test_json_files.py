from __future__ import annotations

import json

import pytest

from solin.core.storage import json_files
from solin.core.storage.json_files import JsonDirectorySyncError, read_json_file, write_json_atomic
from solin.core.storage.json_repository import JsonFileRepository


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


@pytest.mark.parametrize("after_replacement", [False, True])
def test_atomic_json_write_distinguishes_replacement_from_durability(
    tmp_path, monkeypatch, after_replacement,
) -> None:
    path = tmp_path / "state.json"
    write_json_atomic(path, {"revision": 1})

    def fail(*_args):
        raise OSError("injected write failure")

    if after_replacement:
        monkeypatch.setattr(json_files, "_sync_parent_directory", fail)
    else:
        monkeypatch.setattr(json_files.os, "replace", fail)

    with pytest.raises(OSError) as result:
        write_json_atomic(path, {"revision": 2})

    assert isinstance(result.value, JsonDirectorySyncError) is after_replacement
    assert read_json_file(path) == {"revision": 2 if after_replacement else 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_read_json_file_propagates_json_decode_errors(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{invalid", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        read_json_file(path)


def test_json_file_repository_owns_one_atomic_json_document(tmp_path) -> None:
    repository = JsonFileRepository(tmp_path / "state" / "playlists.json")

    repository.write({"playlists": [{"id": "main"}]}, sort_keys=True)

    assert repository.path == tmp_path / "state" / "playlists.json"
    assert repository.exists()
    assert repository.read() == {"playlists": [{"id": "main"}]}
    assert list((tmp_path / "state").glob(".*.tmp")) == []
