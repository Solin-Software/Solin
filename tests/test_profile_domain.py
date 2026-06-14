from __future__ import annotations

import pytest

import solin.core.profiles.manager as profile_manager
from solin.core.profiles.models import (
    ProfileInfo,
    normalize_profile_name,
    profile_slug,
    unique_profile_slug,
    validate_profile_id,
)
from solin.core.profiles.settings import ProfileSettings


def test_profile_name_is_normalized_at_the_entity_boundary():
    profile = ProfileInfo("main_hall", "  Main Hall  ", created_at=1.0)

    assert profile.name == "Main Hall"
    profile.rename("  East Hall  ")
    assert profile.name == "East Hall"


@pytest.mark.parametrize("name", ["", "   ", "Hall\nName", "Hall\tName"])
def test_profile_name_rejects_empty_or_control_characters(name: str):
    with pytest.raises(ValueError):
        normalize_profile_name(name)


@pytest.mark.parametrize("profile_id", ["../escape", "hall/name", "hall name", "CON", ""])
def test_profile_id_rejects_unsafe_path_or_os_values(profile_id: str):
    with pytest.raises(ValueError):
        validate_profile_id(profile_id)
    with pytest.raises(ValueError):
        ProfileSettings.for_profile_id(profile_id)


def test_profile_slug_is_safe_and_unique():
    assert profile_slug(" Main Hall - East ") == "main_hall_east"
    assert profile_slug("CON") == "profile_con"
    assert unique_profile_slug("main_hall", {"main_hall", "main_hall_2"}) == "main_hall_3"


def test_profile_models_are_not_reexported_by_manager():
    assert not hasattr(profile_manager, "ProfileInfo")


@pytest.mark.parametrize("created_at", [float("nan"), float("inf"), float("-inf")])
def test_profile_rejects_non_finite_creation_time(created_at: float):
    with pytest.raises(ValueError):
        ProfileInfo("main_hall", "Main Hall", created_at=created_at)


def test_profile_registry_entry_must_be_an_object():
    with pytest.raises(ValueError):
        ProfileInfo.from_dict([])  # type: ignore[arg-type]
