"""Installed camera identity, platform selection and observed sink progress."""
import plistlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from solin.core.scenes import libobs_macos_virtual_camera as mac
from solin.core.scenes import libobs_virtual_camera as camera_module
from solin.core.scenes.output_health import OutputHealthStatus

UID = "7626645E-4425-469E-9D8B-97E0FA59AC75"


@pytest.fixture
def installed(monkeypatch, tmp_path):
    workspace = Mock()
    workspace.sharedWorkspace.return_value = workspace
    workspace.URLForApplicationWithBundleIdentifier_.return_value = SimpleNamespace(path=lambda: str(tmp_path))
    module = ModuleType("AppKit")
    module.NSWorkspace = workspace
    monkeypatch.setitem(sys.modules, "AppKit", module)
    metadata = tmp_path / "Contents/PlugIns/mac-virtualcam.plugin/Contents/Info.plist"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.obsproject.mac-virtualcam", "OBSCameraDeviceUUID": UID}))
    return SimpleNamespace(workspace=workspace, metadata=metadata)


def test_installed_camera_identity_comes_from_vendor_metadata(installed):
    assert mac.installed_obs_camera_uid() == UID
    installed.workspace.URLForApplicationWithBundleIdentifier_.assert_called_once_with("com.obsproject.obs-studio")


@pytest.mark.parametrize("metadata", [[], {}, {"OBSCameraDeviceUUID": "invalid"},
                                     {"CFBundleIdentifier": "another-plugin", "OBSCameraDeviceUUID": UID}])
def test_invalid_installed_metadata_does_not_select_another_device(installed, metadata):
    installed.metadata.write_bytes(plistlib.dumps(metadata))
    assert mac.installed_obs_camera_uid() is None


def test_missing_obs_does_not_create_or_install_a_camera(installed):
    installed.workspace.URLForApplicationWithBundleIdentifier_.return_value = None
    assert mac.installed_obs_camera_uid() is None


def test_removed_metadata_is_unavailable(installed):
    installed.metadata.unlink()
    assert mac.installed_obs_camera_uid() is None


def test_damaged_xml_metadata_is_unavailable(installed):
    installed.metadata.write_bytes(b'<?xml version="1.0"?><plist><dict>')
    assert mac.installed_obs_camera_uid() is None


@pytest.fixture
def backend(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    output = SimpleNamespace(active=True, frames_consumed=0, close=Mock(), last_error="")
    factory = Mock(return_value=output)
    module = ModuleType("pylibobs.macos_camera")
    module.CameraOutput = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(mac, "installed_obs_camera_uid", lambda: UID)
    monkeypatch.setattr(mac.time, "monotonic", lambda: clock.now)
    return SimpleNamespace(clock=clock, output=output, factory=factory)


def test_readiness_requires_service_consumption_and_stall_is_observable(backend):
    camera = mac.LibobsMacVirtualCamera(object())
    assert camera.start()
    assert camera.health.status is OutputHealthStatus.STARTING
    backend.output.frames_consumed = 1
    assert camera.health.status is OutputHealthStatus.READY
    backend.clock.now += 3
    assert camera.health.status is OutputHealthStatus.FAILED
    camera.stop()
    camera.stop()
    backend.output.close.assert_called_once()


def test_extension_unavailable_does_not_leave_an_active_owner(backend):
    backend.factory.side_effect = OSError("extension unavailable")
    camera = mac.LibobsMacVirtualCamera(object())
    assert not camera.start() and not camera.active
    backend.output.close.assert_not_called()


def test_darwin_uses_installed_camera_backend_and_existing_recovery(backend, monkeypatch):
    monkeypatch.setattr(camera_module.sys, "platform", "darwin")
    runtime = SimpleNamespace(ob=SimpleNamespace(enum_output_types=Mock(side_effect=AssertionError("No v4l2 on macOS"))))
    camera = camera_module.LibobsVirtualCamera(runtime)
    try:
        assert camera.start()
        backend.factory.assert_called_once_with(UID)
        backend.output.frames_consumed = 1
        assert camera.poll().status is OutputHealthStatus.READY
        backend.output.active = False
        assert camera.poll().status is OutputHealthStatus.RECOVERING
        backend.output.close.assert_called_once()
    finally:
        camera.stop()
