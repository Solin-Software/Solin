"""Tests for the follow-the-projector rule model."""

from __future__ import annotations

import solin.core.media.vcam_model as m
from solin.core.media.vcam_model import (
    CameraLayout,
    MeetingMode,
    VcamComposition,
    VcamSceneConfig,
    composition_for,
)


def test_idle_like_states_show_camera_full_in_both_modes():
    for mode in MeetingMode:
        for key in ("idle", "timer", "__black__", "idle_video", "idle_image", None):
            comp = composition_for(mode, key)
            assert comp.program_visible is False
            assert comp.camera is CameraLayout.FULL


def test_regular_meeting_media_is_full_no_camera():
    comp = composition_for(MeetingMode.REGULAR, "media")
    assert comp.program_visible is True
    assert comp.camera is CameraLayout.OFF


def test_regular_meeting_image_gets_camera_pip():
    comp = composition_for(MeetingMode.REGULAR, "image")
    assert comp.program_visible is True
    assert comp.camera is CameraLayout.PIP


def test_sign_language_keeps_interpreter_over_media():
    # The distinguishing rule: media keeps the camera (signer) as a PiP.
    comp = composition_for(MeetingMode.SIGN_LANGUAGE, "media")
    assert comp.program_visible is True
    assert comp.camera is CameraLayout.PIP


def test_projected_camera_is_mirrored_not_doubled():
    for mode in MeetingMode:
        comp = composition_for(mode, "camera")
        assert comp.program_visible is True
        assert comp.camera is CameraLayout.OFF  # mirror the projector's camera


def test_unknown_key_falls_back_to_mode_default():
    assert composition_for(MeetingMode.REGULAR, "surprise").camera is CameraLayout.OFF
    assert composition_for(MeetingMode.SIGN_LANGUAGE, "surprise").camera is CameraLayout.PIP


def test_rule_keys_match_program_content_keys():
    """Guard against drift: every projector key the group map switches on must be
    a real projector content key produced by the program / driver."""
    import solin.core.media.vcam_model as m

    # Keys the projection program / driver actually set as current_key.
    from solin.projection import program_driver as d

    driver_keys = {
        d._KEY_IDLE, d._KEY_IMAGE, d._KEY_TIMER, d._KEY_BROWSER, d._KEY_CAMERA,
        d._KEY_NDI, d._KEY_IDLE_VIDEO, d._KEY_IDLE_IMAGE,
    }
    program_keys = {"media", "__black__"}  # obs_program: show_media / black
    real_keys = driver_keys | program_keys

    model_keys = set(m._KEY_TO_GROUP.keys())
    assert model_keys <= real_keys, f"stale rule keys: {model_keys - real_keys}"


def test_every_mode_group_pair_has_a_default():
    for mode in MeetingMode:
        for group in m.ContentGroup:
            assert isinstance(m.default_composition(mode, group), VcamComposition)


def test_all_group_defaults_are_selectable_presets():
    # The Scenes panel shows each rule as one of the SCENE_PRESETS; a default that
    # isn't a preset would render blank/wrong, so every default must be a preset.
    for mode in MeetingMode:
        for group in m.ContentGroup:
            assert m.preset_id_for(m.default_composition(mode, group)) is not None


def test_scene_config_defaults_match_curated_tables():
    cfg = VcamSceneConfig()
    for key in ("__black__", "image", "media", "camera", "browser"):
        assert cfg.composition_for(key) == composition_for(MeetingMode.REGULAR, key)


def test_scene_config_override_then_revert():
    cfg = VcamSceneConfig(mode=MeetingMode.REGULAR)
    from solin.core.media.vcam_model import ContentGroup

    pinned = VcamComposition(program_visible=True, camera=CameraLayout.PIP)
    cfg.set_rule(MeetingMode.REGULAR, ContentGroup.VIDEO, pinned)
    assert cfg.composition_for("media") == pinned  # override wins

    # Setting a rule back to its default drops the override (keeps config lean).
    default = m.default_composition(MeetingMode.REGULAR, ContentGroup.VIDEO)
    cfg.set_rule(MeetingMode.REGULAR, ContentGroup.VIDEO, default)
    assert (MeetingMode.REGULAR, ContentGroup.VIDEO) not in cfg.overrides


def test_scene_config_round_trips_through_dict():
    from solin.core.media.vcam_model import ContentGroup, PipCorner

    cfg = VcamSceneConfig(mode=MeetingMode.SIGN_LANGUAGE, pip_corner=PipCorner.TOP_LEFT, pip_fraction=0.33)
    cfg.set_rule(
        MeetingMode.REGULAR, ContentGroup.VIDEO,
        VcamComposition(program_visible=True, camera=CameraLayout.PIP),
    )
    restored = VcamSceneConfig.from_dict(cfg.to_dict())
    assert restored.mode is MeetingMode.SIGN_LANGUAGE
    assert restored.pip_corner is PipCorner.TOP_LEFT
    assert abs(restored.pip_fraction - 0.33) < 1e-9
    assert restored.overrides == cfg.overrides


def test_scene_config_from_dict_tolerates_garbage():
    cfg = VcamSceneConfig.from_dict(
        {"mode": "nonsense", "pip_corner": "??", "pip_fraction": "x",
         "overrides": [{"mode": "bad", "group": "bad", "composition": {}}]}
    )
    assert cfg.mode is MeetingMode.REGULAR
    assert cfg.pip_corner is m.PipCorner.BOTTOM_RIGHT
    assert cfg.overrides == {}
