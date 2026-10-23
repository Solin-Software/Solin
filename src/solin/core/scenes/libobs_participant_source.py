"""The remote meeting participant's video, inside libobs (scene-engine sidecar).

The meeting publisher subscribes to the spotlighted remote part-holder and writes the
decoded frames (packed I420) into a shared-memory ring the app created; the app hands the
ring to the sidecar as a :class:`~solin.core.scenes.engine.ParticipantIngressDescriptor`.
This component owns one long-lived, video-only ``solin_frame_source`` that every
"Remote participant (meeting)" layer borrows, and a thread that pushes the newest frame.

It is deliberately separate from the content ingress: no presentation epochs, no
prepare/take — the participant appears and disappears as frames arrive. The source has no
audio: the participant is already heard in the hall through the meeting speaker and by
attendees directly, so nothing here may add them to a libobs mix.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from solin.core.conference.media_ring import MediaRingReader
from solin.core.scenes.engine import ParticipantIngressDescriptor

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1 / 120
STALE_AFTER_S = 1.5


def _default_frame_source(runtime: Any) -> Any:
    from solin.core.media.obs_frame_source import create_frame_source

    return create_frame_source(runtime, "solin-meeting-participant")


class LibobsParticipantVideo:
    def __init__(self, runtime: Any, *,
                 frame_source_factory: Callable[[Any], Any] = _default_frame_source,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._runtime = runtime
        self._factory = frame_source_factory
        # Created on the first participant ingress (a meeting), then kept: layers
        # borrow it, and hydrate configures ingress before the scene graph is built.
        self._frame_source: Any = None
        self._clock = clock
        self._lock = threading.Lock()
        self._descriptor: ParticipantIngressDescriptor | None = None
        self._ring: MediaRingReader | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.frames = 0

    @property
    def source(self) -> Any | None:
        """The libobs source scene layers borrow (None when the frame source is missing)."""
        return self._frame_source.source if self._frame_source is not None else None

    def configure(self, record: object) -> None:
        descriptor: ParticipantIngressDescriptor | None = None
        if isinstance(record, dict):
            try:
                descriptor = ParticipantIngressDescriptor(**record)
            except (TypeError, ValueError):
                log.warning("Ignoring an invalid participant ingress descriptor", exc_info=True)
        if descriptor == self._descriptor:
            return
        self._detach()
        if descriptor is None or not self._ensure_frame_source():
            return
        try:
            ring = MediaRingReader(descriptor.video_ring)
        except (OSError, ValueError):
            log.warning("Could not attach to the participant video ring", exc_info=True)
            return
        with self._lock:
            self._ring, self._descriptor = ring, descriptor
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, name="solin-participant-video",
                                        daemon=True)
        self._thread.start()

    def _ensure_frame_source(self) -> bool:
        if self._frame_source is None:
            try:
                self._frame_source = self._factory(self._runtime)
            except Exception:  # noqa: BLE001 - libobs source-creation boundary
                log.warning("Could not create the participant video source", exc_info=True)
                return False
            if self._frame_source is None:
                return False
            _silence(self._frame_source.source)
        return True

    def _pump(self) -> None:
        last_frame_at: float | None = None
        while not self._stop.is_set():
            with self._lock:
                ring = self._ring
            if ring is None:
                return
            newest = None
            while (item := ring.read_next()) is not None:
                newest = item
            now = self._clock()
            if newest is not None:
                width, height = newest.meta >> 16, newest.meta & 0xFFFF
                if self._frame_source.push_i420(newest.payload, width, height):
                    self.frames += 1
                    last_frame_at = now
            elif last_frame_at is not None and now - last_frame_at > STALE_AFTER_S:
                # The spotlight ended (or the part-holder dropped): show nothing rather
                # than a frozen face on the hall monitors.
                self._frame_source.clear()
                last_frame_at = None
            self._stop.wait(POLL_INTERVAL_S)

    def _detach(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2)
        with self._lock:
            ring, self._ring = self._ring, None
            self._descriptor = None
        if ring is not None:
            ring.close()
        if self._frame_source is not None:
            self._frame_source.clear()

    def shutdown(self) -> None:
        self._detach()
        frame_source, self._frame_source = self._frame_source, None
        if frame_source is not None:
            frame_source.release()


def _silence(source: Any) -> None:
    """Keep the participant out of every libobs mix and off monitoring."""
    try:
        source.audio_mixers = 0
        source.muted = True
    except Exception:  # noqa: BLE001 - libobs boundary
        log.debug("could not silence the participant source", exc_info=True)
