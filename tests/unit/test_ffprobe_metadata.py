from __future__ import annotations

import json
import subprocess
import sys

import pytest

from solin.core.media import ffprobe_metadata


@pytest.mark.parametrize(
    "title",
    ["Imite os fiéis, não imitações — ação", 'A canção “d\'água” & paz 🎵'],
)
def test_probe_preserves_utf8_title_with_non_utf8_diagnostics(monkeypatch, title):
    payload = json.dumps(
        {"format": {"tags": {"TITLE": title}, "duration": "1.5"}},
        ensure_ascii=False,
    ).encode("utf-8")
    run = subprocess.run
    path = "canção d'água – apresentação.mp4"
    monkeypatch.setattr(ffprobe_metadata, "ffprobe_available", lambda: True)
    # Reproduce Windows' locale even on a UTF-8 development/CI host. The child
    # emits actual bytes on both pipes so subprocess' reader threads are tested.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")

    def run_probe(cmd, **kwargs):
        assert cmd[-1] == path
        child = (
            "import sys; "
            f"sys.stdout.buffer.write({payload!r}); "
            "sys.stderr.buffer.write(b'\\x9d\\xff')"
        )
        return run([sys.executable, "-c", child], **kwargs)

    monkeypatch.setattr(ffprobe_metadata.subprocess, "run", run_probe)
    tags = ffprobe_metadata.probe_tags(path)
    assert tags.title == title
    assert tags.duration_ms == 1500


@pytest.mark.parametrize("payload", [b"{", b'{"format": {"tags": {"title": "\xff"}}}'])
def test_probe_rejects_invalid_json_or_utf8_without_raising(monkeypatch, payload):
    monkeypatch.setattr(ffprobe_metadata, "ffprobe_available", lambda: True)
    monkeypatch.setattr(
        ffprobe_metadata.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, b""),
    )
    assert ffprobe_metadata.probe_tags("bad.mp4") == ffprobe_metadata.MediaTags()


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("ffprobe", 15)])
def test_probe_failure_returns_empty_metadata(monkeypatch, failure):
    monkeypatch.setattr(ffprobe_metadata, "ffprobe_available", lambda: True)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ffprobe_metadata.subprocess, "run", fail)
    assert ffprobe_metadata.probe_tags("unreadable.mp4") == ffprobe_metadata.MediaTags()


def test_probe_nonzero_exit_ignores_binary_diagnostics(monkeypatch):
    monkeypatch.setattr(ffprobe_metadata, "ffprobe_available", lambda: True)
    monkeypatch.setattr(
        ffprobe_metadata.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b"\x9d\xff"),
    )
    assert ffprobe_metadata.probe_tags("bad.mp4") == ffprobe_metadata.MediaTags()
