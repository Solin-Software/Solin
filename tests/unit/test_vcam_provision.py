"""Tests for self-contained v4l2loopback provisioning (no root, mocked)."""

from __future__ import annotations

import pytest

import solin.core.media.vcam_provision as p


class _Res:
    def __init__(self, returncode, stderr=b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


# ── detection ─────────────────────────────────────────────────────────────────

def test_detect_unsupported_off_linux(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "darwin")
    assert p.detect() is p.VcamProvisionState.UNSUPPORTED


def test_detect_module_missing(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "is_loaded", lambda: False)
    monkeypatch.setattr(p, "module_installed", lambda: False)
    assert p.detect() is p.VcamProvisionState.MODULE_MISSING


def test_detect_not_loaded_when_installed(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "is_loaded", lambda: False)
    monkeypatch.setattr(p, "module_installed", lambda: True)
    assert p.detect() is p.VcamProvisionState.NOT_LOADED


def test_detect_wrong_label(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "is_loaded", lambda: True)
    monkeypatch.setattr(p, "find_loopback_device", lambda: ("/dev/video2", "OBS Virtual Camera"))
    assert p.detect() is p.VcamProvisionState.WRONG_LABEL


def test_detect_ready(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "is_loaded", lambda: True)
    monkeypatch.setattr(p, "find_loopback_device", lambda: ("/dev/video2", p.DESIRED_LABEL))
    assert p.detect() is p.VcamProvisionState.READY


def test_detect_loaded_but_no_device_needs_reload(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "is_loaded", lambda: True)
    monkeypatch.setattr(p, "find_loopback_device", lambda: None)
    assert p.detect() is p.VcamProvisionState.NOT_LOADED


# ── privileged command construction (safety) ──────────────────────────────────

def test_provision_argv_is_fixed_pkexec_script():
    argv = p.provision_argv()
    assert argv[0] == "pkexec"
    # Interpreter is a hardcoded absolute path, never resolved via the caller's
    # $PATH (guards against a planted `sh` hijacking the root-authorised command).
    assert argv[1] == "/bin/sh" and argv[2] == "-c"
    script = argv[3]
    assert 'card_label="Solin Virtual Camera"' in script
    assert "/etc/modprobe.d/solin-virtualcam.conf" in script
    assert "/etc/modules-load.d/solin-virtualcam.conf" in script
    assert "modprobe" in script and "v4l2loopback" in script


def test_desired_label_is_injection_safe():
    # The only interpolated value in the root script is the label; it must stay a
    # constrained constant (no shell/quote metacharacters).
    assert p._SAFE_LABEL.match(p.DESIRED_LABEL)


def test_unsafe_label_is_rejected(monkeypatch):
    monkeypatch.setattr(p, "DESIRED_LABEL", 'x"; rm -rf / #')
    with pytest.raises(ValueError, match="unsafe"):
        p._provision_script()


# ── provision() outcomes ──────────────────────────────────────────────────────

def _linux_with_pkexec(monkeypatch, available=True):
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p, "pkexec_available", lambda: available)


def test_provision_success(monkeypatch):
    _linux_with_pkexec(monkeypatch)
    seen = []
    assert p.provision(runner=lambda argv, **kw: seen.append(argv) or _Res(0)) is True
    assert seen and seen[0][0] == "pkexec"


def test_provision_cancelled_is_false(monkeypatch):
    _linux_with_pkexec(monkeypatch)
    assert p.provision(runner=lambda argv, **kw: _Res(126)) is False


def test_provision_failure_is_false(monkeypatch):
    _linux_with_pkexec(monkeypatch)
    assert p.provision(runner=lambda argv, **kw: _Res(1, b"modprobe: boom")) is False


def test_provision_without_pkexec_never_runs(monkeypatch):
    _linux_with_pkexec(monkeypatch, available=False)
    ran = []
    assert p.provision(runner=lambda argv, **kw: ran.append(argv) or _Res(0)) is False
    assert ran == []


def test_provision_off_linux_is_false(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "win32")
    assert p.provision(runner=lambda argv, **kw: _Res(0)) is False


# ── guidance + misc ───────────────────────────────────────────────────────────

def test_install_hint_names_the_package():
    hint = p.install_hint()
    assert "v4l2loopback" in hint and "apt install v4l2loopback-dkms" in hint


def test_find_loopback_device_none_off_linux(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "darwin")
    assert p.find_loopback_device() is None
