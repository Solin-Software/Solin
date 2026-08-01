"""Self-contained v4l2loopback provisioning for the virtual camera (Linux).

The virtual-camera sink is a ``v4l2loopback`` device whose visible name (what
Zoom/Meet show) is its ``card_label`` — fixed when the kernel module loads, a root
operation. Solin bundles the libobs virtualcam plugin (no OBS app needed), but it
can't rename a live device unprivileged. This module drives the one-time,
polkit-authenticated setup that loads the module labelled "Solin Virtual Camera"
and persists that across reboots — the same mechanism OBS uses (``pkexec
modprobe``), branded for Solin and made persistent so it prompts only once.

Nothing here is bundled that touches the kernel: ``v4l2loopback`` itself is a
system package (a kernel module can't ship in an app — it must match the running
kernel). When it is absent, :func:`detect` reports ``MODULE_MISSING`` and
:func:`install_hint` tells the operator the one package to install.

The privileged command contains **no user input** — the label is a validated
constant — so there is no injection surface.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from enum import Enum

log = logging.getLogger(__name__)

#: The card label the loopback device should advertise (what meeting apps show).
DESIRED_LABEL = "Solin Virtual Camera"

#: Persistent config so the module loads (labelled) automatically at every boot —
#: this is what makes the polkit prompt one-time rather than per-session.
_MODPROBE_CONF = "/etc/modprobe.d/solin-virtualcam.conf"
_MODULES_LOAD_CONF = "/etc/modules-load.d/solin-virtualcam.conf"

_MODULE = "v4l2loopback"
_V4L2LOOPBACK_SYSFS = "/sys/module/v4l2loopback"
#: Labels never valid for a config line (defence-in-depth: DESIRED_LABEL is a
#: constant, but validate anyway so the label can never inject shell/config).
_SAFE_LABEL = re.compile(r"\A[A-Za-z0-9 ]{1,32}\Z")


class VcamProvisionState(Enum):
    """What, if anything, must happen before the vcam sink is correctly named."""

    UNSUPPORTED = "unsupported"  # not Linux — vcam sink handled by the OS
    MODULE_MISSING = "module_missing"  # v4l2loopback not installed (system package)
    NOT_LOADED = "not_loaded"  # installed but no loopback device present
    WRONG_LABEL = "wrong_label"  # a loopback exists, but not named DESIRED_LABEL
    READY = "ready"  # a loopback exists, already named DESIRED_LABEL


def is_loaded() -> bool:
    """True when the ``v4l2loopback`` kernel module is currently loaded."""
    return os.path.isdir(_V4L2LOOPBACK_SYSFS)


def module_installed() -> bool:
    """True when ``v4l2loopback`` is installed for the running kernel.

    Loaded implies installed; otherwise ask ``modinfo``; if that is unavailable,
    fall back to scanning the modules tree.
    """
    if is_loaded():
        return True
    modinfo = shutil.which("modinfo")
    if modinfo:
        try:
            r = subprocess.run(
                [modinfo, _MODULE], capture_output=True, timeout=5, check=False
            )
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    import glob

    release = os.uname().release
    return bool(
        glob.glob(f"/lib/modules/{release}/**/{_MODULE}.ko*", recursive=True)
    )


def find_loopback_device() -> tuple[str, str] | None:
    """Best-effort ``(device_path, card_name)`` of the v4l2loopback sink (Linux).

    Loopback devices are identified by their v4l2loopback-specific sysfs knobs
    (real cameras lack ``max_openers``), so this works regardless of the label the
    module was loaded with — and reports the real name to the operator.
    """
    if not sys.platform.startswith("linux"):
        return None
    import glob

    for dev_dir in sorted(glob.glob("/sys/class/video4linux/video*")):
        if not os.path.exists(os.path.join(dev_dir, "max_openers")):
            continue  # not a v4l2loopback device
        try:
            with open(os.path.join(dev_dir, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            name = ""
        return "/dev/" + os.path.basename(dev_dir), name
    return None


def detect() -> VcamProvisionState:
    """Classify the current v4l2loopback situation for the vcam sink."""
    if not sys.platform.startswith("linux"):
        return VcamProvisionState.UNSUPPORTED
    if not is_loaded():
        return (
            VcamProvisionState.NOT_LOADED
            if module_installed()
            else VcamProvisionState.MODULE_MISSING
        )
    device = find_loopback_device()
    if device is None:
        # Loaded but no usable loopback device — treat as needing a (re)load.
        return VcamProvisionState.NOT_LOADED
    return (
        VcamProvisionState.READY
        if device[1] == DESIRED_LABEL
        else VcamProvisionState.WRONG_LABEL
    )


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def install_hint() -> str:
    """Operator guidance to install the v4l2loopback kernel module."""
    return (
        "The virtual camera needs the 'v4l2loopback' kernel module, which is a "
        "system package (not part of Solin, and not OBS). Install it once, e.g.:\n"
        "  Debian/Ubuntu:  sudo apt install v4l2loopback-dkms\n"
        "  Fedora:         sudo dnf install v4l2loopback\n"
        "  Arch:           sudo pacman -S v4l2loopback-dkms\n"
        "then restart Solin."
    )


def _provision_script() -> str:
    """The fixed root script: persist the labelled config, then (re)load it.

    ``DESIRED_LABEL`` is validated to a safe charset, so it cannot inject into the
    config line or the shell. The reload is best-effort — if the device is busy
    (another app has it open) the ``modprobe -r`` is skipped and the persistent
    config applies at the next boot instead.
    """
    if not _SAFE_LABEL.match(DESIRED_LABEL):
        raise ValueError(f"unsafe virtual-camera label: {DESIRED_LABEL!r}")
    options = f'options v4l2loopback exclusive_caps=1 card_label="{DESIRED_LABEL}"'
    # Every interpolated value is a fixed, metacharacter-free constant; the paths
    # and module name are also single-quoted so the "interpolate only safe values"
    # invariant survives any future edit that adds a space/metacharacter.
    return (
        "set -e\n"
        f"printf '%s\\n' '{options}' > '{_MODPROBE_CONF}'\n"
        f"printf '%s\\n' '{_MODULE}' > '{_MODULES_LOAD_CONF}'\n"
        f"modprobe -r '{_MODULE}' 2>/dev/null || true\n"
        f"modprobe '{_MODULE}'\n"
        "sleep 0.5\n"  # let udev create the /dev/video node before we use it
    )


def provision_argv() -> list[str]:
    """The full ``pkexec`` argv that performs the privileged setup.

    The interpreter is a hardcoded absolute ``/bin/sh`` — never resolved through
    the caller's ``$PATH`` — so a malicious ``sh`` planted earlier in a
    user-writable PATH entry cannot hijack the root-authorised command.
    """
    return ["pkexec", "/bin/sh", "-c", _provision_script()]


def provision(runner=subprocess.run, timeout: int = 120) -> bool:
    """Run the one-time privileged setup (writes the persistent config + loads the
    module labelled ``DESIRED_LABEL``). Shows a polkit password dialog.

    Returns True on success. Returns False (logged) if pkexec is unavailable, the
    operator cancels the prompt, or the command fails. Intended to be called off
    the GUI thread — it blocks on the polkit dialog.
    """
    if not sys.platform.startswith("linux"):
        return False
    if not pkexec_available():
        log.warning(
            "Cannot set up the virtual camera automatically: 'pkexec' is not "
            "available. Run this once manually:\n  sudo modprobe -r %s; sudo "
            'modprobe %s exclusive_caps=1 card_label="%s"',
            _MODULE, _MODULE, DESIRED_LABEL,
        )
        return False
    try:
        result = runner(provision_argv(), capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Virtual-camera setup could not run: %s", exc)
        return False
    if result.returncode == 0:
        log.info("Virtual camera provisioned as '%s'", DESIRED_LABEL)
        return True
    if result.returncode in (126, 127):
        log.info("Virtual-camera setup was cancelled or not authorised.")
    else:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        log.warning(
            "Virtual-camera setup failed (exit %s): %s",
            result.returncode, stderr.strip()[-400:],
        )
    return False


__all__ = [
    "DESIRED_LABEL",
    "VcamProvisionState",
    "detect",
    "find_loopback_device",
    "install_hint",
    "is_loaded",
    "module_installed",
    "pkexec_available",
    "provision",
    "provision_argv",
]
