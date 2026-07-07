from pathlib import Path

from solin.controllers.onboarding_obs_probe import OnboardingOBSProbe


def test_profile_screen_hosts_qml_onboarding_instead_of_legacy_obs_mixin():
    source = Path("src/solin/ui/profile_screen.py").read_text(encoding="utf-8")

    assert "OnboardingQmlHost" in source
    assert "ProfileOBSSetupMixin" not in source
    assert not Path("src/solin/ui/profile_obs_setup.py").exists()


def test_profile_screen_onboarding_uses_typed_settings_stores():
    source = Path("src/solin/ui/profile_screen.py").read_text(encoding="utf-8")

    assert "SettingsKey" not in source
    assert ".setValue(" not in source


def test_qml_onboarding_does_not_construct_obs_services_directly():
    source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

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
