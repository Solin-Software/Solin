"""Process-local shared/exclusive serialization for background resources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Condition, Lock, RLock, get_ident
from typing import TypeVar

from solin.core.foundation.resource_keys import ResourceClaim


_T = TypeVar("_T")


@dataclass(slots=True)
class _Lane:
    condition: Condition = field(default_factory=lambda: Condition(RLock()))
    readers: dict[int, int] = field(default_factory=dict)
    writer: int | None = None
    write_depth: int = 0
    waiting_writers: int = 0
    users: int = 0

    def acquire_shared(self) -> None:
        owner = get_ident()
        with self.condition:
            if self.writer == owner or owner in self.readers:
                self.readers[owner] = self.readers.get(owner, 0) + 1
                return
            while self.writer is not None or self.waiting_writers:
                self.condition.wait()
            self.readers[owner] = 1

    def release_shared(self) -> None:
        owner = get_ident()
        with self.condition:
            depth = self.readers.get(owner, 0)
            if depth <= 0:
                raise RuntimeError("Shared resource lane released by a non-owner")
            if depth == 1:
                self.readers.pop(owner)
            else:
                self.readers[owner] = depth - 1
            self.condition.notify_all()

    def acquire_exclusive(self) -> None:
        owner = get_ident()
        with self.condition:
            if self.writer == owner:
                self.write_depth += 1
                return
            if owner in self.readers:
                raise RuntimeError("Resource lane upgrades are not supported")
            self.waiting_writers += 1
            try:
                while self.writer is not None or self.readers:
                    self.condition.wait()
                self.writer = owner
                self.write_depth = 1
            finally:
                self.waiting_writers -= 1

    def release_exclusive(self) -> None:
        owner = get_ident()
        with self.condition:
            if self.writer != owner:
                raise RuntimeError("Exclusive resource lane released by a non-owner")
            self.write_depth -= 1
            if self.write_depth == 0:
                self.writer = None
                self.condition.notify_all()


class ResourceLaneRegistry:
    """Coordinate exact and hierarchical work without blocking sibling folders."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._lanes: dict[str, _Lane] = {}

    def run(
        self,
        claim: str | ResourceClaim,
        action: Callable[[], _T],
    ) -> _T:
        if not callable(action):
            raise ValueError("Resource-lane work requires an action")
        normalized = self._normalize_claim(claim)
        acquisitions = [(key, True) for key in normalized.shared_keys]
        if normalized.exclusive_key:
            acquisitions.append((normalized.exclusive_key, False))
        acquisitions.sort(key=lambda acquisition: acquisition[0])
        lanes = self._retain_lanes(key for key, _shared in acquisitions)
        acquired: list[tuple[_Lane, bool]] = []
        try:
            for (key, shared) in acquisitions:
                lane = lanes[key]
                if shared:
                    lane.acquire_shared()
                else:
                    lane.acquire_exclusive()
                acquired.append((lane, shared))
            return action()
        finally:
            for lane, shared in reversed(acquired):
                if shared:
                    lane.release_shared()
                else:
                    lane.release_exclusive()
            self._release_lanes(lanes)

    @staticmethod
    def _normalize_claim(claim: str | ResourceClaim) -> ResourceClaim:
        if isinstance(claim, ResourceClaim):
            return claim
        if not isinstance(claim, str) or not claim:
            raise ValueError("Resource-lane work requires a key or claim")
        return ResourceClaim(exclusive_key=claim)

    def _retain_lanes(self, keys) -> dict[str, _Lane]:
        retained: dict[str, _Lane] = {}
        with self._guard:
            for key in keys:
                lane = self._lanes.setdefault(key, _Lane())
                lane.users += 1
                retained[key] = lane
        return retained

    def _release_lanes(self, lanes: dict[str, _Lane]) -> None:
        with self._guard:
            for key, lane in lanes.items():
                lane.users -= 1
                if lane.users == 0 and self._lanes.get(key) is lane:
                    self._lanes.pop(key, None)


__all__ = ["ResourceLaneRegistry"]
