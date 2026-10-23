"""The isolated meeting publisher process (``--meeting-publisher``).

It reads the program video (packed I420) and the meeting audio mix (planar float32) that
the scene-engine sidecar writes into two shared-memory rings, and publishes them into the
LiveKit room as the hall participant. It runs apart from both the app and the sidecar so
the WebRTC stack (encoders, network threads, E2EE) can crash or leak without taking either
down: the app supervises it with :class:`~solin.core.conference.publisher_service.MeetingPublisherService`.

Control is JSON lines. The worker greets with ``{"ready": PROTOCOL_VERSION}``, accepts one
``{"command": "start", ...}`` (see :class:`PublishRequest`) and ``{"command": "stop"}``, and
reports ``{"state": ...}``, ``{"stats": {...}}`` and ``{"error": code}``. EOF on stdin
means the app is gone: the worker disconnects and exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from solin.core.conference.media_ring import MediaRingReader, MediaRingWriter

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 2
POLL_INTERVAL_S = 0.004
STATS_INTERVAL_S = float(os.environ.get("SOLIN_PUBLISHER_STATS_S", "5"))
MAX_BITRATE_RANGE = (100_000, 8_000_000)


@dataclass(frozen=True, slots=True)
class PublishRequest:
    url: str
    token: str
    video_ring: str
    audio_ring: str
    width: int
    height: int
    fps: int
    max_bitrate: int = 800_000
    simulcast: bool = True
    audio_only: bool = False
    e2ee_key: str = ""
    # Hall audio through WebRTC's device module: the meeting microphone (the hall feed,
    # echo-cancelled) is published and attendees' voices play on the meeting speaker.
    # Empty device ids mean the system default.
    hall_audio: bool = False
    recording_device: str = ""
    playout_device: str = ""
    echo_cancellation: bool = True
    noise_suppression: bool = True
    auto_gain_control: bool = False
    # The spotlighted remote part-holder's camera goes to this ring (packed I420) for the
    # scene engine's "Remote participant (meeting)" layers. Empty: no ring / nobody.
    participant_ring: str = ""
    spotlight: str = ""

    def __post_init__(self) -> None:
        for name in ("url", "token", "video_ring", "audio_ring", "e2ee_key",
                     "recording_device", "playout_device", "participant_ring", "spotlight"):
            if type(getattr(self, name)) is not str:
                raise ValueError(f"Invalid {name}")
        if not self.url.startswith(("ws://", "wss://")):
            raise ValueError("Invalid url")
        if not self.token or not self.audio_ring or (not self.audio_only and not self.video_ring):
            raise ValueError("A token and the media rings are required")
        for name, low, high in (
            ("width", 16, 3840), ("height", 16, 2160), ("fps", 1, 60),
            ("max_bitrate", *MAX_BITRATE_RANGE),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        if self.width % 2 or self.height % 2:
            raise ValueError("I420 needs even dimensions")
        if len(self.spotlight) > 128:
            raise ValueError("Invalid spotlight")
        if len(self.recording_device) > 256 or len(self.playout_device) > 256:
            raise ValueError("Invalid audio device")
        for name in ("simulcast", "audio_only", "hall_audio", "echo_cancellation",
                     "noise_suppression", "auto_gain_control"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid {name}")

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> PublishRequest:
        fields = {key: value for key, value in message.items() if key != "command"}
        try:
            return cls(**fields)
        except TypeError as exc:
            raise ValueError("Unexpected publish request fields") from exc


def float_planes_to_s16(payload: bytes, frames: int, channels: int = AUDIO_CHANNELS) -> bytes:
    """Planar float32 (``channels`` planes back to back) → interleaved signed 16-bit."""
    samples = np.frombuffer(payload, dtype=np.float32, count=frames * channels)
    planar = samples.reshape(channels, frames)
    interleaved = np.clip(planar.T * 32767.0, -32768, 32767).astype(np.int16)
    return interleaved.tobytes()


@dataclass(frozen=True, slots=True)
class VideoItem:
    payload: bytes
    width: int
    height: int
    timestamp_us: int


@dataclass(frozen=True, slots=True)
class AudioItem:
    pcm: bytes
    frames: int


class MediaPump:
    """Drains the egress rings into publishable items, newest video only."""

    def __init__(self, video: MediaRingReader | None, audio: MediaRingReader) -> None:
        self._video = video
        self._audio = audio
        self.video_frames = 0
        self.audio_chunks = 0
        self.skipped_video = 0
        self.bad_items = 0

    @property
    def dropped(self) -> int:
        return (self._video.dropped if self._video else 0) + self._audio.dropped

    def drain(self) -> tuple[VideoItem | None, list[AudioItem]]:
        audio: list[AudioItem] = []
        while (item := self._audio.read_next()) is not None:
            frames, channels = item.meta & 0xFFFF, item.meta >> 16
            if channels != AUDIO_CHANNELS or frames <= 0 or len(item.payload) < frames * channels * 4:
                self.bad_items += 1
                continue
            audio.append(AudioItem(float_planes_to_s16(item.payload, frames), frames))
        self.audio_chunks += len(audio)
        latest: VideoItem | None = None
        if self._video is not None:
            while (item := self._video.read_next()) is not None:
                width, height = item.meta >> 16, item.meta & 0xFFFF
                if not width or not height or len(item.payload) != width * height * 3 // 2:
                    self.bad_items += 1
                    continue
                if latest is not None:
                    self.skipped_video += 1  # a stale frame is worthless to the encoder
                latest = VideoItem(item.payload, width, height, item.timestamp_ns // 1000)
        if latest is not None:
            self.video_frames += 1
        return latest, audio


class _Output:
    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()


def _stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, stream=None) -> None:
    stream = stream or sys.stdin
    for line in stream:
        if len(line) > MAX_MESSAGE_BYTES:
            loop.call_soon_threadsafe(queue.put_nowait, {"command": "invalid"})
            continue
        try:
            message = json.loads(line)
        except ValueError:
            message = {"command": "invalid"}
        loop.call_soon_threadsafe(queue.put_nowait, message if isinstance(message, dict) else {"command": "invalid"})
    loop.call_soon_threadsafe(queue.put_nowait, None)  # EOF: the app is gone


class _LiveKitSession:
    """The LiveKit side; imported lazily so the module stays importable without the SDK."""

    def __init__(self, request: PublishRequest, out: _Output) -> None:
        self.request = request
        self._out = out
        self.room: Any = None
        self._video_source: Any = None
        self._audio_source: Any = None
        self._platform_audio: Any = None
        self._hall_source: Any = None
        self.playout_tracks: set[str] = set()  # attendee audio tracks playing in the hall
        self.spotlight = request.spotlight
        self.participant_frames = 0
        self._participant_ring: MediaRingWriter | None = None
        self._participant_task: asyncio.Task | None = None
        self._participant_track_sid = ""
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        from livekit import rtc

        request = self.request
        options = rtc.RoomOptions(auto_subscribe=False, dynacast=True)
        if request.e2ee_key:
            options.encryption = rtc.E2EEOptions(
                key_provider_options=rtc.KeyProviderOptions(shared_key=request.e2ee_key.encode())
            )
        hall_audio = self._open_hall_audio() if request.hall_audio else False
        if request.participant_ring:
            self._participant_ring = MediaRingWriter(request.participant_ring)
        room = rtc.Room()
        room.on("track_published", self._on_track_published)
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        room.on("reconnecting", lambda: self._out.send({"state": "reconnecting"}))
        room.on("reconnected", lambda: self._out.send({"state": "publishing"}))
        room.on("disconnected", lambda *_: self.closed.set())
        self.room = room
        await room.connect(request.url, request.token, options=options)
        self._audio_source = rtc.AudioSource(SAMPLE_RATE, AUDIO_CHANNELS, queue_size_ms=200)
        audio_track = rtc.LocalAudioTrack.create_audio_track("program-audio", self._audio_source)
        await room.local_participant.publish_track(
            audio_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO)
        )
        if hall_audio:
            hall_track = rtc.LocalAudioTrack.create_audio_track("hall-feed", self._hall_source)
            await room.local_participant.publish_track(
                hall_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
        for participant in room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._on_track_published(publication, participant)
        if not request.audio_only:
            self._video_source = rtc.VideoSource(request.width, request.height)
            video_track = rtc.LocalVideoTrack.create_video_track("program", self._video_source)
            # Software H.264: the spike found the VAAPI path leaking in the released SDK.
            await room.local_participant.publish_track(video_track, rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_CAMERA,
                simulcast=request.simulcast,
                video_codec=rtc.VideoCodec.H264,
                video_encoder=rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE,
                video_encoding=rtc.VideoEncoding(
                    max_bitrate=request.max_bitrate, max_framerate=request.fps
                ),
            ))

    def _open_hall_audio(self) -> bool:
        """Open the meeting microphone and speaker; without them the meeting goes on."""
        from livekit import rtc

        request = self.request
        try:
            platform_audio = rtc.PlatformAudio()
            if request.recording_device:
                platform_audio.set_recording_device(request.recording_device)
            if request.playout_device:
                platform_audio.set_playout_device(request.playout_device)
            self._hall_source = platform_audio.create_audio_source(rtc.PlatformAudioOptions(
                echo_cancellation=request.echo_cancellation,
                noise_suppression=request.noise_suppression,
                auto_gain_control=request.auto_gain_control,
            ))
        except Exception:  # noqa: BLE001 - native device boundary
            log.warning("Hall audio is unavailable", exc_info=True)
            self._out.send({"warning": "hall_audio_unavailable"})
            return False
        self._platform_audio = platform_audio
        return True

    def _on_track_published(self, publication: Any, participant: Any) -> None:
        from livekit import rtc

        if not participant.identity.startswith("att:"):
            return
        if publication.kind == rtc.TrackKind.KIND_AUDIO:
            # Attendees' microphones play in the hall (when hall audio is on). Who may
            # speak is decided by their publish permission, so every one is wanted.
            if self._platform_audio is not None:
                publication.set_subscribed(True)
        elif (publication.kind == rtc.TrackKind.KIND_VIDEO
              and participant.identity == self.spotlight and self._participant_ring is not None):
            publication.set_subscribed(True)

    def set_spotlight(self, identity: str) -> None:
        """Follow another remote part-holder's camera (empty: nobody)."""
        from livekit import rtc

        if identity == self.spotlight or self.room is None:
            self.spotlight = identity
            return
        self.spotlight = identity
        for participant in self.room.remote_participants.values():
            for publication in participant.track_publications.values():
                if publication.kind != rtc.TrackKind.KIND_VIDEO:
                    continue
                wanted = participant.identity == identity and self._participant_ring is not None
                publication.set_subscribed(wanted)

    def _on_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        from livekit import rtc

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self.playout_tracks.add(publication.sid)
            return
        if participant.identity != self.spotlight or self._participant_ring is None:
            return
        if self._participant_task is not None:
            self._participant_task.cancel()
        self._participant_track_sid = publication.sid
        self._participant_task = asyncio.ensure_future(self._forward_participant(track))

    def _on_track_unsubscribed(self, track: Any, publication: Any, _participant: Any) -> None:
        self.playout_tracks.discard(publication.sid)
        if publication.sid == self._participant_track_sid and self._participant_task is not None:
            self._participant_task.cancel()
            self._participant_task = None
            self._participant_track_sid = ""

    async def _forward_participant(self, track: Any) -> None:
        from livekit import rtc

        ring = self._participant_ring
        stream = rtc.VideoStream(track, capacity=1, format=rtc.VideoBufferType.I420)
        try:
            async for event in stream:
                frame = event.frame
                width, height = frame.width, frame.height
                if ring is None or width % 2 or height % 2 or width > 0xFFFF or height > 0xFFFF:
                    continue
                try:
                    ring.write(frame.data, timestamp_ns=event.timestamp_us * 1000,
                               meta=width << 16 | height)
                    self.participant_frames += 1
                except ValueError:
                    continue  # larger than the ring: skip until the layer drops
        finally:
            await stream.aclose()

    def send_video(self, item: VideoItem) -> None:
        from livekit import rtc

        if self._video_source is None:
            return
        frame = rtc.VideoFrame(item.width, item.height, rtc.VideoBufferType.I420, item.payload)
        self._video_source.capture_frame(frame, timestamp_us=item.timestamp_us)

    async def send_audio(self, item: AudioItem) -> None:
        from livekit import rtc

        await self._audio_source.capture_frame(
            rtc.AudioFrame(item.pcm, SAMPLE_RATE, AUDIO_CHANNELS, item.frames)
        )

    async def close(self) -> None:
        if self._participant_task is not None:
            self._participant_task.cancel()
        if self.room is not None:
            await self.room.disconnect()
        if self._participant_ring is not None:
            self._participant_ring.close()
            self._participant_ring = None
        for resource in (self._hall_source, self._platform_audio):
            if resource is not None:
                resource.close()
        self._hall_source = self._platform_audio = None


def list_audio_devices() -> dict[str, list[dict[str, str]]]:
    """The meeting microphones and speakers WebRTC can open on this computer."""
    from livekit import rtc

    try:
        platform_audio = rtc.PlatformAudio()
    except Exception:  # noqa: BLE001 - native device boundary
        log.warning("Could not list audio devices", exc_info=True)
        return {"recording": [], "playout": []}
    try:
        return {
            "recording": [{"id": d.id, "name": d.name} for d in platform_audio.recording_devices()],
            "playout": [{"id": d.id, "name": d.name} for d in platform_audio.playout_devices()],
        }
    finally:
        platform_audio.close()


async def _publish(request: PublishRequest, out: _Output, commands: asyncio.Queue) -> int:
    video = None if request.audio_only else MediaRingReader(request.video_ring)
    audio = MediaRingReader(request.audio_ring)
    pump = MediaPump(video, audio)
    session = _LiveKitSession(request, out)
    out.send({"state": "connecting"})
    try:
        await session.connect()
    except Exception:  # noqa: BLE001 - report and let the service retry
        log.warning("Could not connect the meeting publisher", exc_info=True)
        out.send({"error": "connect_failed"})
        return 2
    out.send({"state": "publishing"})
    loop = asyncio.get_running_loop()
    next_stats = loop.time() + STATS_INTERVAL_S
    try:
        while not session.closed.is_set():
            while not commands.empty():
                message = commands.get_nowait()
                if message is None or message.get("command") == "stop":
                    return 0
                if message.get("command") == "spotlight":
                    identity = message.get("identity")
                    if isinstance(identity, str) and len(identity) <= 128:
                        session.set_spotlight(identity)
            frame, chunks = pump.drain()
            for chunk in chunks:
                await session.send_audio(chunk)
            if frame is not None:
                session.send_video(frame)
            if loop.time() >= next_stats:
                next_stats += STATS_INTERVAL_S
                out.send({"stats": {
                    "video_frames": pump.video_frames, "audio_chunks": pump.audio_chunks,
                    "skipped_video": pump.skipped_video, "dropped": pump.dropped,
                    "bad_items": pump.bad_items,
                    "hall_playout_tracks": len(getattr(session, "playout_tracks", ())),
                    "participant_frames": getattr(session, "participant_frames", 0),
                }})
            await asyncio.sleep(POLL_INTERVAL_S)
        out.send({"error": "disconnected"})
        return 3
    finally:
        await session.close()
        for ring in (video, audio):
            if ring is not None:
                ring.close()


async def run(out: _Output | None = None, stdin=None) -> int:
    out = out or _Output()
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_stdin_reader, args=(loop, commands, stdin), daemon=True).start()
    out.send({"ready": PROTOCOL_VERSION})
    message = await commands.get()
    if message is not None and message.get("command") == "list_devices":
        out.send({"devices": list_audio_devices()})
        return 0
    if message is None or message.get("command") == "stop":
        return 0
    if message.get("command") != "start":
        out.send({"error": "invalid_command"})
        return 1
    try:
        request = PublishRequest.from_message(message)
    except ValueError:
        out.send({"error": "invalid_request"})
        return 1
    try:
        return await _publish(request, out, commands)
    except (OSError, ValueError):
        log.warning("Meeting publisher could not attach to the egress rings", exc_info=True)
        out.send({"error": "rings_unavailable"})
        return 1


def _private_protocol_stream():
    """Keep stdout for the protocol only: the native WebRTC library prints to fd 1."""
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(_Output(_private_protocol_stream())))


if __name__ == "__main__":
    raise SystemExit(main())
