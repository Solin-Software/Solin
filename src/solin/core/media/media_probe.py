"""Single-frame video probing on libobs — thumbnails and duration, no Qt.

The media list needs two things from a video it is *not* playing: how long it is,
and one decoded frame to show as a thumbnail. Qt's ``QMediaPlayer`` used to supply
both, but Solin's media pipeline is libobs now, and running a second, entirely
different decoder just for thumbnails means a file can render in the list and fail
to project (or vice versa). Probing through the same ``ffmpeg_source`` the
projector uses keeps the list honest about what will actually play.

libobs has no per-source frame readback, so a frame is obtained the same way the
virtual camera gets its picture: composite the source on a private ``obs_view``,
tap that view's mix with ``video_output_connect``, and keep an output attached so
the view actually renders (a raw callback alone yields zero frames — see
``obs_virtual_camera``). The view is private, so probing never disturbs what is on
the projector.

That harness is expensive, so there is exactly one, shared and serialized: probes
queue up and run one at a time, and the whole thing is torn down once the queue
drains. Thumbnail extraction is background work — serializing it costs nothing the
operator can see.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)


#: Thumbnails are small; let libobs scale the mix down for us rather than copying
#: full canvas frames per probe. 16:9 at a size the list can upscale from.
PROBE_WIDTH = 480
PROBE_HEIGHT = 270

#: libobs ``enum video_format`` / ``video_range_type`` / ``video_colorspace``.
_VIDEO_FORMAT_BGRA = 7
_VIDEO_RANGE_FULL = 1
_VIDEO_CS_709 = 2

#: The view renders only while an output consumes its mix. Encoding is pure waste
#: here, so raw video goes into the null muxer — the same pump the Windows virtual
#: camera uses, for the same reason.
_PUMP_KIND = "ffmpeg_output"

#: Channel on the private view that carries the source being probed.
_CH_PROBE = 0

#: How long a single probe may take before it is abandoned.
_PROBE_TIMEOUT_MS = 10_000
#: Poll cadence for duration/seek state and for draining decoded frames.
_POLL_MS = 100
#: After seeking, ignore frames briefly so the grab lands on the seek target
#: rather than on whatever frame was already decoded at position 0.
_SEEK_SETTLE_MS = 500


def _pump_settings() -> dict:
    return {
        "url": os.devnull if sys.platform != "win32" else "NUL",
        "format_name": "null",
        "video_encoder": "rawvideo",
        "audio_encoder": "pcm_s16le",
    }


def _seek_target(duration_ms: int) -> int:
    """Where to grab the thumbnail from, in milliseconds.

    5% in, because frame zero is so often a black or blank leader — but never
    past the midpoint of a short clip. Seeking a two-second clip to a flat 2s
    floor lands on (or past) the final frame and yields a black thumbnail.
    """
    target = min(duration_ms * 5 // 100, 10_000)
    return max(target, min(2_000, duration_ms // 2))


@dataclass
class _Job:
    """One queued probe request."""

    token: int
    url: str
    want_frame: bool = True
    want_duration: bool = True
    #: Set once the source reports a duration, so it is emitted at most once.
    duration_sent: bool = False
    seeked: bool = False
    settle_ticks: int = 0
    elapsed_ms: int = 0
    source: object | None = None
    #: Scene wrapping :attr:`source` so it can carry letterbox bounds.
    scene: object | None = None
    #: True once the source's active/showing refcounts were incremented.
    activated: bool = False
    cancelled: bool = False


@dataclass
class _FrameBuffer:
    """Latest frame handed over by the libobs graphics thread."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    data: bytes | None = None
    width: int = 0
    height: int = 0
    stride: int = 0

    def put(self, data: bytes, width: int, height: int, stride: int) -> None:
        with self.lock:
            self.data = data
            self.width = width
            self.height = height
            self.stride = stride

    def take(self) -> tuple[bytes, int, int, int] | None:
        with self.lock:
            if self.data is None:
                return None
            out = (self.data, self.width, self.height, self.stride)
            self.data = None
            return out

    def clear(self) -> None:
        with self.lock:
            self.data = None


class LibobsVideoProbe(QObject):
    """Serialized ``ffmpeg_source`` probe: one decoded frame + duration per URL.

    Results are reported by ``token`` so a caller can correlate them with its own
    request and ignore anything it has since cancelled.
    """

    #: token, raw BGRA tuple ``(bytes, width, height, stride)``
    frame_ready = Signal(int, object)
    #: token, duration in milliseconds
    duration_ready = Signal(int, int)
    #: token, human-readable reason
    failed = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: list[_Job] = []
        self._current: _Job | None = None
        self._frames = _FrameBuffer()

        self._view = None
        self._video = None
        self._output = None
        self._callback = None  # cffi trampoline; libobs holds a raw pointer
        self._conversion = None  # cffi struct; must outlive the connect call
        self._ffi = None
        self._lib = None

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_MS)
        self._poll.timeout.connect(self._on_poll)

    # ── Public API ────────────────────────────────────────────────────────

    def submit(
        self,
        token: int,
        url: str,
        *,
        want_frame: bool = True,
        want_duration: bool = True,
    ) -> None:
        """Queue a probe. Runs as soon as any earlier probe finishes."""
        if not url:
            self.failed.emit(token, "No media URL to probe")
            return
        self._queue.append(
            _Job(token=token, url=url, want_frame=want_frame, want_duration=want_duration)
        )
        self._advance()

    def cancel(self, token: int) -> None:
        """Drop a queued probe, or abandon it if it is the one running."""
        self._queue = [j for j in self._queue if j.token != token]
        if self._current is not None and self._current.token == token:
            self._current.cancelled = True
            self._finish_current()

    def shutdown(self) -> None:
        """Abandon everything and release the libobs harness."""
        self._queue.clear()
        if self._current is not None:
            self._current.cancelled = True
            self._release_source(self._current)
            self._current = None
        self._poll.stop()
        self._teardown_harness()

    # ── Queue pump ────────────────────────────────────────────────────────

    def _advance(self) -> None:
        if self._current is not None or not self._queue:
            return
        job = self._queue.pop(0)
        if not self._ensure_harness():
            self.failed.emit(job.token, "Solin's media engine is unavailable")
            # The harness is unavailable for every queued probe, not just this
            # one; fail them all rather than retrying the same setup per item.
            pending, self._queue = self._queue, []
            for other in pending:
                self.failed.emit(other.token, "Solin's media engine is unavailable")
            return
        if not self._open_source(job):
            self.failed.emit(job.token, "Could not open the media for probing")
            self._current = None
            QTimer.singleShot(0, self._advance)
            return
        self._current = job
        self._frames.clear()
        self._poll.start()

    def _open_source(self, job: _Job) -> bool:
        from .obs_runtime import obs_runtime

        runtime = obs_runtime()
        ob = runtime.ob
        is_remote = job.url.startswith(("http://", "https://"))
        settings: dict
        if is_remote:
            settings = {"is_local_file": False, "input": job.url}
        else:
            settings = {"is_local_file": True, "local_file": job.url}
        # Deliberately NO hw_decode. Hardware decoding is a per-machine gamble --
        # measured here, DXVA2 surface creation fails ("Could not create the
        # surfaces") and ffmpeg_source goes straight to ENDED with 0x0
        # dimensions, i.e. a silently black thumbnail. Software decode of one
        # frame in the background is cheap and always works.
        try:
            source = ob.Source.create_private(
                "ffmpeg_source", f"solin-probe-{job.token}", settings
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Probe source creation failed for %r", job.url, exc_info=True)
            return False
        if source is None:
            return False
        try:
            source.muted = True
            source.audio_mixers = 0
        except Exception:  # noqa: BLE001 - not fatal; probe is still usable
            log.debug("Could not mute the probe source", exc_info=True)
        job.source = source
        # A source put straight on a channel renders at its NATIVE size, anchored
        # top-left — a 1280x720 clip on a 1920x1080 canvas would give a thumbnail
        # sitting in the corner of a black frame. Wrapping it in a scene lets the
        # item carry letterbox bounds, so any clip fills the frame the way the
        # projector and the virtual camera already composite media.
        try:
            scene = ob.Scene.create(f"solin-probe-scene-{job.token}")
            item = scene.add(source)
            canvas = runtime.video
            item.bounds_type = int(ob.BoundsType.SCALE_INNER)
            item.bounds = (float(canvas.width), float(canvas.height))
            item.bounds_alignment = int(ob.Alignment.CENTER)
            job.scene = scene
            self._view.set_source(_CH_PROBE, scene.as_source())
            # A private obs_view does NOT activate a media source: ffmpeg_source
            # only starts decoding once libobs considers it active/showing, and
            # a view channel alone does not count. Without this the source sits
            # at media_state NONE with 0x0 dimensions and the mix stays black.
            # (A colour source renders regardless, which is what made this look
            # like a working harness.) Global channels would activate it, but
            # they also feed the programme -- which would project the probe.
            from pylibobs._ffi import get_lib
            lib = get_lib()
            lib.obs_source_inc_active(source._ptr)
            lib.obs_source_inc_showing(source._ptr)
            job.activated = True
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not bind the probe source to the view", exc_info=True)
            self._release_source(job)
            return False
        return True

    def _on_poll(self) -> None:
        job = self._current
        if job is None:
            self._poll.stop()
            return

        job.elapsed_ms += _POLL_MS
        source = job.source
        if source is None:
            self._finish_current()
            return

        try:
            duration = int(source.media_duration)
        except Exception:  # noqa: BLE001 - libobs boundary
            duration = 0

        if duration > 0 and not job.duration_sent:
            job.duration_sent = True
            if job.want_duration:
                self.duration_ready.emit(job.token, duration)
            if not job.want_frame:
                self._finish_current()
                return
            # Seek a little way in: frame zero is very often a black or blank
            # leader, which makes a useless thumbnail.
            if not job.seeked:
                job.seeked = True
                target = _seek_target(duration)
                try:
                    source.media_time = target
                except Exception:  # noqa: BLE001 - seek is best-effort
                    log.debug("Probe seek failed; taking any frame", exc_info=True)

        if job.want_frame:
            # Let the decoder land on the seek target before accepting a frame.
            if job.seeked and job.settle_ticks * _POLL_MS < _SEEK_SETTLE_MS:
                job.settle_ticks += 1
                self._frames.clear()
            else:
                latest = self._frames.take()
                if latest is not None:
                    data, width, height, stride = latest
                    self.frame_ready.emit(job.token, (data, width, height, stride))
                    self._finish_current()
                    return

        if job.elapsed_ms >= _PROBE_TIMEOUT_MS:
            # A duration with no frame is still useful; only call it a failure
            # when nothing at all came back.
            if not job.duration_sent:
                self.failed.emit(job.token, "Timed out probing the media")
            elif job.want_frame:
                self.failed.emit(job.token, "The video yielded no decodable frame")
            self._finish_current()

    def _finish_current(self) -> None:
        job = self._current
        self._current = None
        self._poll.stop()
        if job is not None:
            self._release_source(job)
        self._frames.clear()
        if self._queue:
            QTimer.singleShot(0, self._advance)
        else:
            # Nothing else waiting: give the GPU and the null muxer back.
            self._teardown_harness()

    def _release_source(self, job: _Job) -> None:
        if self._view is not None:
            try:
                self._view.set_source(_CH_PROBE, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not unbind the probe source", exc_info=True)
        if job.activated and job.source is not None:
            job.activated = False
            try:
                from pylibobs._ffi import get_lib

                lib = get_lib()
                lib.obs_source_dec_showing(job.source._ptr)
                lib.obs_source_dec_active(job.source._ptr)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not drop the probe source refcounts", exc_info=True)
        scene, job.scene = job.scene, None
        if scene is not None:
            try:
                scene.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release the probe scene", exc_info=True)
        source = job.source
        job.source = None
        if source is None:
            return
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Could not release the probe source", exc_info=True)

    # ── libobs harness ────────────────────────────────────────────────────

    def _ensure_harness(self) -> bool:
        """Create the private view, its pump output and the frame tap."""
        if self._view is not None and self._callback is not None:
            return True
        try:
            from .obs_runtime import obs_runtime

            runtime = obs_runtime()
            runtime.ensure_started()
            ob = runtime.ob
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Probe harness: libobs runtime unavailable", exc_info=True)
            return False

        try:
            if self._view is None:
                self._view = ob.View.create()
            if self._video is None:
                self._video = self._view.add()
            if self._video is None:
                log.debug("Probe harness: obs_view_add returned no mix")
                self._teardown_harness()
                return False
            if not self._connect_tap():
                self._teardown_harness()
                return False
            if self._output is None:
                if _PUMP_KIND not in set(ob.enum_output_types()):
                    log.debug("Probe harness: no %r output available", _PUMP_KIND)
                    self._teardown_harness()
                    return False
                self._output = ob.Output.create(
                    _PUMP_KIND, "solin-probe-pump", _pump_settings()
                )
                # The pump declares an audio encoder, so it must be given a real
                # audio_t: handed None it waits forever for audio that never
                # arrives and never pulls a single video frame.
                self._output.set_media(self._video, self._obs_audio(runtime))
                if not self._output.start():
                    log.debug("Probe harness: pump output refused to start")
                    self._teardown_harness()
                    return False
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Probe harness construction failed", exc_info=True)
            self._teardown_harness()
            return False
        return True

    @staticmethod
    def _obs_audio(runtime):
        try:
            return runtime.context.get_audio()
        except Exception:  # noqa: BLE001 - libobs boundary
            return None

    def _connect_tap(self) -> bool:
        """Attach a raw callback to the private view's mix, scaled to thumbnail size."""
        if self._callback is not None:
            return True
        try:
            from pylibobs._ffi import ffi, get_lib
        except Exception:  # noqa: BLE001 - optional dependency
            log.debug("Probe harness: pylibobs ffi unavailable", exc_info=True)
            return False
        lib = get_lib()
        frames = self._frames

        @ffi.callback("void(void*, struct video_data*)")
        def _on_frame(_param, frame):  # libobs graphics thread — keep it short
            try:
                stride = int(frame.linesize[0])
                if stride <= 0:
                    return
                buf = ffi.buffer(frame.data[0], stride * PROBE_HEIGHT)
                frames.put(bytes(buf), PROBE_WIDTH, PROBE_HEIGHT, stride)
            except Exception:  # noqa: BLE001 - never raise into libobs
                log.debug("Probe frame tap failed", exc_info=True)

        # Ask libobs to scale and convert; without this the frames arrive at
        # canvas size in the canvas format and a naive copy crops instead of
        # scaling (the bug that put the vcam logo in the corner).
        conversion = ffi.new("struct video_scale_info *")
        conversion.format = _VIDEO_FORMAT_BGRA
        conversion.width = PROBE_WIDTH
        conversion.height = PROBE_HEIGHT
        conversion.range = _VIDEO_RANGE_FULL
        conversion.colorspace = _VIDEO_CS_709
        self._conversion = conversion  # libobs keeps the pointer

        if not lib.video_output_connect(self._video, conversion, _on_frame, ffi.NULL):
            log.debug("Probe harness: video_output_connect refused the mix")
            self._conversion = None
            return False

        self._callback = _on_frame
        self._ffi = ffi
        self._lib = lib
        return True

    def _teardown_harness(self) -> None:
        if self._callback is not None and self._lib is not None:
            try:
                self._lib.video_output_disconnect(
                    self._video, self._callback, self._ffi.NULL
                )
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not disconnect the probe frame tap", exc_info=True)
        self._callback = None
        self._conversion = None
        self._lib = None
        self._ffi = None

        if self._output is not None:
            try:
                self._output.stop()
                self._output.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release the probe pump output", exc_info=True)
            self._output = None

        if self._view is not None:
            try:
                self._view.set_source(_CH_PROBE, None)
                self._view.remove()
                self._view.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not release the probe view", exc_info=True)
            self._view = None
        self._video = None
        self._frames.clear()


_probe: LibobsVideoProbe | None = None


def video_probe() -> LibobsVideoProbe:
    """The process-wide probe. One harness, shared by every extractor."""
    global _probe
    if _probe is None:
        _probe = LibobsVideoProbe()
    return _probe


__all__ = ["LibobsVideoProbe", "video_probe", "PROBE_WIDTH", "PROBE_HEIGHT"]
