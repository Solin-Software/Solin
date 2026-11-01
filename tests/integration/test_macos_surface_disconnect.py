"""A closing Cocoa receiver retires its sidecar producer without stopping peers."""
from __future__ import annotations

from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="native Cocoa surface transport")
def test_closed_receivers_release_sidecar_producers_and_render_callback():
    with subprocess.Popen(
        [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail((stdout + stderr)[-16000:])
        assert process.returncode == 0, (stdout + stderr)[-16000:]
    assert "SURFACE_DISCONNECT_COMPLETE" in stdout
    assert "Surface output failed" not in stderr
    assert "Exception ignored from cffi callback" not in stderr


def _produce(endpoints, connection):
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes.libobs_surface_output import LibobsSurfaceOutput

    try:
        with ExitStack() as stack:
            runtime = ObsRuntime()
            stack.callback(runtime.shutdown)
            runtime.ensure_started(width=320, height=180, fps=30)
            outputs = LibobsSurfaceOutput(runtime, threading.RLock(), lambda _: None)
            stack.callback(outputs.shutdown)
            outputs.set_targets([
                {"surface_endpoint": endpoint, "target_id": str(index), "bus_id": "media_windows"}
                for index, endpoint in enumerate(endpoints)
            ])
            connection.send({"ready": True})
            while True:
                outputs.poll()
                if not connection.poll(0.02):
                    continue
                message = connection.recv()
                if message == "stop":
                    return
                assert message == "status"
                connection.send({"active": len(outputs.render_targets),
                                 "callback": outputs._callback is not None})
    except BaseException:
        connection.send({"error": traceback.format_exc()})
        raise
    finally:
        connection.close()


def _scenario():
    import AppKit
    import objc
    from pylibobs.macos_surfaces import SurfacePresenter

    AppKit.NSApplication.sharedApplication()
    windows, presenters = [], []
    receive, send = multiprocessing.get_context("spawn").Pipe()
    process = None
    try:
        for index in range(2):
            window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(30 + index * 340, 30, 320, 180),
                AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False,
            )
            window.setReleasedWhenClosed_(False)
            window.makeKeyAndOrderFront_(None)
            windows.append(window)
            presenters.append(SurfacePresenter(objc.pyobjc_id(window.contentView())))
        process = multiprocessing.get_context("spawn").Process(
            target=_produce, args=([presenter.endpoint for presenter in presenters], send),
        )
        process.start()
        send.close()
        assert receive.poll(20), "producer did not initialize"
        ready = receive.recv()
        assert ready.get("ready"), ready

        def settle(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for presenter in presenters:
                    presenter.poll()
                if predicate():
                    return
                AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.01))
            raise AssertionError("native surface state did not settle")

        settle(lambda: all(presenter.frames_presented > 0 for presenter in presenters))
        closed = presenters.pop(0)
        closed.close()
        windows[0].close()
        reports = []

        def status_is(active):
            receive.send("status")
            assert receive.poll(5)
            report = receive.recv()
            assert "error" not in report, report
            reports.append(report)
            return report["active"] == active and report["callback"] == bool(active)

        settle(lambda: status_is(1))
        healthy = presenters[0]
        before = healthy.frames_presented
        settle(lambda: healthy.frames_presented > before + 2)
        healthy.close()
        presenters.clear()
        windows[1].close()
        settle(lambda: status_is(0))
        receive.send("stop")
        process.join(10)
        assert process.exitcode == 0
        print("SURFACE_DISCONNECT_COMPLETE " + json.dumps(reports[-1]))
    finally:
        if process is not None and process.is_alive():
            process.terminate()
            process.join(5)
        receive.close()
        send.close()
        for presenter in presenters:
            presenter.close()
        for window in windows:
            window.close()


if __name__ == "__main__":
    _scenario()
