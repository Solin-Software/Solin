"""Windows virtual-camera provisioning — DirectShow filter registration.

The Windows analogue of the Linux ``v4l2loopback`` flow. The sink is the same
``virtualcam_output`` Solin already asks libobs for, but on Windows that output is
registered by ``win-dshow`` and writes NV12 frames into a shared-memory ring
buffer. The *reader* is a DirectShow push-source COM filter
(``obs-virtualcam-module{32,64}.dll``) that each conferencing app loads
**in-process** — so the device only shows up in Zoom/Meet once that DLL is
COM-registered. ``regsvr32`` here is what ``modprobe`` is on Linux.

Registration writes under ``HKLM\\SOFTWARE\\Classes`` (via ``HKEY_CLASSES_ROOT``),
so it needs elevation: the UAC prompt is the analogue of polkit. Like the Linux
config files, it persists across reboots, so the operator is prompted once.

Two things differ from Linux and shape the state machine here:

* **The device name is baked into the DLL.** It is a compile-time ``Description``
  in the filter's ``IFilterMapper2::RegisterFilter`` call, not a runtime knob like
  ``card_label``. A bundled *stock OBS* module therefore enumerates as "OBS
  Virtual Camera" and :func:`detect` reports ``WRONG_LABEL`` — expected until a
  branded filter with its own CLSID ships.
* **``WRONG_LABEL`` must not re-prompt.** Because re-registering the same DLL can
  never rename it, :func:`provision` treats "already registered from the DLL we
  would register anyway" as done and returns without a UAC prompt. Otherwise the
  operator would be prompted on every single camera start.

All registry access here is **read-only**; the one privileged step is a fixed
``regsvr32 /i /s`` argv. Interpreter, tool and DLL paths are absolute and
validated, so there is no injection surface.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import vcam_provision as _base

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

#: DirectShow's ``CLSID_VideoInputDeviceCategory`` — every capture device (real
#: webcam or virtual) is registered as an ``Instance`` beneath it.
_VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"

#: The OBS filter DLLs Solin registers, newest-bitness first.
#: Solin's own DirectShow filter, both bitnesses. A DirectShow filter is loaded
#: *in the consuming application's process*, so a 32-bit host (plenty of older
#: capture tools still are) can only ever load the 32-bit build — shipping and
#: registering both is not optional.
_SOLIN_FILTER_DLLS = ("solin-dshowcam-x64.dll", "solin-dshowcam-x86.dll")

#: Where an installed filter lives. The registry records an absolute path, so a
#: registered filter must keep living here.
_INSTALLED_FILTER_DIR = Path(r"C:\ProgramData\Solin")

#: OBS's equivalent, kept only as a last-resort fallback: registering it gives a
#: working camera under the wrong name ("OBS Virtual Camera"), which detect()
#: reports as WRONG_LABEL.
_FILTER_DLLS = ("obs-virtualcam-module64.dll", "obs-virtualcam-module32.dll")

#: The libobs plugin that owns both the ``virtualcam_output`` and the filter DLLs.
_DSHOW_MODULE = "win-dshow"

#: Paths are interpolated into a PowerShell command, so reject quotes/newlines
#: (defence-in-depth: these paths come from the bundle layout, not user input).
_SAFE_PATH = re.compile(r"\A[^'\"\r\n]+\Z")


# ── locating the bundled filter DLLs ──────────────────────────────────────────


def _plugin_data_dir() -> Path | None:
    """The bundled ``data/obs-plugins/win-dshow`` directory, if pylibobs has one."""
    try:
        from pylibobs._lib import get_obs_module_dirs  # type: ignore[import-not-found]

        _bin_dir, data_dir = get_obs_module_dirs()
    except Exception:  # noqa: BLE001 - optional dependency / bundle boundary
        log.debug("pylibobs module dirs unavailable", exc_info=True)
        return None
    if not data_dir:
        return None
    # get_obs_module_dirs() returns libobs' "%module%" template, not a real path.
    return Path(data_dir.replace("%module%", _DSHOW_MODULE))


def _obs_install_data_dirs() -> Iterator[Path]:
    """A co-installed OBS Studio's copy of the filter DLLs (fallback source)."""
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            yield Path(root) / "obs-studio" / "data" / "obs-plugins" / _DSHOW_MODULE


def _solin_filter_dirs() -> Iterator[Path]:
    """Where Solin's own filter ships.

    Frozen builds put it next to the executable; a source checkout has it in
    ``tools/solin-dshowcam`` after ``build.bat``. ProgramData is where the
    registration ultimately points, so an already-installed copy counts too —
    the registry records an absolute path, and re-registering from a different
    location would silently repoint every consumer at the new file.

    The frozen check tests ``__compiled__`` as well as ``sys.frozen`` because
    **Nuitka sets only the former** — Solin ships a Nuitka build, so testing
    ``sys.frozen`` alone silently skips the shipped ``camera/`` directory and the
    camera never installs. It still works on a developer machine, where
    ProgramData happens to hold a manually-deployed copy, so the failure only
    shows up on a clean install. See bootstrap/profile_flow.py for the same idiom.
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        yield Path(sys.executable).resolve().parent / "camera"
    yield _INSTALLED_FILTER_DIR
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tools").is_dir():
            yield parent / "tools" / "solin-dshowcam"
            break


def solin_filter_dlls() -> list[Path]:
    """Solin's own filter DLLs, at most one per bitness.

    Deduplicated by *filename*, not by path: the same DLL commonly exists in both
    the install location and a developer checkout, and registering one CLSID
    twice from two paths would leave the registry pointing at whichever ran last
    — silently repointing every consumer at a possibly stale build.
    """
    found: list[Path] = []
    claimed: set[str] = set()
    for root in _solin_filter_dirs():
        for name in _SOLIN_FILTER_DLLS:
            if name.lower() in claimed:
                continue
            dll = root / name
            try:
                exists = dll.is_file()
            except OSError:
                continue
            if exists:
                claimed.add(name.lower())
                found.append(dll)
    return found


def filter_dlls() -> list[Path]:
    """Every virtual-camera filter DLL present on disk, best first.

    Solin's own filter wins outright — it is the one that enumerates under the
    branded name. OBS's module is only a fallback so an operator who already has
    OBS installed still gets a working (if wrongly-named) camera.
    """
    solin = solin_filter_dlls()
    if solin:
        return solin

    found: list[Path] = []
    seen: set[str] = set()
    roots = [_plugin_data_dir(), *_obs_install_data_dirs()]
    for root in roots:
        if root is None:
            continue
        for name in _FILTER_DLLS:
            dll = root / name
            key = str(dll).lower()
            if key in seen:
                continue
            try:
                exists = dll.is_file()
            except OSError:
                continue
            if exists:
                seen.add(key)
                found.append(dll)
    return found


def module_installed() -> bool:
    """True when at least one filter DLL is on disk (registered or not)."""
    return bool(filter_dlls())


# ── read-only registry probing ────────────────────────────────────────────────


def _registry_views() -> list[tuple[int, int]]:
    """``(hive, wow64 access flag)`` pairs covering every place a filter can land.

    Both bitnesses of the filter are registered, and a per-user (non-elevated)
    registration redirects to ``HKCU\\Software\\Classes`` — so all four
    combinations have to be searched before concluding "not registered".
    """
    import winreg

    return [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
    ]


def _inproc_server(hive: int, wow: int, clsid: str) -> str:
    """The DLL backing ``clsid`` in one registry view ("" when absent)."""
    import winreg

    path = f"SOFTWARE\\Classes\\CLSID\\{clsid}\\InprocServer32"
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | wow) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return ""
    return str(value or "")


def _iter_registered_cameras() -> Iterator[tuple[str, str, str]]:
    """Yield ``(clsid, friendly_name, dll_path)`` for every DirectShow camera."""
    import winreg

    for hive, wow in _registry_views():
        instances = f"SOFTWARE\\Classes\\CLSID\\{_VIDEO_INPUT_CATEGORY}\\Instance"
        try:
            key = winreg.OpenKey(hive, instances, 0, winreg.KEY_READ | wow)
        except OSError:
            continue
        with key:
            for index in range(1024):  # bounded: never spin on a hostile hive
                try:
                    clsid = winreg.EnumKey(key, index)
                except OSError:
                    break  # no more subkeys
                try:
                    with winreg.OpenKey(key, clsid) as sub:
                        name, _kind = winreg.QueryValueEx(sub, "FriendlyName")
                except OSError:
                    name = ""
                yield clsid, str(name or ""), _inproc_server(hive, wow, clsid)


def _is_obs_filter(dll_path: str) -> bool:
    """True when a registered CLSID is backed by an OBS virtual-camera module."""
    return Path(dll_path).name.lower() in {n.lower() for n in _FILTER_DLLS}


def find_loopback_device() -> tuple[str, str] | None:
    """``(clsid, friendly_name)`` of the registered virtual camera, else ``None``.

    A branded filter named exactly ``DESIRED_LABEL`` wins over the stock OBS one,
    so a Phase-2 install is reported as ``READY`` even while OBS's own camera is
    still registered alongside it.
    """
    fallback: tuple[str, str] | None = None
    try:
        for clsid, name, dll in _iter_registered_cameras():
            if name == _base.DESIRED_LABEL:
                return clsid, name
            if fallback is None and _is_obs_filter(dll):
                fallback = (clsid, name)
    except OSError:
        log.debug("DirectShow camera enumeration failed", exc_info=True)
        return None
    return fallback


def registered_dll() -> str:
    """The DLL path backing the currently-registered camera ("" when none)."""
    try:
        for _clsid, name, dll in _iter_registered_cameras():
            if name == _base.DESIRED_LABEL or _is_obs_filter(dll):
                return dll
    except OSError:
        log.debug("DirectShow camera enumeration failed", exc_info=True)
    return ""


def is_loaded() -> bool:
    """True when a virtual-camera filter is COM-registered and enumerable."""
    return find_loopback_device() is not None


def detect() -> _base.VcamProvisionState:
    """Classify the Windows virtual-camera sink. Never raises."""
    try:
        device = find_loopback_device()
        if device is None:
            return (
                _base.VcamProvisionState.NOT_LOADED
                if module_installed()
                else _base.VcamProvisionState.MODULE_MISSING
            )
        return (
            _base.VcamProvisionState.READY
            if device[1] == _base.DESIRED_LABEL
            else _base.VcamProvisionState.WRONG_LABEL
        )
    except Exception:  # noqa: BLE001 - called on the GUI thread; must never raise
        log.debug("virtual-camera detection failed", exc_info=True)
        return _base.VcamProvisionState.MODULE_MISSING


def install_hint() -> str:
    """Operator guidance when no filter DLL is on disk at all."""
    return (
        "The virtual camera needs its DirectShow filter, which ships with Solin. "
        "No copy of 'solin-dshowcam-x64.dll' was found, so the install looks "
        "incomplete.\n"
        "  Reinstall Solin, then start the virtual camera again."
    )


def prerequisite_hint() -> str:
    """Operator guidance when the filter is present but not registered."""
    return (
        "The virtual-camera filter is not registered with Windows, so meeting apps "
        "cannot see the camera yet. Solin registers it once, with an "
        "administrator prompt — accept it, then start the virtual camera again."
    )


# ── privileged registration ───────────────────────────────────────────────────


def _is_elevated() -> bool:
    """True when this process can already write HKLM (no UAC prompt needed)."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Win32 boundary; absence means "not elevated"
        log.debug("elevation check failed", exc_info=True)
        return False


def _regsvr32(bits: int) -> Path:
    """Absolute path to the ``regsvr32.exe`` matching a DLL's bitness.

    A 64-bit DLL must be registered by the 64-bit ``regsvr32`` and a 32-bit DLL by
    the 32-bit one. ``System32``/``SysWOW64`` mean different things depending on
    the *host* process bitness, so resolve against that rather than hardcoding —
    ``Sysnative`` is the alias a 32-bit process uses to reach real ``System32``.
    """
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    host_is_64 = sys.maxsize > 2**32
    if bits == 64:
        return root / ("System32" if host_is_64 else "Sysnative") / "regsvr32.exe"
    return root / ("SysWOW64" if host_is_64 else "System32") / "regsvr32.exe"


def _powershell() -> Path:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _dll_bits(dll: Path) -> int:
    """32 or 64, from the filename.

    Two naming schemes in play: OBS's ``obs-virtualcam-module32.dll`` and Solin's
    ``solin-dshowcam-x86.dll``. Getting this wrong hands the DLL to the wrong
    ``regsvr32``, which fails with a module-format error.
    """
    stem = dll.stem.lower()
    return 32 if stem.endswith("32") or stem.endswith("x86") else 64


def _is_solin_filter(dll: Path) -> bool:
    return dll.name.lower() in {name.lower() for name in _SOLIN_FILTER_DLLS}


def _register_commands(dlls: list[Path]) -> list[str]:
    """One PowerShell ``&``-call per DLL, each with its bitness-matched tool.

    ``/s`` silences the result dialog. OBS's module additionally needs ``/i``,
    which invokes ``DllInstall`` — that is what its own ``virtualcam-install.bat``
    does. Solin's filter exports no ``DllInstall``, so passing ``/i`` to it fails
    with "the entry-point DllInstall was not found".
    """
    commands = []
    for dll in dlls:
        tool = _regsvr32(_dll_bits(dll))
        for path in (tool, dll):
            if not _SAFE_PATH.match(str(path)):
                raise ValueError(f"unsafe virtual-camera path: {path!r}")
        flags = "/s" if _is_solin_filter(dll) else "/i /s"
        commands.append(f"& '{tool}' {flags} '{dll}'")
    return commands


def provision_argv(dlls: list[Path] | None = None) -> list[str]:
    """The full argv that registers every filter DLL.

    When Solin already runs elevated the registration is done directly. Otherwise
    a single elevated child is spawned via ``Start-Process -Verb RunAs``, which is
    what raises the UAC prompt — one prompt for both bitnesses, rather than one
    per DLL. The interpreter and ``regsvr32`` are absolute paths, never resolved
    through the caller's ``%PATH%``.
    """
    targets = filter_dlls() if dlls is None else dlls
    if not targets:
        raise ValueError("no virtual-camera filter DLL to register")
    commands = _register_commands(targets)
    shell = str(_powershell())
    if not _SAFE_PATH.match(shell):
        raise ValueError(f"unsafe virtual-camera path: {shell!r}")
    if _is_elevated():
        return [shell, "-NoProfile", "-NonInteractive", "-Command", "; ".join(commands)]
    # The inner script becomes ONE single-quoted literal inside the outer script,
    # and it already quotes its own paths — so its quotes must be doubled, which is
    # how PowerShell escapes a quote inside a single-quoted string. Without this
    # the first inner quote closes the literal early and the command is garbage.
    inner = "; ".join(commands).replace("'", "''")
    outer = (
        "$ErrorActionPreference='Stop'; "
        "try { $p = Start-Process -FilePath '" + shell + "' -ArgumentList "
        "'-NoProfile','-NonInteractive','-Command','" + inner + "' "
        "-Verb RunAs -Wait -PassThru; exit $p.ExitCode } catch { exit 1223 }"
    )
    return [shell, "-NoProfile", "-NonInteractive", "-Command", outer]


def provision(runner=None, timeout: int = 120) -> bool:
    """Register the filter so meeting apps can see the camera.

    Returns ``True`` when the camera is registered (including when it already was
    — this is idempotent and, crucially, does **not** prompt in that case).
    Returns ``False`` (logged, non-fatal) when there is nothing to register, the
    operator dismisses UAC, or ``regsvr32`` fails. Blocks on the UAC dialog, so
    call it off the GUI thread; it never touches Qt.
    """
    import subprocess

    if runner is None:
        runner = subprocess.run

    dlls = filter_dlls()
    if not dlls:
        log.warning("Virtual camera: no filter DLL to register. %s", install_hint())
        return False

    # Short-circuit ONLY when Solin's own filter is already registered —
    # re-registering it would prompt for nothing.
    #
    # A registered OBS filter is deliberately not good enough. It carries the
    # wrong name, and more importantly it reads OBS's shared memory, not Solin's
    # frame transport, so the meeting app would sit on OBS's placeholder forever.
    # Registering ours alongside it is safe: different CLSID, different device.
    current = registered_dll()
    if current and _is_solin_filter(Path(current)):
        log.info("Virtual camera: already registered (%s).", current)
        return True
    if current:
        log.info(
            "Virtual camera: %s is registered but cannot show Solin's output; "
            "installing Solin's own filter alongside it.",
            Path(current).name,
        )

    try:
        argv = provision_argv(dlls)
    except ValueError as exc:
        log.warning("Virtual-camera setup could not run: %s", exc)
        return False
    try:
        result = runner(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Virtual-camera setup could not run: %s", exc)
        return False
    if result.returncode == 0:
        log.info("Virtual camera registered (%s).", ", ".join(d.name for d in dlls))
        return True
    if result.returncode == 1223:  # ERROR_CANCELLED — the operator dismissed UAC
        log.info("Virtual-camera setup was cancelled or not authorised.")
    else:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        log.warning(
            "Virtual-camera registration failed (exit %s): %s",
            result.returncode, stderr.strip()[-400:],
        )
    return False


__all__ = [
    "detect",
    "filter_dlls",
    "find_loopback_device",
    "install_hint",
    "is_loaded",
    "module_installed",
    "prerequisite_hint",
    "provision",
    "provision_argv",
    "registered_dll",
]
