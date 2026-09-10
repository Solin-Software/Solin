"""Credentials must never reach the log file users attach to bug reports.

The sidecar's stdout is dup2'd onto its stderr and every line it prints is
re-emitted into Solin's rotating log. libobs prints camera and media URLs in the
ordinary course of business — ffmpeg_source dumps its `input` setting on update,
media-playback prints the whole URL when a stream fails to open — and those URLs
routinely carry the camera password.
"""

from __future__ import annotations

from solin.core.scenes.process_engine import redact_secrets


def test_userinfo_in_a_url_is_masked() -> None:
    line = "info: MP: Failed to open media: 'rtsp://admin:hunter2@192.168.0.100:554/s'"

    redacted = redact_secrets(line)

    assert "hunter2" not in redacted
    assert "admin:hunter2" not in redacted
    assert "rtsp://***@192.168.0.100:554/s" in redacted


def test_a_login_inside_the_path_is_masked() -> None:
    """Camera firmware often wants the login in the path, not as userinfo.

    The character before "password" is an underscore, so a word boundary never
    matches there — the pattern must not rely on one.
    """
    line = (
        "info:  input: rtsp://192.168.0.100:554/"
        "user=admin_password=FrwI7MsQ_channel=0_stream=0&protocol=unicast.sdp"
    )

    redacted = redact_secrets(line)

    assert "FrwI7MsQ" not in redacted
    assert "password=***" in redacted


def test_secret_bearing_keys_are_masked_wherever_they_appear() -> None:
    for key, secret in (
        ("password", "p1"),
        ("passwd", "p2"),
        ("pwd", "p3"),
        ("secret", "p4"),
        ("token", "p5"),
        ("auth", "p6"),
    ):
        redacted = redact_secrets(f"debug: connecting with {key}={secret} now")
        assert secret not in redacted, key
        assert f"{key}=***" in redacted, key


def test_masking_is_case_insensitive() -> None:
    assert "hunter2" not in redact_secrets("warn: PASSWORD=hunter2")


def test_ordinary_engine_output_is_left_alone() -> None:
    """Over-redaction would make the log useless for diagnosing anything else."""
    for line in (
        "info: video settings reset: base resolution 1920x1080",
        "debug: source 'solin-scene-abc' (scene) created",
        "info: Loading up D3D11 on adapter NVIDIA GeForce GTX 1660 SUPER (0)",
        "warning: Failed to initialize module 'aja.dll'",
    ):
        assert redact_secrets(line) == line


def test_a_url_without_credentials_survives_intact() -> None:
    line = "info:  input: rtsp://192.168.0.100:554/stream1"

    assert redact_secrets(line) == line
