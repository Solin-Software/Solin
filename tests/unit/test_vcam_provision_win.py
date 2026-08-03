"""Tests for the Windows vcam sink (DirectShow filter registration, mocked).

Nothing here touches the real registry or ``regsvr32``: the registry probe is
faked at :func:`_iter_registered_cameras` (so ``winreg`` is never imported and the
suite runs on any OS) and the privileged step goes through the ``runner`` seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import solin.core.media.vcam_provision_win as w


#: Captured before the isolation fixture below replaces it, so the one test that
#: exercises the real search order can opt back in.
_REAL_SOLIN_FILTER_DIRS = w._solin_filter_dirs


@pytest.fixture(autouse=True)
def _isolate_from_the_installed_filter(monkeypatch):
    """Keep the suite independent of what is installed on the machine running it.

    ``filter_dlls()`` consults Solin's own filter first, so a developer box with
    the camera actually installed would otherwise satisfy every "nothing found"
    case and pass vacuously. Tests that care about Solin's filter opt back in by
    patching ``_solin_filter_dirs`` or ``solin_filter_dlls`` themselves.
    """
    monkeypatch.setattr(w, "_solin_filter_dirs", lambda: iter(()))


class _Res:
    def __init__(self, returncode, stderr=b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _cameras(monkeypatch, *entries):
    """Fake the DirectShow ``(clsid, friendly_name, dll_path)`` enumeration."""
    monkeypatch.setattr(w, "_iter_registered_cameras", lambda: iter(entries))


_OBS_DLL = r"C:\Solin\data\obs-plugins\win-dshow\obs-virtualcam-module64.dll"


# ── locating the filter DLL ───────────────────────────────────────────────────

def test_filter_dlls_finds_bundled_modules(monkeypatch, tmp_path):
    (tmp_path / "obs-virtualcam-module64.dll").write_bytes(b"")
    (tmp_path / "obs-virtualcam-module32.dll").write_bytes(b"")
    monkeypatch.setattr(w, "_plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(w, "_obs_install_data_dirs", lambda: iter(()))
    assert [p.name for p in w.filter_dlls()] == [
        "obs-virtualcam-module64.dll",
        "obs-virtualcam-module32.dll",
    ]
    assert w.module_installed() is True


def test_module_installed_false_without_any_dll(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "_plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(w, "_obs_install_data_dirs", lambda: iter(()))
    assert w.filter_dlls() == []
    assert w.module_installed() is False


def test_filter_dlls_survives_missing_pylibobs(monkeypatch, tmp_path):
    # No bundle at all must degrade to "nothing found", never raise.
    monkeypatch.setattr(w, "_plugin_data_dir", lambda: None)
    monkeypatch.setattr(w, "_obs_install_data_dirs", lambda: iter((tmp_path,)))
    assert w.filter_dlls() == []


# ── detection ─────────────────────────────────────────────────────────────────

def test_detect_module_missing_when_no_dll(monkeypatch):
    _cameras(monkeypatch)
    monkeypatch.setattr(w, "module_installed", lambda: False)
    assert w.detect() is w._base.VcamProvisionState.MODULE_MISSING


def test_detect_not_loaded_when_dll_present_but_unregistered(monkeypatch):
    _cameras(monkeypatch)
    monkeypatch.setattr(w, "module_installed", lambda: True)
    assert w.detect() is w._base.VcamProvisionState.NOT_LOADED


def test_detect_wrong_label_for_stock_obs_filter(monkeypatch):
    _cameras(monkeypatch, ("{CLSID-OBS}", "OBS Virtual Camera", _OBS_DLL))
    assert w.detect() is w._base.VcamProvisionState.WRONG_LABEL


def test_detect_ready_for_branded_filter(monkeypatch):
    _cameras(monkeypatch, ("{CLSID-SOLIN}", w._base.DESIRED_LABEL, _OBS_DLL))
    assert w.detect() is w._base.VcamProvisionState.READY


def test_detect_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("hive on fire")

    monkeypatch.setattr(w, "find_loopback_device", _boom)
    assert w.detect() is w._base.VcamProvisionState.MODULE_MISSING


def test_real_webcams_are_not_mistaken_for_the_vcam(monkeypatch):
    # A registered device backed by some other vendor's DLL is somebody else's
    # camera — claiming it would report READY/WRONG_LABEL with no vcam at all.
    _cameras(monkeypatch, ("{CLSID-LOGI}", "HD Pro Webcam C920", r"C:\Windows\System32\logi.dll"))
    monkeypatch.setattr(w, "module_installed", lambda: True)
    assert w.find_loopback_device() is None
    assert w.is_loaded() is False
    assert w.detect() is w._base.VcamProvisionState.NOT_LOADED


def test_branded_filter_wins_over_stock_obs_camera(monkeypatch):
    _cameras(
        monkeypatch,
        ("{CLSID-OBS}", "OBS Virtual Camera", _OBS_DLL),
        ("{CLSID-SOLIN}", w._base.DESIRED_LABEL, _OBS_DLL),
    )
    assert w.find_loopback_device() == ("{CLSID-SOLIN}", w._base.DESIRED_LABEL)


def test_find_loopback_device_reports_clsid_and_name(monkeypatch):
    _cameras(monkeypatch, ("{CLSID-OBS}", "OBS Virtual Camera", _OBS_DLL))
    assert w.find_loopback_device() == ("{CLSID-OBS}", "OBS Virtual Camera")
    assert w.is_loaded() is True


# ── privileged command construction (safety) ──────────────────────────────────

def _unelevated(monkeypatch):
    monkeypatch.setattr(w, "_is_elevated", lambda: False)


def test_provision_argv_elevates_regsvr32_with_install_flags(monkeypatch):
    _unelevated(monkeypatch)
    argv = w.provision_argv([Path(_OBS_DLL)])
    # Interpreter is an absolute path, never resolved via the caller's %PATH%.
    assert argv[0].endswith("powershell.exe") and "\\" in argv[0]
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    script = argv[4]
    assert "-Verb RunAs" in script  # this is what raises the UAC prompt
    assert "regsvr32.exe" in script
    # /i invokes DllInstall; /s silences the dialog. OBS ships exactly this pair.
    assert "/i /s" in script
    assert _OBS_DLL in script


def test_provision_argv_skips_elevation_when_already_admin(monkeypatch):
    monkeypatch.setattr(w, "_is_elevated", lambda: True)
    script = w.provision_argv([Path(_OBS_DLL)])[4]
    assert "RunAs" not in script  # already privileged — prompting would be noise
    assert "regsvr32.exe" in script and "/i /s" in script


def test_provision_argv_matches_tool_bitness_to_dll(monkeypatch):
    _unelevated(monkeypatch)
    dll32 = Path(r"C:\Solin\data\obs-plugins\win-dshow\obs-virtualcam-module32.dll")
    # A 32-bit filter registered by the 64-bit regsvr32 is silently invisible to
    # 32-bit hosts, so the tool must follow the DLL's bitness.
    assert "SysWOW64" in w.provision_argv([dll32])[4]
    assert "System32" in w.provision_argv([Path(_OBS_DLL)])[4]


def test_provision_argv_registers_both_bitnesses_in_one_prompt(monkeypatch):
    _unelevated(monkeypatch)
    dll32 = Path(r"C:\Solin\data\obs-plugins\win-dshow\obs-virtualcam-module32.dll")
    script = w.provision_argv([Path(_OBS_DLL), dll32])[4]
    assert script.count("Start-Process") == 1  # one UAC prompt, not one per DLL
    assert _OBS_DLL in script and str(dll32) in script


def test_provision_argv_escapes_quotes_for_the_elevated_child(monkeypatch):
    _unelevated(monkeypatch)
    script = w.provision_argv([Path(_OBS_DLL)])[4]
    # The inner command rides inside a single-quoted PowerShell literal *and*
    # quotes its own paths, so every inner quote must be doubled. Undoubled, the
    # first one closes the literal early and the elevated child runs garbage.
    inner = script.split("'-Command','", 1)[1].rsplit("' -Verb RunAs", 1)[0]
    assert "'" not in inner.replace("''", "")  # no unescaped quote survives
    assert inner.replace("''", "'") == f"& '{w._regsvr32(64)}' /i /s '{_OBS_DLL}'"


def test_provision_argv_rejects_quoted_paths():
    # Paths are interpolated into a PowerShell command; a quote would break out.
    with pytest.raises(ValueError, match="unsafe"):
        w._register_commands([Path("C:\\evil'; calc.exe; '\\module64.dll")])


def test_provision_argv_without_any_dll_is_an_error():
    with pytest.raises(ValueError, match="no virtual-camera filter"):
        w.provision_argv([])


# ── provision() outcomes ──────────────────────────────────────────────────────

def _registerable(monkeypatch, *, registered=""):
    monkeypatch.setattr(w, "filter_dlls", lambda: [Path(_OBS_DLL)])
    monkeypatch.setattr(w, "registered_dll", lambda: registered)
    _unelevated(monkeypatch)


def test_provision_success(monkeypatch):
    _registerable(monkeypatch)
    seen = []
    assert w.provision(runner=lambda argv, **kw: seen.append(argv) or _Res(0)) is True
    assert seen and seen[0][0].endswith("powershell.exe")


def test_provision_is_idempotent_and_does_not_reprompt(monkeypatch):
    # Once SOLIN's own filter is registered there is nothing left to do, and
    # re-registering must not fire a UAC prompt on every camera start.
    _registerable(monkeypatch, registered=r"C:\ProgramData\Solin\solin-dshowcam-x64.dll")
    ran = []
    assert w.provision(runner=lambda argv, **kw: ran.append(argv) or _Res(0)) is True
    assert ran == []


def test_provision_cancelled_uac_is_false(monkeypatch):
    _registerable(monkeypatch)
    assert w.provision(runner=lambda argv, **kw: _Res(1223)) is False


def test_provision_failure_is_false(monkeypatch):
    _registerable(monkeypatch)
    assert w.provision(runner=lambda argv, **kw: _Res(5, b"access denied")) is False


def test_provision_without_any_dll_never_runs(monkeypatch):
    monkeypatch.setattr(w, "filter_dlls", lambda: [])
    monkeypatch.setattr(w, "registered_dll", lambda: "")
    ran = []
    assert w.provision(runner=lambda argv, **kw: ran.append(argv) or _Res(0)) is False
    assert ran == []


def test_provision_survives_a_failing_runner(monkeypatch):
    _registerable(monkeypatch)

    def _boom(argv, **kw):
        raise OSError("no shell")

    assert w.provision(runner=_boom) is False


# ── guidance ──────────────────────────────────────────────────────────────────

def test_install_hint_names_the_missing_filter():
    assert "solin-dshowcam-x64.dll" in w.install_hint()


# ── Solin's own filter ────────────────────────────────────────────────────────


def test_dll_bits_reads_both_naming_schemes():
    """OBS names its 32-bit module '...32'; Solin's is '...-x86'."""
    assert w._dll_bits(Path("obs-virtualcam-module32.dll")) == 32
    assert w._dll_bits(Path("obs-virtualcam-module64.dll")) == 64
    assert w._dll_bits(Path("solin-dshowcam-x86.dll")) == 32
    assert w._dll_bits(Path("solin-dshowcam-x64.dll")) == 64


def test_solin_filter_is_registered_without_the_dllinstall_flag(monkeypatch):
    """Solin's filter exports no DllInstall, so /i would fail; OBS's needs it."""
    solin = [Path(r"C:\ProgramData\Solin\solin-dshowcam-x64.dll")]
    obs = [Path(r"C:\obs\obs-virtualcam-module64.dll")]

    assert "/i" not in w._register_commands(solin)[0]
    assert "/s" in w._register_commands(solin)[0]
    assert "/i /s" in w._register_commands(obs)[0]


def test_filter_dlls_prefers_solin_over_a_co_installed_obs(monkeypatch):
    solin = [Path(r"C:\ProgramData\Solin\solin-dshowcam-x64.dll")]
    monkeypatch.setattr(w, "solin_filter_dlls", lambda: solin)
    monkeypatch.setattr(w, "_plugin_data_dir", lambda: Path(r"C:\obs\data"))

    assert w.filter_dlls() == solin  # OBS's copy is never even consulted


def test_provision_installs_solins_filter_even_when_obs_is_registered(monkeypatch):
    """A registered OBS filter must not satisfy provisioning.

    It reads OBS's shared memory, not Solin's frame transport, so leaving it in
    place means the meeting app shows OBS's placeholder forever. Ours registers
    alongside it under its own CLSID.
    """
    monkeypatch.setattr(
        w, "filter_dlls", lambda: [Path(r"C:\dist\camera\solin-dshowcam-x64.dll")]
    )
    monkeypatch.setattr(w, "registered_dll", lambda: _OBS_DLL)
    ran = []

    assert w.provision(runner=lambda argv, **kw: ran.append(argv) or _Res(0)) is True
    assert ran, "provisioning was skipped even though Solin's filter is not registered"


def test_frozen_build_looks_beside_the_executable(monkeypatch, tmp_path):
    """A Nuitka build must find the filter it ships in ``camera/``.

    Nuitka sets ``__compiled__``, NOT ``sys.frozen`` — checking only the latter
    skips the shipped directory entirely, so the camera never installs on a clean
    machine. It still passes on a developer box, where ProgramData holds a
    manually-deployed copy, which is exactly how this shipped broken.
    """
    exe_dir = tmp_path / "dist"
    (exe_dir / "camera").mkdir(parents=True)
    for name in w._SOLIN_FILTER_DLLS:
        (exe_dir / "camera" / name).write_bytes(b"")
    # This test is about the real search order, so opt out of the isolation
    # fixture — but keep ProgramData out of it by pointing the module elsewhere.
    monkeypatch.setattr(w, "_solin_filter_dirs", _REAL_SOLIN_FILTER_DIRS)
    monkeypatch.setattr(w, "_INSTALLED_FILTER_DIR", tmp_path / "nowhere", raising=False)
    monkeypatch.setattr(w.sys, "executable", str(exe_dir / "Solin.exe"))
    monkeypatch.setattr(w.sys, "frozen", False, raising=False)
    monkeypatch.setitem(w.__dict__, "__compiled__", True)

    found = w.solin_filter_dlls()

    assert [d.name for d in found] == list(w._SOLIN_FILTER_DLLS)
    assert all(d.parent == exe_dir / "camera" for d in found)


def test_solin_filter_dlls_takes_one_copy_per_bitness(monkeypatch, tmp_path):
    """The same DLL usually exists both installed and in a checkout.

    Registering one CLSID from two paths leaves the registry pointing at
    whichever ran last, silently repointing consumers at a stale build.
    """
    first = tmp_path / "installed"
    second = tmp_path / "checkout"
    for root in (first, second):
        root.mkdir()
        for name in w._SOLIN_FILTER_DLLS:
            (root / name).write_bytes(b"")
    monkeypatch.setattr(w, "_solin_filter_dirs", lambda: iter([first, second]))

    found = w.solin_filter_dlls()

    assert len(found) == 2  # not four
    assert {d.parent for d in found} == {first}  # first location wins


def test_prerequisite_hint_explains_the_admin_prompt():
    hint = w.prerequisite_hint()
    assert hint and "administrator" in hint
