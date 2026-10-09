"""Qt-free libobs media source (``ffmpeg_source``) for the scene-engine sidecar.

Under Fork A, media files are decoded by libobs — not pushed as frames — so a
media content item is an ``ffmpeg_source`` created in the sidecar. This wraps one
with transport (open / play / pause / stop / seek) and state polling, with **no
Qt dependency** (the sidecar has no event loop). Its ``source`` drops straight
into the scene builder's ``content_source`` slot, so it composites like any other
source.

The richer behaviours the in-process ``obs_playback`` engine grew (trim, speed
persistence, stream reconnect, download handoff, tag metadata) are follow-ups;
this is the decode + basic transport core.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from solin.core.scenes.audio_tempo import (
    TempoAudioCompanion,
    is_unity_rate,
)

log = logging.getLogger(__name__)


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
# Options for a progressive HTTP input. media-playback has no notion of a
# recoverable read: any error out of av_read_frame ends its decode thread, which
# ffmpeg_source reports as ENDED — so one dropped packet looked like the end of the
# video. Letting ffmpeg's own http layer re-issue the request with a Range header
# keeps the failure below av_read_frame, where nothing upstream ever sees it.
#
# reconnect_at_eof stays off so a real end of file still ends. rw_timeout also caps
# how long teardown can block: marking a stream seekable drops the interrupt
# callback media-playback would otherwise install.
_PROGRESSIVE_FFMPEG_OPTIONS = (
    "reconnect=1 "
    "reconnect_streamed=1 "
    "reconnect_on_network_error=1 "
    "reconnect_delay_max=4 "
    "rw_timeout=10000000"
)

# Poll ticks to give the rebuilt decoder to start moving before the stretched
# audio is opened anyway. Generous: overshooting only costs a little sync on
# media that never advances, while giving up early costs sync on all of it.
_TEMPO_MOTION_TICKS = 40

# Poll ticks to wait for the rebuilt decoder to announce itself by falling back
# to the start. It has always arrived within one tick in practice; the cap is
# only so a decoder that keeps its place is not waited on forever.
_RESUME_REBUILD_TICKS = 8

# How far past the target counts as "back where it was": one poll tick of
# playback, so a restore is not called a failure for landing a frame late.
_RESTORE_TOLERANCE_MS = 500

# How closely the hand-over watches for the picture to start moving again, and
# how long it waits before going ahead without it.
_MOTION_STEP_S = 0.02
_MOTION_TIMEOUT_S = 1.5


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
    """An ``ffmpeg_source`` wrapper with Qt-free transport + state."""

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
        # Mirrors the rate libobs already has, so an unchanged value never reaches
        # obs_source_update (see set_speed for why that matters).
        self._speed_percent = 100
        # Where to return to after a rate change rebuilds the decoder.
        self._resume_ms = 0
        self._resume_tries = 0
        self._resume_restarted = False
        self._resume_paused = False
        self._volume_percent = 100
        # Carries the audio, pitch intact, whenever the rate is not normal.
        self._tempo_audio = TempoAudioCompanion(runtime)
        self._tempo_audio_running = False
        self._tempo_pending = False
        self._tempo_anchor = 0
        self._tempo_waits = 0

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
        self._require_startup_video()
        wake = threading.Event()
        while source is not None and source is self._source:
            self._apply_startup_transport()
            state = source.media_state
            if state in (STATE_ERROR, STATE_ENDED) or (
                state == STATE_STOPPED and self._startup is None
            ):
                return False
            if (
                self._video_readiness is not None and self._video_readiness.ready
                and self._startup is None
            ):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wake.wait(min(1 / 120, remaining))
        return False

    def _require_startup_video(self) -> None:
        """Visual preparation needs a frame even when audio already acknowledged startup."""
        with self._transport_lock:
            source, watch = self._source, self._video_readiness
            if source is None or watch is None or watch.ready:
                return
            if source.media_state in (STATE_ERROR, STATE_ENDED, STATE_STOPPED):
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
        """Create an ffmpeg_source for ``path`` (local file or remote URL).

        ``is_local_file`` overrides the scheme-based guess (the app knows which);
        ``volume_percent`` / ``speed_percent`` set the initial volume and playback
        rate (100 = unity / normal speed).
        """
        self.close()
        if not path:
            return False
        local = (not _is_remote(path)) if is_local_file is None else bool(is_local_file)
        settings: dict[str, object] = (
            {"is_local_file": True, "local_file": path}
            if local
            else {"is_local_file": False, "input": path}
        )
        if not local and _is_seekable_remote(path):
            # media-playback wraps av_seek_frame in `if (m->is_local_file)`, and
            # ffmpeg_source composes that flag as `is_local_file || seekable`. With
            # neither set, a seek is accepted, queued, and then silently dropped —
            # the position keeps running and the slider snaps back. HTTP origins
            # serve byte ranges, so mark them seekable and pay the network options
            # back through ffmpeg_options. A origin that refuses ranges just logs
            # "MP: Failed to seek" and behaves as before. Live transports are left
            # alone: they cannot seek and do need the interrupt callback.
            settings["seekable"] = True
            settings["ffmpeg_options"] = _PROGRESSIVE_FFMPEG_OPTIONS
            # If ffmpeg's own reconnect does give up, ffmpeg_source rebuilds the
            # media after this delay. Its default is 10 s, which reads as a dead
            # video in the middle of a meeting.
            settings["reconnect_delay_sec"] = 2
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
            initial_settings = dict(settings)
            initial_settings["local_file" if local else "input"] = ""
            source = self._runtime.ob.Source.create(
                "ffmpeg_source", "solin-content-media", initial_settings,
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
        self._tempo_pending = False
        self._speed_percent = max(1, int(speed_percent or 100))
        self._resume_ms = 0
        self._resume_tries = 0
        self._resume_restarted = False
        self._resume_paused = False
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
        self._reconcile_tempo_audio(0)
        return True

    def _apply_startup_transport(self) -> None:
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
        if self._tempo_audio is not None and self._tempo_audio_running:
            self._tempo_audio.set_volume(self._volume_percent)
        self._apply_source_volume()

    def set_speed(self, speed_percent: int) -> None:
        """Set the playback rate (100 = normal), keeping the current position.

        ffmpeg_source has no live rate control: the rate is baked into the media
        object at creation, so a change tears the decoder down and replays from
        zero — for local files as much as for streams, since the restart check is
        on the rate, not on where the media came from. It also force-resumes a
        paused source.

        So: do nothing when the rate has not changed — volume travels on the same
        properties message, and re-sending an unchanged rate was enough to restart
        a stream every time the operator touched the volume slider. When it has
        changed, remember where we were and let :meth:`apply_pending_resume` put us
        back once the new decoder exists. The seek cannot be issued here: this is
        an async source, so obs_source_update only marks the source for a deferred
        update and the media object is still the old one.
        """
        with self._transport_lock:
            self._set_speed(speed_percent)

    def _set_speed(self, speed_percent: int) -> None:
        speed = max(1, int(speed_percent))
        source = self._source
        if source is None or speed == self._speed_percent:
            return
        self._apply_startup_transport()
        resume_ms = self.position_ms
        resume_paused = self.state == STATE_PAUSED
        try:
            self._video_readiness.update({"speed_percent": speed})
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media speed", exc_info=True)
            return
        self._resume_ms = resume_ms
        self._resume_paused = resume_paused
        if self._startup is not None:
            self._startup = _StartupTransport(
                self._startup.state,
                require_video=self._startup.require_video,
            )
            self._resume_ms = 0
        self._speed_percent = speed
        # The stretch is a second decode with its own clock, so it is started
        # only once the rebuilt decoder has taken the position back — otherwise the
        # audio would begin while the video is still winding back to meet it.
        if self._tempo_audio is not None:
            self._tempo_audio.stop()
        self._tempo_audio_running = False
        self._tempo_pending = self._wants_tempo_audio()
        self._tempo_anchor = self._resume_ms
        self._tempo_waits = 0
        self._resume_tries = 0
        self._resume_restarted = False
        self._apply_source_volume()

    def _wants_tempo_audio(self) -> bool:
        """Whether this media, at this rate, should be stretched rather than resampled.

        Only local files: stretching re-reads the media from the start position, and
        doing that over the network would double the bandwidth of something already
        struggling — or, on a live stream, be meaningless. Remote media keeps libobs'
        varispeed, which is how it behaved before.
        """
        return (
            self._tempo_audio is not None
            and self._local
            and not is_unity_rate(self._speed_percent / 100.0)
        )

    def _reconcile_tempo_audio(self, position_ms: int) -> None:
        """Play the audio stretched rather than varispeeded, when off normal rate.

        libobs changes rate by resampling, so its audio rises in pitch with the
        speed. ffmpeg's atempo does the same stretch without touching pitch, so at
        anything other than normal speed the real source is muted and a companion
        plays the stretched audio alongside it.

        Best-effort throughout: if the companion cannot start, the source is
        unmuted and libobs' own varispeed is heard, which is what happened before.
        """
        companion = self._tempo_audio
        self._tempo_pending = False
        if not self._wants_tempo_audio():
            if companion is not None:
                companion.stop()
            self._tempo_audio_running = False
            self._apply_source_volume()
            return
        rate = self._speed_percent / 100.0
        # Hold the picture still over the whole hand-over. Starting ffmpeg, getting
        # obs to buffer the stream and letting the decoder resume all take a
        # noticeable moment, and whatever the picture does during them the audio
        # cannot be wound back to match — nothing pulls the two together again
        # afterwards, so an item would stay out of step to its end.
        paused = self.state == STATE_PAUSED
        if not paused:
            self._set_paused(True)
        started = companion.start(
            self._path,
            position_ms=position_ms,
            rate=rate,
            volume_percent=self._volume_percent,
            # Opened but held: it is released below, once the picture is moving.
            paused=True,
        )
        self._tempo_audio_running = started
        released = paused
        try:
            if not started or paused:
                # A media that is paused keeps its stretch held too; play() lets
                # the two go together when it resumes.
                return
            # The stretch is open but held, so only the little it played while
            # connecting has to be skipped. The picture is moved to meet it, and
            # the two are released together below.
            resume_at = position_ms + int(companion.elapsed_ms * rate)
            self._rewind_to(resume_at)
            self._set_paused(False)
            released = True
            self._await_motion(resume_at)
            companion.set_paused(False)
        finally:
            if not released:
                self._set_paused(False)
            self._apply_source_volume()

    def _await_motion(self, from_ms: int) -> None:
        """Block until the picture is past ``from_ms``, so the audio can join it."""
        clock = threading.Event()
        waited = 0.0
        while waited < _MOTION_TIMEOUT_S:
            if self.position_ms > from_ms:
                return
            clock.wait(_MOTION_STEP_S)
            waited += _MOTION_STEP_S
        log.info("The picture did not resume in time to meet the stretched audio")

    def _set_paused(self, paused: bool) -> None:
        source = self._source
        if source is None:
            return
        try:
            source.media_play_pause(bool(paused))
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not hold the picture", exc_info=True)

    def _rewind_to(self, position_ms: int) -> None:
        """Move the picture without touching the companion (seek() would restart it)."""
        source = self._source
        if source is None:
            return
        try:
            source.media_time = max(0, int(position_ms))
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not align media to the stretched audio", exc_info=True)

    def _apply_source_volume(self) -> None:
        """Mute the media source itself while the companion carries the audio."""
        source = self._source
        if source is None:
            return
        carried_elsewhere = (
            self._tempo_audio_running or self._tempo_pending
            or (self._startup is not None and (
                self._startup.state != STATE_PLAYING or self._requested_seek_ms > 0
            ))
        )
        percent = 0 if carried_elsewhere else self._volume_percent
        try:
            source.volume = max(0.0, int(percent) / 100.0)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media volume", exc_info=True)

    def apply_pending_resume(self) -> None:
        """Restore the position a rate change threw away, once it can land.

        Called from the sidecar's media poll rather than straight after the update,
        because the decoder is rebuilt on a later tick — a seek issued here and now
        would be applied to the media object about to be destroyed.
        """
        self._apply_startup_transport()
        if self._source is None or self._startup is not None:
            return
        if self._resume_ms <= 0 and not self._tempo_pending:
            return
        if self.state in (STATE_NONE, STATE_OPENING):
            return  # still rebuilding; try again on the next poll
        if self._resume_ms > 0 and not self._restore_position():
            return  # the rebuilt decoder has not shown itself yet
        if self._tempo_pending and self._ready_for_tempo_audio():
            self._reconcile_tempo_audio(self._tempo_anchor)
        if self._resume_paused:
            self.pause()

    def _restore_position(self) -> bool:
        """Put playback back where it was; True once that question is settled.

        Changing the rate makes ffmpeg_source throw its media object away and build
        a new one, which starts from zero. The new object does not appear on the
        tick that asks for it — for a moment the old one is still answering, with
        the very position being preserved — so seeking straight away would be a
        no-op that the restart then goes on to undo. Waiting for the position to
        fall back is what makes the restore land.

        The seek is keyframe-granular, so playback resumes at the keyframe before
        where it was rather than exactly on it.
        """
        target = self._resume_ms
        self._resume_tries += 1
        position = self.position_ms
        if position + _RESTORE_TOLERANCE_MS < target:
            # The rebuilt decoder has shown itself. Ask once and let it land:
            # re-asking every tick restarts each seek before the last has finished
            # and playback sits at the start going nowhere.
            self._resume_restarted = True
            self._resume_ms = 0
            self._tempo_anchor = target
            self.seek(target)
            return True
        if self._resume_restarted or self._resume_tries > _RESUME_REBUILD_TICKS:
            self._resume_ms = 0
            self._tempo_anchor = max(self._tempo_anchor, position)
            return True
        return False

    def _ready_for_tempo_audio(self) -> bool:
        """Whether the rebuilt decoder is really running yet.

        It reports PLAYING for a second or two before the position starts moving,
        and audio opened during that window would run that far ahead of the picture
        for the rest of the item. Waiting for actual movement makes the hand-over
        self-correcting whatever the rebuild costs — but only up to a point, so
        media that legitimately never advances still gets its stretched audio.
        """
        self._tempo_waits += 1
        if self._resume_paused or self._tempo_waits > _TEMPO_MOTION_TICKS:
            return True
        if self.position_ms > self._tempo_anchor:
            self._tempo_anchor = self.position_ms
            return True
        return False

    def play(self) -> None:
        resuming = self.state == STATE_PAUSED
        with self._transport_lock:
            if self._startup is not None:
                self._startup.state = STATE_PLAYING
            self._apply_source_volume()
        at = self.position_ms
        if self._source is not None:
            self._source.media_play_pause(False)
        if self._tempo_audio_running:
            if resuming:
                # The picture takes a moment to pick up again. Letting the audio
                # go first would leave the two out of step for the rest of the
                # item, since nothing pulls them back together afterwards.
                self._await_motion(at)
            self._tempo_audio.set_paused(False)

    def pause(self) -> None:
        with self._transport_lock:
            if self._source is not None and self._startup is not None:
                self._startup.state = STATE_PAUSED
                self._apply_source_volume()
                return
        if self._source is not None:
            self._source.media_play_pause(True)
        if self._tempo_audio_running:
            self._tempo_audio.set_paused(True)

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
        if self._tempo_audio_running:
            # The companion decodes its own timeline, so it has to be restarted at
            # the new position rather than followed.
            self._reconcile_tempo_audio(target)

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
        self._resume_ms = 0
        self._resume_tries = 0
        self._resume_restarted = False
        self._resume_paused = False
        if self._tempo_audio is not None:
            self._tempo_audio.stop()
        self._tempo_audio_running = False
        self._tempo_pending = False
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
