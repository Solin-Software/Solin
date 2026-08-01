"""Tests for the follow-the-projector director (rule engine)."""

from __future__ import annotations

from solin.core.media.vcam_director import VcamDirector
from solin.core.media.vcam_model import (
    CameraLayout,
    MeetingMode,
    VcamComposition,
    composition_for,
)


class _FakeVcam:
    def __init__(self) -> None:
        self.applied: list[VcamComposition] = []
        self.pip = None

    def apply_composition(self, comp) -> None:
        self.applied.append(comp)

    def set_pip_placement(self, corner, fraction=None) -> None:
        self.pip = (corner, fraction)


class _FakeProgram:
    def __init__(self, key=None) -> None:
        self.current_key = key


def _director(key=None, mode=MeetingMode.REGULAR):
    vcam = _FakeVcam()
    prog = _FakeProgram(key)
    return VcamDirector(vcam, prog, mode=mode), vcam, prog


def test_sync_applies_composition_for_current_key():
    director, vcam, _prog = _director(key="image")
    director.sync()
    assert vcam.applied[-1] == composition_for(MeetingMode.REGULAR, "image")


def test_sync_is_idempotent_until_key_changes():
    director, vcam, prog = _director(key="idle")
    director.sync()
    director.sync()
    director.sync()
    assert len(vcam.applied) == 1  # unchanged content → applied once

    prog.current_key = "media"
    director.sync()
    assert len(vcam.applied) == 2
    assert vcam.applied[-1] == composition_for(MeetingMode.REGULAR, "media")


def test_set_mode_reapplies_under_new_table():
    director, vcam, prog = _director(key="media", mode=MeetingMode.REGULAR)
    director.sync()
    assert vcam.applied[-1].camera is CameraLayout.OFF  # regular: media, no camera

    director.set_mode(MeetingMode.SIGN_LANGUAGE)
    assert vcam.applied[-1].camera is CameraLayout.PIP  # sign-language: keep signer


def test_override_pins_composition_for_current_content():
    director, vcam, prog = _director(key="media")
    director.sync()
    pinned = VcamComposition(program_visible=True, camera=CameraLayout.FULL)

    director.set_override(pinned)
    assert vcam.applied[-1] == pinned
    assert director.has_override is True

    # Same content: auto-follow does not override the operator's pin.
    director.sync()
    assert vcam.applied[-1] == pinned


def test_override_lifts_when_projector_content_changes():
    director, vcam, prog = _director(key="media")
    director.sync()
    director.set_override(VcamComposition(program_visible=True, camera=CameraLayout.FULL))

    prog.current_key = "image"
    director.sync()

    assert director.has_override is False
    assert vcam.applied[-1] == composition_for(MeetingMode.REGULAR, "image")


def test_start_following_is_idempotent_and_stoppable(qtbot=None):
    # QTimer needs a QApplication; the unit suite provides one.
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:  # pragma: no cover - safety for isolated runs
        QApplication([])
    director, vcam, _prog = _director(key="idle")

    director.start_following(interval_ms=100000)  # long interval: never fires here
    timer = director._timer
    assert timer is not None
    assert len(vcam.applied) == 1  # start_following syncs once immediately

    director.start_following()  # idempotent — same timer, no extra sync
    assert director._timer is timer
    assert len(vcam.applied) == 1

    director.stop_following()
    assert director._timer is None


def test_set_pip_pushes_to_vcam_and_config():
    from solin.core.media.vcam_model import PipCorner

    director, vcam, _prog = _director(key="idle")
    director.set_pip(PipCorner.TOP_LEFT, 0.3)
    assert vcam.pip[0] is PipCorner.TOP_LEFT
    assert director.config.pip_corner is PipCorner.TOP_LEFT
    assert abs(director.config.pip_fraction - 0.3) < 1e-9


def test_set_rule_reapplies_current_mode_live():
    from solin.core.media.vcam_model import ContentGroup

    director, vcam, _prog = _director(key="media", mode=MeetingMode.REGULAR)
    director.sync()
    assert vcam.applied[-1].camera is CameraLayout.OFF  # regular media: no camera

    director.set_rule(
        MeetingMode.REGULAR, ContentGroup.VIDEO,
        VcamComposition(program_visible=True, camera=CameraLayout.PIP),
    )
    assert vcam.applied[-1].camera is CameraLayout.PIP  # applied live


def test_set_config_applies_mode_and_pip():
    from solin.core.media.vcam_model import PipCorner, VcamSceneConfig

    director, vcam, _prog = _director(key="media")
    director.set_config(
        VcamSceneConfig(mode=MeetingMode.SIGN_LANGUAGE, pip_corner=PipCorner.TOP_RIGHT)
    )
    assert director.mode is MeetingMode.SIGN_LANGUAGE
    assert vcam.pip[0] is PipCorner.TOP_RIGHT
    assert vcam.applied[-1].camera is CameraLayout.PIP  # sign-language media → PiP


def test_set_config_copies_so_later_mutation_does_not_leak_in():
    from solin.core.media.vcam_model import PipCorner, VcamSceneConfig

    director, _vcam, _prog = _director(key="idle")
    cfg = VcamSceneConfig(pip_corner=PipCorner.BOTTOM_RIGHT)
    director.set_config(cfg)
    # A caller (e.g. the Scenes page) keeps mutating its own object afterward.
    cfg.pip_corner = PipCorner.TOP_LEFT
    assert director.config.pip_corner is PipCorner.BOTTOM_RIGHT


def test_clear_override_resumes_following():
    director, vcam, prog = _director(key="media")
    director.sync()
    director.set_override(VcamComposition(program_visible=False, camera=CameraLayout.FULL))

    director.clear_override()

    assert director.has_override is False
    assert vcam.applied[-1] == composition_for(MeetingMode.REGULAR, "media")
