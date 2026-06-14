from pathlib import Path

from solin.ui.profile_obs_setup import ProfileOBSSetupMixin
from solin.ui.profile_screen import ProfileScreen


def test_profile_screen_uses_obs_setup_mixin():
    assert issubclass(ProfileScreen, ProfileOBSSetupMixin)
    assert ProfileScreen._ob_obs_teardown is ProfileOBSSetupMixin._ob_obs_teardown
    assert ProfileScreen._ob_obs_populate_combos is (
        ProfileOBSSetupMixin._ob_obs_populate_combos
    )
    assert ProfileScreen._refresh_ob_obs_status is (
        ProfileOBSSetupMixin._refresh_ob_obs_status
    )


def test_profile_screen_onboarding_uses_typed_settings_stores():
    source = Path("src/solin/ui/profile_screen.py").read_text(encoding="utf-8")

    assert "SettingsKey" not in source
    assert ".setValue(" not in source
