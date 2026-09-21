from __future__ import annotations

import pytest

from solin.core.ingest.sync.activation import (
    ActivationConflict,
    ActivationError,
    ActivationStore,
)


def test_activation_marker_presence_selects_one_immutable_document(tmp_path) -> None:
    store = ActivationStore(tmp_path, "meeting")
    first = "a" * 64
    second = "b" * 64
    subject = {"tree_key": "meeting:2026-05-25"}

    assert store.read() is None
    assert store.publish(first, subject).document_id == first
    assert store.publish(first, subject).document_id == first
    with pytest.raises(ActivationConflict):
        store.publish(second, subject)
    with pytest.raises(ActivationConflict):
        store.remove(second)

    assert store.read().document_id == first
    assert store.remove(first)
    assert store.read() is None


def test_partial_activation_is_pending_data_instead_of_absence(tmp_path) -> None:
    store = ActivationStore(tmp_path, "meeting")
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b'{"version":1')

    with pytest.raises(ActivationError, match="Incomplete"):
        store.read()
