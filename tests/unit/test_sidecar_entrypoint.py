from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from solin.bootstrap import entrypoint
from solin.core.foundation.constants import LIBOBS_SIDECAR_ARGUMENT
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, encode_envelope, read_envelope
from tests._paths import REPO_ROOT


def test_sidecar_role_exits_with_engine_status_before_application_import(monkeypatch):
    from solin.core.scenes import libobs_sidecar

    monkeypatch.setattr(sys, "argv", ["Solin.exe", LIBOBS_SIDECAR_ARGUMENT])
    monkeypatch.setitem(sys.modules, "solin.bootstrap.application", None)
    monkeypatch.setattr(libobs_sidecar, "main", lambda: 9)
    with pytest.raises(SystemExit) as error:
        entrypoint.run()
    assert error.value.code == 9


def test_sidecar_role_rejects_application_arguments_without_launching_ui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Solin.exe", LIBOBS_SIDECAR_ARGUMENT, "--create-profile"])
    monkeypatch.setitem(sys.modules, "solin.bootstrap.application", None)
    with pytest.raises(SystemExit, match="accepts no application arguments"):
        entrypoint.run()


def test_launcher_sidecar_dispatch_speaks_binary_ipc_without_loading_qt(tmp_path: Path):
    # Import poisoning makes accidental GUI bootstrap fail even in a developer environment.
    code = (
        "import runpy, sys, os; "
        "sys.modules['PySide6'] = None; "
        "sys.modules['solin.bootstrap.application'] = None; "
        "os.environ['SOLIN_LIBOBS_SIDECAR_NO_RUNTIME'] = '1'; "
        f"sys.argv = [{str(REPO_ROOT / 'main.py')!r}, {LIBOBS_SIDECAR_ARGUMENT!r}]; "
        f"runpy.run_path({str(REPO_ROOT / 'main.py')!r}, run_name='__main__')"
    )
    request = SceneIpcEnvelope(
        message_type="hello",
        request_id="hello",
        session_id="role-test",
        process_generation="role-test",
        sequence=1,
        document_revision=0,
        deadline_monotonic_ms=0,
        payload={},
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=encode_envelope(request),
        capture_output=True,
        timeout=10,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    response = read_envelope(io.BytesIO(result.stdout))
    assert response is not None and response.message_type == "hello_ack"
    assert response.request_id == request.request_id
