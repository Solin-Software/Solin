"""Tests for virtual-camera scene-config persistence (in-memory settings)."""

from __future__ import annotations

from solin.core.foundation.settings_keys import SettingsKey
from solin.core.media.vcam_model import (
    CameraLayout,
    ContentGroup,
    MeetingMode,
    PipCorner,
    VcamComposition,
    VcamSceneConfig,
)
from solin.core.media.vcam_settings import VcamSettingsStore


class _FakeSettings:
    def __init__(self) -> None:
        self.data: dict = {}

    def string(self, key, default=""):
        return str(self.data.get(key, default))

    def set_value(self, key, value, **_kw):
        self.data[key] = value


def test_scene_config_default_when_unset():
    store = VcamSettingsStore(_FakeSettings())
    assert store.scene_config() == VcamSceneConfig()


def test_scene_config_round_trips_through_settings():
    store = VcamSettingsStore(_FakeSettings())
    cfg = VcamSceneConfig(mode=MeetingMode.SIGN_LANGUAGE, pip_corner=PipCorner.TOP_LEFT)
    cfg.set_rule(
        MeetingMode.REGULAR, ContentGroup.VIDEO,
        VcamComposition(program_visible=True, camera=CameraLayout.PIP),
    )
    store.save_scene_config(cfg)

    loaded = store.scene_config()
    assert loaded.mode is MeetingMode.SIGN_LANGUAGE
    assert loaded.pip_corner is PipCorner.TOP_LEFT
    assert loaded.overrides == cfg.overrides


def test_scene_config_corrupt_json_returns_default():
    fake = _FakeSettings()
    fake.data[SettingsKey.VCAM_SCENE_CONFIG] = "{not valid json"
    store = VcamSettingsStore(fake)
    assert store.scene_config() == VcamSceneConfig()
