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
