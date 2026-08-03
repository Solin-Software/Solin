"""macOS virtual-camera provisioning — detection and operator guidance.

macOS is the odd one out. Linux loads a kernel module and Windows registers a
COM server, and Solin can perform both itself. macOS cannot work that way: since
12.3 the supported virtual camera is a **Camera Extension** (``CMIOExtension``),
a system extension that

* lives *inside* Solin's ``.app`` bundle (it cannot be installed separately),
* must be signed with an Apple Developer ID and notarised — an unsigned
  extension is refused by the OS outright, and
* is installed by the host app via ``OSSystemExtensionRequest``, then explicitly
  approved by the user in System Settings.

None of that can be driven from Python: the request API is Swift/Objective-C and
has to come from the signed app bundle. So this module **detects and explains**;
it never attempts installation. :func:`provision` deliberately returns False with
a hint rather than pretending.

The Swift half is specified in ``docs/macos-vcam-handover.md``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from . import vcam_provision as _base

log = logging.getLogger(__name__)

#: Bundle identifier of the camera extension shipped inside Solin.app. The Swift
#: target must use exactly this, and it must be a sub-identifier of the host app
#: — macOS rejects a system extension whose id is not prefixed by its host's.
EXTENSION_ID = "com.solin.Solin.CameraExtension"

#: `systemextensionsctl list` marks an approved, running extension "activated
#: enabled". Anything else (e.g. "activated waiting for user") means the user has
#: not approved it yet.
_ACTIVE_MARKERS = ("activated enabled",)


def _systemextensionsctl() -> str:
    """Raw `systemextensionsctl list` output ("" when unavailable)."""
    try:
        result = subprocess.run(
            ["/usr/bin/systemextensionsctl", "list"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("systemextensionsctl unavailable", exc_info=True)
        return ""
    return (result.stdout or b"").decode("utf-8", "replace")


def _extension_lines() -> list[str]:
    return [line for line in _systemextensionsctl().splitlines() if EXTENSION_ID in line]


def extension_bundle() -> Path | None:
    """The camera extension bundled inside the running .app, if present.

    A frozen Solin.app keeps it at ``Contents/Library/SystemExtensions``. Absent
    means the build did not ship it — no amount of user action will help, so
    detection reports MODULE_MISSING rather than "not approved yet".
    """
    if sys.platform != "darwin":
        return None
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            candidate = parent / "Contents" / "Library" / "SystemExtensions"
            try:
                if candidate.is_dir():
                    for entry in candidate.iterdir():
                        if entry.name.startswith(EXTENSION_ID):
                            return entry
            except OSError:
                return None
            return None
    return None


def module_installed() -> bool:
    """True when the extension ships inside this build."""
    return extension_bundle() is not None


def is_loaded() -> bool:
    """True when the extension is installed AND approved by the user."""
    return any(
        any(marker in line for marker in _ACTIVE_MARKERS) for line in _extension_lines()
    )


def find_loopback_device() -> tuple[str, str] | None:
    """``(identifier, friendly name)`` for the active camera, else None.

    Mirrors the Linux/Windows contract so ``_log_started`` and ``detect`` work
    unchanged. The name is the constant the extension advertises, so a mismatch
    here means a stale extension is installed.
    """
    if not is_loaded():
        return None
    return (EXTENSION_ID, _base.DESIRED_LABEL)


def detect() -> _base.VcamProvisionState:
    """Classify the macOS situation. Never raises."""
    try:
        if not module_installed():
            return _base.VcamProvisionState.MODULE_MISSING
        if not is_loaded():
            # Shipped but not approved — the user has to allow it in System
            # Settings. This is the common first-run state.
            return _base.VcamProvisionState.NOT_LOADED
        return _base.VcamProvisionState.READY
    except Exception:  # noqa: BLE001 - detection must never raise into the GUI
        log.debug("macOS vcam detection failed", exc_info=True)
        return _base.VcamProvisionState.MODULE_MISSING


def install_hint() -> str:
    """Guidance when the extension is not in the bundle at all."""
    return (
        "The virtual camera needs Solin's camera extension, which ships inside "
        "Solin.app. This copy does not contain it, so the install looks "
        "incomplete.\n"
        "  Reinstall Solin from the official .dmg, then try again."
    )


def prerequisite_hint() -> str:
    """Guidance when the extension is present but not yet approved."""
    return (
        "macOS needs your permission before Solin's virtual camera can run.\n"
        "  Open System Settings › General › Login Items & Extensions, find "
        "Camera Extensions, and enable Solin.\n"
        "Then start the virtual camera again."
    )


def provision(runner=subprocess.run, timeout: int = 120) -> bool:
    """Not possible from here — always False, with a logged explanation.

    Installing a camera extension requires ``OSSystemExtensionRequest`` from the
    signed app bundle; there is no command-line or Python equivalent, and macOS
    ignores anything unsigned. Returning False lets the caller surface
    :func:`prerequisite_hint` instead of silently doing nothing.

    ``runner``/``timeout`` exist only to match the cross-platform signature.
    """
    del runner, timeout
    log.info(
        "Virtual camera: macOS installs its camera extension through the app "
        "bundle and user approval, not through provisioning."
    )
    return False


def provision_argv() -> list[str]:
    """No privileged command exists on macOS — the contract's empty case."""
    return []


def open_extension_settings() -> bool:
    """Open the System Settings pane where the user approves the extension."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["/usr/bin/open", "x-apple.systempreferences:com.apple.ExtensionsPreferences"],
            check=False,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        log.debug("could not open the extensions settings pane", exc_info=True)
        return False
