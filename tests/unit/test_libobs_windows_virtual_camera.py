"""Tests for the Windows vcam orchestrator that are safe on any OS.

The producer's file mapping + broker are Windows-only; this covers the ring
filename contract, the non-Windows construction guard, and that ``LibobsVirtualCamera``
routes to the Windows path on win32 without disturbing the Linux v4l2 path.
"""
from __future__ import annotations

import sys

import pytest

from solin.core.scenes.libobs_windows_virtual_camera import temporary_frame_file_name


def test_frame_file_name_matches_the_native_pattern():
    name = temporary_frame_file_name("{ABCD}")
    assert name == "Solin.VirtualCamera.{ABCD}.frames"


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows file mapping and ACLs")
def test_native_ring_mapping_is_writable_and_removed_on_close(tmp_path, monkeypatch):
    from pathlib import Path

    from solin.core.scenes import libobs_windows_virtual_camera as camera
    from solin.core.scenes.windows_vcam_identity import current_user_sid

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    # Restrict this lifecycle check to its own ring, without sweeping other producers.
    monkeypatch.setattr(camera, "_sweep_orphaned_rings", lambda: 0)
    ring = camera._WindowsSharedRingFile(4096, current_user_sid())
    path = Path(ring.path)
    try:
        assert path.parent == tmp_path
        assert path.stat().st_size == 4096
        ring.buffer[:4] = b"test"
        assert path.read_bytes()[:4] == b"test"
    finally:
        ring.close()
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="guard is for non-Windows hosts")
def test_orchestrator_construction_refused_off_windows():
    from solin.core.scenes.libobs_windows_virtual_camera import LibobsWindowsVirtualCamera

    with pytest.raises(RuntimeError):
        LibobsWindowsVirtualCamera(object())


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the non-Windows v4l2 branch")
def test_virtual_camera_uses_the_v4l2_path_off_windows():
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    class _Ctx:
        def get_video(self):
            return "video"

        def get_audio(self):
            return "audio"

    class _Output:
        def __init__(self):
            self.started = False

        def set_media(self, video, audio):
            self.video, self.audio = video, audio

        def start(self):
            self.started = True
            return True

        def stop(self):
            self.started = False

        def release(self):
            pass

    created = {}

    class _Ob:
        def enum_output_types(self):
            return ["virtualcam_output"]

        class Output:
            @staticmethod
            def create(kind, name, settings):
                created["kind"] = kind
                return _Output()

    class _Runtime:
        ob = _Ob()
        context = _Ctx()

    camera = LibobsVirtualCamera(_Runtime())
    assert camera.start() is True
    assert camera.active is True
    assert created["kind"] == "virtualcam_output"
    camera.stop()
    assert camera.active is False
