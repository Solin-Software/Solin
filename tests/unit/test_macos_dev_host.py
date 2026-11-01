from __future__ import annotations

import os
from pathlib import Path

import pytest

from solin.bootstrap import macos_dev_host


@pytest.mark.parametrize(
    "platform,backend", [("linux", "cocoa"), ("darwin", "offscreen"), ("darwin", "minimal")]
)
def test_non_cocoa_launch_does_not_prepare_or_replace_process(monkeypatch, platform, backend):
    monkeypatch.setattr(macos_dev_host.sys, "platform", platform)
    monkeypatch.setenv("QT_QPA_PLATFORM", backend)
    monkeypatch.setattr(
        macos_dev_host, "prepare_development_bundle", lambda _: pytest.fail("unexpected build")
    )
    macos_dev_host.ensure_development_bundle()


def test_existing_bundle_host_does_not_replace_itself(monkeypatch):
    repository = Path(macos_dev_host.__file__).resolve().parents[3]
    executable = repository / "build/dev/SolinDev.app/Contents/MacOS/SolinDev"
    monkeypatch.setattr(macos_dev_host.sys, "platform", "darwin")
    monkeypatch.setattr(macos_dev_host.sys, "executable", str(executable))
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    monkeypatch.setattr(
        macos_dev_host, "prepare_development_bundle", lambda _: pytest.fail("recursive build")
    )
    macos_dev_host.ensure_development_bundle()


def test_gui_reexec_preserves_python_arguments_and_environment(monkeypatch, tmp_path):
    host = tmp_path / "SolinDev"
    monkeypatch.setattr(macos_dev_host.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_dev_host.sys, "orig_argv", ["python", "-X", "utf8", "main.py", "a file.solinplaylist"]
    )
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    monkeypatch.setenv("SOLIN_TEST_ENV", "retained")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "old-launcher")
    monkeypatch.setattr(macos_dev_host, "prepare_development_bundle", lambda _: host)
    calls = []
    monkeypatch.setattr(os, "execve", lambda *args: calls.append(args))

    macos_dev_host.ensure_development_bundle()

    executable, arguments, environment = calls.pop()
    assert executable == host
    assert arguments == [str(host), "-X", "utf8", "main.py", "a file.solinplaylist"]
    assert environment["SOLIN_TEST_ENV"] == "retained"
    assert "__PYVENV_LAUNCHER__" not in environment


def test_unsupported_interpreter_fails_before_creating_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_dev_host.sys, "platform", "darwin")
    monkeypatch.setattr(macos_dev_host.sysconfig, "get_config_var", lambda _: "")
    with pytest.raises(RuntimeError, match="framework build"):
        macos_dev_host.prepare_development_bundle(tmp_path)
    assert not (tmp_path / "build").exists()


def test_non_utf8_host_path_is_rejected_explicitly():
    with pytest.raises(RuntimeError, match="valid UTF-8"):
        macos_dev_host._c_string("invalid-\udcff")
