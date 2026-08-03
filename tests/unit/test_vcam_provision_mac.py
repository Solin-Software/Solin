"""Tests for the macOS vcam sink (Camera Extension detection, mocked).

Nothing here shells out: ``systemextensionsctl`` and the bundle probe are faked,
so the suite runs on any OS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import solin.core.media.vcam_provision_mac as m
from solin.core.media.vcam_provision import VcamProvisionState


def _ctl(monkeypatch, output: str) -> None:
    monkeypatch.setattr(m, "_systemextensionsctl", lambda: output)


_APPROVED = (
    "1 extension(s)\n"
    "--- com.apple.system_extension.camera\n"
    f"*\t*\t{m.EXTENSION_ID} (1.0/1.0)\tSolin Camera\t[activated enabled]\n"
)
_AWAITING = (
    "1 extension(s)\n"
    f"*\t*\t{m.EXTENSION_ID} (1.0/1.0)\tSolin Camera\t[activated waiting for user]\n"
)


# ── detection ─────────────────────────────────────────────────────────────────


def test_detect_module_missing_when_the_extension_is_not_bundled(monkeypatch):
    monkeypatch.setattr(m, "extension_bundle", lambda: None)
    assert m.detect() is VcamProvisionState.MODULE_MISSING


def test_detect_not_loaded_when_bundled_but_awaiting_user_approval(monkeypatch):
    """The common first-run state: shipped, but macOS is still asking the user."""
    monkeypatch.setattr(m, "extension_bundle", lambda: Path("/Solin.app/ext"))
    _ctl(monkeypatch, _AWAITING)
    assert m.detect() is VcamProvisionState.NOT_LOADED


def test_detect_ready_once_the_extension_is_enabled(monkeypatch):
    monkeypatch.setattr(m, "extension_bundle", lambda: Path("/Solin.app/ext"))
    _ctl(monkeypatch, _APPROVED)
    assert m.detect() is VcamProvisionState.READY


def test_detect_never_raises(monkeypatch):
    def _boom():
        raise OSError("no such tool")

    monkeypatch.setattr(m, "extension_bundle", _boom)
    assert m.detect() is VcamProvisionState.MODULE_MISSING


def test_unrelated_extensions_do_not_count(monkeypatch):
    monkeypatch.setattr(m, "extension_bundle", lambda: Path("/Solin.app/ext"))
    _ctl(monkeypatch, "*\t*\tcom.example.Other (1.0)\tOther\t[activated enabled]\n")
    assert m.is_loaded() is False
    assert m.detect() is VcamProvisionState.NOT_LOADED


# ── device identity ───────────────────────────────────────────────────────────


def test_find_loopback_device_matches_the_cross_platform_contract(monkeypatch):
    """(identifier, friendly name) — the same shape Linux and Windows return."""
    monkeypatch.setattr(m, "is_loaded", lambda: True)
    device = m.find_loopback_device()
    assert device == (m.EXTENSION_ID, "Solin Virtual Camera")


def test_find_loopback_device_is_none_until_approved(monkeypatch):
    monkeypatch.setattr(m, "is_loaded", lambda: False)
    assert m.find_loopback_device() is None


# ── provisioning is not possible from Python ──────────────────────────────────


def test_provision_reports_failure_rather_than_pretending(monkeypatch):
    """A camera extension is installed by the signed .app, not by us.

    Returning True would make the caller skip the guidance and leave the operator
    with no camera and no explanation.
    """
    ran = []
    assert m.provision(runner=lambda argv, **kw: ran.append(argv)) is False
    assert ran == []


def test_provision_argv_is_empty():
    assert m.provision_argv() == []


# ── guidance ──────────────────────────────────────────────────────────────────


def test_install_hint_points_at_reinstalling_the_app():
    assert "Reinstall Solin" in m.install_hint()


def test_prerequisite_hint_explains_the_system_settings_approval():
    hint = m.prerequisite_hint()
    assert "System Settings" in hint
    assert "Camera Extensions" in hint


def test_extension_bundle_is_none_off_macos(monkeypatch):
    monkeypatch.setattr(m.sys, "platform", "win32")
    assert m.extension_bundle() is None


@pytest.mark.parametrize("marker", ["activated enabled"])
def test_only_activated_enabled_counts_as_loaded(monkeypatch, marker):
    monkeypatch.setattr(m, "extension_bundle", lambda: Path("/Solin.app/ext"))
    _ctl(monkeypatch, f"*\t*\t{m.EXTENSION_ID} (1.0)\tSolin\t[{marker}]\n")
    assert m.is_loaded() is True
