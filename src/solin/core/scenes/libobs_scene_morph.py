"""The merge transition: morph what two scenes share, fade what they do not.

libobs has no such transition, so a merge composes one. A private scene holds the
union of both layer stacks: shared layers animate from the origin's geometry to the
destination's, and every other layer fades on its own through libobs' per-item show
and hide transitions. It replaces the origin on the output with a cut, because its
first frame looks like the origin, and the destination replaces it the same way when
its last frame looks like the destination.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import enum
import logging
import time
from typing import Any

from solin.core.scenes.model import Crop, NormalizedRect

log = logging.getLogger(__name__)

MERGE_TRANSITION_KIND = "merge"
_ITEM_FADE_SOURCE_ID = "fade_transition"


class MorphRole(enum.Enum):
    MOVE = "move"
    FADE_OUT = "fade_out"
    FADE_IN = "fade_in"


@dataclass(frozen=True)
class MorphEntry:
    """One item of the merged stack. ``record`` supplies the source it shows."""

    role: MorphRole
    record: dict[str, Any]
    destination: dict[str, Any] | None = None


def _same_native(left: Any, right: Any) -> bool:
    return getattr(left, "_ptr", left) == getattr(right, "_ptr", right)


def _is_shared(origin: dict[str, Any], destination: dict[str, Any]) -> bool:
    """Whether a destination layer continues an origin layer on screen.

    Images and colors are built per layer, so the document's source id identifies
    them. Content and nested scenes are generational: the same id can show other
    pixels after a Take, and then only a fade is honest.
    """
    if origin["layer"].get("source_id") != destination["layer"].get("source_id"):
        return False
    if origin["is_content"] or origin["referenced_scene_id"]:
        return _same_native(origin["item"].source, destination["item"].source)
    return True


def plan_scene_morph(
    origin: Sequence[dict[str, Any]],
    destination: Sequence[dict[str, Any]],
) -> tuple[MorphEntry, ...]:
    """Merge two bottom-to-top layer stacks, following the destination's order.

    A layer repeated in a scene pairs in stacking order. An origin-only layer stays
    directly above the origin layer it sat on, so it keeps covering the same content
    while it fades.
    """
    unmatched = list(origin)
    pairs: dict[int, dict[str, Any]] = {}
    for record in destination:
        match = next((other for other in unmatched if _is_shared(other, record)), None)
        if match is not None:
            unmatched.remove(match)
            pairs[id(record)] = match
    entries = [
        MorphEntry(MorphRole.MOVE, pairs[id(record)], record)
        if id(record) in pairs
        else MorphEntry(MorphRole.FADE_IN, record)
        for record in destination
    ]
    position_of_origin = {
        id(entry.record): index
        for index, entry in enumerate(entries)
        if entry.role is MorphRole.MOVE
    }
    insert_at = 0
    for record in origin:
        if id(record) in position_of_origin:
            insert_at = next(
                index + 1
                for index, entry in enumerate(entries)
                if entry.record is record
            )
        else:
            entries.insert(insert_at, MorphEntry(MorphRole.FADE_OUT, record))
            insert_at += 1
    return tuple(entries)


def _ease(progress: float) -> float:
    clamped = min(1.0, max(0.0, progress))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _mix(start: float, end: float, amount: float) -> float:
    return start + (end - start) * amount


def interpolate_layer(start: dict, end: dict, amount: float) -> dict:
    """The layer ``amount`` of the way from ``start`` to ``end``.

    Rotation takes the short way round. Mirroring and fit mode have no in-between,
    so they switch halfway.
    """
    start_rect = NormalizedRect.from_record(start.get("rect") or NormalizedRect().to_record())
    end_rect = NormalizedRect.from_record(end.get("rect") or NormalizedRect().to_record())
    start_crop = Crop.from_record(start.get("crop") or {})
    end_crop = Crop.from_record(end.get("crop") or {})
    start_rotation = float(start.get("rotation_degrees", 0.0))
    turn = (float(end.get("rotation_degrees", 0.0)) - start_rotation + 180.0) % 360.0 - 180.0
    discrete = start if amount < 0.5 else end
    return {
        "rect": {
            "x": _mix(start_rect.x, end_rect.x, amount),
            "y": _mix(start_rect.y, end_rect.y, amount),
            "width": _mix(start_rect.width, end_rect.width, amount),
            "height": _mix(start_rect.height, end_rect.height, amount),
        },
        "crop": {
            "left": _mix(start_crop.left, end_crop.left, amount),
            "top": _mix(start_crop.top, end_crop.top, amount),
            "right": _mix(start_crop.right, end_crop.right, amount),
            "bottom": _mix(start_crop.bottom, end_crop.bottom, amount),
        },
        "rotation_degrees": start_rotation + turn * amount,
        "fit_mode": discrete.get("fit_mode"),
        "mirror_x": discrete.get("mirror_x", False),
        "mirror_y": discrete.get("mirror_y", False),
    }


def _start_item_fade(item: Any, transition: Any, *, show: bool, duration_ms: int) -> None:
    from pylibobs._ffi import get_lib

    # CFFI resolves these unwrapped symbols dynamically.
    lib: Any = get_lib()
    lib.obs_sceneitem_set_transition(item._ptr, show, transition._ptr)
    lib.obs_sceneitem_set_transition_duration(item._ptr, show, duration_ms)
    lib.obs_sceneitem_do_transition(item._ptr, show)
    item.visible = show


class LibobsSceneMorph:
    """One merge in flight, from its private scene to the destination's cut."""

    def __init__(
        self,
        runtime: Any,
        name: str,
        plan: Sequence[MorphEntry],
        destination_source: Any,
        duration_ms: int,
        apply_geometry: Callable[[Any, dict], None],
        forget_item: Callable[[Any], None],
        complete: Callable[[], None],
    ) -> None:
        self.destination_source = destination_source
        self._runtime = runtime
        self._name = name
        self._plan = tuple(plan)
        self._duration_ms = duration_ms
        self._apply_geometry = apply_geometry
        self._forget_item = forget_item
        self._complete = complete
        self._scene: Any = None
        self._moving: list[tuple[Any, dict, dict]] = []
        self._fading: list[tuple[Any, Any, bool]] = []
        self._started_at = 0.0

    @property
    def source(self) -> Any:
        return self._scene.as_source()

    @property
    def content_sources(self) -> tuple[Any, ...]:
        """Content this morph still shows, which must outlive a committed Take."""
        return tuple(
            entry.record["content_source"]
            for entry in self._plan
            if entry.record["content_source"] is not None
        )

    def build(self) -> None:
        ob = self._runtime.ob
        self._scene = ob.Scene.create_private(self._name)
        try:
            for index, entry in enumerate(self._plan):
                item = self._scene.add(entry.record["item"].source)
                self._apply_geometry(item, entry.record["layer"])
                if entry.role is MorphRole.MOVE:
                    assert entry.destination is not None
                    self._moving.append((item, entry.record["layer"], entry.destination["layer"]))
                    continue
                show = entry.role is MorphRole.FADE_IN
                # The item keeps its own reference to the fade once it is attached.
                fade = ob.Transition.create(_ITEM_FADE_SOURCE_ID, f"{self._name}-fade-{index}", {})
                self._fading.append((item, fade, show))
                item.visible = not show
        except Exception:  # noqa: BLE001 - release the partial scene before propagating failure
            self.release()
            raise

    def start(self) -> None:
        """Begin animating; the private scene must already be on the output."""
        for item, fade, show in self._fading:
            _start_item_fade(item, fade, show=show, duration_ms=self._duration_ms)
        self._started_at = time.monotonic()

    def tick(self, now: float) -> bool:
        """Advance every shared layer. Returns whether the morph has reached its end."""
        progress = (now - self._started_at) * 1000.0 / self._duration_ms
        amount = _ease(progress)
        for item, start, end in self._moving:
            self._apply_geometry(item, interpolate_layer(start, end, amount))
        return progress >= 1.0

    def finish(self) -> None:
        """Hand the output to the destination, whatever the morph's progress.

        If that fails the output is still showing the private scene, so it stays.
        """
        self._complete()
        self.release()

    def release(self) -> None:
        for item, _start, _end in self._moving:
            self._forget_item(item)
        for item, fade, _show in self._fading:
            self._forget_item(item)
            try:
                fade.release()
            except Exception:  # noqa: BLE001 - release remaining owned resources
                log.debug("morph item fade release errored", exc_info=True)
        self._moving.clear()
        self._fading.clear()
        scene, self._scene = self._scene, None
        if scene is not None:
            scene.release()
