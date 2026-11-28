from pathlib import Path

import pytest

from PySide6.QtCore import QCoreApplication, QObject, QPointF, QSize, QTranslator, Signal, Qt
from PySide6.QtGui import QAccessible, QAccessibleActionInterface, QDesktopServices
from PySide6.QtTest import QSignalSpy
from PySide6.QtQuickWidgets import QQuickWidget

from solin.core.meetings.schedule import MIDWEEK, WEEKEND, MeetingSchedule, MeetingSlot
from solin.core.profiles.models import ProfileInfo
from solin.core.scenes.virtual_camera_identity import virtual_camera_device_name
from tests._paths import REPO_ROOT
from tests._qt import dispose_widget, key_click, mouse_click, show_and_activate, wait_until

from solin.ui.qml.onboarding import OnboardingBridge, OnboardingQmlHost


class _CongregationLookup(QObject):
    suggestions_ready = Signal(list)
    schedule_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.schedule_requests = []
        self.searches = []

    def fetch_schedule(self, guid):
        self.schedule_requests.append(guid)

    def search(self, text):
        self.searches.append(text)


class _OnboardingService:
    def __init__(self):
        self.commands = []
        self.fail = False
        self.during_complete = None

    def complete(self, command):
        self.commands.append(command)
        if self.during_complete is not None:
            self.during_complete()
        if self.fail:
            raise OSError("profile storage unavailable")
        return ProfileInfo("main_hall", command.name)


class _Picker(QObject):
    position_picked = Signal(int, int)
    cancelled = Signal()

    def __init__(self, owner, **kwargs):
        super().__init__(owner)
        self.arguments = kwargs
        self.shown = False
        self.closed = False

    def show_overlay(self):
        self.shown = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def available_zoom(monkeypatch):
    monkeypatch.setattr(OnboardingBridge, "_zoom_capability", staticmethod(lambda: (True, "")))


@pytest.fixture
def bridge_factory(qt_object_owner):
    bridges = []

    def create(**kwargs):
        kwargs.setdefault("language_manager", None)
        kwargs.setdefault("onboarding_service", _OnboardingService())
        kwargs.setdefault("congregation_lookup", _CongregationLookup(qt_object_owner))
        bridge = OnboardingBridge(**kwargs, parent=qt_object_owner)
        bridges.append(bridge)
        return bridge

    yield create
    for bridge in reversed(bridges):
        bridge.shutdown()


def _advance_to_integrations(bridge):
    bridge.start("Main Hall", allow_cancel=True)
    bridge.advance()
    bridge.advance()
    assert bridge.state["currentPage"] == "integrations"


def _advance_to_zoom(bridge):
    _advance_to_integrations(bridge)
    bridge.updateField("zoomSelected", True)
    bridge.advance()
    assert bridge.state["currentPage"] == "zoom"


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


def test_qml_onboarding_hotkey_capture_is_explicit_and_one_shot():
    source = Path("src/solin/qml/Solin/OnboardingView.qml").read_text(encoding="utf-8")

    assert "property bool recording: false" in source
    assert "onboardingBridge.captureHotkey(event.key, event.modifiers)" in source
    assert "hotkeyCapture.recording = false" in source
    assert "root.forceActiveFocus()" in source


def test_qml_onboarding_language_sheet_uses_flickable_list_handlers():
    source = Path("src/solin/qml/Solin/OnboardingView.qml").read_text(encoding="utf-8")

    assert "id: languageList" in source
    assert "TapHandler" in source
    assert "HoverHandler" in source
    assert "AppSelectionList {" in source
    assert "width: languageList.delegateWidth" in source
    assert "root.borderStrong" not in source
    assert "id: languageScroll\n" not in source


def test_qml_onboarding_manual_download_option_uses_explicit_label_and_icon():
    qml_source = Path("src/solin/qml/Solin/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")
    icons_source = Path("src/solin/styles/icons.py").read_text(encoding="utf-8")

    assert 'iconName: "manual_download"' in qml_source
    assert "Download when used" not in qml_source
    assert '"manual_download": ICON_MANUAL_DOWNLOAD' in bridge_source
    assert "ICON_MANUAL_DOWNLOAD" in icons_source


def test_qml_onboarding_media_language_uses_media_icon():
    qml_source = Path("src/solin/qml/Solin/OnboardingView.qml").read_text(encoding="utf-8")
    bridge_source = Path("src/solin/ui/qml/onboarding.py").read_text(encoding="utf-8")
    icons_source = Path("src/solin/styles/icons.py").read_text(encoding="utf-8")

    assert 'iconName: "media_language"' in qml_source
    assert '"media_language": ICON_MEDIA_LANGUAGE' in bridge_source
    assert "ICON_MEDIA_LANGUAGE" in icons_source


def test_qml_onboarding_places_congregation_choice_with_profile_name():
    qml_source = Path("src/solin/qml/Solin/OnboardingView.qml").read_text(encoding="utf-8")
    profile_page = qml_source.split("id: profilePage", 1)[1].split("id: preferencesPage", 1)[0]
    preferences_page = qml_source.split("id: preferencesPage", 1)[1].split(
        "id: integrationsPage", 1
    )[0]

    assert "onClicked: congregationSheet.openSheet()" in profile_page
    assert "onClicked: congregationSheet.openSheet()" not in preferences_page


def test_onboarding_congregation_replaces_untouched_profile_name(bridge_factory):
    bridge = bridge_factory()

    bridge.start("Profile 2", allow_cancel=True)
    bridge.chooseCongregation("central-id", "Central")
    bridge.chooseCongregation("north-id", "North")

    assert bridge.state["profileName"] == "North"


def test_onboarding_congregation_preserves_user_profile_name_unless_empty(bridge_factory):
    bridge = bridge_factory()

    bridge.start("Profile 2", allow_cancel=True)
    bridge.updateField("profileName", "Main hall")
    bridge.chooseCongregation("central-id", "Central")
    assert bridge.state["profileName"] == "Main hall"

    bridge.updateField("profileName", "  ")
    bridge.chooseCongregation("north-id", "North")
    assert bridge.state["profileName"] == "North"


def test_onboarding_hotkey_capture_reports_only_complete_shortcuts(bridge_factory):
    bridge = bridge_factory()

    assert bridge.captureHotkey(int(Qt.Key.Key_Control), 0) is False
    assert bridge.state["zoomHotkey"] == ""

    modifiers = int(
        Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.ShiftModifier.value
    )
    assert bridge.captureHotkey(int(Qt.Key.Key_S), modifiers) is True
    assert bridge.state["zoomHotkey"] == "Ctrl+Shift+S"


def test_onboarding_reselects_zoom_after_skip_back_and_successful_continue(bridge_factory):
    bridge = bridge_factory()
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


def test_onboarding_default_navigation_needs_no_optional_integration(bridge_factory):
    bridge = bridge_factory()
    bridge.start("Main Hall", allow_cancel=True)

    assert bridge.state["zoomSelected"] is False
    for current, following in [
        ("profile", "preferences"),
        ("preferences", "integrations"),
        ("integrations", "review"),
    ]:
        assert bridge.state["currentPage"] == current
        bridge.advance()
        assert bridge.state["currentPage"] == following
        assert bridge.state["errorText"] == ""


@pytest.fixture
def onboarding_host(request, qt_object_owner):
    host = OnboardingQmlHost(
        language_manager=None,
        onboarding_service=_OnboardingService(),
        congregation_lookup=_CongregationLookup(qt_object_owner),
        target_picker_factory=None,
    )

    def cleanup():
        host.shutdown()
        dispose_widget(host)

    request.addfinalizer(cleanup)
    assert host.status() == QQuickWidget.Status.Ready, [error.toString() for error in host.errors()]
    show_and_activate(host, size=QSize(800, 560))
    _advance_to_integrations(host.bridge)
    wait_until(
        lambda: not host.rootObject().findChild(QObject, "onboardingPageStack").property("busy"),
        description="onboarding page transition",
    )
    return host


def _quick_item(root, name):
    if root.objectName() == name:
        return root
    for child in root.childItems():
        found = _quick_item(child, name)
        if found is not None:
            return found
    return None


def test_scenes_card_stays_checked_for_pointer_keyboard_and_accessibility(onboarding_host):
    host = onboarding_host
    scenes = _quick_item(host.rootObject(), "onboardingScenesCard")
    assert scenes is not None
    assert scenes.property("selected") is True
    assert scenes.property("selectable") is False
    assert scenes.isEnabled()
    mouse_click(
        host,
        Qt.MouseButton.LeftButton,
        pos=scenes.mapToScene(QPointF(scenes.width() / 2, scenes.height() / 2)).toPoint(),
    )
    scenes.forceActiveFocus()
    key_click(host, Qt.Key.Key_Space)
    key_click(host, Qt.Key.Key_Return)
    accessible = QAccessible.queryAccessibleInterface(scenes)
    assert accessible.state().checked
    assert accessible.state().readOnly
    accessible.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
    assert scenes.property("selected") is True
    assert host.bridge.state["zoomSelected"] is False


def test_zoom_card_remains_optional_and_can_toggle_with_each_input(onboarding_host):
    host = onboarding_host
    zoom = _quick_item(host.rootObject(), "onboardingZoomCard")
    assert zoom.property("selected") is False
    zoom.forceActiveFocus()
    key_click(host, Qt.Key.Key_Space)
    assert host.bridge.state["zoomSelected"] is True
    accessible = QAccessible.queryAccessibleInterface(zoom)
    accessible.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
    assert host.bridge.state["zoomSelected"] is False
    mouse_click(
        host,
        Qt.MouseButton.LeftButton,
        pos=zoom.mapToScene(QPointF(zoom.width() / 2, 40)).toPoint(),
    )
    assert host.bridge.state["zoomSelected"] is True
    host.bridge.advance()
    assert host.bridge.state["currentPage"] == "zoom"


def test_cards_fit_minimum_window_with_separated_wrapped_descriptions(onboarding_host):
    root = onboarding_host.rootObject()
    footer = _quick_item(root, "onboardingFooter")
    cards = [_quick_item(root, name) for name in ("onboardingScenesCard", "onboardingZoomCard")]
    for card in cards:
        description = _quick_item(card, "integrationDescription")
        assert description.property("text")
        assert description.height() >= description.property("contentHeight")
        assert description.mapToScene(QPointF(0, description.height())).y() <= footer.y()
        assert 0 <= card.mapToScene(QPointF()).x() < root.width()
        assert card.mapToScene(QPointF(card.width(), 0)).x() <= root.width()
    assert cards[0].height() == cards[1].height()
    assert (
        _quick_item(cards[0], "integrationDescription").y()
        == _quick_item(cards[1], "integrationDescription").y()
    )


def test_unavailable_zoom_card_cannot_toggle_but_scenes_remains_available(onboarding_host):
    host = onboarding_host
    host.bridge._state.update(zoomAvailable=False, zoomUnavailableReason="unavailable")
    host.bridge.stateChanged.emit()
    zoom = _quick_item(host.rootObject(), "onboardingZoomCard")
    scenes = _quick_item(host.rootObject(), "onboardingScenesCard")
    assert not zoom.isEnabled()
    assert scenes.isEnabled()
    accessible = QAccessible.queryAccessibleInterface(zoom)
    accessible.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
    assert host.bridge.state["zoomSelected"] is False
    assert scenes.property("selected") is True
    description = _quick_item(zoom, "integrationDescription")
    opacity = 1.0
    item = description
    while item is not None:
        opacity *= item.opacity()
        item = item.parentItem()
    assert opacity == 1.0


@pytest.mark.parametrize(
    "locale",
    sorted(
        path.stem
        for path in (REPO_ROOT / "src/solin/resources/translations/locales").glob("*.json")
    ),
)
def test_localized_card_descriptions_fit_and_resolve_camera_name(
    compiled_translation_catalogs,
    locale,
    qt_object_owner,
    monkeypatch,
):
    monkeypatch.setattr("solin.ui.qml.onboarding.sys.platform", "linux")
    translator = QTranslator()
    assert translator.load(str(compiled_translation_catalogs / f"solin_{locale}.qm"))
    app = QCoreApplication.instance()
    app.installTranslator(translator)
    host = None
    try:
        host = OnboardingQmlHost(
            language_manager=None,
            onboarding_service=_OnboardingService(),
            congregation_lookup=_CongregationLookup(qt_object_owner),
            target_picker_factory=None,
        )
        assert host.status() == QQuickWidget.Status.Ready
        show_and_activate(host, size=QSize(800, 560))
        _advance_to_integrations(host.bridge)
        wait_until(
            lambda: (
                not host.rootObject().findChild(QObject, "onboardingPageStack").property("busy")
            ),
            description=f"{locale} onboarding layout",
        )
        for name in ("onboardingScenesCard", "onboardingZoomCard"):
            card = _quick_item(host.rootObject(), name)
            description = _quick_item(card, "integrationDescription")
            assert description.property("contentWidth") <= description.width() + 1
            assert description.height() >= description.property("contentHeight")
            assert (
                description.mapToItem(card, QPointF(0, description.height())).y() <= card.height()
            )
        zoom = _quick_item(host.rootObject(), "onboardingZoomCard")
        assert host.bridge.virtualCameraDeviceName in zoom.property("description")
        assert "%1" not in zoom.property("description")
    finally:
        if host is not None:
            host.shutdown()
            dispose_widget(host)
        app.removeTranslator(translator)


@pytest.mark.parametrize(
    ("platform", "device", "requirement"),
    [
        ("win32", "Solin Virtual Camera", None),
        ("darwin", "OBS Virtual Camera", "OBS"),
        ("linux", "OBS Virtual Camera", "v4l2loopback"),
    ],
)
def test_virtual_camera_guidance_matches_platform(
    bridge_factory, monkeypatch, platform, device, requirement
):
    monkeypatch.setattr("solin.ui.qml.onboarding.sys.platform", platform)
    bridge = bridge_factory()
    assert bridge.virtualCameraDeviceName == device == virtual_camera_device_name(platform)
    if requirement is None:
        assert bridge.virtualCameraRequirement == ""
    else:
        assert requirement in bridge.virtualCameraRequirement


def test_unavailable_zoom_does_not_block_scenes_or_profile_completion(bridge_factory, monkeypatch):
    monkeypatch.setattr(
        OnboardingBridge, "_zoom_capability", staticmethod(lambda: (False, "unavailable"))
    )
    service = _OnboardingService()
    bridge = bridge_factory(onboarding_service=service)
    completed = QSignalSpy(bridge.completed)
    _advance_to_integrations(bridge)
    bridge.advance()
    bridge.advance()
    assert completed.count() == 1
    assert service.commands[0].zoom_share.enabled is False


def test_onboarding_complete_preserves_profile_choices_and_zoom_setup(bridge_factory):
    service = _OnboardingService()
    bridge = bridge_factory(onboarding_service=service)
    _advance_to_zoom(bridge)
    bridge.chooseLanguage("interface", "pt_BR")
    bridge.chooseLanguage("media", "S")
    bridge.updateField("downloadMeetingMedia", True)
    schedule = MeetingSchedule(MeetingSlot.from_values(MIDWEEK, 2, "19:45"), MeetingSlot(WEEKEND))
    bridge._meeting_schedule = schedule
    modifiers = int(
        Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.ShiftModifier.value
    )
    bridge.captureHotkey(int(Qt.Key.Key_S), modifiers)
    bridge._on_target_picked(300, 250)
    bridge.advance()
    completed = QSignalSpy(bridge.completed)
    bridge.advance()
    assert completed.count() == 1
    command = service.commands[0]
    assert command.interface_language == "pt_BR"
    assert command.media_language == "S"
    assert command.download_meeting_media is True
    assert command.meeting_schedule == schedule
    assert command.zoom_share.enabled is True
    assert (command.zoom_share.click_x, command.zoom_share.click_y) == (300, 250)


def test_onboarding_can_retry_failed_completion_and_ignores_reentrant_submit(bridge_factory):
    service = _OnboardingService()
    service.fail = True
    bridge = bridge_factory(onboarding_service=service)
    service.during_complete = bridge.advance
    _advance_to_integrations(bridge)
    bridge.advance()
    completed = QSignalSpy(bridge.completed)
    bridge.advance()
    assert completed.count() == 0
    assert bridge.state["busy"] is False
    assert bridge.state["errorText"]
    assert len(service.commands) == 1
    service.fail = False
    bridge.advance()
    assert completed.count() == 1
    assert len(service.commands) == 2


def test_target_picker_reuses_position_and_closes_on_cancel(bridge_factory, qt_object_owner):
    pickers = []

    def factory(**kwargs):
        picker = _Picker(qt_object_owner, **kwargs)
        pickers.append(picker)
        return picker

    bridge = bridge_factory(target_picker_factory=factory)
    _advance_to_zoom(bridge)
    bridge.configureZoomTarget()
    assert pickers[0].shown
    pickers[0].position_picked.emit(300, 250)
    assert bridge._target_picker is pickers[0]
    bridge.configureZoomTarget()
    assert pickers[0].closed
    assert (pickers[1].arguments["current_x"], pickers[1].arguments["current_y"]) == (300, 250)
    cancelled = QSignalSpy(bridge.cancelled)
    bridge.cancel()
    assert pickers[1].closed
    assert cancelled.count() == 1


def test_zoom_help_opens_existing_setup_guide(bridge_factory, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    bridge_factory().openZoomSetupGuide()
    assert opened == ["https://solinav.vercel.app/guide/#zoom-meetings-integration"]
