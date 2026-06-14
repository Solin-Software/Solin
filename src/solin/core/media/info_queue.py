"""Qt-free scheduling state for media metadata extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class MediaInfoVersion:
    generation: int
    revision: int
    index: int


@dataclass(frozen=True, slots=True)
class MediaInfoJob:
    generation: int
    revision: int
    index: int
    url: str
    media_type: str


class MediaInfoScheduler:
    """Owns queued/active metadata jobs and stale-result rejection."""

    def __init__(self, *, max_concurrent: int = 2) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.max_concurrent = max_concurrent
        self.generation = 0
        self.index_revisions: dict[int, int] = {}
        self.pending: list[MediaInfoJob] = []
        self.active: dict[int, MediaInfoJob] = {}

    def is_scheduled(self, index: int) -> bool:
        return index in self.active or any(job.index == index for job in self.pending)

    def version_for(self, index: int) -> MediaInfoVersion:
        return MediaInfoVersion(
            generation=self.generation,
            revision=self.index_revisions.get(index, 0),
            index=index,
        )

    def is_current(self, version: MediaInfoVersion) -> bool:
        return (
            version.generation == self.generation
            and version.revision == self.index_revisions.get(version.index, 0)
        )

    def enqueue(self, index: int, url: str, media_type: str) -> MediaInfoJob:
        active = self.active.get(index)
        if active is not None:
            return active
        for pending in self.pending:
            if pending.index == index:
                return pending

        job = MediaInfoJob(
            generation=self.generation,
            revision=self.index_revisions.get(index, 0),
            index=index,
            url=url,
            media_type=media_type,
        )
        self.pending.append(job)
        return job

    def pump(self, *, blocked_indices: Iterable[int] = ()) -> list[MediaInfoJob]:
        blocked = set(blocked_indices)
        started: list[MediaInfoJob] = []
        while self.pending and len(self.active) < self.max_concurrent:
            job = self.pending.pop(0)
            if job.generation != self.generation:
                continue
            if job.revision != self.index_revisions.get(job.index, 0):
                continue
            if job.index in blocked or job.index in self.active:
                continue
            self.active[job.index] = job
            started.append(job)
        return started

    def accepts_result(self, job: MediaInfoJob, index: int) -> bool:
        return (
            job.index == index
            and self.is_current(
                MediaInfoVersion(
                    generation=job.generation,
                    revision=job.revision,
                    index=job.index,
                )
            )
            and self.active.get(index) is job
        )

    def complete(self, job: MediaInfoJob, index: int) -> bool:
        if not self.accepts_result(job, index):
            return False
        self.active.pop(index, None)
        return True

    def fail(self, job: MediaInfoJob, index: int) -> bool:
        return self.complete(job, index)

    def invalidate(self, index: int) -> list[MediaInfoJob]:
        self.index_revisions[index] = self.index_revisions.get(index, 0) + 1
        self.pending = [job for job in self.pending if job.index != index]
        active = self.active.pop(index, None)
        return [active] if active is not None else []

    def clear(self) -> list[MediaInfoJob]:
        self.generation += 1
        self.pending.clear()
        self.index_revisions.clear()
        active_jobs = list(self.active.values())
        self.active.clear()
        return active_jobs

    def shutdown(self) -> list[MediaInfoJob]:
        return self.clear()
