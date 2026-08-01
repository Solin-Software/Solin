"""Unit tests for the virtual-camera service (libobs fully faked)."""

from __future__ import annotations

import types

import solin.core.media.obs_virtual_camera as vcam_mod
from solin.core.media.obs_virtual_camera import (
    _CH_CAMERA,
    _CH_IDLE,
    _CH_MIRROR,
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
        self._source = object()

    def add(self, _source):
        self.item = _FakeItem()
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


class _FakeOb:
    class BoundsType:
        SCALE_INNER = 2

    class Alignment:
        CENTER = 0

    def __init__(self, output, types=("virtualcam_output",)) -> None:
        self._types = list(types)
        self.View = types_ns(create=lambda: _FakeView())
        self.Output = types_ns(create=lambda kind, name, settings: output)
        self.Scene = types_ns(create=lambda name: _FakeScene(name))
        self.Source = types_ns(create=lambda kind, name, settings: _FakeSource())

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


def _vcam(monkeypatch, *, output=None, types=("virtualcam_output",), prereq=True):
    # Decouple from the host platform + avoid any real image/file work.
    monkeypatch.setattr(VirtualCamera, "platform_prerequisite_ok", staticmethod(lambda: prereq))
    monkeypatch.setattr(vcam_mod, "render_idle_logo", lambda w, h: types_ns(save=lambda p: None))
    out = output if output is not None else _FakeOutput()
    rt = _FakeRuntime(_FakeOb(out, types=types))
    prog = _FakeProgram()
    cam = VirtualCamera(runtime=rt, program=prog)
    return cam, rt, out, prog


def test_prerequisite_ok_linux_requires_v4l2loopback(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "linux")
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: True)
    assert VirtualCamera.platform_prerequisite_ok() is True
    monkeypatch.setattr(vcam_mod, "is_loaded", lambda: False)
    assert VirtualCamera.platform_prerequisite_ok() is False


def test_prerequisite_hint_mentions_modprobe(monkeypatch):
    monkeypatch.setattr(vcam_mod.sys, "platform", "linux")
    assert "modprobe v4l2loopback" in VirtualCamera.prerequisite_hint()


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


def test_apply_mirror_only_shows_projector_no_camera(monkeypatch):
    cam, rt, _out, prog = _vcam(monkeypatch)

    cam.apply_composition(_MIRROR_ONLY)

    view = cam._view
    assert view.sources[_CH_MIRROR] == prog.transition  # projector mirrored
    assert view.sources[_CH_CAMERA] is None  # no camera
    assert prog.ensured >= 1  # the program was ensured before mirroring
    assert _CH_IDLE in view.sources  # idle logo floor exists


def test_apply_camera_full_without_camera_leaves_logo(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)

    cam.apply_composition(_CAMERA_FULL)

    view = cam._view
    assert view.sources[_CH_MIRROR] is None  # not mirroring → logo shows through
    assert view.sources[_CH_CAMERA] is None  # no camera configured → logo only


def test_camera_full_fills_canvas(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    assert cam.set_meeting_camera("/dev/video0") is True

    cam.apply_composition(_CAMERA_FULL)

    view = cam._view
    assert view.sources[_CH_CAMERA] == cam._camera_scene.as_source()
    item = cam._camera_scene.item
    assert item.bounds == (1280.0, 720.0)  # fills the 1280x720 canvas
    assert item.pos == (640.0, 360.0)  # centred


def test_camera_pip_sits_bottom_right(monkeypatch):
    cam, rt, _out, prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")

    cam.apply_composition(_MIRROR_PIP)

    view = cam._view
    assert view.sources[_CH_MIRROR] == prog.transition  # mirror behind the PiP
    assert view.sources[_CH_CAMERA] == cam._camera_scene.as_source()
    item = cam._camera_scene.item
    assert item.bounds == (320.0, 180.0)  # a quarter of the canvas
    # bottom-right quadrant → both coordinates past the canvas centre
    assert item.pos[0] > 640.0 and item.pos[1] > 360.0


def test_set_meeting_camera_switch_keeps_shared_source_and_clear_removes(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    assert cam.set_meeting_camera("/dev/video0") is True
    first_source = cam._camera_source
    first_scene = cam._camera_scene
    cam.apply_composition(_CAMERA_FULL)

    # Switching device rebuilds the wrapping SCENE, but the per-device SOURCE is
    # owned by the runtime — the vcam must NOT release it (the projector may use it).
    assert cam.set_meeting_camera("/dev/video1") is True
    assert first_scene.released >= 1  # old wrapping scene released
    assert first_source.released == 0  # shared source left alone
    assert cam._camera_device == "/dev/video1"
    assert cam._camera_source is not first_source

    # Clearing releases the scene, blanks the channel, and holds no source.
    assert cam.set_meeting_camera("") is True
    assert cam._camera_source is None
    assert cam._view.sources[_CH_CAMERA] is None


def test_set_meeting_camera_same_device_is_noop(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    scene = cam._camera_scene
    assert cam.set_meeting_camera("/dev/video0") is True
    assert cam._camera_scene is scene  # not rebuilt


def test_show_idle_clears_mirror_and_camera(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.apply_composition(_MIRROR_PIP)

    cam.show_idle()

    view = cam._view
    assert view.sources[_CH_MIRROR] is None
    assert view.sources[_CH_CAMERA] is None


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
        px, py = cam._camera_scene.item.pos
        assert (round(px, 1), round(py, 1)) == (ex, ey)


def test_set_pip_placement_moves_live_pip(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.apply_composition(_MIRROR_PIP)  # default bottom-right
    cam.set_pip_placement(PipCorner.TOP_LEFT)  # re-applies live
    px, py = cam._camera_scene.item.pos
    assert (round(px, 1), round(py, 1)) == _CORNER_CENTRES[PipCorner.TOP_LEFT]


def test_pip_fraction_resizes_box(monkeypatch):
    cam, rt, _out, _prog = _vcam(monkeypatch)
    cam.set_meeting_camera("/dev/video0")
    cam.set_pip_placement(PipCorner.BOTTOM_RIGHT, fraction=0.4)
    cam.apply_composition(_MIRROR_PIP)
    bw, bh = cam._camera_scene.item.bounds
    assert (round(bw, 1), round(bh, 1)) == (512.0, 288.0)
