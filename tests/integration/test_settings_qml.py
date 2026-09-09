"""Settings navigation and controls exercised through the real Qt Quick surface."""
from __future__ import annotations

import os
import math
import subprocess
import sys
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QObject, QPointF, Property, Qt, QTranslator, QUrl, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtQml import QQmlExpression
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from solin.styles.icons import ICON_NAV_SETTINGS
from solin.styles.theme import PALETTE, activate_theme, current_theme
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.settings.catalogue import SECTIONS
from solin.ui.qml.settings.domain import SettingsDomain
from solin.ui.qml.settings.navigation import SettingsNavigation
from solin.ui.qml.svg_icons import SvgIconProvider


class RecordingDomain(SettingsDomain):
    def __init__(self, state):
        super().__init__()
        self.calls = []
        self.reject_enable = False
        self.publish(**state)

    @Slot(str, "QVariant")
    def setValue(self, key, value):  # noqa: N802 - QML slot
        self.calls.append((key, value))
        if key == "enabled" and value and self.reject_enable:
            self.fail("Configuration required.")
            return
        self.publish(**{key: value})

    @Slot(str)
    def invoke(self, action):
        self.calls.append((action,))

    @Property(int, constant=True)
    def searchDebounceMs(self):  # noqa: N802 - QML API
        return 900

    @Slot(str)
    def searchCongregation(self, query):  # noqa: N802 - QML API
        self.calls.append(("searchCongregation", query))

    @Slot(str, str)
    def chooseCongregation(self, guid, name):  # noqa: N802 - QML API
        self.calls.append(("chooseCongregation", guid, name))


def _domain_states():
    states = {name: {} for name in ("general", "integrations", "remote")}
    for section in SECTIONS:
        for group in _catalogue_groups(section.groups):
            state = states[group.domain]
            if group.visible_when:
                state[group.visible_when] = True
            if group.status_key:
                state[group.status_key] = "Ready"
            for row in group.rows:
                state.setdefault(row.key, False if row.kind == "toggle" else
                                 [] if row.kind in {"screens", "shortcuts"} else "")
                if row.options:
                    state[row.options] = [{"value": "one", "label": "Example option"}]
                if row.enabled_when:
                    state[row.enabled_when] = True
                if row.visible_when:
                    state[row.visible_when] = False
                if row.description_key:
                    state[row.description_key] = "Description of this setting."
    states["general"].update(
        themeId="dark", themeOptions=[{"value": "dark", "label": "Dark"},
                                       {"value": "light", "label": "Light"}],
        interfaceLanguage="pt", mediaLanguage="T", yeartextQuote="Annual text",
        yeartextReference="Isaiah 41:10", midweekTime="19:30", weekendTime="09:30",
        mediaLanguagesError="", yeartextDirty=True, scenesRestartRequired=False,
        congregationQuery="", congregationName="", congregationSuggestions=[],
        congregationStatusKind="idle",
        congregationStatusText="Search your congregation to fill the days and times below.",
    )
    states["integrations"].update(
        shortcutEditorOpen=False, shortcutEnabled=True, shortcutError="", shortcutEvent="play",
        shortcutEvents=[{"value": "play", "label": "Play"}], shortcutSequence="",
        shortcutShareMode=False, shortcuts=[], shortcutCaptureText="", shortcutKeyLabel="",
    )
    states["remote"].update(
        setupVisible=False, setupUrl="", fingerprint="", fingerprintVisible=False,
        qrBusy=False, qrError="", qrImage="", verificationCode="",
        endpoint="https://192.168.1.2:8443", username="", password="", passwordConfirmation="",
    )
    return states


def _catalogue_groups(groups):
    for group in groups:
        yield group
        yield from _catalogue_groups(group.children)


def _translated_groups(groups):
    for group in groups:
        yield group
        yield from _translated_groups(group.get("children", []))


def _items(item):
    yield item
    for child in item.childItems():
        yield from _items(child)


def _find(root, name):
    return next((item for item in _items(root) if item.objectName() == name), None)


def _control(row, class_fragment):
    controls = [item for item in _items(row)
                if class_fragment in item.metaObject().className() and item.isVisible()]
    assert controls, [(item.metaObject().className(), item.width(), item.height(), item.isVisible())
                      for item in _items(row)]
    return controls[0]


def _settle():
    QApplication.processEvents()
    QTest.qWait(210)
    QApplication.processEvents()


@pytest.fixture
def create_view():
    surfaces = []
    original_theme = current_theme().id

    def create(width=1000, height=740, theme="dark"):
        activate_theme(theme)
        domains = {name: RecordingDomain(state) for name, state in _domain_states().items()}
        navigation = SettingsNavigation(domains)
        widget = QQuickWidget()
        widget.resize(width, height)
        warnings = []
        widget.engine().warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
        icons = {name: ICON_NAV_SETTINGS for name in (
            "settings", "appearance", "media", "meetings", "projection", "integrations",
            "automations", "remote", "about", "back", "next", "down", "up", "close",
        )}
        handle = configure_qml_host(
            widget, type_name="SettingsView", clear_color=PALETTE.bg0,
            context_properties={"settingsGeneral": domains["general"],
                                "settingsIntegrations": domains["integrations"],
                                "settingsRemote": domains["remote"],
                                "settingsNavigation": navigation},
            image_providers={"settingsicons": SvgIconProvider(icons, default_icon="settings")},
            mouse_tracking=True,
        )
        view = SimpleNamespace(widget=widget, root=widget.rootObject(), domains=domains,
                               navigation=navigation, warnings=warnings, handle=handle)
        surfaces.append(view)
        widget.show()
        _settle()
        assert widget.errors() == [], "\n".join(error.toString() for error in widget.errors())
        assert view.root is not None
        return view

    yield create
    for view in reversed(surfaces):
        view.widget.setSource(QUrl())
        shiboken6.delete(view.widget)
    activate_theme(original_theme)


@pytest.mark.parametrize("width", [360, 480, 839, 840, 1280])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_settings_all_sections_fit_available_width(create_view, width, theme):
    view = create_view(width=width, theme=theme)
    assert view.root.property("compact") is (width < 840)
    assert view.root.property("showingPage") is (width >= 840)
    sections = _find(view.root, "settingsSections")
    assert sections is not None
    assert sections.isVisible()
    if width >= 840:
        pane = _find(view.root, "settingsNavigationPane")
        assert pane.width() == pytest.approx(292, abs=1)
        assert sections.property("delegateWidth") == pytest.approx(
            pane.width() - 40, abs=1
        )
    assert len(view.navigation.sections) == 8
    for section in view.navigation.sections:
        for group in _translated_groups(section["groups"]):
            view.navigation.expand(group["id"], True)
        view.navigation.openSection(section["id"], "", "")
        _settle()
        assert view.root.property("showingPage") is True
        assert sections.isVisible() is (width >= 840)
        for group in _translated_groups(section["groups"]):
            for row in group["rows"]:
                item = _find(view.root, "setting_" + row["key"])
                assert item is not None, row["key"]
                state = view.domains[group["domain"]].state
                if not row["visibleWhen"] or state.get(row["visibleWhen"]):
                    assert item.isVisible(), row["key"]
        for item in _items(view.root):
            if item.objectName().startswith("setting_") and item.isVisible():
                left = item.mapToItem(view.root, QPointF(0, 0)).x()
                assert left >= -1, item.objectName()
                assert left + item.width() <= width + 1, item.objectName()
                assert item.height() >= 44, item.objectName()
        assert view.widget.errors() == []
    assert view.warnings == []


def test_compact_search_result_reveals_control_and_back_restores_query(create_view):
    view = create_view(width=360)
    view.navigation.search("websocket")
    _settle()
    assert view.root.property("showingSearch") is True
    results = _find(view.root, "settingsResults")
    results.forceActiveFocus()
    results.setProperty("currentIndex", 0)
    QTest.keyClick(view.widget, Qt.Key.Key_Return)
    _settle()
    assert view.navigation.currentSection["id"] == "integrations"
    assert view.navigation.targetKey == "obsPort"
    row = _find(view.root, "setting_obsPort")
    assert row is not None and row.isVisible()
    assert row.property("highlighted") is True
    assert view.widget.quickWindow().activeFocusItem() in list(_items(row))
    QTest.keyClick(view.widget, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    _settle()
    assert view.navigation.query == "websocket"
    assert view.root.property("showingSearch") is True
    assert view.warnings == []


def test_toggle_dispatch_and_draft_survive_page_navigation_and_resize(create_view):
    view = create_view()
    view.navigation.openSection("media", "playback", "startVideosPaused")
    _settle()
    row = _find(view.root, "setting_startVideosPaused")
    toggle = _control(row, "Switch")
    position = toggle.mapToItem(view.root, QPointF(toggle.width() / 2, toggle.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    _settle()
    assert ("startVideosPaused", True) in view.domains["general"].calls
    view.navigation.openSection("projection", "yeartext", "yeartextReference")
    _settle()
    row = _find(view.root, "setting_yeartextReference")
    field = _control(row, "TextField")
    field.forceActiveFocus()
    QTest.keyClick(view.widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(view.widget, "Draft reference")
    _settle()
    assert view.domains["general"].state["yeartextReference"] == "Draft reference"
    for width in (360, 1280, 480):
        view.widget.resize(width, 360)
        _settle()
        assert view.navigation.currentSection["id"] == "projection"
        assert view.root.property("showingPage") is True
        assert _control(_find(view.root, "setting_yeartextReference"), "TextField").property("text") == "Draft reference"
    view.navigation.openSection("about", "", "")
    _settle()
    view.navigation.openSection("projection", "yeartext", "yeartextReference")
    _settle()
    assert _control(_find(view.root, "setting_yeartextReference"), "TextField").property("text") == "Draft reference"
    assert view.warnings == []


class _AccentedTranslator(QTranslator):
    def translate(self, context, source, disambiguation=None, n=-1):
        if context == "SettingsWidget" and source == "Playback protection":
            return "Proteção de reprodução"
        return ""


def test_translated_search_ignores_accents_and_private_values():
    translator = _AccentedTranslator()
    app = QApplication.instance()
    app.installTranslator(translator)
    try:
        domains = {
            name: RecordingDomain(state) for name, state in _domain_states().items()
        }
        navigation = SettingsNavigation(domains)
        navigation.search("PROTECAO reproducao")
        assert [result["key"] for result in navigation.results] == ["playbackProtection"]
        domains["remote"].setValue("password", "unique-private-secret")
        navigation.search("unique-private-secret")
        assert navigation.results == []
    finally:
        app.removeTranslator(translator)


def test_desktop_selection_control_is_to_right_of_label(create_view):
    view = create_view(width=1280)
    row = _find(view.root, "setting_themeId")
    button = _control(row, "Button")
    label = next(item for item in _items(row) if item.property("text") == "Theme")
    label_top = label.mapToItem(row, QPointF(0, 0)).y()
    button_top = button.mapToItem(row, QPointF(0, 0)).y()
    assert button_top < label_top + label.height()
    assert button_top + button.height() > label_top
    assert button.mapToItem(row, QPointF(0, 0)).x() > row.width() / 2
    assert view.warnings == []


@pytest.mark.parametrize("width", [360, 1280])
def test_section_header_controls_center_on_combined_heading(create_view, width):
    view = create_view(width=width)
    view.navigation.openSection("appearance", "", "")
    _settle()
    page = _find(view.root, "settingsPage")
    title = next(item for item in _items(page)
                 if item.property("text") == "Appearance and languages"
                 and item.isVisible())
    title_block = title.parentItem()
    heading = title_block.parentItem()
    icon = next(
        (item for item in _items(page)
         if item.objectName() == "settingsSectionTitleIcon" and item.isVisible()),
        None,
    )
    text_center = title_block.mapToItem(heading, QPointF(0, title_block.height() / 2)).y()
    if width < 840:
        back = _find(page, "settingsBackButton")
        back_center = back.mapToItem(heading, QPointF(0, back.height() / 2)).y()
        assert back.isVisible() and icon is None
        assert back_center == pytest.approx(text_center, abs=1)
        assert heading.mapToItem(page, QPointF(0, 0)).y() <= 18
    else:
        assert not _find(page, "settingsBackButton").isVisible()
        assert icon is not None and icon.isVisible()
        assert icon.width() == 24 and icon.height() == 24
        icon_center = icon.mapToItem(heading, QPointF(0, icon.height() / 2)).y()
        assert icon_center == pytest.approx(text_center, abs=1)
    assert view.warnings == []


@pytest.mark.parametrize("width", [360, 1280])
def test_action_status_aligns_with_actions_and_stacks_on_narrow_width(create_view, width):
    view = create_view(width=width)
    view.domains["general"].publish(yeartextStatusText="Annual text updated for 2026")
    view.navigation.openSection("projection", "yeartext", "yeartextStatusText")
    _settle()
    row = _find(view.root, "setting_yeartextStatusText")
    status = next(item for item in _items(row)
                  if item.property("text") == "Annual text updated for 2026")
    button = _control(row, "Button")
    label = next(item for item in _items(row)
                 if item.property("text") == "Update" and "Text" in item.metaObject().className()
                 and item is not button.property("contentItem"))
    expected_alignment = Qt.AlignmentFlag.AlignLeft if width < 480 else Qt.AlignmentFlag.AlignRight
    expression = QQmlExpression(view.widget.rootContext(), status, "Number(horizontalAlignment)")
    assert expression.evaluate()[0] == expected_alignment.value
    status_pos = status.mapToItem(row, QPointF(0, 0))
    button_pos = button.mapToItem(row, QPointF(0, 0))
    assert status_pos.y() + status.height() <= button_pos.y()
    assert status_pos.x() + status.width() == pytest.approx(button_pos.x() + button.width(), abs=1)
    if width < 480:
        label_pos = label.mapToItem(row, QPointF(0, 0))
        assert status_pos.y() >= label_pos.y() + label.height()
        assert status_pos.x() == pytest.approx(button_pos.x(), abs=1)
    else:
        assert status_pos.x() > row.width() / 2
    assert view.warnings == []


def test_settings_high_dpi_short_window_layout(create_view):
    # Qt samples scale environment variables while creating QApplication. Run
    # this scenario in a fresh process to exercise real 200% scaling.
    if os.environ.get("SOLIN_SETTINGS_DPI_TEST") != "1":
        environment = dict(os.environ, QT_SCALE_FACTOR="2", SOLIN_SETTINGS_DPI_TEST="1")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", __file__, "-k", "test_settings_high_dpi_short_window_layout",
             "-q", "--basetemp=.pytest-tmp-settings-dpi"],
            env=environment, capture_output=True, text=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    view = create_view(width=840, height=320)
    assert view.widget.devicePixelRatioF() >= 2
    assert view.root.width() == 840
    view.navigation.openSection("projection", "yeartext", "yeartextReference")
    _settle()
    row = _find(view.root, "setting_yeartextReference")
    assert row is not None and row.isVisible()
    position = row.mapToItem(view.root, QPointF(0, 0))
    assert 0 <= position.y() < view.root.height()
    assert row.width() + position.x() <= view.root.width() + 1
    assert view.warnings == []


def test_page_scroll_and_rapid_navigation_are_preserved(create_view):
    view = create_view(width=1000, height=360)
    view.navigation.openSection("media", "", "")
    _settle()
    flickable = _find(view.root, "settingsPageViewport")
    flickable.setProperty("contentY", 80.0)
    _settle()
    assert view.navigation.scrollPosition("media") == pytest.approx(80, abs=1)
    for name in ("projection", "integrations", "about", "media"):
        view.navigation.openSection(name, "", "")
    _settle()
    assert view.navigation.currentSection["id"] == "media"
    flickable = _find(view.root, "settingsPageViewport")
    assert flickable.property("contentY") == pytest.approx(80, abs=1)
    assert view.warnings == []


def test_settings_page_scrolls_with_a_mouse_drag(create_view):
    view = create_view(width=1000, height=360)
    view.navigation.openSection("media", "", "")
    _settle()
    page = _find(view.root, "settingsPage")
    flickable = _find(view.root, "settingsPageViewport")
    assert flickable.property("contentHeight") > page.height()
    start = page.mapToItem(view.root, page.width() / 2, page.height() - 24)
    end = page.mapToItem(view.root, page.width() / 2, 72)
    QTest.mousePress(view.widget, Qt.MouseButton.LeftButton, pos=start.toPoint())
    for step in range(1, 7):
        position = start + (end - start) * (step / 6)
        QTest.mouseMove(view.widget, position.toPoint(), delay=12)
    QTest.mouseRelease(view.widget, Qt.MouseButton.LeftButton, pos=end.toPoint())
    _settle()
    assert flickable.property("contentY") > 20


def test_compact_section_header_stays_fixed_while_content_scrolls(create_view):
    view = create_view(width=480, height=360)
    view.navigation.openSection("media", "", "")
    _settle()
    page = _find(view.root, "settingsPage")
    header = _find(page, "settingsCompactHeader")
    viewport = _find(page, "settingsPageViewport")
    title = next(item for item in _items(header) if item.property("text") == "Media and files")
    description = next(
        item for item in _items(header)
        if item.property("text") == "Playback, downloads and folders"
    )
    assert header.isVisible() and title.isVisible() and description.isVisible()
    assert header.mapToItem(page, QPointF(0, 0)).y() == pytest.approx(0, abs=1)
    assert viewport.mapToItem(page, QPointF(0, 0)).y() == pytest.approx(header.height(), abs=1)
    viewport.setProperty("contentY", 90.0)
    _settle()
    assert viewport.property("contentY") == pytest.approx(90, abs=1)
    assert header.mapToItem(page, QPointF(0, 0)).y() == pytest.approx(0, abs=1)
    assert 0 <= title.mapToItem(header, QPointF(0, 0)).y() < header.height()
    assert view.warnings == []


def test_section_scrollbar_has_a_dedicated_gutter(create_view):
    view = create_view(width=1000, height=360)
    sections = _find(view.root, "settingsSections")
    first = _find(view.root, "settingsSection_appearance")
    assert sections.property("scrollbarGutter") >= 12
    assert first.width() <= sections.width() - sections.property("scrollbarGutter")


def test_settings_button_uses_pointing_cursor(create_view):
    view = create_view(width=1000, height=740)
    row = _find(view.root, "setting_themeId")
    button = _control(row, "SettingsButton")
    position = button.mapToItem(
        view.root, QPointF(button.width() / 2, button.height() / 2)
    )
    QTest.mouseMove(view.widget, position.toPoint())
    QApplication.processEvents()
    assert view.widget.cursor().shape() == Qt.CursorShape.PointingHandCursor
    QTest.mouseMove(view.widget, QPointF(1, 1).toPoint())
    QApplication.processEvents()
    assert view.widget.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_rejected_remote_enable_restores_toggle_state(create_view):
    view = create_view(width=1000, height=740)
    view.navigation.openSection("remote", "", "")
    view.domains["remote"].reject_enable = True
    _settle()
    row = _find(view.root, "setting_enabled")
    toggle = _control(row, "Switch")
    assert not toggle.property("checked")
    position = toggle.mapToItem(
        view.root, QPointF(toggle.width() / 2, toggle.height() / 2)
    )
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    _settle()
    assert view.domains["remote"].state["enabled"] is False
    assert toggle.property("checked") is False
    view.domains["remote"].reject_enable = False
    view.domains["remote"].publish(enabled=True)
    _settle()
    assert toggle.property("checked") is True


def test_remote_page_has_no_manual_network_refresh_action(create_view):
    view = create_view(width=1000, height=740)
    view.navigation.openSection("remote", "", "")
    _settle()
    assert _find(view.root, "setting_statusMessage") is None
    assert all(
        row.get("action") != "refreshNetworks"
        for group in view.navigation.currentSection["groups"]
        for row in group["rows"]
    )


def test_remote_setup_reserves_scrollbar_gutter_and_scrolls_with_mouse_drag(create_view):
    view = create_view(width=620, height=380)
    view.domains["remote"].publish(
        setupVisible=True,
        setupUrl="https://192.168.1.2:8443/setup",
        verificationCode="123 456",
        fingerprint="AA:BB:CC:DD " * 12,
        fingerprintVisible=True,
    )
    _settle()
    window_root = view.widget.quickWindow().contentItem()
    dialog = next(
        (obj for obj in view.root.findChildren(QObject)
         if obj.objectName() == "settingsRemoteSetup"),
        None,
    )
    viewport = _find(window_root, "remoteSetupViewport")
    scrollbar = _find(window_root, "remoteSetupScrollBar")
    assert dialog is not None and dialog.property("visible") is True
    assert viewport is not None and scrollbar is not None
    assert viewport.property("contentHeight") > viewport.height()
    assert viewport.x() + viewport.width() + scrollbar.width() <= (
        viewport.parentItem().width() + 1
    )

    start = viewport.mapToScene(QPointF(viewport.width() / 2, viewport.height() - 20))
    end = viewport.mapToScene(QPointF(viewport.width() / 2, 50))
    QTest.mousePress(view.widget, Qt.MouseButton.LeftButton, pos=start.toPoint())
    for step in range(1, 7):
        position = start + (end - start) * (step / 6)
        QTest.mouseMove(view.widget, position.toPoint(), delay=12)
    QTest.mouseRelease(view.widget, Qt.MouseButton.LeftButton, pos=end.toPoint())
    _settle()
    assert viewport.property("contentY") > 20
    assert view.warnings == []


def test_compact_toggle_remains_beside_its_label(create_view):
    view = create_view(width=360)
    view.navigation.openSection("media", "playback", "startVideosPaused")
    _settle()
    row = _find(view.root, "setting_startVideosPaused")
    toggle = _control(row, "Switch")
    label = next(item for item in _items(row) if item.property("text") == "Start videos paused")
    label_position = label.mapToItem(row, QPointF(0, 0))
    toggle_position = toggle.mapToItem(row, QPointF(0, 0))
    assert toggle_position.y() < label_position.y() + label.height()
    assert toggle_position.y() + toggle.height() > label_position.y()
    assert toggle_position.x() >= label_position.x() + label.width()
    assert toggle_position.x() + toggle.width() <= row.width()
    assert view.warnings == []


def test_desktop_section_delegates_have_symmetric_padding(create_view):
    view = create_view(width=1280)
    pane = _find(view.root, "settingsNavigationPane")
    sections = _find(view.root, "settingsSections")
    delegates = [item for item in _items(sections)
                 if "ItemDelegate" in item.metaObject().className() and item.isVisible()]
    assert len(delegates) == 8
    for delegate in delegates:
        left = delegate.mapToItem(pane, QPointF(0, 0)).x()
        right = pane.width() - left - delegate.width()
        assert left == pytest.approx(20, abs=1)
        assert left == pytest.approx(right, abs=1)
    assert view.warnings == []


def test_wide_navigation_pane_contains_plain_title_search_and_sections(create_view):
    view = create_view(width=1280)
    pane = _find(view.root, "settingsNavigationPane")
    content = _find(view.root, "settingsContentPane")
    search = _find(view.root, "settingsSearch")
    sections = _find(view.root, "settingsSections")
    title = next(item for item in _items(pane) if item.property("text") == "Settings")

    for item in (title, search, sections):
        position = item.mapToItem(pane, QPointF(0, 0))
        assert 0 <= position.x() <= pane.width() - item.width()
        assert 0 <= position.y() <= pane.height() - item.height()
    search_top = search.mapToItem(pane, QPointF(0, 0)).y()
    header_icons = [
        item for item in _items(pane)
        if "SettingsIcon" in item.metaObject().className()
        and item.mapToItem(pane, QPointF(0, 0)).y() < search_top
    ]
    assert header_icons == []
    assert content.mapToItem(view.root, QPointF(0, 0)).x() == pytest.approx(
        pane.mapToItem(view.root, QPointF(0, 0)).x() + pane.width() + 12,
        abs=1,
    )
    assert view.warnings == []


def test_wide_search_results_stay_in_content_pane(create_view):
    view = create_view(width=1280)
    pane = _find(view.root, "settingsNavigationPane")
    view.navigation.search("websocket")
    _settle()
    results = _find(view.root, "settingsResults")
    assert results is not None and results.isVisible()
    assert results.mapToItem(view.root, QPointF(0, 0)).x() > (
        pane.mapToItem(view.root, QPointF(0, 0)).x() + pane.width()
    )
    assert _find(view.root, "settingsSearch").isVisible()
    assert _find(view.root, "settingsSections").isVisible()
    assert view.warnings == []


def test_noncollapsible_group_title_has_no_filled_header(create_view):
    view = create_view(width=1280)
    theme_button = _control(_find(view.root, "setting_themeId"), "SettingsButton")
    button_position = theme_button.mapToItem(
        view.root, QPointF(theme_button.width() / 2, theme_button.height() / 2)
    )
    QTest.mouseMove(view.widget, button_position.toPoint())
    QApplication.processEvents()
    assert view.widget.cursor().shape() == Qt.CursorShape.PointingHandCursor
    for group_name in ("appearance", "languages"):
        heading = _find(view.root, "groupHeading_" + group_name)
        assert heading.property("background") is None
        heading_position = heading.mapToItem(
            view.root, QPointF(heading.width() / 2, heading.height() / 2)
        )
        QTest.mouseMove(view.widget, heading_position.toPoint())
        QApplication.processEvents()
        assert view.widget.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert not _find(view.root, "groupSurface_" + group_name).isVisible()
        assert _find(view.root, "groupBodySurface_" + group_name).isVisible()
    assert view.warnings == []


def test_obs_encloses_nested_ndi_and_animates_expansion(create_view):
    view = create_view(width=1280, height=900)
    view.navigation.openSection("integrations", "", "")
    _settle()
    obs = _find(view.root, "group_obs")
    body = _find(view.root, "groupBody_obs")
    heading = _find(view.root, "groupHeading_obs")
    surface = _find(view.root, "groupSurface_obs")
    assert obs.property("expanded") is False
    assert body.height() == 0

    position = heading.mapToScene(QPointF(heading.width() / 2, heading.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    QTest.qWait(55)
    intermediate_height = body.height()
    assert intermediate_height > 0
    assert 0 < body.opacity() < 1
    _settle()
    assert body.height() > intermediate_height
    assert body.opacity() == pytest.approx(1)
    assert surface.height() == pytest.approx(obs.height(), abs=1)
    assert not _find(view.root, "groupBodySurface_obs").isVisible()

    view.navigation.search("Available NDI sources")
    result = next(result for result in view.navigation.results if result["key"] == "ndiSource")
    view.navigation.openSection(result["section"], result["group"], result["key"])
    _settle()
    # The ancestor follows the nested group's changing implicit height, so
    # both expansion animations must finish before checking containment.
    QTest.qWait(250)
    assert view.navigation.expanded("obs") and view.navigation.expanded("ndi")
    ndi = _find(view.root, "group_ndi")
    assert ndi in list(_items(obs))
    assert ndi.property("expanded") is True
    for key in ("obsEnabled", "obsPort", "ndiEnabled", "ndiSource", "ndiStatus"):
        row = _find(view.root, "setting_" + key)
        assert row.isVisible()
        top_left = row.mapToItem(surface, QPointF(0, 0))
        assert top_left.x() >= 0 and top_left.y() >= 0
        assert top_left.x() + row.width() <= surface.width() + 1
        assert top_left.y() + row.height() <= surface.height() + 1
    assert view.warnings == []


def test_remote_credentials_expand_from_header_with_summary_and_keep_drafts(create_view):
    view = create_view(width=1280, height=900)
    view.domains["remote"].publish(credentialsSummary="Configured as operator", password="draft")
    view.navigation.openSection("remote", "", "")
    _settle()
    group = _find(view.root, "group_credentials")
    heading = _find(view.root, "groupHeading_credentials")
    body = _find(view.root, "groupBody_credentials")
    assert group.property("expanded") is False
    assert body.height() == 0
    assert any(item.property("text") == "Configured as operator" for item in _items(heading))
    assert _find(view.root, "setting_credentialsSummary") is None
    assert not any(item.property("text") == "Change" for item in _items(group))
    position = heading.mapToScene(QPointF(heading.width() / 2, heading.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    _settle()
    assert group.property("expanded") is True
    for key in ("username", "password", "passwordConfirmation", "saveCredentials"):
        assert _find(view.root, "setting_" + key).isVisible()
    view.navigation.expand("credentials", False)
    _settle()
    view.navigation.search("Confirm password")
    result = next(result for result in view.navigation.results if result["key"] == "passwordConfirmation")
    view.navigation.openSection(result["section"], result["group"], result["key"])
    _settle()
    assert _find(view.root, "group_credentials").property("expanded") is True
    assert view.domains["remote"].state["password"] == "draft"
    assert view.domains["remote"].calls == []
    assert view.warnings == []


@pytest.mark.parametrize(("kind", "time_key"), [
    ("midweek", "midweekTime"),
    ("weekend", "weekendTime"),
])
def test_meeting_time_picker_requires_configured_day(create_view, kind, time_key):
    view = create_view(width=1000, height=740)
    domain = view.domains["general"]
    domain.publish(**{f"{kind}Day": -1, f"{kind}TimeEnabled": False})
    view.navigation.openSection("meetings", "schedule", time_key)
    _settle()
    button = _control(_find(view.root, f"setting_{time_key}"), "Button")

    assert button.property("enabled") is False
    position = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    QTest.keyClick(view.widget, Qt.Key.Key_Return)
    _settle()

    assert not any(obj.property("modal") is True and obj.property("visible") is True
                   for obj in view.root.findChildren(QObject))
    assert domain.calls == []
    assert view.warnings == []


def test_congregation_lookup_uses_shared_search_and_selection_controls(create_view):
    view = create_view(width=1000, height=740)
    view.navigation.openSection("meetings", "schedule", "congregationName")
    _settle()
    button = _control(_find(view.root, "setting_congregationName"), "Button")
    position = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    _settle()

    window_root = view.widget.quickWindow().contentItem()
    search = _find(window_root, "settingsCongregationSearch")
    results = _find(window_root, "settingsCongregationResults")
    assert search is not None and search.property("searchIcon") is True
    assert results is not None

    view.domains["general"].publish(congregationSuggestions=[{
        "guid": "congregation-id",
        "name": "Central",
        "label": "Central · São Paulo",
        "description": "Central",
    }])
    _settle()
    option = _find(window_root, "settingsCongregationOption_congregation-id")
    position = option.mapToScene(QPointF(option.width() / 2, option.height() / 2))
    QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
    _settle()

    assert ("chooseCongregation", "congregation-id", "Central") in (
        view.domains["general"].calls
    )
    assert view.warnings == []


def test_meeting_time_picker_applies_clock_selection(create_view):
    view = create_view(width=1000, height=740)
    view.navigation.openSection("meetings", "schedule", "midweekTime")
    _settle()
    row = _find(view.root, "setting_midweekTime")
    button = _control(row, "Button")

    def click(item):
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(view.widget, Qt.MouseButton.LeftButton, pos=position.toPoint())
        _settle()

    click(button)
    assert any(obj.property("modal") is True and obj.property("visible") is True
               for obj in view.root.findChildren(QObject))
    window_root = view.widget.quickWindow().contentItem()
    clock = next(item for item in _items(window_root)
                 if item.property("hour") == 19 and item.property("minute") == 30)
    hour_buttons = [item for item in _items(clock)
                    if isinstance(item.property("displayValue"), int) and item.isVisible()]
    assert {item.property("displayValue") for item in hour_buttons} == set(range(24))
    assert not any(item.isVisible() and item.property("text") in {"AM", "PM"}
                   for item in _items(clock))
    radii = set()
    for item in hour_buttons:
        face = item.parentItem()
        center_x = item.x() + item.width() / 2 - face.width() / 2
        center_y = item.y() + item.height() / 2 - face.height() / 2
        radii.add(round((center_x ** 2 + center_y ** 2) ** 0.5))
    assert len(radii) == 2
    hour_button = next(item for item in hour_buttons if item.property("displayValue") == 21)
    click(hour_button)
    assert clock.property("hour") == 21
    assert clock.property("mode") == "minute"
    minute_button = next(item for item in _items(clock)
                         if item.property("displayValue") == 25 and item.isVisible())
    click(minute_button)
    assert clock.property("minute") == 25

    # Move the pointer while its button remains pressed: both stages must
    # update continuously, and hour selection advances only on release.
    click(_find(window_root, "clockHourMode"))
    face = _find(window_root, "clockFace")

    def dial_point(degrees, radius):
        angle = math.radians(degrees)
        return face.mapToScene(QPointF(
            face.width() / 2 + math.sin(angle) * radius,
            face.height() / 2 - math.cos(angle) * radius,
        )).toPoint()

    QTest.mousePress(view.widget, Qt.MouseButton.LeftButton, pos=dial_point(0, 72))
    QApplication.processEvents()
    assert clock.property("hour") == 12
    assert clock.property("mode") == "hour"
    QTest.mouseMove(view.widget, dial_point(270, 72), delay=40)
    QApplication.processEvents()
    assert clock.property("hour") == 21
    assert clock.property("mode") == "hour"
    QTest.mouseRelease(view.widget, Qt.MouseButton.LeftButton, pos=dial_point(270, 72))
    _settle()
    assert clock.property("mode") == "minute"

    QTest.mousePress(view.widget, Qt.MouseButton.LeftButton, pos=dial_point(0, 108))
    QApplication.processEvents()
    assert clock.property("minute") == 0
    QTest.mouseMove(view.widget, dial_point(90, 108), delay=40)
    QApplication.processEvents()
    assert clock.property("minute") == 15
    QTest.mouseMove(view.widget, dial_point(150, 108), delay=40)
    QApplication.processEvents()
    assert clock.property("minute") == 25
    QTest.mouseRelease(view.widget, Qt.MouseButton.LeftButton, pos=dial_point(150, 108))
    _settle()
    assert clock.property("minute") == 25
    apply_button = next(item for item in _items(window_root)
                        if item.property("text") == "Apply" and item.isVisible()
                        and "Button" in item.metaObject().className())
    click(apply_button)
    assert ("midweekTime", "21:25") in view.domains["general"].calls, (
        apply_button.mapToScene(QPointF(0, 0)), apply_button.width(), apply_button.height(),
        [(item.metaObject().className(), item.mapToScene(QPointF(0, 0)), item.width(), item.height())
         for item in _items(window_root) if item.isVisible() and item.clip()],
    )
    assert view.domains["general"].state["midweekTime"] == "21:25"
    assert not any(obj.property("modal") is True and obj.property("visible") is True
                   for obj in view.root.findChildren(QObject))
    assert view.warnings == []
