from __future__ import annotations

import json
from pathlib import Path

import pytest

from solin.bootstrap import media_runtime_verification


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--verify-media-runtime"],
        ["--verify-media-runtime=fixtures"],
        ["--create-profile", "--verify-media-runtime", "fixtures"],
        ["--verify-media-runtime", "fixtures", "--scene-engine-sidecar"],
    ],
)
def test_invalid_arguments_fail_before_extraction(arguments, monkeypatch, capsys):
    def unexpected(fixtures):
        pytest.fail("Invalid qualification arguments must not start extraction")

    monkeypatch.setattr(media_runtime_verification, "_verify", unexpected)
    assert media_runtime_verification.main(arguments) == 2
    assert json.loads(capsys.readouterr().out) == {
        "schema": "solin.media-runtime.v1",
        "ok": False,
        "error": "ValueError",
    }


def test_incomplete_fixtures_fail_before_extraction(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        media_runtime_verification, "_verify", lambda _: pytest.fail("Unexpected extraction")
    )
    assert media_runtime_verification.main(["--verify-media-runtime", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ValueError"


def test_client_failure_is_reported_without_success_checks(tmp_path, monkeypatch, capsys):
    for name in ("video.mp4", "video-cover.mp4", "audio-cover.mp3", "audio.mp3"):
        (tmp_path / name).touch()

    def fail(fixtures: Path):
        raise RuntimeError("Native client unavailable")

    monkeypatch.setattr(media_runtime_verification, "_verify", fail)
    assert media_runtime_verification.main(["--verify-media-runtime", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "Native client unavailable" in captured.err
    assert json.loads(captured.out) == {
        "schema": "solin.media-runtime.v1",
        "ok": False,
        "error": "RuntimeError",
    }
