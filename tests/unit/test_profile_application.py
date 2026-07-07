from __future__ import annotations

from pathlib import Path

import pytest

from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.onboarding.application import (
    OBSOnboardingConfiguration,
    OnboardingService,
    ProfileOnboardingCommand,
    ZoomShareOnboardingConfiguration,
)
from solin.core.profiles.application import ProfileService
from solin.core.profiles.models import ProfileInfo
from solin.core.profiles.settings import ProfileSettings


class _Registry:
    def __init__(self, profiles=()):
        self.current = list(profiles)
        self.saved = []
        self.fail_next_save = False

    def load(self):
        return list(self.current)

    def save(self, profiles):
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("registry unavailable")
        self.current = list(profiles)
        self.saved.append([profile.id for profile in profiles])


class _Storage:
    def __init__(self, root: Path):
        self.root = root
        self.ensured = []
        self.deleted = []
        self.migrated = []
        self.rolled_back = []

    def paths_for(self, profile_id):
        return ProfilePaths.from_roots(
            data_dir=self.root,
            cache_dir=self.root / "cache",
            profile_id=profile_id,
        )

    def ensure_profile_dirs(self, profile_id):
        self.ensured.append(profile_id)

    def delete_profile_data(self, profile_id):
        self.deleted.append(profile_id)

    def stage_profile_deletion(self, profile_id):
        storage = self

        class _Deletion:
            def commit(self):
                storage.deleted.append(profile_id)

            def rollback(self):
                storage.rolled_back.append(profile_id)

        return _Deletion()

    def copy_legacy_data(self, profile_id):
        self.migrated.append(profile_id)

    def cleanup_legacy_data(self):
        pass


class _Preferences:
    def __init__(self):
        self.last_active = ""
        self.cleared = []
        self.migrated = []
        self.prepared = []

    def has_legacy_settings(self):
        return False

    def settings_for(self, profile_id):
        return ProfileSettings.for_profile_id(profile_id)

    def last_active_profile(self):
        return self.last_active

    def set_last_active_profile(self, profile_id):
        self.last_active = profile_id

    def clear_last_active_profile_if(self, profile_id, replacement):
        if self.last_active == profile_id:
            self.last_active = replacement

    def clear_profile_settings(self, profile_id):
        self.cleared.append(profile_id)

    def copy_legacy_settings(self, profile_id):
        self.migrated.append(profile_id)

    def cleanup_legacy_settings(self):
        pass

    def prepare_profile_creation(self, profile_id):
        self.prepared.append(profile_id)


class _Setup:
    def __init__(self, *, fail=False):
        self.commands = []
        self.fail = fail

    def apply(self, profile_id, command):
        self.commands.append((profile_id, command))
        if self.fail:
            raise RuntimeError("setup failed")


def _service(tmp_path, profiles=()):
    registry = _Registry(profiles)
    storage = _Storage(tmp_path)
    preferences = _Preferences()
    service = ProfileService(
        registry,
        storage,
        preferences,
    )
    return service, registry, storage, preferences


def test_create_profile_uses_unique_slug_and_persists_after_storage_is_ready(
    tmp_path,
):
    existing = ProfileInfo("main_hall", "Main Hall", created_at=1.0)
    service, registry, storage, _ = _service(tmp_path, [existing])

    created = service.create_profile(" Main Hall ")

    assert created.id == "main_hall_2"
    assert storage.ensured == ["main_hall_2"]
    assert registry.saved == [["main_hall", "main_hall_2"]]


def test_complete_onboarding_applies_settings_then_activates_profile(tmp_path):
    profiles, _, storage, preferences = _service(tmp_path)
    setup = _Setup()
    onboarding = OnboardingService(profiles, setup)
    command = ProfileOnboardingCommand(
        name="Central Hall",
        interface_language="pt_BR",
        media_language="T",
        obs=OBSOnboardingConfiguration(
            enabled=True,
            port=4455,
            password="secret",
            automatic_scene_switching=True,
            default_scene="Default",
            media_scene="Media",
        ),
    )

    profile = onboarding.complete(command)

    assert setup.commands == [(profile.id, command)]
    assert storage.ensured == [profile.id, profile.id]
    assert preferences.last_active == profile.id


def test_onboarding_failure_rolls_back_registry_storage_and_settings(tmp_path):
    setup = _Setup(fail=True)
    profiles, registry, storage, preferences = _service(tmp_path)
    onboarding = OnboardingService(profiles, setup)

    with pytest.raises(RuntimeError, match="setup failed"):
        onboarding.complete(
            ProfileOnboardingCommand("Main Hall", "en")
        )

    assert profiles.profiles == []
    assert registry.saved == [["main_hall"], []]
    assert storage.deleted == ["main_hall"]
    assert preferences.cleared == ["main_hall"]


def test_deleting_active_profile_selects_replacement(tmp_path):
    profiles = [
        ProfileInfo("first", "First", created_at=1.0),
        ProfileInfo("second", "Second", created_at=2.0),
    ]
    service, registry, storage, preferences = _service(
        tmp_path,
        profiles,
    )
    service.set_active("second")

    assert service.delete_profile("second") is True

    assert registry.saved[-1] == ["first"]
    assert storage.deleted == ["second"]
    assert preferences.cleared == ["second"]
    assert service.active_id == "first"


def test_delete_rolls_back_staged_data_when_registry_save_fails(tmp_path):
    profiles = [
        ProfileInfo("first", "First", created_at=1.0),
        ProfileInfo("second", "Second", created_at=2.0),
    ]
    service, registry, storage, _ = _service(tmp_path, profiles)
    registry.fail_next_save = True

    with pytest.raises(OSError, match="registry unavailable"):
        service.delete_profile("second")

    assert [profile.id for profile in service.profiles] == ["first", "second"]
    assert storage.rolled_back == ["second"]
    assert storage.deleted == []


def test_relaunch_selection_does_not_mutate_active_runtime_profile(tmp_path):
    profiles = [
        ProfileInfo("first", "First", created_at=1.0),
        ProfileInfo("second", "Second", created_at=2.0),
    ]
    service, _, _, preferences = _service(tmp_path, profiles)
    service.set_active("first")

    service.remember_profile_for_next_launch("second")

    assert service.active_id == "first"
    assert preferences.last_active == "second"


def test_enabled_obs_configuration_rejects_invalid_port():
    with pytest.raises(ValueError, match="between 1 and 65535"):
        OBSOnboardingConfiguration(enabled=True, port=0)


@pytest.mark.parametrize(
    ("default_scene", "media_scene", "message"),
    [
        ("", "Media", "requires both scenes"),
        ("Default", "", "requires both scenes"),
        ("Same", "Same", "must be different"),
    ],
)
def test_automatic_obs_configuration_requires_distinct_scenes(
    default_scene,
    media_scene,
    message,
):
    with pytest.raises(ValueError, match=message):
        OBSOnboardingConfiguration(
            enabled=True,
            automatic_scene_switching=True,
            default_scene=default_scene,
            media_scene=media_scene,
        )


def test_automatic_obs_configuration_requires_obs():
    with pytest.raises(ValueError, match="requires OBS"):
        OBSOnboardingConfiguration(automatic_scene_switching=True)


@pytest.mark.parametrize(
    ("hotkey", "click_x", "click_y", "message"),
    [
        ("", 300, 250, "requires a hotkey"),
        ("Ctrl+Shift+S", -1, 250, "requires a share target"),
        ("Ctrl+Shift+S", 300, -1, "requires a share target"),
    ],
)
def test_enabled_zoom_share_configuration_requires_hotkey_and_valid_target(
    hotkey,
    click_x,
    click_y,
    message,
):
    with pytest.raises(ValueError, match=message):
        ZoomShareOnboardingConfiguration(
            enabled=True,
            hotkey=hotkey,
            click_x=click_x,
            click_y=click_y,
        )


def test_zoom_share_configuration_normalizes_hotkey():
    configuration = ZoomShareOnboardingConfiguration(
        enabled=True,
        hotkey="  Ctrl+Shift+S  ",
        click_x=300,
        click_y=250,
    )

    assert configuration.hotkey == "Ctrl+Shift+S"
