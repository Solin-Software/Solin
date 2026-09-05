"""The projection output: a scene shown on the room's screen, independent of program.

The program (virtual camera + recording) is libobs' main mix, fed by a global
output channel. Projection must be able to show a *different* scene, so it gets
its own transition source that is **never placed on an output channel** — a
channel feeds the main canvas, which would composite the projection scene on top
of the program inside the virtual camera and the recording.

A transition is just a source, so the projection displays render it directly with
``render_source_letterboxed``. ``obs_source_inc_showing`` gives the transition (and
through it every scene later set on it) the show refs that make cameras and media
actually run; show refs are *not* activate refs, so projection audio never enters
the program mix.

Owned by the sidecar rather than the scene graph, so a structural-edit rebuild
(``LibobsSceneGraph.clear``) cannot black the projection out: the transition keeps
its own strong ref to the last scene until it is pointed at the new one.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Reuse the graph's model-kind -> obs id mapping so program and projection
# animate identically for the same scene transition policy.
from solin.core.scenes.libobs_scene_builder import (  # noqa: E402
    _FALLBACK_KIND,
    _TRANSITION_IDS,
)


class LibobsProjectionRoute:
    """Owns the projection transition and exposes it to the display callbacks."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._transition: Any = None
        self._transition_kind: str | None = None
        self._showing = False
        self._scene_id = ""
        # pylibobs' Transition exposes no getter, so remember what we last showed
        # and carry it into a replacement transition instead of blanking the screen.
        self._scene_source: Any = None

    @property
    def scene_id(self) -> str:
        return self._scene_id

    @property
    def source_ptr(self) -> object | None:
        """Raw pointer for a display draw callback, or None when unset.

        Read from the graphics thread, so it must never block: the transition is
        created once and released only at shutdown, and libobs itself guards the
        swap of its inner scene.
        """
        transition = self._transition
        return getattr(transition, "_ptr", None) if transition is not None else None

    def _ensure_transition(self, model_kind: str | None = None) -> Any:
        kind = model_kind if model_kind in _TRANSITION_IDS else _FALLBACK_KIND
        if self._transition is not None and kind == self._transition_kind:
            return self._transition
        try:
            transition = self._runtime.ob.Transition.create(
                _TRANSITION_IDS[kind], f"solin-projection-{kind}", {}
            )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not create the projection transition", exc_info=True)
            return self._transition
        # Carry the live scene into the replacement so the room's screen does not
        # blink when the transition kind changes.
        old, self._transition = self._transition, transition
        self._transition_kind = kind
        if self._scene_source is not None:
            try:
                transition.set_source(self._scene_source)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("projection transition carry-over errored", exc_info=True)
        self._inc_showing(transition)
        self._release(old)
        return transition

    def _release(self, transition: Any) -> None:
        if transition is None:
            return
        pointer = getattr(transition, "_ptr", None)
        if pointer is not None:
            try:
                from pylibobs._ffi import get_lib

                get_lib().obs_source_dec_showing(pointer)
            except Exception:  # noqa: BLE001 - unwrapped libobs symbol
                log.debug("projection dec_showing errored", exc_info=True)
        try:
            transition.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("projection transition release errored", exc_info=True)

    def start(
        self,
        scene_id: str,
        scene_source: Any,
        model_kind: str,
        duration_ms: int,
    ) -> bool:
        """Animate the projection to ``scene_source`` with its own transition."""
        transition = self._ensure_transition(model_kind)
        if transition is None:
            return False
        try:
            transition.start(scene_source, int(duration_ms))
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not transition the projection to %r", scene_id, exc_info=True)
            return False
        self._scene_id = scene_id
        return True

    def _inc_showing(self, transition: Any) -> None:
        """Show-ref the transition so its scenes' sources actually run.

        Not an activate ref: projection stays out of the program audio mix.
        """
        pointer = getattr(transition, "_ptr", None)
        if pointer is None:
            return
        try:
            from pylibobs._ffi import get_lib

            get_lib().obs_source_inc_showing(pointer)
        except Exception:  # noqa: BLE001 - unwrapped libobs symbol
            log.warning("Could not show-ref the projection transition", exc_info=True)
            return
        self._showing = True

    def set_scene(self, scene_id: str, scene_source: Any) -> bool:
        """Cut the projection to ``scene_source`` (None clears it)."""
        transition = self._ensure_transition()
        if transition is None:
            return False
        try:
            transition.set_source(scene_source)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not point the projection at scene %r", scene_id, exc_info=True)
            return False
        self._scene_id = scene_id if scene_source is not None else ""
        self._scene_source = scene_source
        return True

    def shutdown(self) -> None:
        transition, self._transition = self._transition, None
        self._scene_id = ""
        self._scene_source = None
        if transition is None:
            return
        pointer = getattr(transition, "_ptr", None)
        if self._showing and pointer is not None:
            try:
                from pylibobs._ffi import get_lib

                get_lib().obs_source_dec_showing(pointer)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.debug("projection dec_showing errored", exc_info=True)
        self._showing = False
        try:
            transition.set_source(None)  # drop the ref to the scene first
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("projection transition clear errored", exc_info=True)
        try:
            transition.release()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("projection transition release errored", exc_info=True)
