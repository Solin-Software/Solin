from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from solin.core.ingest import manifest as manifest_module
from solin.core.ingest.manifest import (
    MANIFEST_FILE,
    ManifestError,
    ManifestRepository,
    ManifestWriteError,
    retry_manifest_write,
)


def _replace_payload(payload: dict):
    def mutation(manifest: dict) -> None:
        manifest.clear()
        manifest.update(payload)

    return mutation


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_replace_failure_is_classified_and_preserves_previous_manifest(
    monkeypatch,
    tmp_path,
    winerror,
) -> None:
    repository = ManifestRepository()
    repository.update(
        tmp_path,
        _replace_payload({"version": 1, "processed": {}, "playlist": {"old": True}}),
    )

    denied = PermissionError("busy")
    denied.winerror = winerror

    def fail_replace(_source, _target) -> None:
        raise denied

    monkeypatch.setattr(manifest_module.os, "replace", fail_replace)

    with pytest.raises(ManifestWriteError) as raised:
        repository.update(
            tmp_path,
            lambda manifest: manifest.update({"playlist": {"new": True}}),
        )

    assert raised.value.operation == "replace"
    assert raised.value.retryable is True
    assert raised.value.winerror == winerror
    assert repository.load(tmp_path, strict=True)["playlist"] == {"old": True}
    assert not list(tmp_path.glob(f".{MANIFEST_FILE}.*.tmp"))


def test_serialization_failure_is_permanent_and_preserves_previous_manifest(
    tmp_path,
) -> None:
    repository = ManifestRepository()
    repository.update(
        tmp_path,
        _replace_payload({"version": 1, "processed": {}, "stable": True}),
    )

    with pytest.raises(ManifestWriteError) as raised:
        repository.update(
            tmp_path,
            lambda manifest: manifest.update({"invalid": object()}),
        )

    assert raised.value.operation == "serialize"
    assert raised.value.retryable is False
    assert repository.load(tmp_path, strict=True)["stable"] is True
    assert not list(tmp_path.glob(f".{MANIFEST_FILE}.*.tmp"))


def test_transaction_updates_preserve_independent_namespaces(tmp_path) -> None:
    repository = ManifestRepository()
    repository.update(
        tmp_path,
        lambda manifest: manifest.update({"playlist": {"items": ["one"]}}),
    )
    repository.update(
        tmp_path,
        lambda manifest: manifest["processed"].update({"source.pdf": {"size": 1}}),
    )
    repository.update(
        tmp_path,
        lambda manifest: manifest.update({"meeting_tree": {"revision": 2}}),
    )

    saved = repository.load(tmp_path, strict=True)
    assert saved["playlist"] == {"items": ["one"]}
    assert saved["processed"] == {"source.pdf": {"size": 1}}
    assert saved["meeting_tree"] == {"revision": 2}


def test_concurrent_thread_transactions_do_not_lose_updates(tmp_path) -> None:
    repository = ManifestRepository()
    start = threading.Barrier(5)

    def update(index: int) -> None:
        start.wait()
        repository.update(
            tmp_path,
            lambda manifest: manifest["processed"].update({str(index): index}),
        )

    threads = [threading.Thread(target=update, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert repository.load(tmp_path, strict=True)["processed"] == {
        str(index): index for index in range(4)
    }


def test_strict_update_never_overwrites_corrupted_manifest(tmp_path) -> None:
    target = tmp_path / MANIFEST_FILE
    target.write_text("{not-json", encoding="utf-8")
    repository = ManifestRepository()

    with pytest.raises(ManifestError):
        repository.update(
            tmp_path,
            lambda manifest: manifest.update({"playlist": {}}),
            strict=True,
        )

    assert target.read_text(encoding="utf-8") == "{not-json"
    with pytest.raises(json.JSONDecodeError):
        json.loads(target.read_text(encoding="utf-8"))


def test_cleanup_retry_recovers_from_transient_manifest_lock(monkeypatch) -> None:
    attempts = 0
    cause = PermissionError("busy")
    cause.winerror = 5
    monkeypatch.setattr(manifest_module.time, "sleep", lambda _delay: None)

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ManifestWriteError(
                Path("manifest.json"),
                operation="replace",
                retryable=True,
                cause=cause,
            )
        return "saved"

    assert retry_manifest_write(operation) == "saved"
    assert attempts == 3
