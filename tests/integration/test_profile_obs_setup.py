from pathlib import Path

from PySide6.QtCore import Qt

from solin.controllers.onboarding_obs_probe import OnboardingOBSProbe
from solin.ui.qml.onboarding import OnboardingBridge


class _Signal:
    def connect(self, _slot):
        return None


class _OnboardingProbe:
    state_changed = _Signal()
    scenes_updated = _Signal()

    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class _CongregationLookup:
    suggestions_ready = _Signal()
    schedule_ready = _Signal()
    failed = _Signal()


def _bridge() -> OnboardingBridge:
    return OnboardingBridge(
        language_manager=None,
        onboarding_service=object(),
        obs_probe=_OnboardingProbe(),
        congregation_lookup=_CongregationLookup(),
    )


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


def test_qml_onboarding_host_tracks_mouse_for_hover_and_cursor_state():
    source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

    assert "configure_qml_host(" in source
    assert "mouse_tracking=True" in source


def test_qml_onboarding_signal_handlers_use_formal_parameters():
    source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")

    assert 'onToggled: checked => onboardingBridge.updateField("obsAutomatic", checked)' in source
    assert 'onSelected: value => onboardingBridge.updateField("obsDefaultScene", value)' in source
    assert 'onSelected: value => onboardingBridge.updateField("obsMediaScene", value)' in source
    assert 'onToggled: onboardingBridge.updateField("obsAutomatic", checked)' not in source
    assert 'onSelected: onboardingBridge.updateField("obsDefaultScene", value)' not in source
    assert 'onSelected: onboardingBridge.updateField("obsMediaScene", value)' not in source


def test_qml_onboarding_hotkey_capture_is_explicit_and_one_shot():
    source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")

    assert "property bool recording: false" in source
    assert "onboardingBridge.captureHotkey(event.key, event.modifiers)" in source
    assert "hotkeyCapture.recording = false" in source
    assert "root.forceActiveFocus()" in source


def test_qml_onboarding_language_sheet_uses_flickable_list_handlers():
    source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")

    assert "id: languageList" in source
    assert "TapHandler" in source
    assert "HoverHandler" in source
    assert "id: languageScrollBar" in source
    assert "opacity: (languageScrollBar.active" in source
    assert "root.borderStrong" not in source
    assert "id: languageScroll\n" not in source


def test_qml_onboarding_manual_download_option_uses_explicit_label_and_icon():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")
    icons_source = Path("src/solin/styles/icons.py").read_text(encoding="utf-8")

    assert 'iconName: "manual_download"' in qml_source
    assert 'title: qsTr("Manual download")' in qml_source
    assert "Download when used" not in qml_source
    assert '"manual_download": ICON_MANUAL_DOWNLOAD' in bridge_source
    assert "ICON_MANUAL_DOWNLOAD" in icons_source


def test_qml_onboarding_media_language_uses_media_icon():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")
    icons_source = Path("src/solin/styles/icons.py").read_text(encoding="utf-8")

    assert 'iconName: "media_language"' in qml_source
    assert '"media_language": ICON_MEDIA_LANGUAGE' in bridge_source
    assert "ICON_MEDIA_LANGUAGE" in icons_source


def test_qml_onboarding_scene_choice_uses_custom_polished_popup():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

    assert "component SceneChoice: ColumnLayout" in qml_source
    assert "delegate: ItemDelegate" in qml_source
    assert "popup: Popup" in qml_source
    assert "popupScrollbarGutter" in qml_source
    assert "rightMargin: sceneChoice.popupScrollbarGutter" in qml_source
    assert "root.accentTint" in qml_source
    assert '"chevron_down": ICON_CHEVRON_DOWN' in bridge_source


def test_qml_onboarding_obs_action_uses_connect_language():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")
    obs_status_source = Path("src/solin/ui/obs_status_text.py").read_text(encoding="utf-8")

    assert 'qsTr("Connect")' in qml_source
    assert "Test connection" not in qml_source
    assert "Not connected" in bridge_source
    assert "Not connected" in obs_status_source
    assert "Incorrect password." in obs_status_source
    assert "OBS requires a password but none was provided." in obs_status_source
    assert "translated_obs_status_text" in bridge_source
    assert "Not tested" not in bridge_source
    assert "Connect to OBS or choose Set up later." in bridge_source
    assert "Test the OBS connection" not in bridge_source


def test_qml_onboarding_dynamic_messages_are_translatable():
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

    assert 'QCoreApplication.translate("OnboardingView", "Enter a profile name.")' in bridge_source
    assert "Record the Zoom share shortcut." in bridge_source
    assert "Choose the target in Zoom's share dialog." in bridge_source
    assert "The share target picker is unavailable." in bridge_source
    assert "root.draft.errorText" in Path("src/solin/qml/OnboardingView.qml").read_text(
        encoding="utf-8"
    )


def test_qml_onboarding_copy_stays_operational_and_evergreen():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")

    assert 'qsTr("Set up a clean, reliable workspace for meetings and media.")' in qml_source
    assert 'qsTr("Prepare Solin for your setup")' in qml_source
    assert (
        'qsTr("Choose languages and decide how meeting media should be handled.")'
        in qml_source
    )
    assert 'qsTr("Download automatically")' in qml_source
    assert (
        'qsTr("Only download media when you click the cloud button.")'
        in qml_source
    )
    assert 'qsTr("Choose how Solin should help")' in qml_source
    assert (
        'qsTr("Use OBS scenes and cameras from Solin. Automatic switching is optional.")'
        in qml_source
    )
    assert 'qsTr("Automatic scene switching")' in qml_source
    assert 'qsTr("Set up Zoom sharing")' in qml_source
    assert (
        'qsTr("Record the share shortcut and choose the click target Solin should use.")'
        in qml_source
    )

    assert 'qsTr("Each profile keeps its own settings and playlists.")' in qml_source
    assert (
        'qsTr("Keep this week and next week available offline.")'
        in qml_source
    )
    assert 'qsTr("Choose one option, both, or set them up later.")' in qml_source
    assert 'qsTr("Everything looks ready")' in qml_source
    assert (
        'qsTr("Review your choices. You can change them later in Settings.")'
        in qml_source
    )

    assert "Make Solin yours" not in qml_source
    assert "Download ahead" not in qml_source
    assert "How do you want to use Solin?" not in qml_source


def test_qml_onboarding_obs_help_link_opens_setup_guide():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

    assert 'qsTr("Need help setting up OBS?")' in qml_source
    assert "onboardingBridge.openObsSetupGuide()" in qml_source
    assert "_OBS_SETUP_GUIDE_URL" in bridge_source
    assert "https://solinav.vercel.app/guide/#obs-studio-integration" in bridge_source


def test_qml_onboarding_zoom_help_link_opens_setup_guide():
    qml_source = Path("src/solin/qml/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")

    assert 'qsTr("Need help setting up Zoom?")' in qml_source
    assert "onboardingBridge.openZoomSetupGuide()" in qml_source
    assert "_ZOOM_SETUP_GUIDE_URL" in bridge_source
    assert "https://solinav.vercel.app/guide/#zoom-meetings-integration" in bridge_source


def test_onboarding_hotkey_capture_reports_only_complete_shortcuts():
    bridge = _bridge()

    assert bridge.captureHotkey(int(Qt.Key.Key_Control), 0) is False
    assert bridge.state["zoomHotkey"] == ""

    modifiers = int(
        Qt.KeyboardModifier.ControlModifier.value
        | Qt.KeyboardModifier.ShiftModifier.value
    )
    assert bridge.captureHotkey(int(Qt.Key.Key_S), modifiers) is True
    assert bridge.state["zoomHotkey"] == "Ctrl+Shift+S"


def test_onboarding_keeps_target_picker_alive_after_pick_for_marker_feedback():
    bridge = _bridge()
    picker = object()
    bridge._target_picker = picker

    bridge._on_target_picked(300, 250)

    assert bridge._target_picker is picker
    assert bridge.state["zoomTargetConfigured"] is True


def test_onboarding_target_picker_receives_current_saved_position():
    captured = {}

    class _PickerSignal:
        def connect(self, slot):
            self.slot = slot

    class _Picker:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.position_picked = _PickerSignal()
            self.destroyed = _PickerSignal()
            self.cancelled = _PickerSignal()
            self.shown = False

        def show_overlay(self):
            self.shown = True

    def _picker_factory(**kwargs):
        return _Picker(**kwargs)

    bridge = OnboardingBridge(
        language_manager=None,
        onboarding_service=object(),
        obs_probe=_OnboardingProbe(),
        congregation_lookup=_CongregationLookup(),
        target_picker_factory=_picker_factory,
    )
    bridge._state["zoomAvailable"] = True
    bridge._state["zoomTargetConfigured"] = True
    bridge._state["zoomClickX"] = 300
    bridge._state["zoomClickY"] = 250

    bridge.configureZoomTarget()

    assert captured["current_x"] == 300
    assert captured["current_y"] == 250
    assert captured["parent"] is None


def test_onboarding_reselects_obs_after_skip_back_and_successful_continue():
    bridge = _bridge()
    bridge._state.update(
        {
            "currentPage": "obs",
            "obsSelected": True,
            "zoomSelected": True,
        }
    )

    bridge.skipCurrentIntegration()

    assert bridge.state["obsSelected"] is False
    assert bridge.state["currentPage"] == "zoom"

    bridge.back()
    bridge._state.update(
        {
            "obsConnected": True,
            "obsAutomatic": False,
        }
    )

    bridge.advance()

    assert bridge.state["obsSelected"] is True
    assert bridge.state["currentPage"] == "zoom"


def test_onboarding_reselects_zoom_after_skip_back_and_successful_continue():
    bridge = _bridge()
    bridge._state.update(
        {
            "currentPage": "zoom",
            "zoomSelected": True,
            "zoomAvailable": True,
        }
    )

    bridge.skipCurrentIntegration()

    assert bridge.state["zoomSelected"] is False
    assert bridge.state["currentPage"] == "review"

    bridge.back()
    bridge._state.update(
        {
            "zoomHotkey": "Ctrl+Shift+S",
            "zoomTargetConfigured": True,
        }
    )

    bridge.advance()

    assert bridge.state["zoomSelected"] is True
    assert bridge.state["currentPage"] == "review"


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
