"""Follow-the-projector rule engine for the virtual camera.

Watches the projection program's current content and drives the virtual camera to
the composition the active :class:`MeetingMode` prescribes (see
:mod:`vcam_model`). The operator can pin a specific scene for the current content
via :meth:`set_override`; the pin lifts automatically when the projected content
changes. Content is followed by polling the program on a GUI-thread ``QTimer`` —
content changes are human-paced, and polling keeps the program free of Qt.
"""

from __future__ import annotations

import logging

from .vcam_model import (
    ContentGroup,
    MeetingMode,
    PipCorner,
    VcamComposition,
    VcamSceneConfig,
)

log = logging.getLogger(__name__)

_DEFAULT_POLL_MS = 150
_UNSET = object()  # distinct from a None projector key (blank program)


class VcamDirector:
    """Maps projector content → virtual-camera composition, live.

    Holds a :class:`VcamSceneConfig` (meeting mode + per-mode overrides + PiP
    placement) — the operator edits it via the Scenes panel; the controller loads
    and persists it. The director just applies whatever the config resolves to.
    """

    def __init__(
        self,
        vcam,
        program=None,
        *,
        mode: MeetingMode | None = None,
        config: VcamSceneConfig | None = None,
    ) -> None:
        self._vcam = vcam
        self._program = program  # injectable; else projection_program() (lazy)
        self._config = config or VcamSceneConfig(mode=mode or MeetingMode.REGULAR)
        self._override: VcamComposition | None = None
        self._override_key: object = _UNSET
        self._applied_key: object = _UNSET
        self._timer = None

    @property
    def mode(self) -> MeetingMode:
        return self._config.mode

    @property
    def config(self) -> VcamSceneConfig:
        return self._config

    def set_mode(self, mode: MeetingMode) -> None:
        """Switch the active rule table and re-follow the current content."""
        if mode is self._config.mode:
            return
        self._config.mode = mode
        self._reapply()

    def set_config(self, config: VcamSceneConfig) -> None:
        """Replace the whole configuration (e.g. loaded from settings) and apply.

        Stores an independent copy so the director never aliases a config object
        that some other owner (e.g. the Scenes page) keeps mutating.
        """
        self._config = config.copy()
        self._push_pip()
        self._reapply()

    def set_rule(
        self, mode: MeetingMode, group: ContentGroup, comp: VcamComposition
    ) -> None:
        """Edit one rule; re-applies live if it affects the current mode."""
        self._config.set_rule(mode, group, comp)
        if mode is self._config.mode:
            self._reapply()

    def set_pip(self, corner: PipCorner, fraction: float | None = None) -> None:
        """Move/resize the PiP camera (persisted in the config; applied live)."""
        self._config.pip_corner = corner
        if fraction is not None:
            self._config.pip_fraction = max(0.1, min(0.5, float(fraction)))
        self._push_pip()

    def _reapply(self) -> None:
        # A config change may alter the composition for the current key; a manual
        # override is dropped and the next sync re-applies from scratch.
        self._override = None
        self._override_key = _UNSET
        self._applied_key = _UNSET
        self.sync()

    def _push_pip(self) -> None:
        self._vcam.set_pip_placement(self._config.pip_corner, self._config.pip_fraction)

    # ── operator override ──────────────────────────────────────────────────

    def set_override(self, comp: VcamComposition) -> None:
        """Pin a specific composition for the *current* projector content.

        Lifts automatically the next time the projected content changes (so the
        operator's choice applies to what is on screen now, then auto-follow
        resumes), or immediately on :meth:`clear_override`.
        """
        self._override = comp
        self._override_key = self._current_key()
        self._vcam.apply_composition(comp)

    def clear_override(self) -> None:
        self._override = None
        self._override_key = _UNSET
        self._applied_key = _UNSET
        self.sync()

    @property
    def has_override(self) -> bool:
        return self._override is not None

    # ── follow loop ─────────────────────────────────────────────────────────

    def sync(self) -> None:
        """Apply the composition for the projector's current content (idempotent).

        Skips the libobs work when nothing has changed, so it is cheap to call on
        a fast timer.
        """
        key = self._current_key()
        # A content change retires a per-content manual override.
        if self._override is not None and key != self._override_key:
            self._override = None
            self._override_key = _UNSET
        if self._override is not None:
            return  # the pinned composition is already applied
        if key == self._applied_key:
            return
        self._vcam.apply_composition(self._config.composition_for(key))
        self._applied_key = key

    def start_following(self, interval_ms: int = _DEFAULT_POLL_MS) -> None:
        """Begin polling projector content on a QTimer (idempotent)."""
        if self._timer is not None:
            return
        from PySide6.QtCore import QTimer

        self._timer = QTimer()
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self.sync)
        self._timer.start()
        self.sync()

    def stop_following(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # ── internals ───────────────────────────────────────────────────────────

    def _current_key(self):
        prog = self._program_ref()
        return prog.current_key if prog is not None else None

    def _program_ref(self):
        if self._program is None:
            from .obs_program import projection_program

            self._program = projection_program()
        return self._program


_director: VcamDirector | None = None


def vcam_director() -> VcamDirector:
    """Return the process-wide :class:`VcamDirector` singleton (drives the
    process-wide virtual camera off the shared projection program)."""
    global _director
    if _director is None:
        from .obs_virtual_camera import virtual_camera

        _director = VcamDirector(virtual_camera())
    return _director


__all__ = ["VcamDirector", "vcam_director"]
