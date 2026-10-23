"""Native sidecar graph routing into Cocoa-only windows in another process."""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "darwin" or importlib.util.find_spec("pylibobs") is None,
        reason="macOS and the native pylibobs runtime are required",
    ),
]
_REPORT = "MACOS_PROJECTION_REPORT="
_PROGRAM_BGRA = [0, 0, 255, 255]
_INITIAL_RGBA = {"projection": [0, 0, 255, 255], "editor": [0, 255, 0, 255]}
_SWITCHED_RGBA = {"projection": [0, 255, 0, 255], "editor": [0, 0, 255, 255]}


@pytest.mark.parametrize("frontend", ["cocoa", "qt"])
@pytest.mark.parametrize("transition", ["cut", "fade"])
def test_native_projection_take_and_editor_switch_remain_independent(frontend, transition):
    with subprocess.Popen(
        [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), frontend,
         transition],
        cwd=Path(__file__).resolve().parents[2], start_new_session=True,
        env={**os.environ, "QT_QPA_PLATFORM": "cocoa"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            # The scenario owns a second process. Kill the whole isolated group
            # on timeout rather than leaving its sidecar or native windows alive.
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail("Native projection timed out: " + (stdout + stderr)[-16000:])
        output = stdout + stderr
        assert process.returncode == 0, output[-16000:]
    assert "Exception ignored from cffi callback" not in output, output[-16000:]
    reports = [json.loads(line.removeprefix(_REPORT)) for line in stdout.splitlines()
               if line.startswith(_REPORT)]
    assert len(reports) == 1, output[-16000:]
    report = reports[0]
    assert report["initial"] == _INITIAL_RGBA
    assert report["switched"] == _SWITCHED_RGBA
    assert report["program_before"] == report["program_after"] == _PROGRAM_BGRA
    assert not report["ui_loaded_libobs"]
    assert report["shutdown_complete"]
    assert report["sidecar_generations"] == (2 if frontend == "qt" and transition == "fade" else 1)


def _sidecar(endpoints, stop, connection, transition):
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import render_source_to_bgra, shutdown
    from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
    from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine

    try:
        with ExitStack() as stack:
            runtime = ObsRuntime()
            stack.callback(runtime.shutdown)
            runtime.ensure_started(width=640, height=360, fps=30)
            engine = LibobsSidecarEngine(lambda: runtime)
            stack.callback(engine.shutdown)
            sequence = 0

            def request(kind, payload=None):
                nonlocal sequence
                sequence += 1
                return SceneIpcEnvelope(
                    kind, str(sequence), "projection-test", "generation-1", sequence, 7,
                    int((time.monotonic() + 20) * 1000), payload or {},
                )

            def program_pixel():
                deadline = time.monotonic() + 6
                pixel = None
                # A control ACK commits the graph; it does not promise that the
                # native video thread has ticked the newly created sources yet.
                while time.monotonic() < deadline:
                    frame = render_source_to_bgra(
                        engine._scene_graph.program_source, 16, 16,
                        canvas_width=runtime.video.width, canvas_height=runtime.video.height,
                    )
                    if frame is not None:
                        data, stride = frame
                        pixel = list(data[8 * stride + 32:8 * stride + 36])
                        if pixel == _PROGRAM_BGRA:
                            return pixel
                    stop.wait(0.01)
                raise AssertionError(f"Program render did not become ready: {pixel!r}")

            hello = engine.handle(request("hello"))
            assert hello is not None and hello.payload["hardware_compositing"]
            # Egress callbacks stop before their shared readback pool, which must
            # release its GPU resources before the engine destroys the runtime.
            stack.callback(shutdown)
            for egress in (engine._preview_egress, engine._program_egress):
                if egress is not None:
                    stack.callback(egress.shutdown)
            document = {
                "revision": 7,
                "sources": [{"id": color, "type": "color", "configuration": {"color": value}}
                            for color, value in (("red", "#ff0000"), ("blue", "#0000ff"),
                                                 ("green", "#00ff00"))],
                "scenes": [{"id": name, "layers": [{"id": name + "-layer", "source_id": color}]}
                           for name, color in (("program", "red"), ("projection", "blue"),
                                               ("editor", "green"))],
            }
            response = engine.handle(request("hydrate", {
                "document": document,
                "active_scenes": {"virtual_camera": "program", "media_windows": "projection",
                                  "editor": "editor"},
                "render_enabled": {"virtual_camera": True, "media_windows": True, "editor": True},
                "output_enabled": {"virtual_camera": False},
            }))
            assert response is not None and response.payload.get("applied"), response
            targets = [{
                "bus_id": "media_windows", "target_id": name, "surface_endpoint": endpoint,
                "scene_id": "editor" if name == "editor" else "", "visible": True,
                "native_handle": 0, "screen_id": name, "x": 0, "y": 0,
                "width": 640, "height": 360, "device_pixel_ratio": 1,
            } for name, endpoint in endpoints.items()]
            response = engine.handle(request("set_window_targets", {"window_targets": targets}))
            assert response is not None and response.payload.get("applied"), response
            connection.send({"stage": "initial", "program": program_pixel()})
            assert connection.poll(20), "UI did not request the independent scene changes"
            assert connection.recv() == "switch"
            prepared = engine.handle(request("prepare_scene", {
                "bus_id": "media_windows", "scene_id": "editor",
                "transition": {"kind": transition, "duration_ms": 250 if transition == "fade" else 0},
                "content_media_epoch": None, "content_source_kind": "frames",
            }))
            assert prepared is not None and prepared.message_type == "scene_prepared", prepared
            response = engine.handle(request("take_prepared", {
                "bus_id": "media_windows", "scene_id": "editor",
                "preparation_token": prepared.payload["preparation_token"],
            }))
            assert response is not None and response.payload.get("applied"), response
            targets[1]["scene_id"] = "projection"
            response = engine.handle(request("set_window_targets", {"window_targets": targets}))
            assert response is not None and response.payload.get("applied"), response
            connection.send({"stage": "switched", "program": program_pixel()})
            assert stop.wait(20), "UI did not finish validating the native windows"
        connection.send({"shutdown_complete": True})
    except BaseException:  # noqa: BLE001 - report native child failures to the isolated GUI test
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


def _presented_pixel(window):
    """Validation-only readback of one displayed pixel; transport stays on GPU."""
    import AppKit

    gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
    gl.glReadPixels.argtypes = [ctypes.c_int] * 4 + [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    gl.glReadBuffer.argtypes = [ctypes.c_uint]
    if hasattr(window, "native_handle"):
        import objc

        parent = objc.objc_object(c_void_p=window.native_handle)
    else:
        parent = window.contentView()
    view = next(view for view in parent.subviews() if view.respondsToSelector_("openGLContext"))
    context = view.openGLContext()
    previous = AppKit.NSOpenGLContext.currentContext()
    context.makeCurrentContext()
    try:
        backing = view.convertRectToBacking_(view.bounds())
        pixel = (ctypes.c_ubyte * 4)()
        gl.glReadBuffer(0x0404)  # GL_FRONT, after the native presenter's swap
        gl.glReadPixels(int(backing.size.width) // 2, int(backing.size.height) // 2,
                        1, 1, 0x1908, 0x1401, pixel)
        return list(pixel)
    finally:
        gl.glReadBuffer(0x0405)  # GL_BACK
        AppKit.NSOpenGLContext.clearCurrentContext()
        if previous is not None:
            previous.makeCurrentContext()


def _wait_stage(connection, windows, presenters, stage, expected, qt_app=None):
    import AppKit

    deadline = time.monotonic() + 20
    report = None
    pixels = {}
    while time.monotonic() < deadline:
        if qt_app is None:
            for presenter in presenters.values():
                presenter.poll()
        else:
            qt_app.processEvents()
        if connection.poll():
            report = connection.recv()
            assert "error" not in report, report.get("error")
        if report is not None and report.get("stage") == stage:
            pixels = {name: _presented_pixel(window) for name, window in windows.items()}
            if pixels == expected:
                return pixels, report["program"]
        AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
            AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.01)
        )
    raise AssertionError(f"Native stage {stage} did not settle: report={report!r}, pixels={pixels!r}")


def _native_scenario(frontend, transition):
    import AppKit
    import objc
    from pylibobs import _ffi
    from pylibobs.macos_surfaces import SurfacePresenter

    app = None
    if frontend == "qt":
        from PySide6.QtWidgets import QApplication
        from solin.widgets.projection.native_surface import NativeVideoSurface

        app = QApplication([])
    else:
        AppKit.NSApplication.sharedApplication()
    windows, presenters = {}, {}
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    receive, send = context.Pipe(duplex=True)
    process = None
    try:
        for index, name in enumerate(("projection", "editor")):
            if app is not None:
                surface = NativeVideoSurface()
                surface.setWindowTitle("Projection surface test " + name)
                surface.resize(400, 250)
                surface.show()
                _ = surface.surface_endpoint
                windows[name] = surface
                presenters[name] = surface._cocoa_presenter._presenter
                continue
            window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(30 + index * 420, 30, 400, 250), AppKit.NSWindowStyleMaskTitled,
                AppKit.NSBackingStoreBuffered, False,
            )
            window.setReleasedWhenClosed_(False)
            window.setTitle_("Native " + name + " test")
            window.makeKeyAndOrderFront_(None)
            windows[name] = window
            presenters[name] = SurfacePresenter(objc.pyobjc_id(window.contentView()))
        generations = 2 if app is not None and transition == "fade" else 1
        for generation in range(generations):
            if generation:
                receive.close()
                receive, send = context.Pipe(duplex=True)
                stop.clear()
            # Keep the same windows/endpoints across a sidecar restart. Frames
            # and acknowledgements from the old producer must not corrupt reuse.
            process = context.Process(target=_sidecar, args=(
                {name: presenter.endpoint for name, presenter in presenters.items()},
                stop, send, transition,
            ))
            process.start()
            send.close()
            initial, program_before = _wait_stage(
                receive, windows, presenters, "initial", _INITIAL_RGBA, app,
            )
            if app is not None:
                windows["projection"].resize(600, 320)
                app.processEvents()
            receive.send("switch")
            switched, program_after = _wait_stage(
                receive, windows, presenters, "switched", _SWITCHED_RGBA, app,
            )
            assert _ffi._lib is None, "The GUI process unexpectedly loaded libobs"
            stop.set()
            assert receive.poll(10), "Sidecar did not confirm orderly native shutdown"
            shutdown = receive.recv()
            assert shutdown.get("shutdown_complete"), shutdown
            process.join(timeout=5)
            assert process.exitcode == 0
        print(_REPORT + json.dumps({
            "initial": initial, "switched": switched,
            "program_before": program_before, "program_after": program_after,
            "ui_loaded_libobs": _ffi._lib is not None, "shutdown_complete": True,
            "sidecar_generations": generations,
        }))
    finally:
        stop.set()
        send.close()
        if process is not None:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        receive.close()
        for presenter in presenters.values():
            presenter.close()
        for window in windows.values():
            window.close()


if __name__ == "__main__":
    _native_scenario(sys.argv[1], sys.argv[2])
