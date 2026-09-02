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
from typing import Any

log = logging.getLogger(__name__)

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


def _is_remote(path: str) -> bool:
    return path.startswith(_REMOTE_SCHEMES)


class LibobsMediaSource:
    """An ``ffmpeg_source`` wrapper with Qt-free transport + state."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._source: Any = None
        self._path = ""

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
        self.set_volume(volume_percent)
        try:
            from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY

            self._runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
        except Exception:  # noqa: BLE001 - monitoring is best-effort
            log.debug("Could not set media source monitoring", exc_info=True)
        source.media_play_pause(not autoplay)
        return True

    def set_volume(self, volume_percent: int) -> None:
        """Set the source volume (100 = unity gain)."""
        if self._source is None:
            return
        try:
            self._source.volume = max(0.0, int(volume_percent) / 100.0)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media volume", exc_info=True)

    def set_speed(self, speed_percent: int) -> None:
        """Set the playback rate (100 = normal). Applied on the next decode pass."""
        if self._source is None:
            return
        try:
            self._source.update({"speed_percent": max(1, int(speed_percent))})
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set media speed", exc_info=True)

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
