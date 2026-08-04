"""Unit tests for the virtual-camera service (libobs fully faked)."""

from __future__ import annotations

import sys
import types

import solin.core.media.obs_virtual_camera as vcam_mod
from solin.core.media.obs_virtual_camera import (
    _CH_COMPOSITE,
    _CH_IDLE,
    VirtualCamera,
)
from solin.core.media.vcam_model import CameraLayout, PipCorner, VcamComposition


class _FakeView:
    def __init__(self) -> None:
        self.sources: dict = {}
        self.added = False
        self.removed = False
        self.released = False

    def set_source(self, channel, source) -> None:
        self.sources[channel] = source

    def add(self):
        self.added = True
        return object()  # a non-None video_t handle

    def remove(self) -> None:
        self.removed = True

    def release(self) -> None:
        self.released = True


class _FakeOutput:
    def __init__(self, *, start_ok: bool = True) -> None:
        self.media = None
        self.started = False
        self.stopped = 0
        self.released = False
        self._start_ok = start_ok

    def set_media(self, video, audio) -> None:
        self.media = (video, audio)

    def start(self) -> bool:
        self.started = self._start_ok
        return self._start_ok

    def stop(self) -> None:
        self.stopped += 1

    def release(self) -> None:
        self.released = True


class _FakeItem:
    def __init__(self) -> None:
        self.bounds_type = None
        self.bounds = None
        self.bounds_alignment = None
        self.alignment = None
        self.pos = None


class _FakeScene:
    def __init__(self, name) -> None:
        self.name = name
        self.released = 0
        self.item = None
        self.added: list = []      # every source composited into this scene
        self.items: list = []
        self._source = object()

    def add(self, source):
        self.added.append(source)
        self.item = _FakeItem()
        self.items.append(self.item)
        return self.item

    def as_source(self):
        return self._source

    def release(self) -> None:
        self.released += 1


class _FakeProgram:
    """Stands in for the projection program the vcam mirrors."""

    def __init__(self) -> None:
        self.ensured = 0
        self.transition = "TRANSITION"

    def ensure(self) -> None:
        self.ensured += 1

    def transition_source(self):
        return self.transition


class _FakeSource:
    def __init__(self, *_a) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


class _FakeTransition:
    """Records the crossfades the vcam asks for."""

    def __init__(self) -> None:
        self.size = None
        self.seeded = None
        self.starts: list = []
        self.cleared = 0
        self.released = 0
        self.start_ok = True

    def set_size(self, cx, cy) -> None:
        self.size = (cx, cy)

    def set_source(self, source) -> None:
        self.seeded = source

    def start(self, destination, duration_ms=500, mode=None) -> bool:
        self.starts.append((destination, duration_ms, mode))
        return self.start_ok

    def clear(self) -> None:
        self.cleared += 1

    def release(self) -> None:
        self.released += 1


class _FakeOb:
    class BoundsType:
        SCALE_INNER = 2

    class Alignment:
        CENTER = 0

    class TransitionMode:
        AUTO = 0

    # Both, because the output the vcam binds to is platform-dependent: Linux
    # drives the camera through virtualcam_output, Windows uses ffmpeg_output
    # purely as a render pump (see VirtualCamera._output_spec).
    def __init__(self, output, types=("virtualcam_output", "ffmpeg_output")) -> None:
        self._types = list(types)
        self.View = types_ns(create=lambda: _FakeView())
        self.Output = types_ns(create=lambda kind, name, settings: output)
        self.Scene = types_ns(create=lambda name: _FakeScene(name))
        self.Source = types_ns(create=lambda kind, name, settings: _FakeSource())
        self.transition = _FakeTransition()
        self.Transition = types_ns(create=lambda kind, name: self.transition)

    def enum_output_types(self):
        return list(self._types)


def types_ns(**kw):
    return types.SimpleNamespace(**kw)


class _FakeRuntime:
    def __init__(self, ob) -> None:
        self.ob = ob
        self.context = types_ns(get_audio=lambda: object())
        self.video = types_ns(width=1280, height=720)
        self.started = 0
        self._cameras: dict = {}

    def ensure_started(self, **_kw) -> None:
        self.started += 1

    def camera_source(self, device_path, device_name=""):
        # Mirror ObsRuntime: ONE shared source per device, owned by the runtime.
        src = self._cameras.get(device_path)
        if src is None:
            src = _FakeSource()
            self._cameras[device_path] = src
        return src


def _vcam(
    monkeypatch,
    *,
    output=None,
    # Advertise both: the output the vcam binds to is platform-dependent
    # (virtualcam_output on Linux, ffmpeg_output as a pump on Windows), and these
    # tests are about the view/output wiring, not that choice.
    types=("virtualcam_output", "ffmpeg_output"),
    prereq=True,
):
    # Decouple from the host platform + avoid any real image/file work.
    monkeypatch.setattr(VirtualCamera, "platform_prerequisite_ok", staticmethod(lambda: prereq))
    monkeypatch.setattr(vcam_mod, "render_idle_logo", lambda w, h: types_ns(save=lambda p: None))
    out = output if output is not None else _FakeOutput()
    rt = _FakeRuntime(_FakeOb(out, types=types))
    prog = _FakeProgram()
    cam = VirtualCamera(runtime=rt, program=prog)
    # Run deferred work (fade settle, scene disposal) immediately: these tests
    # assert the composite the vcam ends up with, not the wall-clock timing, and
    # a real QTimer would never fire in a unit test.
    cam._defer = lambda _ms, fn: fn()
    return cam, rt, out, prog


def _composite(cam):
    """The scene currently faded to (what the camera is showing)."""
    return cam._current_entry.scene


def test_prerequisite_ok_linux_requires_v4l2loopback(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "linux")
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: True)
    assert VirtualCamera.platform_prerequisite_ok() is True
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: False)
    assert VirtualCamera.platform_prerequisite_ok() is False


def test_prerequisite_hint_mentions_modprobe(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "linux")
    assert "modprobe v4l2loopback" in VirtualCamera.prerequisite_hint()


def test_prerequisite_ok_windows_requires_a_registered_filter(monkeypatch):
    # An unregistered DirectShow filter still lets the output start, but no
    # meeting app can see the camera — so it must gate exactly like v4l2loopback.
    monkeypatch.setattr(vcam_mod.sys, "platform", "win32")
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: False)
    assert VirtualCamera.platform_prerequisite_ok() is False
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: True)
    assert VirtualCamera.platform_prerequisite_ok() is True


def test_prerequisite_hint_windows_mentions_registration(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "win32")
    assert "registered" in VirtualCamera.prerequisite_hint()


def test_prerequisite_ok_macos_still_assumes_ready(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "darwin")
    assert VirtualCamera.platform_prerequisite_ok() is True
    assert VirtualCamera.prerequisite_hint() == ""


def test_start_builds_independent_view_with_idle_and_starts_output(monkeypatch):
    cam, rt, out, _prog = _vcam(monkeypatch)

    assert cam.start() is True
    assert cam.active is True
    # An independent view was created, its channel 0 shows the idle scene,
    # and the output is bound to the VIEW's mix (not the main channel 0).
    view = cam._view
    assert view is not None and 0 in view.sources
    assert view.added is True
    assert out.media is not None  # set_media(view_video, audio)
    assert out.started is True


def test_start_is_idempotent(monkeypatch):
    cam, rt, out, _prog = _vcam(monkeypatch)
    assert cam.start() is True
    assert cam.start() is True  # already active → no second output start attempt
    assert out.stopped == 0


def test_windows_does_not_drive_the_camera_through_virtualcam_output(monkeypatch):
    """On Windows the pump must not be virtualcam_output.

    OBS's win-dshow plugin only registers that output when OBS's *own* DirectShow
    filter is in the registry, so on a machine without OBS Studio installed it
    does not exist — and the camera silently fails to start. This was a real
    field failure: it worked on a developer box with OBS installed and nowhere
    else.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    kind, settings = vcam_mod.VirtualCamera._output_spec()

    assert kind != "virtualcam_output"
    assert kind == "ffmpeg_output"
    assert settings  # rawvideo into the null muxer: consume the mix, don't encode


def test_linux_still_drives_the_camera_through_virtualcam_output(monkeypatch):
    """Linux is different: there the output IS the sink (v4l2loopback)."""
    monkeypatch.setattr(sys, "platform", "linux")
    kind, settings = vcam_mod.VirtualCamera._output_spec()

    assert kind == "virtualcam_output"
    assert settings == {}


def test_start_survives_an_unusable_frame_transport(monkeypatch):
    """On Windows the camera must still come up if the transport cannot attach.

    The libobs output is already running at that point, and Solin's DirectShow
    filter shows its own placeholder when no frames arrive. Losing the picture is
    recoverable; losing the device mid-meeting is not.
    """
    cam, _rt, out, _prog = _vcam(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        cam, "_start_transport", lambda video: False, raising=False
    )

    assert cam.start() is True
    assert cam.active is True
    assert out.started is True


def test_start_returns_false_when_prerequisite_missing(monkeypatch):
    cam, rt, out, _prog = _vcam(monkeypatch, prereq=False)
    assert cam.start() is False
    assert cam.active is False
    assert out.started is False


def test_start_returns_false_when_output_type_absent(monkeypatch):
    cam, rt, out, _prog = _vcam(monkeypatch, types=())  # virtualcam_output not registered
    assert cam.start() is False
    assert cam.active is False


def test_start_returns_false_when_output_fails_to_start(monkeypatch):
    out = _FakeOutput(start_ok=False)
    cam, rt, _out, _prog = _vcam(monkeypatch, output=out)
    assert cam.start() is False
    assert cam.active is False


def test_shutdown_releases_output_and_view(monkeypatch):
    cam, rt, out, _prog = _vcam(monkeypatch)
    cam.start()
    view = cam._view

    cam.shutdown()

    assert cam.active is False
    assert out.stopped >= 1 and out.released is True
    assert view.removed is True and view.released is True
    assert cam._view is None and cam._output is None


# ── composition (follow-the-projector) ────────────────────────────────────────

_MIRROR_ONLY = VcamComposition(program_visible=True, camera=CameraLayout.OFF)
_MIRROR_PIP = VcamComposition(program_visible=True, camera=CameraLayout.PIP)
_CAMERA_FULL = VcamComposition(program_visible=False, camera=CameraLayout.FULL)
_IDLE = VcamComposition(program_visible=False, camera=CameraLayout.OFF)


def test_composition_changes_crossfade_rather_than_cut(monkeypatch):
    """The whole point: a composition change dissolves, it does not cut.

    Regression: layers used to be swapped straight onto view channels, so going
    from "camera full" to "media + camera PiP" changed in one frame. The only
    motion an operator saw was the PROJECTOR's own fade leaking through the
    newly revealed mirror — which is why it looked like a blink to the yeartext
    followed by a fade, instead of the camera fading to the picture.
    """
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    tr = rt.ob.transition

    cam.apply_composition(_CAMERA_FULL)
    cam.apply_composition(_MIRROR_PIP)

    # The transition drives the picture, and it is what sits on the view.
    assert cam._view.sources[_CH_COMPOSITE] is tr
    assert len(tr.starts) == 2
    assert all(duration > 0 for _dest, duration, _mode in tr.starts)
    assert _CH_IDLE in cam._view.sources  # branded floor still underneath


def test_apply_mirror_only_shows_projector_no_camera(monkeypatch):
    cam, rt, _out, prog = _vcam(monkeypatch)

    cam.apply_composition(_MIRROR_ONLY)

    scene = _composite(cam)
    assert prog.transition in scene.added  # projector mirrored
    assert len(scene.added) == 1  # and nothing else: no camera
    assert prog.ensured >= 1
    assert _CH_IDLE in cam._view.sources  # idle logo floor exists


def test_apply_camera_full_without_camera_leaves_logo(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)

    cam.apply_composition(_CAMERA_FULL)

    # Nothing composited above the floor, so the branded logo shows through.
    assert _composite(cam).added == []


def test_camera_full_fills_canvas(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    assert cam.set_meeting_camera("/dev/video0") is True

    cam.apply_composition(_CAMERA_FULL)

    scene = _composite(cam)
    assert scene.added == [cam._camera_source]
    item = scene.items[-1]
    assert item.bounds == (1280.0, 720.0)  # fills the 1280x720 canvas
    assert item.pos == (640.0, 360.0)  # centred


def test_camera_pip_sits_bottom_right(monkeypatch):
    cam, rt, _out, prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")

    cam.apply_composition(_MIRROR_PIP)

    scene = _composite(cam)
    # Mirror first, camera on top — z-order is insertion order.
    assert scene.added == [prog.transition, cam._camera_source]
    item = scene.items[-1]
    assert item.bounds == (320.0, 180.0)  # a quarter of the canvas
    assert item.pos[0] > 640.0 and item.pos[1] > 360.0


def test_each_composite_gets_its_own_camera_item(monkeypatch):
    """A shared source laid out per composite must not deform the outgoing one.

    Both composites reference the SAME camera source; if they shared a scene item
    the incoming PiP transform would shrink the full-screen camera that is still
    dissolving away.
    """
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")

    cam.apply_composition(_CAMERA_FULL)
    full_item = _composite(cam).items[-1]
    cam.apply_composition(_MIRROR_PIP)
    pip_item = _composite(cam).items[-1]

    assert full_item is not pip_item
    assert full_item.bounds == (1280.0, 720.0)  # untouched by the PiP layout
    assert pip_item.bounds == (320.0, 180.0)


def test_set_meeting_camera_switch_keeps_shared_source_and_clear_removes(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    assert cam.set_meeting_camera("/dev/video0") is True
    first_source = cam._camera_source
    cam.apply_composition(_CAMERA_FULL)
    first_scene = _composite(cam)

    # Switching device recomposes, but the per-device SOURCE is owned by the
    # runtime — the vcam must NOT release it (the projector may be using it).
    assert cam.set_meeting_camera("/dev/video1") is True
    assert first_scene.released >= 1  # the composite that held it was retired
    assert first_source.released == 0  # shared source left alone
    assert cam._camera_device == "/dev/video1"
    assert cam._camera_source is not first_source

    # Clearing holds no source, and the composite no longer references one.
    assert cam.set_meeting_camera("") is True
    assert cam._camera_source is None
    assert _composite(cam).added == []


def test_set_meeting_camera_same_device_is_noop(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    source = cam._camera_source
    assert cam.set_meeting_camera("/dev/video0") is True
    assert cam._camera_source is source  # not reopened


def test_show_idle_clears_mirror_and_camera(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.apply_composition(_MIRROR_PIP)

    cam.show_idle()

    # Idle composites to nothing: the branded floor is all that remains.
    assert _composite(cam).added == []


# ── PiP placement (configurable corner + size) ────────────────────────────────

# Canvas 1280x720, margin 0.035, fraction 0.25 → box 320x180; centres per corner.
_CORNER_CENTRES = {
    PipCorner.BOTTOM_RIGHT: (1075.2, 604.8),
    PipCorner.BOTTOM_LEFT: (204.8, 604.8),
    PipCorner.TOP_RIGHT: (1075.2, 115.2),
    PipCorner.TOP_LEFT: (204.8, 115.2),
}


def test_pip_placement_per_corner(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    for corner, (ex, ey) in _CORNER_CENTRES.items():
        cam.set_pip_placement(corner)
        cam.apply_composition(_MIRROR_PIP)
        px, py = _composite(cam).items[-1].pos
        assert (round(px, 1), round(py, 1)) == (ex, ey)


def test_set_pip_placement_moves_live_pip(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.apply_composition(_MIRROR_PIP)  # default bottom-right
    cam.set_pip_placement(PipCorner.TOP_LEFT)  # re-applies live
    px, py = _composite(cam).items[-1].pos
    assert (round(px, 1), round(py, 1)) == _CORNER_CENTRES[PipCorner.TOP_LEFT]


def test_pip_fraction_resizes_box(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.set_pip_placement(PipCorner.BOTTOM_RIGHT, fraction=0.4)
    cam.apply_composition(_MIRROR_PIP)
    bw, bh = _composite(cam).items[-1].bounds
    assert (round(bw, 1), round(bh, 1)) == (512.0, 288.0)
