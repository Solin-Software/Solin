"""The hosted-meeting program output, inside the libobs sidecar.

libobs encodes the program (the main video mix) with x264 into a ``null_output``; a
packet callback hands every encoded access unit to ``solin-galene-bridge publish``,
which sends it to Galene over WHIP. Audio is the **meeting mix** (libobs track 6:
media playback + the hall feed input, never the loopback captures), taken as raw PCM
and encoded by the bridge with Opus in-band FEC.

Clocks: video packets are stamped with ``sys_dts_usec`` (system time; the output
rebases ``dts_usec``) and raw audio with its own system timestamp, so both share one
clock and the bridge can drive RTP timestamps and sender reports from capture time.

Writes to the bridge go through a bounded queue on a writer thread, so a stalled
bridge never blocks libobs' encoder or audio threads (excess media is dropped).
"""

from __future__ import annotations

import json
import logging
from array import array
import queue
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from solin.core.media.obs_runtime import (
    MEETING_AUDIO_MIXERS,
    MEETING_MIX_INDEX,
    MONITORING_MONITOR_AND_OUTPUT,
    MONITORING_MONITOR_ONLY,
)

log = logging.getLogger(__name__)

KIND_VIDEO = 1
KIND_PCM = 2
_QUEUE_LIMIT = 600  # ~10 s of video + audio chunks
_X264_OPTS = "bframes=0 scenecut=0"

_INPUT_CAPTURE_KINDS = {
    "win32": ("wasapi_input_capture",),
    "darwin": ("coreaudio_input_capture",),
}
_DEFAULT_INPUT_KINDS = ("pulse_input_capture", "alsa_input_capture")


@dataclass(frozen=True, slots=True)
class MeetingOutputRequest:
    bridge: str
    whip_url: str
    token: str
    width: int = 640
    height: int = 360
    video_kbps: int = 450
    audio_only: bool = False
    fec: bool = True
    hall_feed_mode: str = "system_default"  # "system_default" | "device" | "none"
    hall_feed_device: str = ""
    hall_feed_delay_ms: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> MeetingOutputRequest:
        def text(key: str, default: str = "") -> str:
            value = payload.get(key, default)
            return str(value) if value is not None else default

        return cls(
            bridge=text("bridge"), whip_url=text("whip_url"), token=text("token"),
            width=int(payload.get("width") or 640), height=int(payload.get("height") or 360),
            video_kbps=int(payload.get("video_kbps") or 450),
            audio_only=bool(payload.get("audio_only", False)), fec=bool(payload.get("fec", True)),
            hall_feed_mode=text("hall_feed_mode", "system_default"),
            hall_feed_device=text("hall_feed_device"),
            hall_feed_delay_ms=max(0, min(500, int(payload.get("hall_feed_delay_ms") or 0))),
        )


def frame(kind: int, pts_ns: int, data: bytes) -> bytes:
    return struct.pack(">BqI", kind, pts_ns, len(data)) + data


def interleave_stereo(planes: list[bytes], frames: int) -> bytes:
    """Planar float32 (libobs) → interleaved little-endian stereo float32 (the bridge)."""

    left = array("f", planes[0][:frames * 4])
    right = array("f", planes[1][:frames * 4]) if len(planes) > 1 and planes[1] else left
    out = array("f", bytes(8 * len(left)))
    out[0::2] = left
    out[1::2] = right[:len(left)]
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


class LibobsMeetingOutput:
    def __init__(
        self,
        runtime: Any,
        *,
        media_sources: Callable[[], list[Any]] = list,
        on_state: Callable[[str, str], None] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._runtime = runtime
        self._media_sources = media_sources
        self._on_state = on_state
        self._popen = popen
        self._process: Any = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_LIMIT)
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._video_encoder: Any = None
        self._audio_encoder: Any = None
        self._output: Any = None
        self._packet_cb: Any = None
        self._audio_cb: Any = None
        self._hall_feed: Any = None
        self._hall_feed_channel: int | None = None
        self.state = "idle"
        self.dropped = 0
        self._request: MeetingOutputRequest | None = None

    @property
    def active(self) -> bool:
        return self._process is not None

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:  # noqa: BLE001 - observer boundary
                log.exception("meeting output state observer failed")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, request: MeetingOutputRequest) -> bool:
        if self._process is not None:
            self.stop()
        if not request.bridge or not request.whip_url:
            self._set_state("failed", "missing bridge or WHIP URL")
            return False
        self._request = request
        command = [request.bridge, "publish", "--whip", request.whip_url, "--token", request.token]
        if not request.fec:
            command.append("--no-fec")
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
            self._process = self._popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as error:
            self._process = None
            self._set_state("failed", f"could not start the bridge: {error}")
            return False
        self._queue = queue.Queue(maxsize=_QUEUE_LIMIT)
        self._writer = threading.Thread(target=self._write_loop, name="solin-meeting-writer", daemon=True)
        self._writer.start()
        self._reader = threading.Thread(target=self._read_loop, name="solin-meeting-bridge", daemon=True)
        self._reader.start()
        self._set_state("starting")
        rate = audio_sample_rate()
        if rate not in (None, 48000):
            # The bridge encodes 48 kHz Opus; any other mix rate plays back with gaps.
            self.stop()
            self._set_state("failed", f"libobs mixes audio at {rate} Hz; the meeting needs 48000 Hz")
            return False
        try:
            self._route_audio(request)
            self._attach_audio()
            if not request.audio_only:
                self._start_video(request)
        except Exception as error:  # noqa: BLE001 - libobs boundary
            log.warning("could not start the meeting output", exc_info=True)
            self.stop()
            self._set_state("failed", str(error))
            return False
        return True

    def stop(self) -> None:
        self._detach()
        process, self._process = self._process, None
        if process is not None:
            self._queue.put(None)
            writer = self._writer
            if writer is not None:
                writer.join(2.0)
            try:
                process.terminate()
                process.wait(3.0)
            except Exception:  # noqa: BLE001 - the bridge may already be gone
                try:
                    process.kill()
                except OSError:
                    log.debug("meeting bridge kill failed", exc_info=True)
        self._writer = self._reader = None
        if self.state != "failed":
            self._set_state("idle")

    def _detach(self) -> None:
        from pylibobs._ffi import ffi, get_lib  # type: ignore[import-not-found]

        # Release explicitly and in order (stop → callbacks → output → encoders):
        # wrappers left to the garbage collector can be freed after libobs shuts down.
        output, self._output = self._output, None
        if output is not None:
            try:
                output.stop()
            except Exception:  # noqa: BLE001 - libobs boundary during teardown
                log.debug("meeting null output stop failed", exc_info=True)
            if self._packet_cb is not None:
                try:
                    cast(Any, get_lib()).obs_output_remove_packet_callback(output._ptr, self._packet_cb, ffi.NULL)
                except Exception:  # noqa: BLE001 - libobs boundary during teardown
                    log.debug("packet callback removal failed", exc_info=True)
            self._release(output)
        self._packet_cb = None
        audio_cb, self._audio_cb = self._audio_cb, None
        if audio_cb is not None:
            try:
                # The mix index must match the one the callback was added on.
                self._runtime.ob.remove_raw_audio_callback(audio_cb, MEETING_MIX_INDEX)
            except Exception:  # noqa: BLE001 - libobs boundary during teardown
                log.debug("raw audio callback removal failed", exc_info=True)
        for encoder in (self._video_encoder, self._audio_encoder):
            self._release(encoder)
        self._video_encoder = self._audio_encoder = None
        self._unroute_audio()

    @staticmethod
    def _release(wrapper: Any) -> None:
        release = getattr(wrapper, "release", None)
        if callable(release):
            try:
                release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("libobs release failed", exc_info=True)

    # ── audio routing: the meeting mix (track 6) ─────────────────────────

    def _route_audio(self, request: MeetingOutputRequest) -> None:
        for source in self._media_sources():
            route_media_to_meeting(self._runtime, source, True)
        self._runtime.meeting_mix_enabled = True
        if request.hall_feed_mode == "none":
            return
        device = "default" if request.hall_feed_mode != "device" else (request.hall_feed_device or "default")
        kinds = _INPUT_CAPTURE_KINDS.get(sys.platform, _DEFAULT_INPUT_KINDS)
        for kind in kinds:
            try:
                source = self._runtime.ob.Source.create(kind, "solin-meeting-hall-feed", {"device_id": device})
            except Exception:  # noqa: BLE001 - plugin boundary
                continue
            if source is None:
                continue
            source.audio_mixers = MEETING_AUDIO_MIXERS
            if request.hall_feed_delay_ms:
                set_sync_offset(source, request.hall_feed_delay_ms)
            channel = self._runtime.acquire_channel()
            self._runtime.set_channel_source(channel, source)
            self._hall_feed, self._hall_feed_channel = source, channel
            return
        log.warning("no hall-feed input capture is available")

    def _unroute_audio(self) -> None:
        self._runtime.meeting_mix_enabled = False
        for source in self._media_sources():
            route_media_to_meeting(self._runtime, source, False)
        channel, self._hall_feed_channel = self._hall_feed_channel, None
        feed, self._hall_feed = self._hall_feed, None
        if channel is not None:
            try:
                self._runtime.set_channel_source(channel, None)
                self._runtime.release_channel(channel)
            except Exception:  # noqa: BLE001 - libobs boundary during teardown
                log.debug("hall feed release failed", exc_info=True)
        self._release(feed)

    def _attach_audio(self) -> None:
        def on_audio(_mix: int, planes: list[bytes], frames: int, ts_ns: int) -> None:
            self._enqueue(frame(KIND_PCM, int(ts_ns), interleave_stereo(planes, frames)))

        self._audio_cb = self._runtime.ob.add_raw_audio_callback(on_audio, MEETING_MIX_INDEX, channels=2)

    # ── video ─────────────────────────────────────────────────────────────

    def _start_video(self, request: MeetingOutputRequest) -> None:
        from pylibobs._ffi import ffi, get_lib  # type: ignore[import-not-found]

        ob = self._runtime.ob
        self._video_encoder = ob.VideoEncoder.create("obs_x264", "solin-meeting-video", {
            "rate_control": "CBR", "bitrate": int(request.video_kbps), "keyint_sec": 2,
            "preset": "veryfast", "tune": "zerolatency", "profile": "baseline",
            "x264opts": _X264_OPTS, "repeat_headers": True,
        })
        set_scaled_size(self._video_encoder, request.width, request.height)
        # A null output needs an audio encoder too; its packets are ignored (the
        # bridge encodes the raw meeting mix with FEC).
        self._audio_encoder = ob.AudioEncoder.create("ffmpeg_opus", "solin-meeting-audio", {"bitrate": 64},
                                                     mixer_idx=MEETING_MIX_INDEX)
        output = ob.Output.create("null_output", "solin-meeting")
        output.set_video_encoder(self._video_encoder)
        output.set_audio_encoder(self._audio_encoder, 0)

        @ffi.callback("void(void *, struct encoder_packet *)")
        def on_packet(_param: Any, packet: Any) -> None:
            try:
                if packet.type != 1:
                    return
                data = bytes(ffi.buffer(packet.data, packet.size))
                self._enqueue(frame(KIND_VIDEO, int(packet.sys_dts_usec) * 1000, data))
            except Exception:  # noqa: BLE001 - never raise into libobs
                log.debug("meeting packet callback failed", exc_info=True)

        self._packet_cb = on_packet
        cast(Any, get_lib()).obs_output_add_packet_callback(output._ptr, on_packet, ffi.NULL)
        if not output.start():
            raise RuntimeError("the meeting video output did not start")
        self._output = output

    # ── bridge I/O ────────────────────────────────────────────────────────

    def _enqueue(self, data: bytes) -> None:
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self.dropped += 1

    def _write_loop(self) -> None:
        process = self._process
        stdin = getattr(process, "stdin", None)
        while stdin is not None:
            item = self._queue.get()
            if item is None:
                break
            try:
                stdin.write(item)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
        try:
            if stdin is not None:
                stdin.close()
        except OSError:
            pass

    def _read_loop(self) -> None:
        process = self._process
        stdout = getattr(process, "stdout", None)
        if stdout is None:
            return
        for raw in stdout:
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if event.get("event") == "state":
                state = str(event.get("state") or "")
                mapped = {"connected": "live", "connecting": "starting", "reconnecting": "reconnecting",
                          "failed": "failed"}.get(state)
                if mapped:
                    self._set_state(mapped, str(event.get("detail") or ""))
        if self._process is process and process is not None:
            # The bridge exited on its own.
            self._set_state("failed", "the meeting bridge stopped")


def route_media_to_meeting(runtime: Any, source: Any, enabled: bool) -> None:
    """Put a playing media source into the meeting mix (track 6), or take it out.

    Media is normally monitor-only with no mixer tracks (heard in the hall, recorded
    through the speaker loopback). For the meeting it must also be *output* on track
    6 so remote attendees hear it directly, without the hall's echo.
    """

    if source is None:
        return
    try:
        if enabled:
            from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

            cast(Any, get_lib()).obs_source_set_audio_mixers(source._ptr, MEETING_AUDIO_MIXERS)
            runtime.set_source_monitoring(source, MONITORING_MONITOR_AND_OUTPUT, keep_mixers=True)
        else:
            runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
    except Exception:  # noqa: BLE001 - libobs boundary
        log.warning("could not route media audio to the meeting mix", exc_info=True)


def audio_sample_rate() -> int | None:
    """libobs' current mix sample rate, or None when it cannot be read."""

    try:
        from pylibobs._ffi import ffi, get_lib  # type: ignore[import-not-found]

        info = ffi.new("struct obs_audio_info *")
        if not cast(Any, get_lib()).obs_get_audio_info(info):
            return None
        return int(info.samples_per_sec)
    except Exception:  # noqa: BLE001 - libobs boundary; the check is advisory
        return None


def set_sync_offset(source: Any, delay_ms: int) -> None:
    """Delay a source's audio (the camera reaches libobs later than the mixer's sound)."""

    from pylibobs._ffi import get_lib  # type: ignore[import-not-found]

    try:
        cast(Any, get_lib()).obs_source_set_sync_offset(source._ptr, int(delay_ms) * 1_000_000)
    except Exception:  # noqa: BLE001 - libobs boundary
        log.warning("could not delay the hall feed by %d ms", delay_ms, exc_info=True)


def set_scaled_size(encoder: Any, width: int, height: int) -> None:
    """Scale the meeting encoder without changing the global output size."""

    from pylibobs._ffi import ffi, get_lib  # type: ignore[import-not-found]

    try:
        lib: Any = get_lib()
        fn = getattr(lib, "obs_encoder_set_scaled_size", None)
        if fn is None:
            ffi.cdef("void obs_encoder_set_scaled_size(obs_encoder_t *encoder, uint32_t width, uint32_t height);",
                     override=True)
            fn = lib.obs_encoder_set_scaled_size
        fn(encoder._ptr, int(width), int(height))
    except Exception:  # noqa: BLE001 - fall back to the canvas size
        log.warning("could not scale the meeting encoder to %dx%d", width, height, exc_info=True)
