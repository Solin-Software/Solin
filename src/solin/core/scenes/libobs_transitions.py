"""Own prepared transition resources independently of an output's active scene."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

TRANSITION_SOURCE_IDS = {
    "cut": "cut_transition",
    "dissolve": "fade_transition",
    "fade_to_black": "fade_to_color_transition",
}
FALLBACK_TRANSITION_KIND = "cut"


class LibobsTransitionPool:
    """Keep at most one ready resource per supported kind for one render bus.

    Creation can acquire the graphics context and compile effects. Hydration and
    preparation own that work; Take only borrows an already prepared resource.
    """

    def __init__(self, runtime: Any, name_prefix: str) -> None:
        self._runtime = runtime
        self._name_prefix = name_prefix
        self._transitions: dict[str, Any] = {}

    def prepare(self, model_kind: str) -> Any:
        kind = model_kind if model_kind in TRANSITION_SOURCE_IDS else FALLBACK_TRANSITION_KIND
        existing = self._transitions.get(kind)
        if existing is not None:
            return existing
        settings = {"color": 0xFF000000} if kind == "fade_to_black" else {}
        transition = self._runtime.ob.Transition.create(
            TRANSITION_SOURCE_IDS[kind], f"{self._name_prefix}-{kind}", settings
        )
        try:
            canvas = self._runtime.video
            transition.set_size(canvas.width, canvas.height)
        except Exception:  # noqa: BLE001 - release the candidate before propagating failure
            transition.release()
            raise
        self._transitions[kind] = transition
        return transition

    def prepared(self, model_kind: str) -> Any:
        """Borrow a ready resource; never allocate on the live Take path."""
        kind = model_kind if model_kind in TRANSITION_SOURCE_IDS else FALLBACK_TRANSITION_KIND
        return self._transitions[kind]

    def prepare_document(self, document: dict) -> None:
        self.prepare(FALLBACK_TRANSITION_KIND)
        policy = document.get("transition_policy") or {}
        default = policy.get("default") or {}
        self.prepare(str(default.get("kind") or FALLBACK_TRANSITION_KIND))
        for spec in (policy.get("overrides") or {}).values():
            self.prepare(str(spec.get("kind") or FALLBACK_TRANSITION_KIND))

    def reset_sources(self) -> None:
        """Drop graph references while retaining ready graphics resources."""
        for transition in self._transitions.values():
            transition.clear()

    def shutdown(self) -> None:
        transitions, self._transitions = self._transitions, {}
        for transition in transitions.values():
            try:
                transition.release()
            except Exception:  # noqa: BLE001 - release remaining owned resources
                log.warning("Could not release a prepared transition", exc_info=True)
