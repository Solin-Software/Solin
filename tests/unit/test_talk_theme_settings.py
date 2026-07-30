from __future__ import annotations

import uuid

from solin.core.foundation.constants import QSETTINGS_GLOBAL_APP
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.core.talk_theme.settings import (
    DEFAULT_CUSTOM_COLOR,
    MAX_CUSTOM_COLORS,
    TalkThemeOutputSettingsStore,
    TalkThemeSettingsStore,
    merge_custom_colors,
    normalize_custom_colors,
)


def _store(label: str) -> TalkThemeSettingsStore:
    profile = ProfileSettings.for_profile_id(f"talk_theme_{label}_{uuid.uuid4().hex}")
    return TalkThemeSettingsStore.for_profile_settings(profile)


def test_custom_color_normalization_compacts_unique_rgb_swatches() -> None:
    colors = normalize_custom_colors(
        ["#112233", "#80445566", "invalid", None, "#abcdef", "#112233"]
    )

    assert colors == ("#112233", "#445566", "#ABCDEF")
    assert DEFAULT_CUSTOM_COLOR not in colors
    assert normalize_custom_colors("#010203") == ("#010203",)
    assert normalize_custom_colors(object()) == ()

    many_colors = [f"#{index:06X}" for index in range(MAX_CUSTOM_COLORS + 4)]
    assert len(normalize_custom_colors(many_colors)) == MAX_CUSTOM_COLORS


def test_new_custom_colors_append_without_replacing_restored_first_slot() -> None:
    stored = ("#112233", "#445566")
    observed_after_qt_replaced_slot_zero = ("#778899", "#445566", "#FFFFFF")

    assert merge_custom_colors(
        stored,
        observed_after_qt_replaced_slot_zero,
    ) == ("#112233", "#445566", "#778899")

    assert merge_custom_colors(
        ("#111111", "#222222", "#333333"),
        ("#444444", "#222222", "#333333"),
        capacity=3,
    ) == ("#222222", "#333333", "#444444")


def test_custom_colors_persist_in_their_profile_namespace() -> None:
    first = _store("first")
    second = _store("second")
    first.settings.clear()
    second.settings.clear()
    try:
        colors = ("#112233", "#445566")
        first.set_custom_colors(colors)

        recreated = TalkThemeSettingsStore(first.settings)
        assert recreated.custom_colors() == colors
        assert second.custom_colors() == ()

        recreated.set_custom_colors(())
        assert first.custom_colors() == ()
    finally:
        first.settings.clear()
        second.settings.clear()


def test_custom_color_settings_migrate_the_previous_text_only_palette() -> None:
    store = _store("migration")
    store.settings.clear()
    try:
        store.settings.set_value(
            "talk_theme/custom_text_colors",
            ["#112233", "#445566"],
        )

        assert store.custom_colors() == ("#112233", "#445566")

        store.set_custom_colors(("#112233", "#778899"))

        assert store.custom_colors() == ("#112233", "#778899")
        assert "talk_theme/custom_text_colors" not in store.settings.all_keys()
        assert "talk_theme/custom_colors" in store.settings.all_keys()
    finally:
        store.settings.clear()


def test_output_aspect_persists_in_the_global_application_namespace() -> None:
    organization = f"SolinTest_{uuid.uuid4().hex}"
    settings = SettingsStore.for_namespace(organization, QSETTINGS_GLOBAL_APP)
    store = TalkThemeOutputSettingsStore(settings)
    settings.clear()
    try:
        assert store.follow_output_aspect() is True

        store.set_follow_output_aspect(False)

        recreated = TalkThemeOutputSettingsStore(
            SettingsStore.for_namespace(organization, QSETTINGS_GLOBAL_APP)
        )
        assert recreated.follow_output_aspect() is False

        recreated.set_follow_output_aspect(True)
        assert store.follow_output_aspect() is True
    finally:
        settings.clear()
