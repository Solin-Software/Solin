"""The concrete on-GUI checks. Each runs inside its own subprocess.

Every check mirrors a validated per-stage smoke but returns a structured
:class:`~.harness.CheckResult` (with measured pixels/counts and, for visual paths,
a captured PNG artifact) instead of printing. Importing this module registers all
checks into the harness registry.
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import time
from typing import Any

from solin.tools.gui_validation.harness import (
    CAP_DISPLAY,
    CAP_FFPROBE,
    CheckStatus,
    register_check,
)

log = logging.getLogger(__name__)

_SEQ = itertools.count(1)


def _req(message_type: str, payload: dict | None = None):
    from solin.core.scenes.ipc_protocol import SceneIpcEnvelope

    return SceneIpcEnvelope(
        message_type=message_type, request_id=f"r{next(_SEQ)}", session_id="gui-val",
        process_generation="gen", sequence=0, document_revision=0,
        deadline_monotonic_ms=1, payload=payload or {},
    )


def _engine(ctx) -> Any:
    from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine

    return LibobsSidecarEngine(runtime_factory=lambda: ctx.runtime)


# ── foundation ────────────────────────────────────────────────────────────────


@register_check("runtime_boot", category="foundation",
                description="libobs runtime starts and reports a video pipeline")
def runtime_boot(ctx):
    rt = ctx.runtime
    video = rt.video
    ctx_version = getattr(rt.context, "version", "?")
    ok = rt.started and video.width > 0 and video.height > 0
    details = {"obs_version": str(ctx_version),
               "resolution": f"{video.width}x{video.height}", "fps": video.fps}
    return ctx.result(CheckStatus.PASS if ok else CheckStatus.FAIL,
                      f"libobs up at {video.width}x{video.height}@{video.fps}", details=details)


# ── scene compositing ─────────────────────────────────────────────────────────


@register_check("scene_composite", category="scene",
                description="a colour scene composites to the program main mix")
def scene_composite(ctx):
    engine = _engine(ctx)
    engine.handle(_req("hello"))
    engine.handle(_req("hydrate", {"document": ctx.color_document(("prog", "#FF0000")),
                                   "active_scenes": {"virtual_camera": "prog"}}))
    ctx.dwell()
    frame = ctx.capture_program_frame()
    if frame is None:
        return ctx.result(CheckStatus.FAIL, "no program frame captured")
    bgra = frame.center_bgra()
    color = ctx.classify(bgra)
    artifact = ctx.save_png(frame, "scene_composite.png")
    status = CheckStatus.PASS if color == "red" else CheckStatus.FAIL
    return ctx.result(status, f"program centre is {color} (expected red)",
                      details={"center_bgra": list(bgra)}, artifact=artifact)


@register_check("transitions", category="scene",
                description="prepare/take switches the program to a new scene")
def transitions(ctx):
    engine = _engine(ctx)
    engine.handle(_req("hello"))
    doc = ctx.color_document(("prog", "#FF0000"), ("next", "#00FF00"))
    engine.handle(_req("hydrate", {"document": doc, "active_scenes": {"virtual_camera": "prog"}}))
    before = ctx.capture_program_frame()
    prep = engine.handle(_req("prepare_scene", {"bus_id": "virtual_camera", "scene_id": "next"}))
    token = (prep.payload or {}).get("preparation_token") if prep else None
    take_payload = {"bus_id": "virtual_camera", "scene_id": "next"}
    if token:
        take_payload["preparation_token"] = token
    engine.handle(_req("take_prepared", take_payload))
    time.sleep(0.5)
    ctx.dwell()
    after = ctx.capture_program_frame()
    if before is None or after is None:
        return ctx.result(CheckStatus.FAIL, "missing program frame around the take")
    before_c, after_c = ctx.classify(before.center_bgra()), ctx.classify(after.center_bgra())
    artifact = ctx.save_png(after, "transitions_after.png")
    status = CheckStatus.PASS if (before_c == "red" and after_c == "green") else CheckStatus.FAIL
    return ctx.result(status, f"program went {before_c} -> {after_c} (expected red -> green)",
                      details={"before": before_c, "after": after_c}, artifact=artifact)


@register_check("image_source", category="scene",
                description="an image source resolves and composites")
def image_source(ctx):
    image = ctx.ensure_image("#0000FF", "image-blue.png")
    if not image:
        return ctx.skip("could not create a test image (Qt image save failed)")
    os.environ["SOLIN_SCENE_IMAGES_DIR"] = str(ctx.config.resolved_out_dir())
    engine = _engine(ctx)
    engine.handle(_req("hello"))
    doc = {
        "sources": [{"id": "img", "type": "image", "name": "img",
                     "configuration": {"asset_id": "image-blue.png"}}],
        "scenes": [{"id": "prog", "layers": [
            {"id": "l", "source_id": "img", "visible": True,
             "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]}],
    }
    engine.handle(_req("hydrate", {"document": doc, "active_scenes": {"virtual_camera": "prog"}}))
    ctx.dwell()
    frame = ctx.capture_program_frame()
    if frame is None:
        return ctx.result(CheckStatus.FAIL, "no program frame captured")
    color = ctx.classify(frame.center_bgra())
    artifact = ctx.save_png(frame, "image_source.png")
    status = CheckStatus.PASS if color == "blue" else CheckStatus.FAIL
    return ctx.result(status, f"image centre is {color} (expected blue)",
                      details={"center_bgra": list(frame.center_bgra())}, artifact=artifact)


# ── media playback (Fork A) ────────────────────────────────────────────────────


@register_check("media_playback", category="media",
                description="the sidecar decodes a local clip into the content slot")
def media_playback(ctx):
    clip = ctx.ensure_clip()
    if not clip:
        return ctx.skip("no clip available (provide --clip or install ffmpeg)")
    engine = _engine(ctx)
    events: list = []
    engine.set_event_sink(events.append)
    engine.handle(_req("hello"))
    doc = {
        "sources": [{"id": "solin.content.current", "type": "solin_content", "name": "C"}],
        "scenes": [{"id": "s1", "layers": [
            {"id": "content", "source_id": "solin.content.current", "visible": True,
             "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}}]}],
    }
    engine.handle(_req("hydrate", {"document": doc, "active_scenes": {"virtual_camera": "s1"}}))
    ack = engine.handle(_req("open_media", {"path": clip, "is_local_file": True, "autoplay": True,
                                            "volume_percent": 100, "speed_percent": 100,
                                            "trim_start_ms": 0, "trim_end_ms": 0}))
    applied = bool((ack.payload or {}).get("applied")) if ack else False
    time.sleep(1.0)  # let it decode + the poller emit a few state events
    ctx.dwell()
    frame = ctx.capture_program_frame()
    color = ctx.classify(frame.center_bgra()) if frame else "none"
    media_events = [e for e in events if e.message_type == "media_playback_state"]
    engine.handle(_req("control_media", {"action": "pause"}))
    time.sleep(0.2)
    paused = [e for e in events if e.message_type == "media_playback_state"]
    engine.handle(_req("control_media", {"action": "close"}))
    artifact = ctx.save_png(frame, "media_playback.png") if frame else None
    ok = applied and color == "green" and len(media_events) > 0
    status = CheckStatus.PASS if ok else CheckStatus.FAIL
    return ctx.result(status, f"clip composited {color}, {len(media_events)} state events",
                      details={"applied": applied, "center": color,
                               "state_events": len(media_events),
                               "last_state": (paused[-1].payload.get("state") if paused else None)},
                      artifact=artifact)


@register_check("routed_metadata", category="media",
                description="routed media title + embedded cover art are read out of band")
def routed_metadata(ctx):
    path, has_cover = ctx.ensure_tagged_media("Solin Test Title")
    if not path:
        return ctx.skip("could not create a tagged media file (install ffmpeg)")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
    from PySide6.QtGui import QGuiApplication

    from solin.core.media.routed_metadata import RoutedMediaMetadataExtractor

    _app = QGuiApplication.instance() or QCoreApplication.instance() or QGuiApplication([])
    extractor = RoutedMediaMetadataExtractor()
    captured: dict = {}
    loop = QEventLoop()

    def on_ready(session_id, title, cover):
        captured["title"] = title
        captured["cover_null"] = cover is None or cover.isNull()
        loop.quit()

    extractor.metadata_ready.connect(on_ready)
    QTimer.singleShot(12000, loop.quit)  # deadline > extractor timeout
    extractor.request(1, path)
    loop.exec()

    if "title" not in captured:
        return ctx.result(CheckStatus.FAIL, "metadata_ready never fired (backend read timed out)")
    title = captured["title"]
    cover_ok = (not has_cover) or (not captured["cover_null"])
    title_ok = title == "Solin Test Title"
    status = CheckStatus.PASS if (title_ok and cover_ok) else CheckStatus.FAIL
    summary = f"title={title!r}, cover={'present' if not captured['cover_null'] else 'none'}"
    if not has_cover:
        summary += " (cover not embeddable — title-only check)"
    return ctx.result(status, summary,
                      details={"title": title, "cover_present": not captured["cover_null"],
                               "cover_expected": has_cover})


# ── egress (app-owned shared-memory blocks) ────────────────────────────────────


def _egress_reader(ctx):
    from solin.core.scenes.content_frame_channel import SharedFrameChannelReader

    w, h = 320, 180
    reader = SharedFrameChannelReader(None, w, h, create=True)
    descriptor = {"transport": "shared_memory_bgra", "handle_token": reader.name,
                  "width": w, "height": h}
    return reader, descriptor, w, h


def _read_egress(reader, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = reader.read_latest()
        if frame is not None:
            return frame
        time.sleep(0.05)
    return None


@register_check("preview_egress", category="egress",
                description="the editor preview shows the edit scene, not the program")
def preview_egress(ctx):
    reader, descriptor, w, h = _egress_reader(ctx)
    try:
        engine = _engine(ctx)
        engine.handle(_req("hello"))
        doc = ctx.color_document(("prog", "#FF0000"), ("edit", "#00FF00"))
        engine.handle(_req("hydrate", {
            "document": doc,
            "active_scenes": {"virtual_camera": "prog", "media_windows": "edit"},
            "preview_egress": descriptor}))
        engine.handle(_req("set_render_enabled", {"bus_id": "media_windows", "enabled": True}))
        frame = _read_egress(reader)
        if frame is None:
            return ctx.result(CheckStatus.FAIL, "no preview frame in the shared block")
        off = (h // 2) * frame.stride + (w // 2) * 4
        bgra = tuple(frame.data[off:off + 4])
        color = ctx.classify(bgra)
        status = CheckStatus.PASS if color == "green" else CheckStatus.FAIL
        return ctx.result(status, f"preview centre is {color} (expected green edit scene)",
                          details={"center_bgra": list(bgra)})
    finally:
        reader.close()
        reader.unlink()


@register_check("program_egress", category="egress",
                description="the Program tab mirrors the main mix")
def program_egress(ctx):
    reader, descriptor, w, h = _egress_reader(ctx)
    try:
        engine = _engine(ctx)
        engine.handle(_req("hello"))
        doc = ctx.color_document(("prog", "#FF0000"))
        engine.handle(_req("hydrate", {"document": doc,
                                       "active_scenes": {"virtual_camera": "prog"},
                                       "program_egress": descriptor}))
        frame = _read_egress(reader)
        if frame is None:
            return ctx.result(CheckStatus.FAIL, "no program frame in the shared block")
        off = (h // 2) * frame.stride + (w // 2) * 4
        bgra = tuple(frame.data[off:off + 4])
        color = ctx.classify(bgra)
        status = CheckStatus.PASS if color == "red" else CheckStatus.FAIL
        return ctx.result(status, f"program mirror centre is {color} (expected red)",
                          details={"center_bgra": list(bgra)})
    finally:
        reader.close()
        reader.unlink()


# ── outputs ────────────────────────────────────────────────────────────────────


@register_check("recording", category="output", requires=(CAP_FFPROBE,), timeout_s=45.0,
                description="program + audio record to a playable MP4")
def recording(ctx):
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    engine = _engine(ctx)
    engine.handle(_req("hello"))
    engine.handle(_req("hydrate", {"document": ctx.color_document(("prog", "#FF0000")),
                                   "active_scenes": {"virtual_camera": "prog"}}))
    out = ctx.config.resolved_out_dir() / "recording.mp4"
    if out.exists():
        out.unlink()
    recorder = LibobsRecorder(ctx.runtime)
    started = recorder.start(str(out))
    if not started:
        return ctx.result(CheckStatus.FAIL, "recorder failed to start")
    time.sleep(2.5)
    recorder.stop()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "default=nw=1", str(out)],
        capture_output=True, text=True)
    streams = probe.stdout
    size = out.stat().st_size if out.exists() else 0
    has_video = "codec_type=video" in streams
    has_audio = "codec_type=audio" in streams
    # Gate on audio too: the recorder always attaches an AAC encoder to the main
    # mix, so a correct recording always carries an audio track — a video-only
    # file means the audio half regressed, which must not read as PASS.
    ok = probe.returncode == 0 and has_video and has_audio and size > 0
    return ctx.result(CheckStatus.PASS if ok else CheckStatus.FAIL,
                      f"recorded {size} bytes, video={has_video}, audio={has_audio}",
                      details={"ffprobe_rc": probe.returncode, "bytes": size,
                               "streams": streams.strip()}, artifact=str(out) if size else None)


@register_check("audio_devices", category="audio",
                description="audio capture devices enumerate")
def audio_devices(ctx):
    engine = _engine(ctx)
    engine.handle(_req("hello"))
    resp = engine.handle(_req("list_audio_devices"))
    if resp is None or resp.message_type != "audio_device_list":
        return ctx.result(CheckStatus.FAIL, "no audio_device_list reply",
                          details={"reply": resp.message_type if resp else None})
    payload = resp.payload or {}
    if not payload.get("supported"):
        return ctx.skip("audio enumeration unavailable on this host",
                        error_code=payload.get("error_code", ""))
    devices = payload.get("devices") or []
    if not devices:
        return ctx.skip("enumeration works but no capture devices are present")
    directions = sorted({str(d.get("direction", "")) for d in devices})
    return ctx.result(CheckStatus.PASS, f"{len(devices)} capture device(s): {', '.join(directions)}",
                      details={"count": len(devices), "directions": directions})


# ── window binding ─────────────────────────────────────────────────────────────


@register_check("window_output", category="window", requires=(CAP_DISPLAY,),
                description="a projection window binds and libobs paints into it")
def window_output(ctx):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    from PySide6.QtWidgets import QApplication, QWidget

    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph
    from solin.core.scenes.libobs_window_output import LibobsWindowOutput

    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.resize(640, 360)
    widget.show()
    app.processEvents()
    handle = int(widget.winId())

    graph = LibobsSceneGraph(ctx.runtime)
    graph.hydrate(ctx.color_document(("s1", "#FF0000")), {"virtual_camera": "s1"})
    output = LibobsWindowOutput(ctx.runtime)
    output.set_targets([{"native_handle": handle, "width": 640, "height": 360,
                         "device_pixel_ratio": 1.0, "visible": True, "bus_id": "media_windows",
                         "target_id": "t", "screen_id": "s", "x": 0, "y": 0}])
    bound = output.handles == (handle,)
    draws = {"n": 0}
    display = output._displays.get(handle) if bound else None
    if display is not None:
        display.add_draw_callback(lambda cx, cy: draws.__setitem__("n", draws["n"] + 1))
    for _ in range(40):
        app.processEvents()
        time.sleep(0.05)
    ctx.dwell()
    output.shutdown()
    graph.shutdown()
    status = CheckStatus.PASS if (bound and draws["n"] > 0) else CheckStatus.FAIL
    return ctx.result(status, f"window bound={bound}, {draws['n']} draw callbacks",
                      details={"bound": bound, "draw_callbacks": draws["n"], "xid": handle})


# ── virtual camera ─────────────────────────────────────────────────────────────


@register_check("virtual_camera", category="vcam", timeout_s=45.0,
                description="the virtual camera streams the program to a consumer")
def virtual_camera(ctx):
    import sys

    if sys.platform == "win32":
        return ctx.result(
            CheckStatus.MANUAL,
            "start Solin with the libobs engine, enable the virtual camera, and confirm "
            "'Solin Virtual Camera' shows the program in a consumer app (Camera/Meet/OBS)",
            details={"platform": "win32"})

    devices = _loopback_devices()
    if not devices:
        return ctx.skip("no v4l2loopback sink device (modprobe v4l2loopback)")
    device = devices[0]

    from solin.core.scenes.libobs_scene_builder import LibobsSceneGraph
    from solin.core.scenes.libobs_virtual_camera import LibobsVirtualCamera

    graph = LibobsSceneGraph(ctx.runtime)
    graph.hydrate(ctx.color_document(("s1", "#FF0000")), {"virtual_camera": "s1"})
    vcam = LibobsVirtualCamera(ctx.runtime)
    if not vcam.start():
        return ctx.result(CheckStatus.FAIL, "virtual camera failed to start", details={"device": device})
    time.sleep(1.5)  # push frames onto the loopback before we read
    color = _read_loopback_color(device)
    ctx.dwell()
    vcam.stop()
    graph.shutdown()
    if color is None:
        return ctx.result(CheckStatus.FAIL, f"could not read frames from {device}",
                          details={"device": device})
    status = CheckStatus.PASS if color == "red" else CheckStatus.FAIL
    return ctx.result(status, f"{device} delivered a {color} frame (expected red)",
                      details={"device": device, "color": color})


def _loopback_devices() -> list[str]:
    from solin.tools.gui_validation.harness import loopback_devices

    return loopback_devices()


def _read_loopback_color(device: str) -> str | None:
    """Grab one frame off the loopback with ffmpeg and classify its centre."""
    import shutil

    if not shutil.which("ffmpeg"):
        return None
    out = f"/tmp/solin-vcam-grab-{os.getpid()}.png"
    rc = subprocess.run(
        ["ffmpeg", "-y", "-f", "v4l2", "-i", device, "-frames:v", "1", out],
        capture_output=True).returncode
    if rc != 0 or not os.path.exists(out):
        return None
    try:
        from PySide6.QtGui import QImage

        img = QImage(out)
        if img.isNull():
            return None
        c = img.pixelColor(img.width() // 2, img.height() // 2)
        b, g, r = c.blue(), c.green(), c.red()
        from solin.tools.gui_validation.context import HarnessContext

        return HarnessContext.classify((b, g, r, 255))
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# ── cross-process engine (real sidecar subprocess) ─────────────────────────────


@register_check("subprocess_engine", category="engine", timeout_s=45.0,
                description="the real sidecar subprocess handshakes and stays up over IPC")
def subprocess_engine(ctx):
    from solin.core.scenes.engine import SceneEngineStatus
    from solin.core.scenes.libobs_engine import create_libobs_scene_engine

    engine = create_libobs_scene_engine(ctx.config.resolved_out_dir())
    try:
        try:
            future = engine.start(session_id="gui-val-engine", deadline_ms=15000)
            caps = future.result(timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - startup failure is a reportable FAIL
            return ctx.result(CheckStatus.FAIL,
                              f"sidecar did not become ready: {type(exc).__name__}: {exc}")
        # Hold across several heartbeat intervals; a heartbeat protocol error would
        # trip the supervisor into a restart loop (the class of bug this catches).
        time.sleep(3.0)
        health = engine.health
        metrics = engine.metrics
        ready = health.status == SceneEngineStatus.READY
        ok = ready and metrics.restart_count == 0 and metrics.protocol_error_count == 0
        return ctx.result(
            CheckStatus.PASS if ok else CheckStatus.FAIL,
            f"status={health.status.value}, restarts={metrics.restart_count}, "
            f"protocol_errors={metrics.protocol_error_count}",
            details={"status": health.status.value, "restarts": metrics.restart_count,
                     "protocol_errors": metrics.protocol_error_count, "capabilities": bool(caps)})
    finally:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001 - teardown best-effort
            log.debug("engine stop during check teardown errored", exc_info=True)
