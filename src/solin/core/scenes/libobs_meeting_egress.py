"""Meeting egress for the scene-engine sidecar (libobs → meeting publisher process).

While a Solin-hosted meeting is live the app hands the sidecar a
:class:`~solin.core.scenes.engine.MeetingEgressDescriptor` naming two shared-memory rings
it created. This component registers two libobs raw callbacks and copies:

* the program video, converted by libobs to tightly packed I420 at the meeting size;
* the ``audio_mix`` libobs mix (track 6: media + hall feed) as planar float32;

into those rings, where the isolated publisher process picks them up. The callbacks run on
libobs' video/audio threads, so they only take a short lock to read the current writers and
copy bytes — no other component locks, no blocking.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from solin.core.conference.media_ring import MediaRingWriter
from solin.core.scenes.engine import MeetingEgressDescriptor

log = logging.getLogger(__name__)

AUDIO_CHANNELS = 2


def pack_i420(planes: list[bytes], linesizes: list[int], width: int, height: int) -> bytes:
    """Pack possibly strided I420 planes into one contiguous buffer."""
    chroma_width, chroma_height = width // 2, height // 2
    dims = ((width, height), (chroma_width, chroma_height), (chroma_width, chroma_height))
    if all(int(linesizes[i]) == dims[i][0] for i in range(3)):
        return b"".join(planes[i][: dims[i][0] * dims[i][1]] for i in range(3))
    out: list[bytes] = []
    for i, (plane_width, plane_height) in enumerate(dims):
        stride = int(linesizes[i])
        data = memoryview(planes[i])
        out.extend(bytes(data[row * stride : row * stride + plane_width]) for row in range(plane_height))
    return b"".join(out)


class LibobsMeetingEgress:
    """Feeds the meeting publisher's rings from libobs while a descriptor is configured."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._descriptor: MeetingEgressDescriptor | None = None
        self._video: MediaRingWriter | None = None
        self._audio: MediaRingWriter | None = None
        self._video_callback: Any = None
        self._audio_callback: Any = None
        self.video_frames = 0
        self.audio_chunks = 0
        self.write_errors = 0

    @property
    def active(self) -> bool:
        return self._descriptor is not None

    def configure(self, record: object) -> None:
        """Attach to the rings named by ``record`` (a descriptor record), or detach on None."""
        descriptor: MeetingEgressDescriptor | None = None
        if isinstance(record, dict):
            try:
                descriptor = MeetingEgressDescriptor(**record)
            except (TypeError, ValueError):
                log.warning("Ignoring an invalid meeting egress descriptor", exc_info=True)
        if descriptor == self._descriptor:
            return
        self._teardown()
        if descriptor is None:
            return
        try:
            video = MediaRingWriter(descriptor.video_ring)
            audio = MediaRingWriter(descriptor.audio_ring)
        except (OSError, ValueError):
            log.warning("Could not attach to the meeting egress rings", exc_info=True)
            return
        with self._lock:
            self._video, self._audio, self._descriptor = video, audio, descriptor
        ob = self._runtime.ob
        try:
            self._video_callback = ob.add_raw_video_callback(
                self._on_video,
                format=int(ob.VideoFormat.I420),
                width=descriptor.width,
                height=descriptor.height,
            )
            self._audio_callback = ob.add_raw_audio_callback(
                self._on_audio, mix_idx=descriptor.audio_mix, channels=AUDIO_CHANNELS
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not register the meeting egress callbacks", exc_info=True)
            self._teardown()

    def _on_video(self, planes, linesizes, width, height, _fmt, timestamp_ns) -> None:
        # libobs video thread: copy and return.
        with self._lock:
            ring, descriptor = self._video, self._descriptor
        if ring is None or descriptor is None or not planes:
            return
        try:
            frame = pack_i420(planes, linesizes, descriptor.width, descriptor.height)
            ring.write(frame, timestamp_ns=int(timestamp_ns), meta=descriptor.width << 16 | descriptor.height)
            self.video_frames += 1
        except Exception:  # noqa: BLE001 - shared-memory boundary
            self.write_errors += 1

    def _on_audio(self, _mix, planes, frames, timestamp_ns) -> None:
        # libobs audio thread: copy and return.
        with self._lock:
            ring = self._audio
        if ring is None or not planes or frames <= 0:
            return
        try:
            channels = planes[:AUDIO_CHANNELS]
            if len(channels) == 1 or not channels[1]:
                channels = [channels[0], channels[0]]
            ring.write(b"".join(channels), timestamp_ns=int(timestamp_ns),
                       meta=int(frames) | AUDIO_CHANNELS << 16)
            self.audio_chunks += 1
        except Exception:  # noqa: BLE001 - shared-memory boundary
            self.write_errors += 1

    def _teardown(self) -> None:
        ob = self._runtime.ob
        video_callback, self._video_callback = self._video_callback, None
        audio_callback, self._audio_callback = self._audio_callback, None
        with self._lock:
            descriptor = self._descriptor
            video, audio = self._video, self._audio
            self._video = self._audio = None
            self._descriptor = None
        if video_callback is not None:
            try:
                ob.remove_raw_video_callback(video_callback)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("meeting video callback removal errored", exc_info=True)
        if audio_callback is not None and descriptor is not None:
            try:
                ob.remove_raw_audio_callback(audio_callback, mix_idx=descriptor.audio_mix)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("meeting audio callback removal errored", exc_info=True)
        for ring in (video, audio):
            if ring is not None:
                try:
                    ring.close()
                except Exception:  # noqa: BLE001 - best-effort
                    log.debug("meeting ring close errored", exc_info=True)

    def shutdown(self) -> None:
        self._teardown()
