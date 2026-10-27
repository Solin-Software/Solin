"""Qt-free native media transport for files, progressive HTTP and live inputs."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)

MEDIA_SOURCE_ID = "solin_media_source"


def _hw_decode_enabled() -> bool:
    """Whether to request libobs' hardware video decode for media playback.

    On by default. ``ffmpeg_source`` degrades to software **per stream** when the
    codec has no hardware support or no HW device can be created (OBS'
    media-playback ``init_hw_decoder`` only enables HW after a successful
    ``av_hwdevice_ctx_create``), so leaving this on is safe — it uses the GPU
    decoder when the box has one and silently falls back otherwise. Set
    ``SOLIN_MEDIA_HW_DECODE=0`` to force software decode globally (e.g. a
    GPU/driver that produces corrupt hardware-decoded output).
    """
    flag = os.environ.get("SOLIN_MEDIA_HW_DECODE", "1").strip().lower()
    return flag not in ("0", "false", "off", "no")

# obs_media_state enum values (see obs/media-io).
STATE_NONE = 0
STATE_PLAYING = 1
STATE_OPENING = 2
STATE_BUFFERING = 3
STATE_PAUSED = 4
STATE_STOPPED = 5
STATE_ENDED = 6
STATE_ERROR = 7

_REMOTE_SCHEMES = ("http://", "https://", "rtsp://", "rtmp://", "srt://")
# Progressive downloads can be seeked with byte ranges; live transports cannot.
_SEEKABLE_REMOTE_SCHEMES = ("http://", "https://")
# Recover HTTP reads within the same input, preserving byte position. Finite
# inputs use OBS's file lifecycle even when the bytes arrive over HTTP: its live
# lifecycle reconnects from zero after every EOF, independently of looping.
# Keep EOF terminal and bound individual reads; native cancellation interrupts
# pending reads and retries when the source stops or closes.
_PROGRESSIVE_FFMPEG_OPTIONS = (
    "reconnect=1 "
    "reconnect_streamed=1 "
    "reconnect_on_network_error=1 "
    "reconnect_delay_max=4 "
    "rw_timeout=10000000"
)

def _is_remote(path: str) -> bool:
    return path.startswith(_REMOTE_SCHEMES)


def _is_seekable_remote(path: str) -> bool:
    return path.startswith(_SEEKABLE_REMOTE_SCHEMES)


class _StartupPhase(Enum):
    DECODING = auto()
    ACKNOWLEDGING = auto()


@dataclass
class _StartupTransport:
    state: int
    phase: _StartupPhase = _StartupPhase.DECODING
    require_video: bool = False


class LibobsMediaSource:
    """A native media source with Qt-free transport and state."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._source: Any = None
        self._video_readiness: Any = None
        self._startup: _StartupTransport | None = None
        # The last explicit transport target survives startup acknowledgement.
        self._requested_seek_ms = 0
        self._transport_lock = threading.RLock()
        self._path = ""
        self._local = True
        # True while this source holds an activate ref (see _set_active).
        self._active = False
        # Avoid redundant native updates when volume and speed arrive together.
        self._speed_percent = 100
        self._volume_percent = 100

    @property
    def source(self) -> Any:
        return self._source

    @property
    def path(self) -> str:
        return self._path

    def wait_for_video_frame(self, *, deadline: float) -> bool:
        """Wait for a native video texture, bounded by IPC expiry.

        ffmpeg transport state can be PLAYING before any frame exists. For an
        asynchronous source libobs can expose dimensions before uploading its
        texture. Prime a native frame before accepting the first visual Take.
        """
        source = self._source
        self.require_video_frame()
        wake = threading.Event()
        while source is not None and source is self._source:
            self.apply_startup_transport()
            state = source.media_state
            if state in (STATE_ERROR, STATE_ENDED) or (
                state == STATE_STOPPED and self._startup is None
            ):
                return False
            if self.video_frame_ready:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wake.wait(min(1 / 120, remaining))
        return False

    @property
    def video_frame_ready(self) -> bool:
        """Whether visual startup has completed, without waiting on the decoder."""
        with self._transport_lock:
            return (
                self._source is not None and self._video_readiness is not None
                and self._video_readiness.ready and self._startup is None
                and self._source.media_state in (STATE_PLAYING, STATE_PAUSED)
            )

    def require_video_frame(self) -> None:
        """Arm nonblocking visual preparation, including an audio-first pause."""
        with self._transport_lock:
            source, watch = self._source, self._video_readiness
            if source is None or watch is None or watch.ready:
                return
            if source.media_state in (STATE_ERROR, STATE_ENDED) and not watch.updating:
                return
            intent = self._startup
            if intent is None:
                if source.media_state != STATE_PAUSED:
                    return
                intent = self._startup = _StartupTransport(STATE_PAUSED)
            if intent.state == STATE_STOPPED:
                return
            intent.require_video = True
            if intent.phase is _StartupPhase.ACKNOWLEDGING or source.media_state == STATE_PAUSED:
                # Resume after an audio-first pause, including one still in the
                # native action queue. Keep the requested transport state muted.
                intent.phase = _StartupPhase.DECODING
                self._apply_source_volume()
                source.media_play_pause(False)

    def open(
        self,
        path: str,
        *,
        autoplay: bool = True,
        is_local_file: bool | None = None,
        volume_percent: int = 100,
        speed_percent: int = 100,
    ) -> bool:
        """Create a native decoder for ``path`` (local file or remote URL).

        ``is_local_file`` overrides the scheme-based guess (the app knows which);
        ``volume_percent`` / ``speed_percent`` set the initial volume and playback
        rate (100 = unity / normal speed).
        """
        self.close()
        if not path:
            return False
        local = (not _is_remote(path)) if is_local_file is None else bool(is_local_file)
        progressive = not local and _is_seekable_remote(path)
        finite = local or progressive
        input_key = "local_file" if finite else "input"
        settings: dict[str, object] = {"is_local_file": finite, input_key: path}
        if progressive:
            settings["ffmpeg_options"] = _PROGRESSIVE_FFMPEG_OPTIONS
        # Ask libobs to decode on the GPU when hardware is available; it falls
        # back to software per stream (see _hw_decode_enabled).
        settings["hw_decode"] = _hw_decode_enabled()
        # ffmpeg_source normally starts decoding from its `activate` callback, which
        # only fires for the MAIN view — a source on an output channel. Solin shows
        # media on outputs that are deliberately *not* on a channel (the projection
        # route, the editor preview, scene-card thumbnails: show refs, not activate
        # refs), where that callback never comes and playback sits frozen at 0 ms.
        # Solin drives the transport itself, so decouple decoding from activation.
        settings["restart_on_activate"] = False
        if speed_percent and speed_percent != 100:
            settings["speed_percent"] = int(speed_percent)
        try:
            if MEDIA_SOURCE_ID not in self._runtime.ob.enum_source_types():
                log.error("The bundled Solin media source is unavailable")
                return False
            initial_settings = dict(settings)
            initial_settings[input_key] = ""
            source = self._runtime.ob.Source.create(
                MEDIA_SOURCE_ID, "solin-content-media", initial_settings,
            )
        except Exception:  # noqa: BLE001 - source creation boundary
            log.warning("Could not create ffmpeg_source for %r", path, exc_info=True)
            return False
        if source is None:
            return False
        self._source = source
        self._startup = _StartupTransport(STATE_PLAYING if autoplay else STATE_PAUSED)
        try:
            self._video_readiness = self._runtime.watch_source_video(source)
        except Exception:  # noqa: BLE001 - release a candidate whose preparation could not start
            self.close()
            log.warning("Could not open ffmpeg_source input for %r", path, exc_info=True)
            return False
        self._path = path
        self._local = local
        self._speed_percent = max(1, int(speed_percent or 100))
        self.set_volume(volume_percent)
        try:
            from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY

            # Route decoded audio to the monitoring device so it is audible on the
            # host (libobs → PipeWire/Pulse), not only mixed into program output.
            self._runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
        except Exception:  # noqa: BLE001 - monitoring is best-effort
            log.warning("Could not set media source monitoring", exc_info=True)
        self._set_active(source, True)
        try:
            # Configure observation, monitoring and mute before the deferred
            # update can start the decoder on the OBS video thread.
            self._video_readiness.update(settings)
        except Exception:  # noqa: BLE001 - release a candidate whose input could not open
            self.close()
            log.warning("Could not open ffmpeg_source input for %r", path, exc_info=True)
            return False
        return True

    def apply_startup_transport(self) -> None:
        """Apply operator intent after decoder creation and native transport ack.

        A visual wait requires an uploaded native frame. Other startup polling
        can use position advancement, allowing audio-only sources to pause.
        Native transport acknowledgement does not acknowledge seek completion.
        """
        with self._transport_lock:
            source, intent, watch = self._source, self._startup, self._video_readiness
            if source is None or intent is None or watch is None or watch.updating:
                return
            state = source.media_state
            if state in (STATE_ERROR, STATE_ENDED):
                self._startup = None
                self._apply_source_volume()
                return
            if intent.phase is _StartupPhase.ACKNOWLEDGING:
                if state == intent.state:
                    self._startup = None
                    self._apply_source_volume()
                return
            if not watch.ready and (intent.require_video or self.position_ms <= 0):
                return
            if intent.state == STATE_PLAYING:
                if self._requested_seek_ms > 0:
                    source.media_time = self._requested_seek_ms
                self._startup = None
                self._apply_source_volume()
                return
            if intent.state == STATE_STOPPED:
                source.media_stop()
                watch.invalidate()
            else:
                source.media_play_pause(True)
                source.media_time = self._requested_seek_ms
            intent.phase = _StartupPhase.ACKNOWLEDGING

    def _set_active(self, source: Any, active: bool) -> None:
        """Hold an activate ref while the media is open, so it can be heard.

        libobs' audio monitoring drops every buffer while ``activate_refs`` is zero
        (``pulseaudio-output.c`` and ``wasapi-output.c`` both bail out on it), and
        only the MAIN view raises that counter. Solin shows media on outputs that
        deliberately take *show* refs instead — projection, the editor preview, the
        scene-card thumbnails — so a song routed anywhere but the virtual camera was
        decoded and then silently discarded on its way to the speakers.

        This is an activate ref, not an output channel: the source is not composited
        into the program canvas, and monitor-only routing still keeps it out of the
        program mix.
        """
        pointer = getattr(source, "_ptr", None)
        if pointer is None or active == self._active:
            return
        try:
            from pylibobs._ffi import get_lib

            # CFFI resolves these unwrapped symbols dynamically.
            lib: Any = get_lib()
            if active:
                lib.obs_source_inc_active(pointer)
            else:
                lib.obs_source_dec_active(pointer)
        except Exception:  # noqa: BLE001 - unwrapped libobs symbol
            log.warning("Could not change the media source activation", exc_info=True)
            return
        self._active = active

    def set_volume(self, volume_percent: int) -> None:
        """Set the source volume (100 = unity gain)."""
        self._volume_percent = max(0, int(volume_percent))
        self._apply_source_volume()

    def set_speed(self, speed_percent: int) -> None:
        """Update the native playback rate without resetting transport or video."""
        with self._transport_lock:
            speed = max(1, int(speed_percent))
            source = self._source
            if source is None or speed == self._speed_percent:
                return
            try:
                source.update({"speed_percent": speed})
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("could not set media speed", exc_info=True)
                return
            self._speed_percent = speed

    def _apply_source_volume(self) -> None:
        """Keep pending startup transport silent until its intent is applied."""
        source = self._source
        if source is None:
            return
        muted = self._startup is not None and (
            self._startup.state != STATE_PLAYING or self._requested_seek_ms > 0
        )
        percent = 0 if muted else self._volume_percent
        try:
            source.volume = max(0.0, int(percent) / 100.0)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media volume", exc_info=True)

    def play(self) -> None:
        with self._transport_lock:
            if self._startup is not None:
                self._startup.state = STATE_PLAYING
            self._apply_source_volume()
        if self._source is not None:
            self._source.media_play_pause(False)

    def pause(self) -> None:
        with self._transport_lock:
            if self._source is not None and self._startup is not None:
                self._startup.state = STATE_PAUSED
                self._apply_source_volume()
                return
        if self._source is not None:
            self._source.media_play_pause(True)

    def _invalidate_video_readiness(self) -> None:
        if self._video_readiness is not None:
            self._video_readiness.invalidate()

    def stop(self) -> None:
        with self._transport_lock:
            self._requested_seek_ms = 0
            if self._startup is not None:
                self._startup = _StartupTransport(STATE_STOPPED)
                self._apply_source_volume()
                return
        self._invalidate_video_readiness()
        if self._source is not None:
            try:
                self._source.media_stop()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("media_stop errored", exc_info=True)

    def restart(self) -> None:
        with self._transport_lock:
            self._requested_seek_ms = 0
            if self._startup is not None:
                self._startup = _StartupTransport(STATE_PLAYING)
            self._apply_source_volume()
        self._invalidate_video_readiness()
        if self._source is not None:
            self._source.media_restart()

    def seek(self, milliseconds: int) -> None:
        if self._source is None:
            return
        target = max(0, int(milliseconds))
        if not self._local and not _is_seekable_remote(self._path):
            return
        with self._transport_lock:
            self._requested_seek_ms = target
            if self._startup is not None:
                self._startup.phase = _StartupPhase.DECODING
                self._apply_source_volume()
                return
        self._source.media_time = target

    @property
    def position_ms(self) -> int:
        return self._read_int("media_time")

    @property
    def duration_ms(self) -> int:
        return self._read_int("media_duration")

    @property
    def state(self) -> int:
        if self._source is None:
            return STATE_NONE
        try:
            state = int(self._source.media_state)
            if (
                self._startup is not None
                and state not in (STATE_ERROR, STATE_ENDED)
            ):
                return self._startup.state
            return state
        except Exception:  # noqa: BLE001 - libobs boundary
            return STATE_ERROR

    def _read_int(self, attribute: str) -> int:
        if self._source is None:
            return 0
        try:
            return max(0, int(getattr(self._source, attribute)))
        except Exception:  # noqa: BLE001 - libobs boundary
            return 0

    def detach_presentation(self) -> Any | None:
        """Unload transport while leaving its silent last picture drawable.

        The caller owns the returned source and must release it after the scene
        outputs stop showing it, or when ready content replaces it. Keeping the
        source for a transition must not keep its decoder or input open.
        """
        source = self._source
        if source is not None:
            try:
                source.volume = 0.0
                source.media_play_pause(True)
            except Exception:  # noqa: BLE001 - do not orphan a failed native source
                self.close()
                raise
            try:
                # ffmpeg_source unloads the decoder when its input changes;
                # an empty input leaves the native video texture intact. Disable
                # end clearing before teardown callbacks and leave an empty local
                # input so a retired network source cannot schedule reconnection.
                source.update({
                    "is_local_file": True,
                    "local_file": "",
                    "input": "",
                    "clear_on_media_end": False,
                })
            except Exception:  # noqa: BLE001 - borrowed scenes still own the picture
                log.warning("Could not unload retired media input", exc_info=True)
        return self._detach_source()

    def _detach_source(self) -> Any | None:
        readiness = self._video_readiness
        if readiness is not None:
            # Retain both owners if native unregistration fails. close() keeps
            # failed handles alive so an explicit cleanup retry is safe.
            readiness.close()
        self._video_readiness = None
        source, self._source = self._source, None
        self._startup = None
        self._requested_seek_ms = 0
        self._path = ""
        self._local = True
        self._speed_percent = 100
        if source is not None:
            self._set_active(source, False)
        return source

    def close(self) -> None:
        source = self._detach_source()
        if source is None:
            return
        try:
            source.media_stop()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("media_stop during close errored", exc_info=True)
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("media source release errored", exc_info=True)
