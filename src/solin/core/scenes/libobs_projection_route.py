"""The projection output: a scene shown on the room's screen, independent of program.

The program (virtual camera + recording) is libobs' main mix, fed by a global
output channel. Projection must be able to show a *different* scene, so it gets
its own transition source that is **never placed on an output channel** — a
channel feeds the main canvas, which would composite the projection scene on top
of the program inside the virtual camera and the recording.

A transition is just a source, so the projection displays render it directly with
``render_source_letterboxed``. ``obs_source_inc_showing`` gives the transition (and
through it every scene later set on it) the show refs that make cameras actually run.

Show refs are *not* activate refs: they raise ``show_refs`` only, never
``activate_refs``. What keeps projection out of the program mix is that this
transition is not a root source of a main-view output channel — that is the mix.
The activate counter matters for something else: libobs' audio monitoring discards
every buffer while it is zero, which is why an audible media source takes its own
activate ref (see :meth:`LibobsMediaSource._set_active`) instead of relying on
whichever bus happens to be showing it.

Owned by the sidecar rather than the scene graph, so a structural-edit rebuild
(``LibobsSceneGraph.clear``) cannot black the projection out: the transition keeps
its own strong ref to the last scene until it is pointed at the new one.
"""

from __future__ import annotations

import logging
from typing import Any

from solin.core.scenes.libobs_transitions import (
    FALLBACK_TRANSITION_KIND,
    TRANSITION_SOURCE_IDS,
    LibobsTransitionPool,
)

log = logging.getLogger(__name__)


class LibobsProjectionRoute:
    """Owns the projection transition and exposes it to the display callbacks."""

    def __init__(self, runtime: Any) -> None:
        self._transitions = LibobsTransitionPool(runtime, "solin-projection")
        self._transition: Any = None
        self._showing = False
        self._scene_id = ""
        # Carry the last committed source into a prepared replacement so a kind
        # change preserves the transition's origin.
        self._scene_source: Any = None

    @property
    def scene_id(self) -> str:
        return self._scene_id

    @property
    def source_ptr(self) -> object | None:
        """Raw pointer for a display draw callback, or None when unset.

        Read from the graphics thread, so it must never block. Prepared resources
        remain owned until shutdown; libobs guards the swap of their inner scenes.
        """
        transition = self._transition
        return getattr(transition, "_ptr", None) if transition is not None else None

    def prepare_transition(self, model_kind: str) -> None:
        """Allocate a transition without changing what the projection shows."""
        self._transitions.prepare(model_kind)

    def prepare_document(self, document: dict) -> None:
        """Prime the configured kinds before Program starts rendering them."""
        self._transitions.prepare_document(document)

    def _activate_transition(self, transition: Any) -> None:
        if transition is self._transition:
            return
        old = self._transition
        showing = False
        try:
            if self._scene_source is not None:
                transition.set_source(self._scene_source)
            else:
                transition.clear()
            showing = self._inc_showing(transition)
            if self._showing and old is not None:
                self._dec_showing(old)
        except Exception:  # noqa: BLE001 - roll back new showing refs before propagating failure
            if showing:
                self._dec_showing(transition)
            transition.clear()
            raise
        self._transition = transition
        self._showing = showing
        if old is not None:
            old.clear()

    def start(
        self,
        scene_id: str,
        scene_source: Any,
        model_kind: str,
        duration_ms: int,
    ) -> bool:
        """Animate the projection to ``scene_source`` with its own transition."""
        try:
            transition = self._transitions.prepared(model_kind)
            self._activate_transition(transition)
            if not transition.start(scene_source, int(duration_ms)):
                # OBS declines an animation whose destination is already live.
                # A repeated ready-content Take still successfully keeps it live.
                return self._scene_id == scene_id and (
                    getattr(self._scene_source, "_ptr", self._scene_source)
                    == getattr(scene_source, "_ptr", scene_source)
                )
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not transition the projection to %r", scene_id, exc_info=True)
            return False
        self._scene_id = scene_id
        self._scene_source = scene_source
        return True

    def _inc_showing(self, transition: Any) -> bool:
        """Show-ref the transition so its scenes' sources actually run.

        Not an activate ref: projection stays out of the program audio mix.
        """
        pointer = getattr(transition, "_ptr", None)
        if pointer is None:
            return False
        from pylibobs._ffi import get_lib

        # CFFI resolves these unwrapped symbols dynamically.
        lib: Any = get_lib()
        lib.obs_source_inc_showing(pointer)
        return True

    def _dec_showing(self, transition: Any) -> None:
        pointer = getattr(transition, "_ptr", None)
        if pointer is None:
            return
        from pylibobs._ffi import get_lib

        lib: Any = get_lib()
        lib.obs_source_dec_showing(pointer)

    def _reset_inactive_sources(self) -> None:
        for kind in TRANSITION_SOURCE_IDS:
            try:
                transition = self._transitions.prepared(kind)
            except KeyError:
                continue
            if transition is not self._transition:
                transition.clear()

    def set_scene(self, scene_id: str, scene_source: Any) -> bool:
        """Cut the projection to ``scene_source`` (None clears it)."""
        try:
            transition = self._transitions.prepare(FALLBACK_TRANSITION_KIND)
            self._activate_transition(transition)
            if scene_source is None:
                transition.clear()
            else:
                transition.set_source(scene_source)
            self._scene_id = scene_id if scene_source is not None else ""
            self._scene_source = scene_source
            self._reset_inactive_sources()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.warning("Could not point the projection at scene %r", scene_id, exc_info=True)
            return False
        return True

    def shutdown(self) -> None:
        transition, self._transition = self._transition, None
        self._scene_id = ""
        self._scene_source = None
        if self._showing and transition is not None:
            try:
                self._dec_showing(transition)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.debug("projection dec_showing errored", exc_info=True)
        self._showing = False
        try:
            self._transitions.reset_sources()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("projection transition clear errored", exc_info=True)
        self._transitions.shutdown()
