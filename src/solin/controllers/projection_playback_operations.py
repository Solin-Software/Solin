"""Owner-thread playback state and commands shared by projection controls."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, auto
import random
from types import MappingProxyType
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from ..core.foundation.constants import ORDER_NEXT, ORDER_OFF, ORDER_RANDOM
from ..core.media.playback import MediaController
from ..core.media.settings import ProjectionPlaybackSettingsStore
from .playback_protection_controller import PlaybackProtectionController


@dataclass(frozen=True, slots=True)
class ProjectionQueue:
    items: tuple[Mapping[str, Any], ...] = ()
    index: int = 0
    played: frozenset[int] = frozenset()
    order: str = ORDER_OFF
    from_saved_playlist: bool = False

    @property
    def current_item(self) -> Mapping[str, Any] | None:
        return self.items[self.index] if self.items else None

    def select(self, index: int, *, restart_cycle: bool = False) -> ProjectionQueue:
        played = frozenset({index}) if restart_cycle else self.played | {index}
        return replace(self, index=index, played=played)


class PlaybackEndAction(Enum):
    STOP = auto()
    REPLAY = auto()


class _Announcement(Enum):
    OFF = auto()
    LEAD_IN = auto()
    READY = auto()


class _ShareWait(Enum):
    OFF = auto()
    RESUME = auto()
    STAY_PAUSED = auto()


class ProjectionPlaybackOperations(QObject):
    changed = Signal()
    queue_replaced = Signal()
    _ANNOUNCEMENT_LEAD_IN_MS = 3500

    def __init__(
        self,
        media: MediaController,
        settings: ProjectionPlaybackSettingsStore,
        protection: PlaybackProtectionController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._media = media
        self._settings = settings
        self._protection = protection
        self._queue = ProjectionQueue(order=settings.playback_order())
        self._loop = settings.loop_enabled()
        self._announcement = _Announcement.OFF
        self._share_wait = _ShareWait.OFF
        volume = settings.volume()
        self._pre_mute_volume = volume or 0.8
        media.set_volume(volume)
        media.set_playback_rate(settings.speed())
        media.volume_changed.connect(self._on_volume_changed)
        media.position_changed.connect(self._on_position_changed)
        protection.lockedChanged.connect(self.changed)

    @property
    def queue(self) -> ProjectionQueue:
        return self._queue

    def new_queue(
        self,
        items: Iterable[Mapping[str, Any]],
        order: str | None = None,
        *,
        index: int = 0,
        from_saved_playlist: bool = False,
    ) -> ProjectionQueue:
        entries = tuple(MappingProxyType(dict(item)) for item in items)
        if not 0 <= index < max(1, len(entries)):
            raise ValueError("Queue selection is out of range")
        return ProjectionQueue(
            items=entries,
            index=index,
            played=frozenset({index}) if entries else frozenset(),
            order=(
                order
                if order in (ORDER_OFF, ORDER_NEXT, ORDER_RANDOM)
                else self._settings.playback_order()
            ),
            from_saved_playlist=from_saved_playlist,
        )

    def commit_queue(self, queue: ProjectionQueue) -> None:
        previous = self._queue
        if queue is previous:
            return
        self._queue = queue
        if previous.items is not queue.items:
            self.queue_replaced.emit()
        self.changed.emit()

    def clear_queue(self) -> None:
        if self.queue.items:
            self.commit_queue(self.new_queue(()))

    def reset_projection(self) -> None:
        self.reset_transport()
        self.clear_queue()

    def allow_manual_projection_change(self, *, notify: bool = True) -> bool:
        return self._protection.allow_manual_projection_change(notify=notify)

    def can_navigate(self, offset: int) -> bool:
        queue = self.queue
        return not self.protection_locked and 0 <= queue.index + offset < len(queue.items)

    @property
    def loop(self) -> bool:
        return self._loop

    def toggle_loop(self) -> None:
        self._loop = not self._loop
        self._settings.set_loop_enabled(self._loop)
        self.changed.emit()

    def set_order(self, order: str) -> None:
        if order not in (ORDER_OFF, ORDER_NEXT, ORDER_RANDOM):
            return
        self._settings.set_playback_order(order)
        queue = replace(self.queue, order=order)
        self.commit_queue(queue.select(queue.index, restart_cycle=True) if queue.items else queue)

    @property
    def has_pending_automatic_playback(self) -> bool:
        if self.loop:
            return True
        queue = self.queue
        if queue.order == ORDER_NEXT:
            return queue.index + 1 < len(queue.items)
        if queue.order == ORDER_RANDOM:
            return len(queue.played) < len(queue.items)
        return False

    def after_media_ended(self) -> PlaybackEndAction | ProjectionQueue:
        queue = self.queue
        count = len(queue.items)
        when_exhausted = PlaybackEndAction.REPLAY if self.loop else PlaybackEndAction.STOP
        if count <= 1 or queue.order == ORDER_OFF:
            return when_exhausted
        if queue.order == ORDER_NEXT:
            next_index = queue.index + 1
            if next_index < count:
                return queue.select(next_index)
            return queue.select(0) if self.loop else when_exhausted

        remaining = [index for index in range(count) if index not in queue.played]
        if remaining:
            return queue.select(random.choice(remaining))
        if self.loop:
            return queue.select(random.randrange(count), restart_cycle=True)
        return when_exhausted

    @property
    def speed(self) -> float:
        return self._media.playback_rate

    def set_speed(self, rate: float) -> None:
        rate = max(0.5, min(2.0, rate))
        if rate == self.speed:
            return
        self._settings.set_speed(rate)
        self._media.set_playback_rate(rate)
        self.changed.emit()

    @property
    def volume(self) -> float:
        return self._media.volume

    def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, volume))
        if volume != self.volume:
            self._settings.set_volume(volume)
            self._media.set_volume(volume)

    def toggle_mute(self) -> None:
        self.set_volume(0.0 if self.volume else self._pre_mute_volume)

    @Slot(float)
    def _on_volume_changed(self, volume: float) -> None:
        if volume > 0:
            self._pre_mute_volume = volume
        self.changed.emit()

    @property
    def transport_blocked(self) -> bool:
        return self._share_wait is not _ShareWait.OFF or self._announcement is _Announcement.LEAD_IN

    @property
    def protection_locked(self) -> bool:
        return bool(self._protection.locked)

    @property
    def seek_blocked(self) -> bool:
        return (
            self._share_wait is not _ShareWait.OFF
            or self._announcement is not _Announcement.OFF
            or self.protection_locked
        )

    def toggle(self) -> None:
        if self.transport_blocked:
            return
        if self._announcement is _Announcement.READY:
            self.resume()
        else:
            self._media.toggle_play_pause()

    def pause(self) -> bool:
        if self.transport_blocked:
            return False
        self._media.pause()
        return True

    def resume(self) -> bool:
        if self.transport_blocked:
            return False
        restart_announcement = self._announcement is _Announcement.READY
        if restart_announcement:
            self._announcement = _Announcement.OFF
            self._media.seek(0)
            self._media.set_audio_suppressed(False)
        self._media.play()
        if restart_announcement:
            self.changed.emit()
        return True

    def seek(self, position: int) -> bool:
        if self.seek_blocked:
            return False
        return self._protection.request_seek(position)

    def begin_auto_share_wait(self, *, resume_on_success: bool = True) -> None:
        self._share_wait = _ShareWait.RESUME if resume_on_success else _ShareWait.STAY_PAUSED
        self.changed.emit()

    def resolve_auto_share_wait(self, success: bool) -> None:
        wait = self._share_wait
        if wait is _ShareWait.OFF:
            return
        self._share_wait = _ShareWait.OFF
        if self._announcement is _Announcement.LEAD_IN or (success and wait is _ShareWait.RESUME):
            self._media.play()
        self.changed.emit()

    def cancel_auto_share_wait(self) -> None:
        if self._share_wait is not _ShareWait.OFF:
            self._share_wait = _ShareWait.OFF
            self.changed.emit()

    def begin_announcement(self) -> None:
        self._announcement = _Announcement.LEAD_IN
        self._media.set_audio_suppressed(True)
        self.changed.emit()

    @Slot(int)
    def _on_position_changed(self, position: int) -> None:
        if (
            self._share_wait is _ShareWait.OFF
            and self._announcement is _Announcement.LEAD_IN
            and position >= self._ANNOUNCEMENT_LEAD_IN_MS
        ):
            self._announcement = _Announcement.READY
            self._media.pause()
            self.changed.emit()

    def reset_transport(self) -> None:
        announcement = self._announcement
        if announcement is _Announcement.OFF and self._share_wait is _ShareWait.OFF:
            return
        self._announcement = _Announcement.OFF
        self._share_wait = _ShareWait.OFF
        if announcement is not _Announcement.OFF:
            self._media.set_audio_suppressed(False)
        self.changed.emit()
