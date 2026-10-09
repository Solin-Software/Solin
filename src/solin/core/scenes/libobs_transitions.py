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

    Creation can acquire the graphics context and compile effects. Hydration
    primes every supported kind; live Prepare and Take only borrow ready resources.
    """

    def __init__(self, runtime: Any, name_prefix: str) -> None:
        self._runtime = runtime
        self._name_prefix = name_prefix
        self._transitions: dict[str, Any] = {}

    def _prepare(self, kind: str) -> Any:
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
        """Borrow a ready resource; never allocate on live Prepare or Take."""
        kind = model_kind if model_kind in TRANSITION_SOURCE_IDS else FALLBACK_TRANSITION_KIND
        return self._transitions[kind]

    def prepare_all(self) -> None:
        """Prime the bounded set before rendering, independent of controller policy.

        Retain successful resources if creation fails so a hydration retry only
        prepares the remaining kinds. Ownership lasts until shutdown.
        """
        for kind in TRANSITION_SOURCE_IDS:
            self._prepare(kind)

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
