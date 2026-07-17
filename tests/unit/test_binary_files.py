from __future__ import annotations

import os

import pytest

from solin.core.storage import binary_files


def test_write_bytes_atomic_replaces_content_without_leaving_temporary_files(tmp_path) -> None:
    target = tmp_path / "exports" / "certificate.cer"

    binary_files.write_bytes_atomic(target, b"first", mode=0o644)
    binary_files.write_bytes_atomic(target, b"second", mode=0o644)

    assert target.read_bytes() == b"second"
    assert list(target.parent.glob(".certificate.cer.*")) == []


def test_write_bytes_atomic_removes_temporary_file_when_replacement_fails(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "certificate.cer"

    def fail_replace(_source, _target) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(binary_files.os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk unavailable"):
        binary_files.write_bytes_atomic(target, b"certificate")

    assert not target.exists()
    assert list(tmp_path.glob(".certificate.cer.*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_write_bytes_atomic_applies_requested_posix_mode(tmp_path) -> None:
    target = tmp_path / "private.key"

    binary_files.write_bytes_atomic(target, b"secret", mode=0o600)

    assert target.stat().st_mode & 0o777 == 0o600
