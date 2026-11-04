from __future__ import annotations

import hashlib
import io
import lzma
import zipfile
import zlib

import pytest

from solin.core.jw import publication_archive
from solin.core.jw.publication_archive import (
    JwpubArchiveDownloadError,
    download_jwpub_archive,
    resolve_meeting_media,
)
from solin.core.jw.publication_links import PubMediaFile
from solin.core.network.http import HttpError
from solin.core.storage import archive_budget
from tests._jwpub_fixture import build_synthetic_jwpub


class _Stream:
    def __init__(self, payload: bytes, *, headers=None):
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))} if headers is None else headers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def iter_bytes(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]


@pytest.fixture(scope="module")
def jwpub_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("archive-fixture") / "synthetic.jwpub"
    return build_synthetic_jwpub(path).read_bytes()


@pytest.fixture
def destination(tmp_path, jwpub_bytes):
    path = tmp_path / "downloads" / "archive.jwpub"
    path.parent.mkdir()
    path.write_bytes(jwpub_bytes + b"previous version")
    return path


def _assert_preserved(destination, previous):
    assert destination.read_bytes() == previous
    assert set(destination.parent.iterdir()) == {destination}


def _outer_archive(contents: bytes, *, compression=zipfile.ZIP_STORED, extra_members=None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("contents", contents)
        archive.writestr("manifest.json", b"{}")
        for name, payload in (extra_members or {}).items():
            archive.writestr(name, payload)
    return output.getvalue()


def _nested_archive(files, *, extra_members=None):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        for name, payload in files.items():
            inner.writestr(name, payload)
    return _outer_archive(
        contents.getvalue(), compression=zipfile.ZIP_DEFLATED, extra_members=extra_members
    )


def _corrupt_member(payload: bytes, name: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            target.writestr(member.filename, source.read(member))
    payload = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = archive.getinfo(name)
    offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    damaged = bytearray(payload)
    damaged[offset] ^= 0xFF
    return bytes(damaged)


def test_download_jwpub_archive_writes_file_and_reports_progress(tmp_path, jwpub_bytes):
    calls = []
    progress = []

    def stream_factory(url: str, *, timeout: int, headers: dict[str, str]):
        calls.append((url, timeout, headers))
        return _Stream(jwpub_bytes)

    destination = tmp_path / "mwb_T" / "mwb_T_20260600.jwpub"

    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        progress=progress.append,
        stream_factory=stream_factory,
        timeout=12,
        chunk_size=(len(jwpub_bytes) + 2) // 3,
    )

    assert destination.read_bytes() == jwpub_bytes
    assert progress == sorted(set(progress))
    assert len(progress) > 1
    assert progress[-1] == 100
    assert all(0 <= value <= 100 for value in progress)
    assert set(destination.parent.iterdir()) == {destination}
    assert calls == [
        (
            "https://example.test/mwb.jwpub",
            12,
            {"User-Agent": "Mozilla/5.0"},
        )
    ]


def test_download_jwpub_archive_raises_transport_independent_error(tmp_path):
    def stream_factory(url: str, *, timeout: int, headers: dict[str, str]):
        raise HttpError("offline")

    with pytest.raises(JwpubArchiveDownloadError, match="offline"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            tmp_path / "archive.jwpub",
            stream_factory=stream_factory,
        )


def test_download_streams_to_disk_before_requesting_more_chunks(destination, jwpub_bytes):
    previous = destination.read_bytes()
    observed = False

    class ObservedStream(_Stream):
        def iter_bytes(self, chunk_size: int):
            nonlocal observed
            yield self.payload[:chunk_size]
            staging = set(destination.parent.iterdir()) - {destination}
            assert any(path.stat().st_size > 0 for path in staging)
            assert destination.read_bytes() == previous
            observed = True
            yield self.payload[chunk_size:]

    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        stream_factory=lambda *_args, **_kwargs: ObservedStream(jwpub_bytes),
        chunk_size=len(jwpub_bytes) // 2,
    )

    assert observed
    assert destination.read_bytes() == jwpub_bytes
    assert set(destination.parent.iterdir()) == {destination}


@pytest.mark.parametrize("stage", ["before", "during", "after"])
@pytest.mark.parametrize("existing", [False, True])
def test_download_cancellation_cleans_staging_and_preserves_destination(
    destination, jwpub_bytes, stage, existing
):
    previous = destination.read_bytes()
    if not existing:
        destination.unlink()
    cancelled = stage == "before"

    class CancellableStream(_Stream):
        def iter_bytes(self, chunk_size: int):
            nonlocal cancelled
            yield self.payload[:chunk_size]
            if stage == "during":
                cancelled = True
            yield self.payload[chunk_size:]
            cancelled = True

    def stream_factory(*_args, **_kwargs):
        assert stage != "before", "Cancellation must be checked before opening the connection"
        return CancellableStream(jwpub_bytes)

    with pytest.raises(JwpubArchiveDownloadError, match="cancelled"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=stream_factory,
            chunk_size=len(jwpub_bytes) // 2,
            cancelled=lambda: cancelled,
        )

    if existing:
        _assert_preserved(destination, previous)
    else:
        assert not list(destination.parent.iterdir())


@pytest.mark.parametrize("layer", ["outer", "inner"])
def test_download_honors_cancellation_during_validation(
    monkeypatch, destination, jwpub_bytes, layer
):
    previous = destination.read_bytes()
    cancelled = False
    read = zipfile.ZipExtFile.read

    def cancel_while_reading(member, size=-1):
        nonlocal cancelled
        assert not cancelled, "Validation continued reading after cancellation"
        result = read(member, size)
        target_member = (
            member.name == "contents" if layer == "outer" else member.name.endswith(".db")
        )
        if target_member and size > 0 and result:
            cancelled = True
        return result

    monkeypatch.setattr(zipfile.ZipExtFile, "read", cancel_while_reading)
    with pytest.raises(JwpubArchiveDownloadError, match="cancelled"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            cancelled=lambda: cancelled,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


def test_download_rechecks_cancellation_before_replace(monkeypatch, destination, jwpub_bytes):
    previous = destination.read_bytes()
    cancelled = False
    validate = publication_archive._validate_jwpub_archive

    def cancel_after_validation(*args, **kwargs):
        nonlocal cancelled
        validate(*args, **kwargs)
        cancelled = True

    monkeypatch.setattr(publication_archive, "_validate_jwpub_archive", cancel_after_validation)
    with pytest.raises(JwpubArchiveDownloadError, match="cancelled"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            cancelled=lambda: cancelled,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


def test_download_bounds_reads_when_validating_compressed_nested_archives(monkeypatch, destination):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as inner:
        inner.writestr("publication.db", b"x" * (3 * 1024**2))
    payload = _outer_archive(contents.getvalue(), compression=zipfile.ZIP_DEFLATED)
    read = zipfile.ZipExtFile.read

    def bounded_read(member, size=-1):
        assert 0 <= size <= 1024**2, "Nested validation must use bounded, cancellable reads"
        return read(member, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        stream_factory=lambda *_args, **_kwargs: _Stream(payload),
    )

    assert destination.read_bytes() == payload
    assert set(destination.parent.iterdir()) == {destination}


def test_download_limits_uncompressed_validation_spool(monkeypatch, destination, jwpub_bytes):
    previous = destination.read_bytes()
    with zipfile.ZipFile(io.BytesIO(jwpub_bytes)) as outer:
        contents_size = outer.getinfo("contents").file_size
    monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", contents_size - 1)

    with pytest.raises(JwpubArchiveDownloadError, match="budget"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


@pytest.mark.parametrize("layer", ["outer", "inner"])
def test_download_rejects_declared_expansion_before_opening_oversized_layer(
    monkeypatch, destination, layer
):
    previous = destination.read_bytes()
    payload = _nested_archive(
        {"publication.db": b"sqlite", **({"image.jpg": b"x" * 4096} if layer == "inner" else {})},
        extra_members={"unused.bin": b"x" * 4096} if layer == "outer" else {},
    )
    monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", 2048)
    original_open = zipfile.ZipFile.open
    opened = []

    def observe_open(archive, member, *args, **kwargs):
        opened.append("outer" if "contents" in archive.namelist() else "inner")
        return original_open(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", observe_open)
    with pytest.raises(JwpubArchiveDownloadError, match="budget"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload),
        )

    assert layer not in opened
    _assert_preserved(destination, previous)


@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_download_accounts_for_both_layers_and_outer_extras_at_budget_boundary(
    monkeypatch, destination, headroom
):
    previous = destination.read_bytes()
    files = {"publication.db": b"sqlite", "image.jpg": b"x" * 64}
    extras = {"unused.bin": b"y" * 64}
    payload = _nested_archive(files, extra_members=extras)
    with zipfile.ZipFile(io.BytesIO(payload)) as outer:
        outer_size = sum(member.file_size for member in outer.infolist())
    expanded = outer_size + sum(map(len, files.values()))
    monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", expanded + headroom)

    def download():
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload, headers={}),
        )

    if headroom < 0:
        with pytest.raises(JwpubArchiveDownloadError, match="budget"):
            download()
        _assert_preserved(destination, previous)
    else:
        download()
        assert destination.read_bytes() == payload
        assert set(destination.parent.iterdir()) == {destination}


@pytest.mark.parametrize("layer", ["outer", "inner", "combined"])
def test_download_bounds_member_count_before_opening_rejected_layer(
    monkeypatch, destination, layer
):
    previous = destination.read_bytes()
    files = {"publication.db": b"sqlite"}
    extras = {}
    if layer == "inner":
        files.update({f"empty-{index}": b"" for index in range(4)})
    elif layer == "outer":
        extras = {f"empty-{index}": b"" for index in range(4)}
    payload = _nested_archive(files, extra_members=extras)
    # Even one inner member exceeds a combined allowance of two outer members.
    monkeypatch.setattr(archive_budget, "MAX_ARCHIVE_MEMBERS", 2)
    original_open = zipfile.ZipFile.open
    opened = []

    def observe_open(archive, member, *args, **kwargs):
        opened.append("outer" if "contents" in archive.namelist() else "inner")
        return original_open(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", observe_open)
    with pytest.raises(JwpubArchiveDownloadError, match="member.*budget"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload),
        )

    assert ("outer" if layer == "outer" else "inner") not in opened
    _assert_preserved(destination, previous)


@pytest.mark.parametrize("layer", ["outer", "inner"])
def test_download_stops_underdeclared_member_at_remaining_budget(monkeypatch, destination, layer):
    previous = destination.read_bytes()
    files = {"publication.db": b"sqlite", **({"image.jpg": b""} if layer == "inner" else {})}
    extras = {"unused.bin": b""} if layer == "outer" else {}
    payload = _nested_archive(files, extra_members=extras)
    budget = 512
    monkeypatch.setattr(archive_budget, "MAX_EXPANDED_BYTES", budget)
    original_open = zipfile.ZipFile.open
    original_read = zipfile.ZipExtFile.read
    expanded = 0

    class UnderdeclaredStream(io.BytesIO):
        def read(self, size=-1):
            nonlocal expanded
            chunk = super().read(size)
            expanded += len(chunk)
            return chunk

    def underdeclared_open(archive, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if name == ("unused.bin" if layer == "outer" else "image.jpg"):
            return UnderdeclaredStream(b"x" * 8192)
        return original_open(archive, member, *args, **kwargs)

    def observe_read(member, size=-1):
        nonlocal expanded
        chunk = original_read(member, size)
        expanded += len(chunk)
        return chunk

    monkeypatch.setattr(zipfile.ZipFile, "open", underdeclared_open)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", observe_read)
    with pytest.raises(JwpubArchiveDownloadError, match="budget"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload, headers={}),
        )

    assert expanded <= budget + 1
    _assert_preserved(destination, previous)


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_download_rejects_nonpositive_chunk_size(destination, jwpub_bytes, chunk_size):
    previous = destination.read_bytes()
    with pytest.raises(JwpubArchiveDownloadError, match="(?i)chunk size"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            chunk_size=chunk_size,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


@pytest.mark.parametrize("uppercase", [False, True])
def test_download_verifies_expected_jw_md5_checksum(destination, jwpub_bytes, uppercase):
    checksum = hashlib.md5(jwpub_bytes, usedforsecurity=False).hexdigest()
    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        expected_checksum=checksum.upper() if uppercase else checksum,
        stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
    )

    assert destination.read_bytes() == jwpub_bytes
    assert set(destination.parent.iterdir()) == {destination}


def test_download_rejects_incorrect_checksum_and_preserves_destination(destination, jwpub_bytes):
    previous = destination.read_bytes()
    with pytest.raises(JwpubArchiveDownloadError, match="(?i)checksum"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            expected_checksum="0" * 32,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


@pytest.mark.parametrize("length_delta", [-1, 1])
def test_download_rejects_content_length_mismatch(destination, jwpub_bytes, length_delta):
    previous = destination.read_bytes()
    with pytest.raises(JwpubArchiveDownloadError, match="(?i)length"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(
                jwpub_bytes, headers={"Content-Length": str(len(jwpub_bytes) + length_delta)}
            ),
        )

    _assert_preserved(destination, previous)


def test_download_accepts_valid_archive_without_content_length(destination, jwpub_bytes):
    progress = []
    download_jwpub_archive(
        "https://example.test/mwb.jwpub",
        destination,
        progress=progress.append,
        stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes, headers={}),
    )

    assert destination.read_bytes() == jwpub_bytes
    assert all(0 <= value <= 100 for value in progress)
    assert set(destination.parent.iterdir()) == {destination}


@pytest.mark.parametrize(
    "damage", ["not-zip", "truncated", "missing-contents", "bad-contents", "outer-crc", "inner-crc"]
)
def test_download_rejects_invalid_archive_without_content_length(destination, jwpub_bytes, damage):
    previous = destination.read_bytes()
    if damage == "not-zip":
        payload = b"<html>Download unavailable</html>"
    elif damage == "truncated":
        payload = jwpub_bytes[:-30]
    elif damage == "missing-contents":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", b"{}")
        payload = output.getvalue()
    elif damage == "bad-contents":
        payload = _outer_archive(b"incomplete inner ZIP")
    elif damage == "outer-crc":
        payload = _corrupt_member(jwpub_bytes, "contents")
    else:
        with zipfile.ZipFile(io.BytesIO(jwpub_bytes)) as outer:
            contents = outer.read("contents")
        with zipfile.ZipFile(io.BytesIO(contents)) as inner:
            database = next(name for name in inner.namelist() if name.endswith(".db"))
        payload = _outer_archive(_corrupt_member(contents, database))

    with pytest.raises(JwpubArchiveDownloadError):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload, headers={}),
        )

    _assert_preserved(destination, previous)


@pytest.mark.parametrize(
    ("compression", "decompressor_error"),
    [(zipfile.ZIP_DEFLATED, zlib.error), (zipfile.ZIP_LZMA, lzma.LZMAError)],
)
def test_download_wraps_malformed_compressed_payload_errors(
    destination, jwpub_bytes, compression, decompressor_error
):
    previous = destination.read_bytes()
    with zipfile.ZipFile(io.BytesIO(jwpub_bytes)) as archive:
        contents = archive.read("contents")
    payload = _outer_archive(contents, compression=compression)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = archive.getinfo("contents")
    offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    damaged = bytearray(payload)
    if compression == zipfile.ZIP_LZMA:
        damaged[offset + 4] = 0xFF  # Invalid LZMA filter properties.
    else:
        damaged[offset] |= 0x07  # Reserved DEFLATE block type.
    payload = bytes(damaged)
    with pytest.raises(decompressor_error), zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.testzip()

    with pytest.raises(JwpubArchiveDownloadError):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(payload, headers={}),
        )

    _assert_preserved(destination, previous)


def test_download_preserves_destination_and_cleans_staging_when_replace_fails(
    monkeypatch, destination, jwpub_bytes
):
    previous = destination.read_bytes()

    def fail_replace(source, target):
        assert source.parent == destination.parent
        assert target == destination
        assert source.read_bytes() == jwpub_bytes
        raise PermissionError("archive is locked")

    monkeypatch.setattr(publication_archive.os, "replace", fail_replace)
    with pytest.raises(JwpubArchiveDownloadError, match="locked"):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            stream_factory=lambda *_args, **_kwargs: _Stream(jwpub_bytes),
        )

    _assert_preserved(destination, previous)


@pytest.mark.parametrize("source", ["stream", "progress", "cancelled"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_download_cleans_staging_on_transfer_or_callback_exception(
    destination, jwpub_bytes, source, interrupted
):
    previous = destination.read_bytes()
    error = KeyboardInterrupt("interrupted") if interrupted else RuntimeError("callback failed")
    if source == "stream" and not interrupted:
        error = HttpError("transfer failed")
    received = False

    class FailingStream(_Stream):
        def iter_bytes(self, chunk_size):
            yield self.payload[:chunk_size]
            if source == "stream":
                raise error
            yield self.payload[chunk_size:]

    def progress(_value):
        nonlocal received
        received = True
        if source == "progress":
            raise error

    def cancelled():
        if source == "cancelled" and received:
            raise error
        return False

    expected_error = JwpubArchiveDownloadError if isinstance(error, HttpError) else type(error)
    with pytest.raises(expected_error):
        download_jwpub_archive(
            "https://example.test/mwb.jwpub",
            destination,
            progress=progress,
            cancelled=cancelled,
            stream_factory=lambda *_args, **_kwargs: FailingStream(jwpub_bytes),
            chunk_size=len(jwpub_bytes) // 2,
        )

    _assert_preserved(destination, previous)


def test_resolve_meeting_media_returns_simple_result(monkeypatch):
    class Resolver:
        def resolve_media(self, request):
            assert request.key_symbol == "sjjm"
            assert request.is_sign_language is True
            assert request.media_type == "audio"
            return PubMediaFile(
                url="https://example.test/song.mp4",
                title="Song",
                thumbnail_url="https://example.test/thumb.jpg",
                duration_ticks=123_000_000,
            )

    monkeypatch.setattr(publication_archive, "_PUBLICATION_MEDIA_RESOLVER", Resolver())

    assert resolve_meeting_media(
        "sjjm",
        1,
        0,
        0,
        "T",
        is_sign_language=True,
        media_type="audio",
    ) == {
        "url": "https://example.test/song.mp4",
        "title": "Song",
        "thumbnail": "https://example.test/thumb.jpg",
        "duration_ticks": 123_000_000,
    }
