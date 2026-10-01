from __future__ import annotations

import pytest

from solin.core.scenes import windows_vcam_api as api
from solin.core.scenes.libobs_windows_virtual_camera import (
    LibobsWindowsVirtualCamera,
    _WindowsSharedRingFile,
)
from solin.core.scenes.windows_vcam_broker import WindowsVcamBroker
from solin.core.scenes.windows_vcam_identity import (
    current_session_id,
    current_user_broker_pipe_name,
    current_user_sid,
)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize(
    "entrypoint",
    [
        api.require_windows,
        lambda: api.load_windows_library("kernel32"),
        api.windows_last_error,
        lambda: api.windows_file_descriptor(0, 0),
        current_user_sid,
        current_session_id,
        current_user_broker_pipe_name,
        lambda: WindowsVcamBroker("pipe", "sid", lambda: None),
        lambda: LibobsWindowsVirtualCamera(object()),
        lambda: _WindowsSharedRingFile(4096, "sid"),
    ],
)
def test_windows_entrypoints_fail_before_native_access_on_other_platforms(
    monkeypatch, platform, entrypoint,
):
    with monkeypatch.context() as context:
        context.setattr(api.sys, "platform", platform)
        with pytest.raises(RuntimeError, match="Windows-only"):
            entrypoint()
