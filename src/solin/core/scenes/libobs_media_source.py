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
from typing import Any

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


def _is_remote(path: str) -> bool:
    return path.startswith(_REMOTE_SCHEMES)


def _is_seekable_remote(path: str) -> bool:
    return path.startswith(_SEEKABLE_REMOTE_SCHEMES)


class LibobsMediaSource:
    """An ``ffmpeg_source`` wrapper with Qt-free transport + state."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._source: Any = None
        self._path = ""
        # True while this source holds an activate ref (see _set_active).
        self._active = False
        # Mirrors the rate libobs already has, so an unchanged value never reaches
        # obs_source_update (see set_speed for why that matters).
        self._speed_percent = 100
        # Where to return to after a rate change rebuilds the decoder.
        self._resume_ms = 0
        self._resume_paused = False

    @property
    def source(self) -> Any:
        return self._source

    @property
    def path(self) -> str:
        return self._path

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
            source = self._runtime.ob.Source.create("ffmpeg_source", "solin-content-media", settings)
        except Exception:  # noqa: BLE001 - source creation boundary
            log.warning("Could not create ffmpeg_source for %r", path, exc_info=True)
            return False
        if source is None:
            return False
        self._source = source
        self._path = path
        self._speed_percent = max(1, int(speed_percent or 100))
        self._resume_ms = 0
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
        source.media_play_pause(not autoplay)
        return True

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

            lib = get_lib()
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
        if self._source is None:
            return
        try:
            self._source.volume = max(0.0, int(volume_percent) / 100.0)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media volume", exc_info=True)

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
        speed = max(1, int(speed_percent))
        source = self._source
        if source is None or speed == self._speed_percent:
            return
        self._resume_ms = self.position_ms
        self._resume_paused = self.state == STATE_PAUSED
        self._speed_percent = speed
        try:
            source.update({"speed_percent": speed})
        except Exception:  # noqa: BLE001 - libobs boundary
            self._resume_ms = 0
            log.debug("could not set media speed", exc_info=True)

    def apply_pending_resume(self) -> None:
        """Restore the position a rate change threw away, once it can land.

        Called from the sidecar's media poll rather than straight after the update,
        because the decoder is rebuilt on a later tick — a seek issued before that
        would be applied to the media object about to be destroyed.
        """
        target = self._resume_ms
        if target <= 0 or self._source is None:
            return
        state = self.state
        if state in (STATE_NONE, STATE_OPENING):
            return  # still rebuilding; try again on the next poll
        self._resume_ms = 0
        if self.position_ms < target:
            self.seek(target)
        if self._resume_paused:
            self.pause()

    def play(self) -> None:
        if self._source is not None:
            self._source.media_play_pause(False)

    def pause(self) -> None:
        if self._source is not None:
            self._source.media_play_pause(True)

    def stop(self) -> None:
        if self._source is not None:
            try:
                self._source.media_stop()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("media_stop errored", exc_info=True)

    def restart(self) -> None:
        if self._source is not None:
            self._source.media_restart()

    def seek(self, milliseconds: int) -> None:
        if self._source is not None:
            self._source.media_time = max(0, int(milliseconds))

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
            return int(self._source.media_state)
        except Exception:  # noqa: BLE001 - libobs boundary
            return STATE_ERROR

    def _read_int(self, attribute: str) -> int:
        if self._source is None:
            return 0
        try:
            return max(0, int(getattr(self._source, attribute)))
        except Exception:  # noqa: BLE001 - libobs boundary
            return 0

    def close(self) -> None:
        source, self._source = self._source, None
        self._path = ""
        self._speed_percent = 100
        self._resume_ms = 0
        self._resume_paused = False
        if source is None:
            return
        self._set_active(source, False)  # symmetrical: never leak an activate ref
        try:
            source.media_stop()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("media_stop during close errored", exc_info=True)
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("media source release errored", exc_info=True)
