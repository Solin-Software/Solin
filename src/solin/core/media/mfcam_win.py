"""Windows 11 Media Foundation virtual camera — an alternative camera backend.

**Not the default.** Solin's camera is the DirectShow filter in
``tools/solin-dshowcam``, because Windows appends a localized
"(Windows Virtual Camera)" suffix to the name of every camera created through
``MFCreateVirtualCamera``, whereas DirectShow reports the friendly name verbatim.
This module is kept because the Media Foundation route is the better long-term
bet if that ever changes: it is the modern, Microsoft-supported API.

:class:`MfVirtualCamera` calls ``MFCreateVirtualCamera`` and holds the resulting
handle. Frames reach it over the shared transport in :mod:`vcam_transport`.

The COM media source itself lives in ``tools/solin-mfcam`` and must be registered
machine-wide once (see that directory's README) — per-user registration is not
enough, because the Camera Frame Server that hosts it runs as ``LocalService``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

# Must match tools/solin-mfcam/solin-mfcam.cpp exactly.
CLSID_SOLIN_CAMERA = "{6F9C1B24-6E5A-4F4E-9C1D-2C7A5E3B8D41}"
FRIENDLY_NAME = "Solin Virtual Camera"

# The frame transport is shared with the DirectShow backend; re-exported here so
# existing callers of this module keep working.
from .vcam_transport import (  # noqa: E402,F401
    FRAME_BYTES,
    FRAME_HEIGHT,
    FRAME_PATH,
    FRAME_WIDTH,
    FrameTransport,
)

# MFVirtualCamera* enum values from mfvirtualcamera.h.
_TYPE_SOFTWARE_SOURCE = 0
_LIFETIME_SESSION = 0
_LIFETIME_SYSTEM = 1
_ACCESS_CURRENT_USER = 0
_ACCESS_ALL_USERS = 1
_MF_VERSION = 0x00020070

# IMFVirtualCamera vtable slots. It derives from IMFAttributes, so slots 0-2 are
# IUnknown, 3-32 are IMFAttributes' 30 methods, and the camera's own methods only
# begin at 33 (AddDeviceSourceInfo). Verified against the vtable in
# mfvirtualcamera.h — calling slot 3 as "Start" reads a null pointer and crashes.
_SLOT_START = 36
_SLOT_STOP = 37
_SLOT_REMOVE = 38
_SLOT_SHUTDOWN = 43


def available() -> bool:
    """Is this platform capable of hosting the Media Foundation camera at all?"""
    if sys.platform != "win32":
        return False
    return sys.getwindowsversion().build >= 22000  # MFCreateVirtualCamera is Win11+


class MfVirtualCamera:
    """Owns the ``IMFVirtualCamera`` handle that makes the device visible.

    Two lifetimes, and the choice matters more than it looks:

    ``persistent=True`` (system lifetime) registers the device once and leaves it
    in every application's camera list permanently, exactly like OBS's virtual
    camera — Solin merely starts and stops *feeding* it.  This is the right
    default for a product, because **most applications enumerate cameras only at
    startup**: a device that appears after Chrome launched typically will not
    show up until Chrome restarts.  Registering it requires elevation once.

    ``persistent=False`` (session lifetime) publishes the device only while this
    process holds the handle, and needs no elevation.  Useful for development —
    it leaves nothing behind — but users will find the camera missing whenever
    Solin is closed.
    """

    def __init__(self, *, persistent: bool = True) -> None:
        self._handle: ctypes.c_void_p | None = None
        self._mf_started = False
        self._persistent = persistent

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self) -> bool:
        if self._handle is not None:
            return True
        if not available():
            return False

        try:
            mfplat = ctypes.WinDLL("mfplat.dll")
            mfsensorgroup = ctypes.WinDLL("mfsensorgroup.dll")
        except OSError:
            logger.warning("Media Foundation not available", exc_info=True)
            return False

        if not self._mf_started:
            if mfplat.MFStartup(_MF_VERSION, 0) != 0:
                logger.warning("MFStartup failed")
                return False
            self._mf_started = True

        create = mfsensorgroup.MFCreateVirtualCamera
        create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        create.restype = ctypes.c_long

        lifetime = _LIFETIME_SYSTEM if self._persistent else _LIFETIME_SESSION
        access = _ACCESS_ALL_USERS if self._persistent else _ACCESS_CURRENT_USER

        handle = ctypes.c_void_p()
        hr = create(_TYPE_SOFTWARE_SOURCE, lifetime, access,
                    FRIENDLY_NAME, CLSID_SOLIN_CAMERA, None, 0, ctypes.byref(handle))
        if hr == 0x80070005 and self._persistent:  # E_ACCESSDENIED
            logger.warning(
                "registering a persistent virtual camera needs elevation; "
                "falling back to a session-lifetime camera for this run"
            )
            self._persistent = False
            hr = create(_TYPE_SOFTWARE_SOURCE, _LIFETIME_SESSION, _ACCESS_CURRENT_USER,
                        FRIENDLY_NAME, CLSID_SOLIN_CAMERA, None, 0, ctypes.byref(handle))
        if hr != 0:
            logger.warning(
                "MFCreateVirtualCamera failed (0x%08X) — is the media source registered "
                "machine-wide? See tools/solin-mfcam/README.md",
                hr & 0xFFFFFFFF,
            )
            return False

        if not self._invoke(handle, _SLOT_START):
            logger.warning("IMFVirtualCamera::Start failed")
            self._release(handle)
            return False

        self._handle = handle
        logger.info("%s is live", FRIENDLY_NAME)
        return True

    def stop(self) -> None:
        """Stop feeding the camera.

        A persistent camera deliberately stays registered — like OBS's, it
        remains in every application's device list, showing the media source's
        fallback image until Solin feeds it again. Use :meth:`remove` to
        unregister it for good.
        """
        if self._handle is None:
            return
        # For a persistent camera, only drop our reference. Calling Stop() or
        # Shutdown() unpublishes the device, which would defeat the whole point:
        # it must stay in other applications' camera lists after Solin exits.
        if not self._persistent:
            self._invoke(self._handle, _SLOT_STOP)
            self._invoke(self._handle, _SLOT_SHUTDOWN)
        self._release(self._handle)
        self._handle = None
        logger.info("%s stopped (still registered=%s)", FRIENDLY_NAME, self._persistent)

    def remove(self) -> None:
        """Unregister the device entirely, including a persistent registration."""
        if self._handle is None:
            return
        self._invoke(self._handle, _SLOT_REMOVE)
        self._invoke(self._handle, _SLOT_SHUTDOWN)
        self._release(self._handle)
        self._handle = None
        logger.info("%s unregistered", FRIENDLY_NAME)

    # -- COM vtable helpers ---------------------------------------------------
    #
    # ctypes has no COM interface support for IMFVirtualCamera, so call through
    # the vtable directly. See the slot constants above: IMFVirtualCamera derives
    # from IMFAttributes, so its own methods start well past IUnknown.

    @staticmethod
    def _vtable(handle: ctypes.c_void_p):
        return ctypes.cast(
            handle, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents

    @classmethod
    def _invoke(cls, handle: ctypes.c_void_p, slot: int) -> bool:
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            cls._vtable(handle)[slot]
        )
        return fn(handle, None) == 0

    @classmethod
    def _release(cls, handle: ctypes.c_void_p) -> None:
        fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(cls._vtable(handle)[2])
        fn(handle)


def is_source_registered() -> bool:
    """Is the COM media source registered where the frame server can see it?

    Only ``HKLM`` counts: the frame server runs as ``LocalService`` and cannot
    read the user's ``HKCU\\Software\\Classes``, so an HKCU-only registration
    activates in-process but fails at ``IMFVirtualCamera::Start``.
    """
    if sys.platform != "win32":
        return False
    import winreg

    key = rf"SOFTWARE\Classes\CLSID\{CLSID_SOLIN_CAMERA}\InprocServer32"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            path, _ = winreg.QueryValueEx(handle, None)
            return bool(path) and os.path.isfile(path)
    except OSError:
        return False
