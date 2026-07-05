"""Qt-free playback session state for media streaming and local handoff."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalSwitchRequest:
    local_path: str
    local_is_temp: bool
    notify_cache: bool = True


@dataclass(frozen=True)
class DownloadFinishedDecision:
    local_path: str
    local_is_temp: bool
    deferred: bool
    notify_cache_now: bool = False

    @property
    def switch_request(self) -> LocalSwitchRequest | None:
        if self.deferred:
            return None
        return LocalSwitchRequest(self.local_path, self.local_is_temp)


class MediaPlaybackSession:
    """Owns playback session state without depending on native media widgets."""

    def __init__(self) -> None:
        self.current_url = ""
        self.local_path: str | None = None
        self.local_is_temp = False
        self.defer_local_switch = False
        self.stream_persist = True
        self.cover_emitted = False
        self.requested_playing = False
        self.session_id = 0
        self.frame_session = 0
        self._pending_local_switch: tuple[str, bool] | None = None
        self._pending_local_notified = False

    def begin_playback(self, url: str, *, requested_playing: bool = True) -> None:
        self.current_url = url
        self.local_path = None
        self.local_is_temp = False
        self._pending_local_switch = None
        self._pending_local_notified = False
        self.cover_emitted = False
        self.requested_playing = requested_playing
        self.session_id += 1
        self.frame_session = self.session_id

    def begin_stop(self) -> None:
        self.session_id += 1
        self.requested_playing = False

    def finish_stop(self) -> None:
        self.current_url = ""
        self.local_path = None
        self.local_is_temp = False
        self.defer_local_switch = False
        self._pending_local_switch = None
        self._pending_local_notified = False

    def stop(self) -> None:
        self.begin_stop()
        self.finish_stop()

    def set_requested_playing(self, requested: bool) -> None:
        self.requested_playing = requested

    def set_stream_persist(self, persist: bool) -> None:
        self.stream_persist = persist

    def set_cached_local(self, local_path: str) -> None:
        self.local_path = local_path
        self.local_is_temp = False

    def set_local_switch_deferred(self, deferred: bool) -> LocalSwitchRequest | None:
        self.defer_local_switch = deferred
        if deferred or self._pending_local_switch is None:
            return None

        local_path, local_is_temp = self._pending_local_switch
        notify_cache = not self._pending_local_notified
        self._pending_local_switch = None
        self._pending_local_notified = False
        return LocalSwitchRequest(local_path, local_is_temp, notify_cache)

    def download_finished(self, local_path: str) -> DownloadFinishedDecision:
        local_is_temp = not self.stream_persist
        self.local_path = local_path
        self.local_is_temp = local_is_temp

        if not self.defer_local_switch:
            return DownloadFinishedDecision(
                local_path=local_path,
                local_is_temp=local_is_temp,
                deferred=False,
            )

        self._pending_local_switch = (local_path, local_is_temp)
        self._pending_local_notified = False
        notify_cache_now = self.stream_persist and bool(self.current_url)
        if notify_cache_now:
            self._pending_local_notified = True
        return DownloadFinishedDecision(
            local_path=local_path,
            local_is_temp=local_is_temp,
            deferred=True,
            notify_cache_now=notify_cache_now,
        )

    def mark_local_switch(self, local_path: str, local_is_temp: bool) -> None:
        self.local_path = local_path
        self.local_is_temp = local_is_temp

    def should_notify_cache(self, *, requested: bool) -> bool:
        return requested and self.stream_persist and bool(self.current_url)

    def current_temp_path(self) -> str | None:
        if self.local_is_temp and self.local_path:
            return self.local_path
        return None

    def reconnect_source(self) -> str:
        return self.local_path or self.current_url

    def accepts_frame(self) -> bool:
        return self.session_id == self.frame_session

    def mark_media_ended(self) -> None:
        self.requested_playing = False

    def mark_cover_emitted(self) -> None:
        self.cover_emitted = True
