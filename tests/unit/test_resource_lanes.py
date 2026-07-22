from __future__ import annotations

import threading

from solin.core.foundation.resource_keys import (
    ResourceClaim,
    child_folder_resource_claim,
    folder_resource_key,
)
from solin.core.foundation.resource_lanes import ResourceLaneRegistry


def test_sibling_folder_claims_run_concurrently(tmp_path) -> None:
    lanes = ResourceLaneRegistry()
    watched_root = tmp_path / "watched"
    releases = [threading.Event(), threading.Event()]
    started = [threading.Event(), threading.Event()]

    def run(index: int) -> None:
        lanes.run(
            child_folder_resource_claim(watched_root / f"playlist-{index}"),
            lambda: (started[index].set(), releases[index].wait(1)),
        )

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()

    assert all(event.wait(1) for event in started)
    for event in releases:
        event.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()


def test_root_mutation_waits_for_child_claim(tmp_path) -> None:
    lanes = ResourceLaneRegistry()
    watched_root = tmp_path / "watched"
    child_started = threading.Event()
    release_child = threading.Event()
    mutation_started = threading.Event()

    child = threading.Thread(
        target=lambda: lanes.run(
            child_folder_resource_claim(watched_root / "playlist"),
            lambda: (child_started.set(), release_child.wait(1)),
        )
    )
    mutation = threading.Thread(
        target=lambda: lanes.run(
            folder_resource_key(watched_root),
            mutation_started.set,
        )
    )
    child.start()
    assert child_started.wait(1)
    mutation.start()

    assert not mutation_started.wait(0.05)
    release_child.set()
    child.join(1)
    mutation.join(1)

    assert mutation_started.is_set()
    assert not child.is_alive()
    assert not mutation.is_alive()


def test_inverse_claims_follow_one_global_lock_order() -> None:
    lanes = ResourceLaneRegistry()
    start = threading.Barrier(3)
    completed: list[str] = []

    def run(name: str, claim: ResourceClaim) -> None:
        start.wait()
        lanes.run(claim, lambda: completed.append(name))

    threads = [
        threading.Thread(
            target=run,
            args=("first", ResourceClaim(exclusive_key="b", shared_keys=("a",))),
        ),
        threading.Thread(
            target=run,
            args=("second", ResourceClaim(exclusive_key="a", shared_keys=("b",))),
        ),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(1)

    assert sorted(completed) == ["first", "second"]
    assert all(not thread.is_alive() for thread in threads)
