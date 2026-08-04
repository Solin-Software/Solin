"""Virtual camera output — exposes a libobs mix as a system camera device.

Lets videoconferencing apps (Zoom, Meet, …) pick up what Solin shows. The vcam
composites on its OWN ``obs_view`` (a second, independent mix — NOT the projector's
channel 0), so it can differ from the projector: while nothing is projected it
shows a branded, centred JW logo instead of black, and later (the rule engine) it
will show camera / media / camera+media per the projected content.

Video only: the sink is ``v4l2loopback`` on Linux and a DirectShow filter on
Windows — both video devices — so audio to the conference is a separate concern.
Either sink needs a one-time privileged setup (root / administrator); Solin
detects that and guides the operator rather than failing silently.

Process-wide singleton.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from ...projection.brand import render_idle_logo
from .obs_runtime import obs_runtime
from .vcam_model import CameraLayout, PipCorner, VcamComposition
from .vcam_provision import DESIRED_LABEL, find_loopback_device, is_loaded

log = logging.getLogger(__name__)

_VCAM_OUTPUT_KIND = "virtualcam_output"

#: Windows drives its own DirectShow filter over a shared frame transport, so the
#: libobs output exists only to make the vcam's view render at all — a raw
#: callback alone gets nothing (measured: 0 fps without an active output, 30 with).
#:
#: It must NOT be virtualcam_output: OBS's win-dshow plugin only registers that
#: output when OBS's *own* DirectShow filter is present in the registry, so on a
#: machine without OBS Studio installed it does not exist and the camera cannot
#: start. ffmpeg_output lives in obs-ffmpeg.dll with no such dependency.
_VCAM_PUMP_KIND = "ffmpeg_output"

#: rawvideo into the null muxer: the pump has to consume the mix, not encode it.
_VCAM_PUMP_SETTINGS = {
    "url": "NUL",
    "format_name": "null",
    "video_encoder": "rawvideo",
    "audio_encoder": "pcm_s16le",
}
_IDLE_KEY = "vcam-idle"

# ── view channel layout (z-order back→front) ──────────────────────────────────
_CH_IDLE = 0  # branded idle logo — the floor, always present
_CH_MIRROR = 1  # the projector's program transition (mirrored), when visible
_CH_CAMERA = 2  # the camera layer (PiP or full), when shown

#: Picture-in-picture camera size + margin, as a fraction of the canvas.
_PIP_FRACTION = 0.25
_PIP_MARGIN = 0.035


class VirtualCamera:
    """Owns the virtual-camera output and its independent ``obs_view`` mix."""

    def __init__(self, runtime=None, program=None) -> None:
        self._runtime = runtime  # injectable for tests; else the singleton
        self._program = program  # injectable; else projection_program() (lazy)
        self._view: Any = None  # obs_view wrapper (libobs handle)
        self._output: Any = None  # virtualcam_output wrapper
        self._idle_scene: Any = None
        self._idle_source: Any = None
        self._image_dir: Path | None = None
        self._active = False
        # Windows sink: frames go to Solin's own DirectShow filter through a
        # shared mapping rather than through a libobs output.
        self._transport: Any = None
        self._bridge: Any = None
        # Camera layer (channel 2): one shared capture source + its wrapping scene.
        self._camera_source: Any = None
        self._camera_scene: Any = None
        self._camera_item: Any = None
        self._camera_device: str | None = None
        # Picture-in-picture placement (operator-configurable via the Scenes panel).
        self._pip_corner = PipCorner.BOTTOM_RIGHT
        self._pip_fraction = _PIP_FRACTION
        # The composition currently applied, so redundant re-applies are cheap.
        self._composition: VcamComposition | None = None
        #: Why the last start() failed, in operator-readable terms. The camera
        #: failing is otherwise silent — no device, no Scenes button, no clue.
        self._last_error: str = ""

    # ── availability ──────────────────────────────────────────────────────

    @staticmethod
    def platform_prerequisite_ok() -> bool:
        """Cheap, no-runtime readiness check.

        On Linux the ``v4l2loopback`` module must be loaded (it creates the
        /dev/videoN device the conferencing app reads). On Windows the DirectShow
        filter must be COM-registered, otherwise the output starts but no meeting
        app can see it. Both questions are ``is_loaded()``. macOS ships its own
        vcam sink, so assume ready there (refined when that is built out).
        """
        if sys.platform.startswith("linux") or sys.platform == "win32":
            return is_loaded()
        return True

    @staticmethod
    def prerequisite_hint() -> str:
        if sys.platform.startswith("linux"):
            return (
                "The virtual-camera kernel module is not loaded. Run once as root:\n"
                '  sudo modprobe v4l2loopback exclusive_caps=1 '
                'card_label="Solin Virtual Camera"\n'
                "(add it to /etc/modules-load.d/ to persist across reboots)."
            )
        if sys.platform == "win32":
            from .vcam_provision_win import prerequisite_hint as win_hint

            return win_hint()
        return ""

    def is_available(self) -> bool:
        """True when the vcam can actually start (prereq + output registered)."""
        if not self.platform_prerequisite_ok():
            return False
        try:
            rt = self._rt()
            rt.ensure_started()
            return self._output_spec()[0] in rt.ob.enum_output_types()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("vcam availability check failed", exc_info=True)
            return False

    @property
    def active(self) -> bool:
        return self._active

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the virtual camera (idempotent). Returns False (with a logged
        hint) if unavailable, so callers can surface guidance to the operator."""
        if self._active:
            return True
        self._last_error = ""
        if not self.platform_prerequisite_ok():
            log.warning("Virtual camera unavailable. %s", self.prerequisite_hint())
            self._last_error = self.prerequisite_hint()
            return False
        try:
            rt = self._rt()
            rt.ensure_started()
            ob = rt.ob
            kind, settings = self._output_spec()
            if kind not in ob.enum_output_types():
                log.warning(
                    "%s is not registered in this libobs build. Available: %s",
                    kind,
                    ", ".join(sorted(ob.enum_output_types())) or "(none)",
                )
                self._last_error = (
                    f"Solin's media engine has no '{kind}' output, so the virtual "
                    "camera cannot be driven."
                )
                return False
            self._ensure_view()
            # Bind to THIS view's mix (independent of channel 0), so the camera
            # can show something different from what is being projected.
            video = self._view.add()
            if video is None:
                log.warning("obs_view_add returned no video mix for the vcam")
                return False

            if self._output is None:
                self._output = ob.Output.create(kind, "Solin Virtual Camera", settings)
            self._output.set_media(video, self._audio())
            if not self._output.start():
                log.warning("%s failed to start. %s", kind, self.prerequisite_hint())
                self._last_error = self.prerequisite_hint() or (
                    f"Solin's media engine could not start its '{kind}' output."
                )
                return False

            # On Windows the device other applications see is Solin's own
            # DirectShow filter, fed over a shared transport — the output above
            # is what makes the view's mix render at all (a raw callback alone
            # does not: measured 0 frames without an active output).
            #
            # Deliberately non-fatal: the camera is running either way, and a
            # filter with no frames shows its own placeholder rather than
            # breaking the meeting app. Losing the picture is better than losing
            # the device.
            if sys.platform == "win32":
                self._start_transport(video)
            self._active = True
            self._log_started()
            return True
        except Exception as exc:  # noqa: BLE001 - libobs boundary
            log.warning("Could not start the virtual camera", exc_info=True)
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _output_spec() -> tuple[str, dict]:
        """The libobs output to bind the vcam's view to, and its settings.

        Linux drives the camera *through* this output (v4l2loopback via
        virtualcam_output). Windows does not: Solin's own DirectShow filter is
        the device, fed over a shared transport, so the output there is only a
        render pump — see :data:`_VCAM_PUMP_KIND` for why it must not be
        virtualcam_output on Windows.
        """
        if sys.platform == "win32":
            return _VCAM_PUMP_KIND, dict(_VCAM_PUMP_SETTINGS)
        return _VCAM_OUTPUT_KIND, {}

    @property
    def last_error(self) -> str:
        """Why the last :meth:`start` failed ("" if it succeeded).

        Surfaced to the operator: a camera that silently fails to appear looks
        identical to one that was never asked to start.
        """
        return self._last_error

    def _start_transport(self, video) -> bool:
        """Windows: pump this view's mix into the shared frame transport.

        The device other applications see is Solin's DirectShow filter, which
        lives in *their* process and reads frames from that transport — so unlike
        Linux there is no libobs output to start here. The filter stays
        registered whether or not Solin runs, showing its own placeholder until
        frames appear, which is why nothing needs tearing down on stop beyond
        letting the frames cease.
        """
        try:
            from .vcam_transport import FrameTransport, RawVideoBridge, write_standby_frame

            # Leave the branded standby picture on disk for the filter to fall
            # back to. It is what the camera shows while Solin is CLOSED, so it
            # has to be written before we could ever stop producing — refreshing
            # it here also picks up branding changes.
            write_standby_frame()
            if self._transport is None:
                self._transport = FrameTransport()
            if not self._transport.open():
                log.warning("Virtual camera: could not open the frame transport")
                return False
            if self._bridge is None:
                self._bridge = RawVideoBridge(self._transport)
            if not self._bridge.connect(video):
                log.warning("Virtual camera: could not attach to the libobs mix")
                self._transport.close()
                return False
        except Exception:  # noqa: BLE001 - libobs/cffi boundary
            log.warning("Virtual camera: frame transport unavailable", exc_info=True)
            return False
        return True

    def stop(self) -> None:
        """Stop the virtual camera (idempotent). Safe to call on shutdown."""
        if self._bridge is not None:
            self._bridge.disconnect()
        if self._transport is not None:
            self._transport.close()
        if self._output is not None:
            try:
                self._output.stop()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam output stop errored", exc_info=True)
        self._active = False

    # ── composition (follow-the-projector) ────────────────────────────────

    def set_meeting_camera(self, device_path: str, device_name: str = "") -> bool:
        """Choose the camera the vcam composites (interpreter / room camera).

        Uses the runtime's ONE shared per-device source, so the vcam and the
        projector can show the same camera without opening the device twice. An
        empty path clears the camera (leaving idle logo / mirror only). Returns
        False if the source can't be created — the vcam keeps working without a
        camera. Re-applies the active camera layout so the change takes effect now.
        """
        if device_path == self._camera_device:
            return self._camera_source is not None
        self._release_camera()
        if not device_path:
            self._reapply_camera()
            log.info("Virtual camera: camera cleared (idle logo / projector mirror)")
            return True
        source = self._rt().camera_source(device_path, device_name)
        if source is None:
            return False
        self._camera_source = source
        self._camera_device = device_path
        self._build_camera_scene()
        self._reapply_camera()
        log.info("Virtual camera: compositing camera device %s", device_path)
        return True

    def apply_composition(self, comp: VcamComposition) -> None:
        """Set what the vcam shows: mirror the projector? + camera placement."""
        self._ensure_view()
        self._set_mirror(comp.program_visible)
        self._set_camera(comp.camera)
        self._composition = comp

    def show_idle(self) -> None:
        """Show only the branded idle logo (no projector mirror, no camera)."""
        self.apply_composition(
            VcamComposition(program_visible=False, camera=CameraLayout.OFF)
        )

    def set_pip_placement(
        self, corner: PipCorner, fraction: float | None = None
    ) -> None:
        """Set which corner (and optional size fraction) the camera PiP occupies.

        Re-applies the current composition so a live PiP moves at once.
        """
        self._pip_corner = corner
        if fraction is not None:
            self._pip_fraction = max(0.1, min(0.5, float(fraction)))
        if self._composition is not None and self._composition.camera is CameraLayout.PIP:
            self._set_camera(CameraLayout.PIP)

    # ── teardown ──────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Full teardown — stop the output and release the view/scene/sources."""
        self.stop()
        self._release_camera()
        if self._output is not None:
            try:
                self._output.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam output release errored", exc_info=True)
            self._output = None
        if self._view is not None:
            try:
                for channel in (_CH_IDLE, _CH_MIRROR, _CH_CAMERA):
                    self._view.set_source(channel, None)
                self._view.remove()
                self._view.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam view release errored", exc_info=True)
            self._view = None
        self._composition = None
        for obj in (self._idle_scene, self._idle_source):
            try:
                if obj is not None:
                    obj.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam idle release errored", exc_info=True)
        self._idle_scene = self._idle_source = None
        if self._image_dir is not None:
            import shutil

            shutil.rmtree(self._image_dir, ignore_errors=True)
            self._image_dir = None

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _log_started() -> None:
        """Tell the operator the real device name to pick."""
        device = find_loopback_device()
        if device is None:
            log.info("Virtual camera started (branded idle).")
            return
        path, name = device
        log.info(
            "Virtual camera started — select '%s' (%s) in meeting apps (NOT your "
            "real webcam). It only appears while Solin is running.",
            name or DESIRED_LABEL, path,
        )

    def _rt(self):
        return self._runtime if self._runtime is not None else obs_runtime()

    def _audio(self):
        try:
            return self._rt().context.get_audio()
        except Exception:  # noqa: BLE001 - libobs boundary
            return None

    def _ensure_view(self) -> None:
        """Create the vcam's independent view with the branded idle logo as the
        floor (channel 0). The mirror (channel 1) and camera (channel 2) layers
        are set on top by :meth:`apply_composition`."""
        if self._view is not None:
            return
        rt = self._rt()
        rt.ensure_started()
        ob = rt.ob
        canvas = rt.video
        self._view = ob.View.create()
        self._idle_scene, self._idle_source = self._build_idle_scene(ob, canvas)
        self._view.set_source(_CH_IDLE, self._idle_scene.as_source())

    # ── composition internals ──────────────────────────────────────────────

    def _set_mirror(self, visible: bool) -> None:
        """Channel 1: the projector's program transition, or clear it (logo shows)."""
        if self._view is None:
            return
        transition = self._program_transition() if visible else None
        # None-safe: clears the channel when the program isn't ready / not visible.
        self._view.set_source(_CH_MIRROR, transition)

    def _program_transition(self):
        prog = self._program_ref()
        if prog is None:
            return None
        try:
            prog.ensure()
            return prog.transition_source()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not obtain program transition to mirror", exc_info=True)
            return None

    def _program_ref(self):
        if self._program is None:
            from .obs_program import projection_program

            self._program = projection_program()
        return self._program

    def _set_camera(self, layout: CameraLayout) -> None:
        """Channel 2: place the camera (PiP / full), or clear it."""
        if self._view is None:
            return
        if layout is CameraLayout.OFF or self._camera_item is None:
            self._view.set_source(_CH_CAMERA, None)
            return
        self._layout_camera_item(layout)
        self._view.set_source(_CH_CAMERA, self._camera_scene.as_source())

    def _reapply_camera(self) -> None:
        if self._composition is not None:
            self._set_camera(self._composition.camera)

    def _build_camera_scene(self) -> None:
        ob = self._rt().ob
        self._camera_scene = ob.Scene.create("vcam-camera-scene")
        self._camera_item = self._camera_scene.add(self._camera_source)

    def _layout_camera_item(self, layout: CameraLayout) -> None:
        ob = self._rt().ob
        canvas = self._rt().video
        cw, ch = float(canvas.width), float(canvas.height)
        item = self._camera_item
        item.bounds_type = int(ob.BoundsType.SCALE_INNER)
        item.bounds_alignment = int(ob.Alignment.CENTER)
        item.alignment = int(ob.Alignment.CENTER)
        if layout is CameraLayout.FULL:
            item.bounds = (cw, ch)
            item.pos = (cw / 2.0, ch / 2.0)
        else:  # PIP — a fractional box tucked into the configured corner
            box_w, box_h = cw * self._pip_fraction, ch * self._pip_fraction
            mx, my = cw * _PIP_MARGIN, ch * _PIP_MARGIN
            left, right = mx + box_w / 2.0, cw - mx - box_w / 2.0
            top, bottom = my + box_h / 2.0, ch - my - box_h / 2.0
            x, y = {
                PipCorner.BOTTOM_RIGHT: (right, bottom),
                PipCorner.BOTTOM_LEFT: (left, bottom),
                PipCorner.TOP_RIGHT: (right, top),
                PipCorner.TOP_LEFT: (left, top),
            }[self._pip_corner]
            item.bounds = (box_w, box_h)
            item.pos = (x, y)

    def _release_camera(self) -> None:
        if self._view is not None:
            try:
                self._view.set_source(_CH_CAMERA, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam camera channel clear errored", exc_info=True)
        # Release only the wrapping scene — the camera SOURCE is the runtime's
        # shared per-device source (freed on runtime shutdown), never ours to free.
        if self._camera_scene is not None:
            try:
                self._camera_scene.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("vcam camera scene release errored", exc_info=True)
        self._camera_scene = self._camera_item = self._camera_source = None
        self._camera_device = None

    def _build_idle_scene(self, ob, canvas):
        """A canvas-filling scene showing the centred JW idle logo."""
        path = self._save_idle_image(canvas.width, canvas.height)
        source = ob.Source.create(
            "image_source", f"{_IDLE_KEY}-src", {"file": str(path)}
        )
        scene = ob.Scene.create(f"{_IDLE_KEY}-scene")
        item = scene.add(source)
        item.bounds_type = int(ob.BoundsType.SCALE_INNER)
        item.bounds = (float(canvas.width), float(canvas.height))
        item.bounds_alignment = int(ob.Alignment.CENTER)
        return scene, source

    def _save_idle_image(self, width: int, height: int) -> Path:
        if self._image_dir is None:
            self._image_dir = Path(tempfile.mkdtemp(prefix="solin-vcam-"))
        path = self._image_dir / "idle.png"
        render_idle_logo(width, height).save(str(path))
        return path


_vcam: VirtualCamera | None = None


def virtual_camera() -> VirtualCamera:
    """Return the process-wide :class:`VirtualCamera` singleton."""
    global _vcam
    if _vcam is None:
        _vcam = VirtualCamera()
    return _vcam


__all__ = ["VirtualCamera", "virtual_camera"]
