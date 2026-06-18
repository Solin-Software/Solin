from pathlib import Path

from solin.controllers.onboarding_obs_probe import OnboardingOBSProbe
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


def test_profile_widgets_do_not_construct_obs_services():
    source = Path("src/solin/ui/profile_obs_setup.py").read_text(encoding="utf-8")

    assert "OBSWebSocketService" not in source


def test_onboarding_obs_probe_receives_obs_service_factory():
    services = []

    class _Signal:
        def connect(self, slot):
            self.slot = slot

    class _Service:
        def __init__(self, settings, parent):
            self.settings = settings
            self.parent = parent
            self.state_changed = _Signal()
            self.scenes_updated = _Signal()
            self.scenes = ["main"]
            self.starts = 0
            self.stops = []

        def start(self):
            self.starts += 1

        def stop(self, **kwargs):
            self.stops.append(kwargs)

    def _factory(settings, parent):
        service = _Service(settings, parent)
        services.append(service)
        return service

    probe = OnboardingOBSProbe(_factory)
    service = services[0]

    probe.connect_to(4456, "secret")
    probe.stop()
    probe.shutdown()

    assert probe.scenes == ["main"]
    assert service.parent is probe
    assert service.settings.websocket_port() == 4456
    assert service.settings.password() == "secret"
    assert service.starts == 1
    assert service.stops == [{}, {}, {"wait": True}]
