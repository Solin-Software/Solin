"""End-to-end Qt/QML integration tests for the redesigned Timer tab."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleActionInterface
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest

from solin.controllers.media_countdown_automation_controller import MediaCountdownAutomationSnapshot
from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.controllers.timer_operations import TimerOperations
from solin.core.foundation.settings_store import SettingsStore
from solin.core.i18n.date import week_label
from solin.core.i18n.timer_schedule_view import section_palette
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.meetings.schedule import MeetingSchedule as ProfileMeetingSchedule
from solin.core.meetings.schedule import MeetingSlot
from solin.core.storage import json_files
from solin.core.timer import models, state_machine
from solin.core.timer.application import TimerSession
from solin.core.timer.infrastructure import JsonCustomTimerRepository, QSettingsTimerRepository
from solin.core.timer.media_countdown_automation import (
    MediaCountdownAutomationStatus,
    MediaCountdownBlockingReason,
)
from solin.core.timer.media_countdown_settings import MediaCountdownSettings
from solin.core.timer.models import CustomSectionIcon
from solin.core.timer.pacing_models import PacingMode, PacingRun, PacingWindow, ResolvedPacingWindow
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.timer_bridge import TimerBridge
from solin.ui.qml.timer_icons import TimerIconProvider
from tests._qt import (
    dispose_widget,
    mouse_click,
    mouse_move,
    mouse_press,
    mouse_release,
    show_and_activate,
    wait_until,
)


_WEEK = date(2026, 11, 23)


class _DummyAutomation(QObject):
    state_changed = Signal()

    def snapshot(self) -> MediaCountdownAutomationSnapshot:
        return MediaCountdownAutomationSnapshot(
            settings=MediaCountdownSettings(),
            schedule=ProfileMeetingSchedule(MeetingSlot("midweek"), MeetingSlot("weekend")),
            status=MediaCountdownAutomationStatus.DISABLED,
            next_occurrence=None,
            has_projection_target=False,
            active_countdown=False,
            active_origin="",
            active_target=None,
            active_automatic=False,
            blocking_reason=MediaCountdownBlockingReason.NONE,
        )

    def suggested_manual_target(self) -> tuple[int, int, bool]:
        return 20, 0, False


class _DummyMonitors:
    def model(self) -> list:
        return []

    def is_visible(self) -> bool:
        return True


class _RecordingPdfExport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def export(self, schedule, **kwargs) -> None:
        self.calls.append({"schedule": schedule, **kwargs})


def _visual_items(item: QQuickItem):
    yield item
    for child in item.childItems():
        yield from _visual_items(child)


def _find_visual(root: QQuickItem, object_name: str) -> QQuickItem | None:
    return next(
        (child for child in _visual_items(root) if child.objectName() == object_name),
        None,
    )


def _find_visuals(root: QQuickItem, object_name: str) -> list[QQuickItem]:
    return [child for child in _visual_items(root) if child.objectName() == object_name]


@pytest.fixture
def timer_ui_factory(tmp_path, monkeypatch, request, qt_object_owner):
    clock = SimpleNamespace(now=datetime(2026, 11, 25, 19, 30).timestamp())
    monkeypatch.setattr("solin.ui.qml.timer_bridge.current_monday", lambda: _WEEK)
    monkeypatch.setattr("solin.controllers.timer_operations.current_monday", lambda: _WEEK)
    monkeypatch.setattr(models, "_now", lambda: clock.now)
    monkeypatch.setattr(state_machine, "_now", lambda: clock.now)

    def _settings(store):
        path = str(tmp_path / f"{store.namespace.organization}.ini")
        return QSettings(path, QSettings.Format.IniFormat)

    monkeypatch.setattr(SettingsStore, "_settings", _settings)

    def build(
        *,
        width: int = 1080,
        height: int = 720,
        profile: str = "main",
        meeting_schedule=None,
        type_name: str = "AdvancedTimerPage",
        notify_warning=None,
    ):
        repo = QSettingsTimerRepository(SettingsStore.for_namespace(profile, "Timer"))
        custom = JsonCustomTimerRepository(tmp_path / profile / "timer_schedules")
        session = TimerSession(repo, _WEEK, custom, meeting_schedule=meeting_schedule)
        engine = TimerEngine(qt_object_owner)
        operations = TimerOperations(engine, session, parent=qt_object_owner)
        hand_alert = TimerHandAlertController(
            ZoomSettingsStore(SettingsStore.for_namespace(profile, "Prefs")),
            qt_object_owner,
        )
        pdf_export = _RecordingPdfExport()
        bridge = TimerBridge(
            operations=operations,
            output=None,
            monitors=_DummyMonitors(),
            hand_alert=hand_alert,
            pdf_export=pdf_export,
            media_countdown_automation=_DummyAutomation(qt_object_owner),
            notify_warning=notify_warning,
            parent=qt_object_owner,
        )
        widget = QQuickWidget()
        request.addfinalizer(lambda: dispose_widget(widget))
        widget.resize(width, height)
        configure_qml_host(
            widget,
            type_name=type_name,
            clear_color="#0d1117",
            context_properties={"timer": bridge, "timerDigitFontFamily": "Arial"},
            image_providers={"timericons": TimerIconProvider()},
            dismiss_text_focus_on_pointer_press=True,
        )
        assert widget.errors() == []
        root = widget.rootObject()
        assert root is not None
        widget.show()
        QTest.qWait(40)
        return SimpleNamespace(
            clock=clock,
            repo=repo,
            custom=custom,
            session=session,
            engine=engine,
            operations=operations,
            bridge=bridge,
            hand_alert=hand_alert,
            pdf_export=pdf_export,
            widget=widget,
            root=root,
        )

    return build


def test_timer_tab_responsive_widths_and_sidebar_559_560_transition(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1080, height=720)
    root = ctx.root

    # Check 559 <-> 560 window width transitions with collapsed (64px) and expanded (220px) sidebar
    # Effective QML widths: 340 (560-220), 396 (460-64), 495 (559-64), 496 (560-64), 760, 1080.
    for effective_width, expect_split in (
        (495, False),  # 559 px window, sidebar 64 px
        (340, False),  # 560 px window, sidebar expanded 220 px
        (496, False),  # 560 px window, sidebar collapsed 64 px
        (396, False),  # 460 px minimum window, sidebar 64 px
        (760, True),   # split threshold
        (1080, True),  # wide desktop mockup width
    ):
        ctx.widget.resize(effective_width, 640)
        wait_until(
            lambda w=effective_width: abs(root.width() - w) <= 1.0,
            description=f"resize to {effective_width}",
        )
        QTest.qWait(30)
        assert bool(root.property("splitWorkspace")) is expect_split

        sub_bar = _find_visual(root, "timerSubTabBar")
        assert sub_bar is not None
        export_btn = _find_visual(sub_bar, "timerExportPdfButton")
        settings_btn = _find_visual(sub_bar, "timerSettingsButton")
        assert export_btn is not None and export_btn.isVisible()
        assert settings_btn is not None and settings_btn.isVisible()
        assert export_btn.mapToItem(root, QPointF(export_btn.width(), 0)).x() <= effective_width + 0.5
        assert settings_btn.mapToItem(root, QPointF(settings_btn.width(), 0)).x() <= effective_width + 0.5

        meetings_export_x = export_btn.mapToItem(root, QPointF()).x()
        meetings_settings_x = settings_btn.mapToItem(root, QPointF()).x()

        # Switch to Schedules tab and verify Export PDF & Settings stay in the exact same position!
        assert ctx.bridge.selectScheduleTab("custom")
        QTest.qWait(30)
        assert export_btn.isVisible() and not export_btn.isEnabled()
        assert settings_btn.isVisible() and settings_btn.isEnabled()
        assert export_btn.mapToItem(root, QPointF()).x() == pytest.approx(meetings_export_x, abs=0.5)
        assert settings_btn.mapToItem(root, QPointF()).x() == pytest.approx(meetings_settings_x, abs=0.5)

        assert ctx.bridge.selectScheduleTab("meetings")
        QTest.qWait(30)
    assert ctx.widget.errors() == []


def test_resize_preserves_live_workspace_state_and_focus(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=600)
    show_and_activate(ctx.widget)
    panel = _find_visual(ctx.root, "timerWideClockPanel")
    schedule_list = _find_visual(ctx.root, "timerWideScheduleList")
    flick = _find_visual(schedule_list, "timerScheduleFlickable")
    assert panel is not None and schedule_list is not None and flick is not None
    ctx.bridge.startPart(ctx.bridge.parts[0]["id"])
    QTest.qWait(30)
    panel.setProperty("adjustOpen", True)
    flick.setProperty("contentY", 80)
    action = _find_visual(panel, "timerPrimaryActionButton")
    action.forceActiveFocus()
    assert action.hasActiveFocus()

    ctx.widget.resize(340, 600)
    QTest.qWait(60)
    assert _find_visual(ctx.root, "timerCompactClockStrip") == panel
    assert _find_visual(ctx.root, "timerCompactScheduleList") == schedule_list
    assert panel.property("adjustOpen") is True
    assert flick.property("contentY") == pytest.approx(80, abs=1)
    assert schedule_list.height() >= 100
    compact_action = _find_visual(panel, "timerCompactPrimaryActionButton")
    assert compact_action.hasActiveFocus()

    ctx.widget.resize(1040, 600)
    QTest.qWait(60)
    assert _find_visual(ctx.root, "timerWideClockPanel") == panel
    assert _find_visual(ctx.root, "timerWideScheduleList") == schedule_list
    assert panel.property("adjustOpen") is True
    assert action.hasActiveFocus()


def test_keyboard_reorder_keeps_focus_on_moved_part(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=700)
    show_and_activate(ctx.widget)
    sid = ctx.bridge.createCustomSchedule("Training", "First", 300)
    for title in ("Second", "Third"):
        assert ctx.bridge.addCustomPart(sid, ctx.bridge.activeCustomSchedule["revision"], title, 300, "", "")
    ctx.root.setProperty("editingCustomSchedule", True)
    QTest.qWait(50)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    buttons = [item for item in _find_visuals(editor, "timerPartMoveDownButton") if item.isVisible()]
    first = buttons[0]
    part_id = first.property("moveFocusId")
    first.forceActiveFocus()
    assert first.hasActiveFocus()
    QTest.keyClick(ctx.widget, Qt.Key.Key_Space)
    QTest.qWait(50)
    focused = ctx.widget.quickWindow().activeFocusItem()
    assert focused.property("moveFocusId") == part_id
    assert focused.property("moveFocusDirection") == 1
    QTest.keyClick(ctx.widget, Qt.Key.Key_Space)
    QTest.qWait(50)
    focused = ctx.widget.quickWindow().activeFocusItem()
    assert focused.property("moveFocusId") == part_id
    assert focused.property("moveFocusDirection") == -1


def test_resize_preserves_list_keyboard_focus(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=600)
    show_and_activate(ctx.widget)
    part = next(item for item in _find_visuals(ctx.root, "timerPartRow") if item.isVisible())
    part.forceActiveFocus()
    assert part.hasActiveFocus()
    ctx.widget.resize(340, 600)
    QTest.qWait(60)
    assert part.hasActiveFocus()
    ctx.widget.resize(1040, 600)
    QTest.qWait(60)
    assert part.hasActiveFocus()


def test_keyboard_move_preserves_focus_between_root_and_section(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=700)
    show_and_activate(ctx.widget)
    sid = ctx.bridge.createCustomSchedule("Training", "First", 300)
    assert ctx.bridge.addCustomSectionStyled(sid, ctx.bridge.activeCustomSchedule["revision"], "Group", "default", "", "")
    ctx.root.setProperty("editingCustomSchedule", True)
    QTest.qWait(50)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    button = next(item for item in _find_visuals(editor, "timerPartMoveDownButton") if item.isVisible())
    part_id = button.property("moveFocusId")
    button.forceActiveFocus()
    QTest.keyClick(ctx.widget, Qt.Key.Key_Space)
    QTest.qWait(50)
    focused = ctx.widget.quickWindow().activeFocusItem()
    assert focused.property("moveFocusId") == part_id
    assert focused.property("moveFocusDirection") == 1
    QTest.keyClick(ctx.widget, Qt.Key.Key_Space)
    QTest.qWait(50)
    focused = ctx.widget.quickWindow().activeFocusItem()
    assert focused.property("moveFocusId") == part_id
    assert focused.property("moveFocusDirection") == -1


def test_compact_timer_view_keeps_schedule_usable_with_adjustments_open(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=340, height=600, type_name="TimerView")
    ctx.bridge.startPart(ctx.bridge.parts[0]["id"])
    QTest.qWait(30)
    panel = _find_visual(ctx.root, "timerCompactClockStrip")
    schedule_list = _find_visual(ctx.root, "timerCompactScheduleList")
    assert panel is not None and schedule_list is not None
    panel.setProperty("adjustOpen", True)
    QTest.qWait(30)
    assert schedule_list.height() >= 100
    assert _find_visual(panel, "timerCompactPrimaryActionButton").isVisible()
    pacing_status = _find_visual(panel, "timerCompactPacingStatus")
    assert not pacing_status.property("text")
    assert ctx.bridge.setPacingMode("monitor")
    assert pacing_status.property("text")
    assert ctx.widget.errors() == []


@pytest.mark.parametrize(
    ("direction", "labels"),
    [("up", ("01:25", "01:24", "02:24", "01:24")), ("down", ("01:35", "01:36", "00:36", "01:36"))],
)
def test_clock_renders_adjustments_from_live_engine(timer_ui_factory, direction, labels) -> None:
    ctx = timer_ui_factory()
    ctx.bridge.createCustomSchedule("Training", "Intro", 180)
    ctx.bridge.updateClock("direction", direction)
    assert QMetaObject.invokeMethod(_find_visual(ctx.root, "timerPrimaryActionButton"), "clicked")
    ctx.clock.now += 84
    assert QMetaObject.invokeMethod(_find_visual(ctx.root, "timerAdjustToggleButton"), "clicked")
    drawer = _find_visual(ctx.root, "timerAdjustDrawer")
    digits = _find_visual(ctx.root, "timerClockDigits")
    buttons = {
        item.property("text"): item for item in _visual_items(drawer)
        if item.metaObject().indexOfSignal("clicked()") >= 0
    }

    for action, label in zip(("+1 s", "−1 s", "+1 min", "−1 min"), labels, strict=True):
        assert QMetaObject.invokeMethod(buttons[action], "clicked")
        assert digits.property("text") == label


def test_manual_hand_exposes_accessible_action_and_state(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    ctx.hand_alert.set_outputs(available=True)
    hand = _find_visual(ctx.root, "timerManualHandButton")
    accessible = QAccessible.queryAccessibleInterface(hand)
    assert accessible is not None and accessible.state().checkable
    actions = accessible.actionInterface()
    assert actions is not None
    press = QAccessibleActionInterface.pressAction()
    assert press in actions.actionNames()

    actions.doAction(press)
    assert ctx.bridge.manualHandRaised
    assert accessible.state().checked
    actions.doAction(press)
    assert not ctx.bridge.manualHandRaised
    assert not accessible.state().checked


def test_compact_primary_offers_reset_when_no_part_is_selected(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=600)
    show_and_activate(ctx.widget)
    assert ctx.bridge.createCustomSchedule("Training", "Talk", 120)
    panel = _find_visual(ctx.root, "timerWideClockPanel")
    panel.setProperty("selectedPart", None)
    panel.setProperty("summary", {"allCompleted": True, "totalCount": 1})
    QTest.qWait(20)
    wide_reset = _find_visual(panel, "timerResetAllCompletedButton")
    wide_reset.forceActiveFocus()
    assert wide_reset.hasActiveFocus()
    ctx.widget.resize(340, 600)
    QTest.qWait(60)
    action = _find_visual(panel, "timerCompactPrimaryActionButton")
    assert action.isEnabled()
    assert action.hasActiveFocus()
    assert action.property("iconName") == "reset"
    spy = QSignalSpy(panel.resetAllRequested)
    assert QMetaObject.invokeMethod(action, "clicked")
    assert spy.count() == 1


def test_deleting_last_custom_schedule_publishes_only_the_empty_view(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    schedule_id = ctx.bridge.createCustomSchedule("Training", "Talk", 120)
    revision = ctx.bridge.activeCustomSchedule["revision"]
    desktop_views = []
    ctx.bridge.scheduleChanged.connect(
        lambda: desktop_views.append((ctx.bridge.activeScheduleTab, ctx.bridge.parts))
    )

    assert ctx.bridge.deleteCustomSchedule(schedule_id, revision)

    assert desktop_views == [("custom", [])]
    assert ctx.bridge.customSchedules == []


def test_custom_schedule_empty_idle_running_and_completed_states(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=700)
    root = ctx.root

    # 1. Empty state on Schedules tab
    assert ctx.bridge.selectScheduleTab("custom")
    QTest.qWait(30)
    empty_card = _find_visual(root, "timerEmptyStateCard")
    assert empty_card is not None and empty_card.isVisible()
    assert not ctx.bridge.canExportPdf

    # 2. Fast creation of a single-part schedule (sections and times optional)
    sid = ctx.bridge.createCustomSchedule("Volunteer training", "Welcome", 120)
    assert sid
    QTest.qWait(30)
    assert not empty_card.isVisible()
    assert ctx.bridge.canExportPdf
    assert len(ctx.bridge.parts) == 1
    part_id = ctx.bridge.parts[0]["id"]

    # 3. Running state + running lock on tab switch & edit
    ctx.bridge.startPart(part_id)
    assert ctx.bridge.isRunning
    assert not ctx.bridge.selectScheduleTab("meetings")
    assert ctx.bridge.feedback["code"] == "blocked"

    # Overrun exceeds planned time
    ctx.clock.now += 150
    ctx.engine._on_tick()
    QTest.qWait(30)
    assert ctx.bridge.liveState["overrun"] is True
    clock_panel = _find_visual(root, "timerWideClockPanel")
    assert clock_panel is not None and clock_panel.property("isOverrun") is True

    # 4. Completed state + reset results
    ctx.bridge.stopPart(part_id)
    QTest.qWait(30)
    assert not ctx.bridge.isRunning
    assert ctx.bridge.scheduleSummary["allCompleted"] is True
    assert ctx.bridge.parts[0]["resultLabel"] == "02:30"

    summary = ctx.bridge.scheduleSummary
    assert ctx.bridge.resetAllResults(summary["scheduleId"], summary["scheduleRevision"])
    assert ctx.bridge.scheduleSummary["allCompleted"] is False
    assert ctx.bridge.parts[0]["state"] == "idle"
    assert ctx.widget.errors() == []


def test_custom_schedule_editor_sections_icons_move_and_duplicate(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=760)
    root = ctx.root

    sid = ctx.bridge.createCustomSchedule("Training", "Intro", 300)
    assert sid
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSection(sid, rev, "Practice", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSection(sid, rev, "Empty group", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    practice_id = ctx.bridge.activeCustomSchedule["entries"][1]["id"]
    empty_id = ctx.bridge.activeCustomSchedule["entries"][2]["id"]
    intro_id = ctx.bridge.activeCustomSchedule["entries"][0]["id"]

    # Add a part into Practice, keep Empty group empty
    assert ctx.bridge.addCustomPart(sid, rev, "Demo", 600, practice_id, "")
    rev = ctx.bridge.activeCustomSchedule["revision"]

    # Open explicit editor mode and verify empty section is rendered with delete enabled
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(40)
    editor = _find_visual(root, "timerScheduleEditor")
    assert editor is not None and editor.isVisible()
    empty_placeholders = [
        item for item in _find_visuals(editor, "timerEmptySectionPlaceholder") if item.isVisible()
    ]
    assert len(empty_placeholders) == 1
    delete_section_btns = [
        btn for btn in _find_visuals(editor, "timerDeleteSectionButton") if btn.isVisible()
    ]
    assert [btn.isEnabled() for btn in delete_section_btns] == [False, True]

    # Change section icon: default -> specific ("book") -> none -> back to default
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "default"
    assert ctx.bridge.activeCustomSchedule["entries"][1]["iconKey"] == "section_group"
    assert ctx.bridge.saveCustomSectionAppearance(sid, rev, practice_id, "book", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "book"
    assert ctx.bridge.activeCustomSchedule["entries"][1]["iconKey"] == "book"
    assert ctx.bridge.saveCustomSectionAppearance(sid, rev, practice_id, "none", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "none"
    assert ctx.bridge.activeCustomSchedule["entries"][1]["iconKey"] == ""

    # Move Intro into Empty group, then back to root; reorder sections
    assert ctx.bridge.moveCustomPart(sid, rev, intro_id, empty_id, "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.moveCustomPart(sid, rev, intro_id, "", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.moveCustomSection(sid, rev, empty_id, practice_id)
    rev = ctx.bridge.activeCustomSchedule["revision"]

    # Delete now-empty section
    assert ctx.bridge.deleteCustomSection(sid, rev, empty_id)
    rev = ctx.bridge.activeCustomSchedule["revision"]

    # Close editor and verify operation view renders title legibly even with No icon
    root.setProperty("editingCustomSchedule", False)
    QTest.qWait(40)
    demo_row = next(row for row in ctx.bridge.parts if row["title"] == "Demo")
    assert demo_row["sectionTitle"] == "Practice"
    assert demo_row["sectionIconKey"] == ""

    # Record a result, then duplicate: structure + "none" icon preserved, results cleared
    ctx.bridge.startPart(demo_row["id"])
    ctx.clock.now += 45
    ctx.bridge.stopPart(demo_row["id"])
    copy_id = ctx.bridge.duplicateCustomSchedule(sid, ctx.operations.get_custom_schedule(sid).revision, "Training (copy)")
    assert copy_id and copy_id != sid
    assert ctx.bridge.activeCustomSchedule["id"] == copy_id
    copied_section = next(
        entry for entry in ctx.bridge.activeCustomSchedule["entries"] if entry["type"] == "section"
    )
    assert copied_section["icon"] == CustomSectionIcon.NONE.value
    assert copied_section["parts"][0]["state"] == "idle"

    # Delete both schedules cleanly (including the active/last custom schedule)
    copy_rev = ctx.operations.get_custom_schedule(copy_id).revision
    sid_rev = ctx.operations.get_custom_schedule(sid).revision
    assert ctx.bridge.deleteCustomSchedule(copy_id, copy_rev)
    assert ctx.bridge.deleteCustomSchedule(sid, sid_rev)
    assert ctx.bridge.activeScheduleTab == "custom"
    assert ctx.bridge.customSchedules == []
    assert not ctx.bridge.canExportPdf
    assert ctx.widget.errors() == []


def test_meetings_tab_preserves_official_sections_icons_and_weekly_rename(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=700)
    midweek_parts = ctx.bridge.parts
    assert midweek_parts[0]["section"] == "opening_comments"
    assert midweek_parts[0]["showSectionHeader"] is False
    assert midweek_parts[0]["sectionIconKey"] == ""
    assert midweek_parts[-1]["section"] == "concluding_comments"
    assert midweek_parts[-1]["showSectionHeader"] is False
    section_icons = {row["section"]: row["sectionIconKey"] for row in midweek_parts}
    assert section_icons["treasures"] == "treasures"
    assert section_icons["ministry"] == "ministry"
    assert section_icons["living"] == "living"

    # Rename a meeting part for this week, then restore canonical title
    target = midweek_parts[1]
    original_title = target["title"]
    binding = ctx.bridge.scheduleSummary
    assert ctx.bridge.savePartEdit(binding["scheduleId"], binding["scheduleRevision"], target["id"], "Custom talk title", -1)
    assert ctx.bridge.parts[1]["title"] == "Custom talk title"
    assert ctx.bridge.parts[1]["hasTitleOverride"] is True
    binding = ctx.bridge.scheduleSummary
    assert ctx.bridge.restoreEditedPartTitle(binding["scheduleId"], binding["scheduleRevision"], target["id"])
    assert ctx.bridge.parts[1]["title"] == original_title
    assert ctx.bridge.parts[1]["hasTitleOverride"] is False

    # Switch to Weekend and verify official weekend icons
    ctx.bridge.setMeetingType("weekend")
    assert [row["sectionIconKey"] for row in ctx.bridge.parts] == ["public_talk", "watchtower"]


def test_pacing_modes_missing_window_awaiting_reset_and_pdf(timer_ui_factory) -> None:
    profile_schedule = [
        ProfileMeetingSchedule(MeetingSlot("midweek"), MeetingSlot("weekend"))
    ]
    ctx = timer_ui_factory(
        width=1040,
        height=720,
        meeting_schedule=lambda: profile_schedule[0],
    )
    root = ctx.root
    settings_spy = QSignalSpy(ctx.bridge.meetingScheduleConfigurationRequested)

    # Starts off per profile
    assert ctx.bridge.pacingMode == PacingMode.OFF.value

    # Enable automatic pacing through Settings dialog
    settings_dialog = root.findChild(QObject, "timerSettingsDialog")
    pacing_select = root.findChild(QObject, "timerPacingModeSelect")
    assert settings_dialog is not None and pacing_select is not None
    assert QMetaObject.invokeMethod(pacing_select, "picked", Q_ARG(int, 2))
    assert ctx.bridge.pacingMode == PacingMode.AUTOMATIC.value

    # Missing meeting window offers shortcut to profile settings
    assert ctx.bridge.pacingState["availability"] == "missing_window"
    assert ctx.bridge.pacingState["canConfigureMeetingWindow"] is True
    ctx.bridge.configureMeetingSchedule()
    assert settings_spy.count() == 1

    # Once meeting time is configured, backend builds the 1h45 window
    profile_schedule[0] = ProfileMeetingSchedule(
        MeetingSlot.from_values("midweek", 2, "19:30"),
        MeetingSlot.from_values("weekend", 6, "10:00"),
    )
    assert ctx.operations.refresh_meeting_window()
    assert ctx.bridge.pacingState["availability"] == "ready"
    assert ctx.bridge.pacingState["targetEndLabel"] == "21:15"

    # Export PDF passes active schedule and pacing mode
    ctx.bridge.exportSchedulePdf()
    assert len(ctx.pdf_export.calls) == 1
    assert ctx.pdf_export.calls[-1]["pacing_mode"] is PacingMode.AUTOMATIC

    # Custom schedule: set window, observe automatic adjustment + awaiting_reset + budget error
    sid = ctx.bridge.createCustomSchedule("Training", "Talk", 600)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomPart(sid, rev, "Practice", 600, "", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]

    # Budget error when window is too short (10 min window for 20 min of parts)
    assert not ctx.bridge.saveCustomTimingSettings(sid, rev, {"startTime": "19:30", "endTime": "19:40", "untrackedSeconds": -1})
    assert ctx.bridge.feedback["code"] == "duration_budget"
    assert ctx.bridge.feedback["scope"] == "window"

    # Valid 20-min window: 19:30 -> 19:50
    assert ctx.bridge.saveCustomTimingSettings(sid, rev, {"startTime": "19:30", "endTime": "19:50", "untrackedSeconds": -1})
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.pacingState["availability"] == "ready"

    # Start 4 minutes late (19:34) -> automatic mode shortens eligible part targets
    ctx.clock.now = datetime(2026, 11, 25, 19, 34).timestamp()
    first_id = ctx.bridge.parts[0]["id"]
    second_id = ctx.bridge.parts[1]["id"]
    ctx.bridge.startPart(first_id)
    assert ctx.bridge.scheduleSummary["activeHasAdjustedTarget"] is True
    assert ctx.bridge.scheduleSummary["activeEffectiveSeconds"] < 600

    # Changing pacing mode while running preserves active target and affects upcoming parts
    active_target = ctx.bridge.scheduleSummary["activeEffectiveSeconds"]
    assert ctx.bridge.setPacingMode("monitor")
    assert ctx.bridge.scheduleSummary["activeEffectiveSeconds"] == active_target

    ctx.bridge.stopPart(first_id)
    # Updating window after starting without window (or changing window) -> check awaiting_reset
    summary = ctx.bridge.scheduleSummary
    assert ctx.bridge.resetAllResults(summary["scheduleId"], summary["scheduleRevision"])
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.saveCustomTimingSettings(sid, rev, {"startTime": "", "endTime": ""})
    rev = ctx.bridge.activeCustomSchedule["revision"]
    ctx.bridge.startPart(second_id)
    ctx.bridge.stopPart(second_id)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.saveCustomTimingSettings(sid, rev, {"startTime": "19:30", "endTime": "20:00", "untrackedSeconds": -1})
    assert ctx.bridge.pacingState["availability"] == "awaiting_reset"

    # Stale revision produces reloadable feedback
    assert not ctx.bridge.renameCustomSchedule(sid, 0, "Stale title")
    assert ctx.bridge.feedback["code"] == "schedule_stale"
    assert ctx.bridge.feedback["retryable"] is True
    assert ctx.widget.errors() == []


def test_unified_selection_central_action_section_colors_and_dedicated_timing_modal(
    timer_ui_factory,
) -> None:
    ctx = timer_ui_factory(width=1040, height=720)
    root = ctx.root

    # 1. Unified Meetings view has NO per-row action buttons and NO pencil buttons.
    assert _find_visual(root, "timerPartActionButton") is None
    assert _find_visual(root, "timerMeetingRenameButton") is None

    # Selecting a meeting row updates the centralized clock panel to that part.
    meeting_parts = ctx.bridge.parts
    chosen_meeting_part = meeting_parts[2]
    root.setProperty("selectedPartId", chosen_meeting_part["id"])
    QTest.qWait(30)
    clock_panel = _find_visual(root, "timerWideClockPanel")
    assert clock_panel is not None
    active_title = _find_visual(clock_panel, "timerClockActiveTitle")
    assert active_title is not None and active_title.property("text") == chosen_meeting_part["title"]

    # 2. Custom schedule shares the exact same list & central clock panel, plus canonical icons & colors.
    sid = ctx.bridge.createCustomSchedule("Training", "Welcome", 300)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSectionStyled(sid, rev, "Practice", "treasures", "#41b6c4", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    sec_id = ctx.bridge.activeCustomSchedule["entries"][1]["id"]
    assert ctx.bridge.addCustomPart(sid, rev, "Demo", 600, sec_id, "")
    QTest.qWait(40)

    # Canonical icon and custom color are resolved generically on the shared row model.
    demo_row = next(row for row in ctx.bridge.parts if row["title"] == "Demo")
    assert demo_row["sectionIconKey"] == "treasures"
    assert demo_row["sectionCustomColor"] == "#41b6c4"
    assert demo_row["sectionColor"] == "#41b6c4"
    assert demo_row["sectionBadgeBg"].startswith("#")
    assert demo_row["sectionBadgeBg"] != demo_row["sectionColor"]

    # Unified icon catalog includes canonical meeting symbols and color presets are exposed.
    icon_ids = {item["id"] for item in ctx.bridge.sectionIconCatalog}
    assert {"treasures", "ministry", "living", "public_talk", "watchtower", "book"}.issubset(icon_ids)
    assert len(ctx.bridge.sectionColorPresets) >= 5

    # Clicking/selecting "Demo" updates the central panel; Start runs that selected part.
    root.setProperty("selectedPartId", demo_row["id"])
    QTest.qWait(30)
    assert active_title.property("text") == "Demo"
    assert _find_visual(root, "timerPartActionButton") is None
    primary = _find_visual(root, "timerPrimaryActionButton")
    assert QMetaObject.invokeMethod(primary, "clicked")
    assert ctx.bridge.liveState["active_part_id"] == demo_row["id"]
    ctx.bridge.stopPart(demo_row["id"])

    # 3. Dedicated Timing & Pacing modal exists under the ⋯ menu (separate from structure editor).
    timing_menu_item = root.findChild(QObject, "timerTimingMenuItem")
    timing_dialog = root.findChild(QObject, "timerScheduleTimingDialog")
    appearance_dialog = root.findChild(QObject, "timerSectionIconPickerDialog")
    assert timing_menu_item is not None
    assert timing_dialog is not None
    assert appearance_dialog is not None

    # Opening structure organizer hides navigation bar (immersive) and keeps timing out of editor.
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(40)
    sub_bar = _find_visual(root, "timerSubTabBar")
    assert sub_bar is not None and not sub_bar.isVisible()
    editor = _find_visual(root, "timerScheduleEditor")
    assert editor is not None and editor.isVisible()
    assert editor.findChild(QObject, "timerWindowStartInput") is None
    assert ctx.widget.errors() == []


@pytest.mark.parametrize("width", [340, 1040], ids=["compact", "wide"])
def test_meeting_schedule_change_updates_the_visible_pacing_window_immediately(
    timer_ui_factory, width,
) -> None:
    profile_schedule = [
        ProfileMeetingSchedule(
            MeetingSlot.from_values("midweek", 2, "19:30"),
            MeetingSlot.from_values("weekend", 6, "10:00"),
        )
    ]
    ctx = timer_ui_factory(
        width=width,
        height=720,
        meeting_schedule=lambda: profile_schedule[0],
    )
    assert ctx.bridge.setPacingMode("monitor")
    window = next((item for item in _find_visuals(ctx.root, "timerPacingWindow") if item.isVisible()), None)
    assert window is not None
    assert window.property("text") == "19:30–21:15"

    # User updates profile meeting time to 08:50 -> unrecorded current week updates to 10:35!
    profile_schedule[0] = ProfileMeetingSchedule(
        MeetingSlot.from_values("midweek", 2, "08:50"),
        MeetingSlot.from_values("weekend", 6, "10:00"),
    )
    ctx.operations.on_meeting_schedule_changed()
    assert window.property("text") == "08:50–10:35"

    ctx.bridge.updateClock("show_ampm", False)
    ctx.bridge.updateClock("hour_format_24h", False)
    assert window.property("text") == "8:50 AM–10:35 AM"
    assert ctx.bridge.clockConfig["show_ampm"] is False

    assert ctx.bridge.setPacingMode("off")
    assert not window.isVisible()


def test_empty_sections_visible_draft_cancel_save_and_explicit_appearance_save(
    timer_ui_factory,
) -> None:
    ctx = timer_ui_factory(width=1040, height=740)
    root = ctx.root
    show_and_activate(ctx.widget)

    sid = ctx.bridge.createCustomSchedule("Training", "Intro", 300)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSectionStyled(sid, rev, "Empty group", "default", "", "")
    QTest.qWait(40)

    # Empty section is visible in displayEntries and rendered on the live schedule list.
    assert any(row["rowType"] == "empty_section" for row in ctx.bridge.displayEntries)
    empty_rows = [
        item for item in _find_visuals(root, "timerEmptySectionRow") if item.isVisible()
    ]
    assert len(empty_rows) == 1

    # Bottom clearance ensures the scrollable list can clear the floating toolbar.
    flick = _find_visual(root, "timerScheduleFlickable")
    assert flick is not None and flick.property("contentHeight") >= 84

    # Live list does NOT render quick-add buttons; editing lives in Edit mode & context/central UI.
    assert _find_visual(root, "timerQuickAddPartButton") is None
    assert _find_visual(root, "timerQuickAddSectionButton") is None
    edit_btn = _find_visual(root, "timerEditSelectedPartButton")
    assert edit_btn is not None and edit_btn.isVisible()

    # Section Appearance dialog does NOT persist changes until Save is clicked.
    sec_id = ctx.bridge.activeCustomSchedule["entries"][1]["id"]
    appearance_dlg = root.findChild(QObject, "timerSectionIconPickerDialog")
    dialogs_root = appearance_dlg.parent()
    assert QMetaObject.invokeMethod(
        dialogs_root,
        "openSectionAppearance",
        Q_ARG("QVariant", sec_id),
        Q_ARG("QVariant", "Empty group"),
        Q_ARG("QVariant", "default"),
        Q_ARG("QVariant", ""),
        Q_ARG("QVariant", False),
    )
    QTest.qWait(40)
    dialogs_root.setProperty("appearanceIconChoice", "treasures")
    dialogs_root.setProperty("appearanceColor", "#41b6c4")
    # Closing without Save leaves backend untouched!
    assert QMetaObject.invokeMethod(appearance_dlg, "close")
    QTest.qWait(30)
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "default"
    assert ctx.bridge.activeCustomSchedule["entries"][1]["color"] == ""

    # Explicit save through the Section Appearance dialog persists both icon and color atomically.
    assert QMetaObject.invokeMethod(
        dialogs_root,
        "openSectionAppearance",
        Q_ARG("QVariant", sec_id),
        Q_ARG("QVariant", "Practice"),
        Q_ARG("QVariant", "default"),
        Q_ARG("QVariant", ""),
        Q_ARG("QVariant", False),
    )
    QTest.qWait(40)
    assert appearance_dlg.property("opened") is True
    appearance_content = appearance_dlg.property("contentItem")
    treasures_tile = _find_visual(appearance_content, "timerIconOption_treasures")
    hex_input = _find_visual(appearance_content, "timerSectionColorHexInput")
    save_btn = _find_visual(appearance_content, "timerSectionAppearanceSaveButton")
    assert all(item is not None for item in (treasures_tile, hex_input, save_btn))
    tile_pos = treasures_tile.mapToScene(
        QPointF(treasures_tile.width() / 2, treasures_tile.height() / 2)
    ).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=tile_pos)
    QTest.qWait(20)
    assert dialogs_root.property("appearanceIconChoice") == "treasures"
    hex_input.setProperty("text", "#41b6c4")
    assert QMetaObject.invokeMethod(save_btn, "clicked")
    QTest.qWait(40)
    assert appearance_dlg.property("opened") is False
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "treasures"
    assert ctx.bridge.activeCustomSchedule["entries"][1]["color"] == "#41b6c4"

    # Immersive editor draft: Cancel discards mutations; Save commits atomically preserving IDs!
    orig_part_id = ctx.bridge.parts[0]["id"]
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(40)
    editor = _find_visual(root, "timerScheduleEditor")
    assert QMetaObject.invokeMethod(
        editor,
        "applyDraftAddPart",
        Q_ARG("QVariant", "Discarded part"),
        Q_ARG("QVariant", 180),
        Q_ARG("QVariant", sec_id),
    )
    # Cancel discards without changing backend
    root.setProperty("editingCustomSchedule", False)
    QTest.qWait(30)
    assert len(ctx.bridge.parts) == 1

    # Re-enter editor, add a part to the empty section, and Save -> committed atomically!
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(40)
    assert QMetaObject.invokeMethod(
        editor,
        "applyDraftAddPart",
        Q_ARG("QVariant", "Saved part"),
        Q_ARG("QVariant", 240),
        Q_ARG("QVariant", sec_id),
    )
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(40)
    assert root.property("editingCustomSchedule") is False
    assert [part["title"] for part in ctx.bridge.parts] == ["Intro", "Saved part"]
    assert ctx.bridge.parts[0]["id"] == orig_part_id
    assert ctx.widget.errors() == []


def test_meeting_week_navigation_buttons_switch_weeks_and_return_home(
    timer_ui_factory,
) -> None:
    ctx = timer_ui_factory(width=1040, height=720)
    root = ctx.root
    show_and_activate(ctx.widget)

    initial_label = ctx.bridge.weekLabel
    assert ctx.bridge.isCurrentWeek is True

    prev_btn = _find_visual(root, "timerPreviousWeekButton")
    next_btn = _find_visual(root, "timerNextWeekButton")
    current_btn = _find_visual(root, "timerCurrentWeekButton")
    week_pill = _find_visual(root, "timerWeekSummaryPill")
    not_current_notice = _find_visual(root, "timerNotCurrentWeekNotice")
    assert all(
        item is not None
        for item in (prev_btn, next_btn, current_btn, week_pill, not_current_notice)
    )
    # Home button stays always visible (disabled on current week) and notice is hidden.
    assert current_btn.isVisible() is True
    assert current_btn.isEnabled() is False
    assert not_current_notice.isVisible() is False

    # Capture initial geometry for strict layout stability (zero horizontal/vertical shift!).
    next_x_before = next_btn.mapToItem(root, QPointF()).x()
    home_x_before = current_btn.mapToItem(root, QPointF()).x()
    pill_h_before = week_pill.height()

    prev_pos = prev_btn.mapToItem(root, QPointF(prev_btn.width() / 2, prev_btn.height() / 2)).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=prev_pos)
    QTest.qWait(30)
    assert ctx.bridge.isCurrentWeek is False
    assert ctx.bridge.weekLabel == week_label(_WEEK - timedelta(days=7))
    assert current_btn.isVisible() is True
    assert current_btn.isEnabled() is True
    assert not_current_notice.isVisible() is True
    assert next_btn.mapToItem(root, QPointF()).x() == pytest.approx(next_x_before, abs=0.5)
    assert current_btn.mapToItem(root, QPointF()).x() == pytest.approx(home_x_before, abs=0.5)
    assert week_pill.height() == pytest.approx(pill_h_before, abs=0.5)

    # Clicking current-week chip jumps back to current week
    cur_pos = current_btn.mapToItem(
        root, QPointF(current_btn.width() / 2, current_btn.height() / 2)
    ).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=cur_pos)
    QTest.qWait(30)
    assert ctx.bridge.isCurrentWeek is True
    assert ctx.bridge.weekLabel == initial_label
    assert current_btn.isEnabled() is False
    assert not_current_notice.isVisible() is False

    # Clicking next-week arrow moves forward one week with zero layout shift
    next_pos = next_btn.mapToItem(root, QPointF(next_btn.width() / 2, next_btn.height() / 2)).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=next_pos)
    QTest.qWait(30)
    assert ctx.bridge.isCurrentWeek is False
    assert ctx.bridge.weekLabel == week_label(_WEEK + timedelta(days=7))
    assert next_btn.mapToItem(root, QPointF()).x() == pytest.approx(next_x_before, abs=0.5)
    assert current_btn.mapToItem(root, QPointF()).x() == pytest.approx(home_x_before, abs=0.5)
    assert ctx.widget.errors() == []


def test_reset_part_and_orphaned_pacing_run_heal_meeting_target_window(
    timer_ui_factory,
) -> None:
    profile_schedule = [
        ProfileMeetingSchedule(
            MeetingSlot.from_values("midweek", 2, "19:30"),
            MeetingSlot.from_values("weekend", 6, "10:00"),
        )
    ]
    ctx = timer_ui_factory(
        width=1040,
        height=720,
        meeting_schedule=lambda: profile_schedule[0],
    )
    assert ctx.bridge.setPacingMode("monitor")
    assert ctx.bridge.pacingState["targetEndLabel"] == "21:15"

    # Simulate starting & stopping a part under the old 19:30 schedule, then resetting it.
    first_part = ctx.bridge.parts[0]["id"]
    ctx.bridge.startPart(first_part)
    ctx.clock.now += 25
    ctx.bridge.stopPart(first_part)
    ctx.bridge.resetPart(first_part)
    assert ctx.session.schedule.pacing_run is None

    # Now update profile meeting time to 08:50 -> target updates cleanly to 10:35.
    profile_schedule[0] = ProfileMeetingSchedule(
        MeetingSlot.from_values("midweek", 2, "08:50"),
        MeetingSlot.from_values("weekend", 6, "10:00"),
    )
    ctx.operations.on_meeting_schedule_changed()
    assert ctx.bridge.pacingState["targetEndLabel"] == "10:35"

    # Also verify auto-healing a previously persisted orphaned pacing_run (reached_ids=(), all IDLE).
    sched = ctx.session.schedule
    old_start = int(datetime(2026, 11, 25, 19, 30).timestamp())
    old_end = int(datetime(2026, 11, 25, 21, 15).timestamp())
    sched.pacing_window = PacingWindow("19:30", "21:15")
    sched.pacing_run = PacingRun(
        window=ResolvedPacingWindow(start_epoch=old_start, end_epoch=old_end),
        boundary_epoch=old_start,
        reached_ids=frozenset(),
    )
    ctx.repo.save_schedule(sched)

    ctx.operations.on_meeting_schedule_changed()
    assert ctx.session.schedule.pacing_run is None
    assert ctx.bridge.pacingState["targetEndLabel"] == "10:35"


def test_reset_and_new_meeting_window_commit_together(timer_ui_factory, monkeypatch) -> None:
    profile_schedule = ProfileMeetingSchedule(
        MeetingSlot.from_values("midweek", 2, "19:30"),
        MeetingSlot.from_values("weekend", 6, "10:00"),
    )
    ctx = timer_ui_factory(meeting_schedule=lambda: profile_schedule)
    part_id = ctx.bridge.parts[0]["id"]
    ctx.bridge.startPart(part_id)
    ctx.clock.now += 25
    ctx.bridge.stopPart(part_id)
    summary = ctx.bridge.scheduleSummary
    binding = (summary["scheduleId"], summary["scheduleRevision"], part_id)

    profile_schedule = ProfileMeetingSchedule(
        MeetingSlot.from_values("midweek", 2, "20:30"),
        MeetingSlot.from_values("weekend", 6, "10:00"),
    )
    save_schedule = ctx.repo.save_schedule
    writes = []

    def save(schedule):
        writes.append(schedule.pacing_window.start_time)
        if schedule.pacing_window.start_time == "20:30":
            raise OSError("injected meeting schedule write failure")
        save_schedule(schedule)

    with monkeypatch.context() as patch:
        patch.setattr(ctx.repo, "save_schedule", save)
        assert not ctx.bridge.resetEditedPart(*binding)

    assert writes == ["20:30"]
    assert ctx.bridge.parts[0]["state"] == "stopped"
    assert ctx.bridge.scheduleSummary["scheduleRevision"] == binding[1]
    assert ctx.session.schedule.pacing_window.start_time == "19:30"
    assert ctx.bridge.resetEditedPart(*binding)
    assert ctx.bridge.parts[0]["state"] == "idle"
    assert ctx.bridge.scheduleSummary["scheduleRevision"] == binding[1] + 1
    stored = ctx.repo.load_schedule(_WEEK, ctx.session.meeting_type)
    assert stored.parts[0].state.value == "idle"
    assert stored.pacing_window.start_time == "20:30"


def test_timing_save_publishes_replaced_file_when_directory_sync_fails(
    timer_ui_factory, monkeypatch,
) -> None:
    warnings = []
    ctx = timer_ui_factory(notify_warning=warnings.append)
    schedule_id = ctx.bridge.createCustomSchedule("Training", "Intro", 300)
    revision = ctx.bridge.activeCustomSchedule["revision"]

    def fail_sync(_directory):
        raise OSError("injected directory sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(json_files, "_sync_parent_directory", fail_sync)
        assert ctx.bridge.saveCustomTimingSettings(
            schedule_id, revision, {"startTime": "19:30", "endTime": "19:40"},
        )

    assert len(warnings) == 1
    assert not ctx.bridge.feedback
    assert ctx.session.schedule is ctx.engine.schedule
    assert ctx.bridge.activeCustomSchedule["revision"] == revision + 1
    assert ctx.bridge.customSchedules[0]["revision"] == revision + 1
    assert ctx.custom.load_schedule(schedule_id) == ctx.session.schedule
    assert ctx.bridge.saveCustomTimingSettings(
        schedule_id, revision + 1, {"startTime": "19:30", "endTime": "19:45"},
    )
    assert len(warnings) == 1


def test_pacing_label_agrees_with_projection_at_fractional_boundary(timer_ui_factory, monkeypatch) -> None:
    ctx = timer_ui_factory()
    schedule_id = ctx.bridge.createCustomSchedule("Training", "Intro", 120)
    assert ctx.bridge.saveCustomTimingSettings(
        schedule_id, ctx.bridge.activeCustomSchedule["revision"],
        {"startTime": "19:30", "endTime": "19:32", "untrackedSeconds": 0},
    )
    assert ctx.bridge.setPacingMode("monitor")
    pacing_state = ctx.operations.pacing_state

    monkeypatch.setattr(ctx.operations, "pacing_state", lambda: pacing_state(ctx.clock.now + 30))
    ctx.engine.refresh_schedule()
    assert ctx.bridge.pacingState["status"] == "on_time"
    assert ctx.bridge.pacingState["deviationMinutes"] == 0

    monkeypatch.setattr(ctx.operations, "pacing_state", lambda: pacing_state(ctx.clock.now + 30.25))
    ctx.engine.refresh_schedule()
    assert ctx.bridge.pacingState["status"] == "behind"
    assert ctx.bridge.pacingState["deviationMinutes"] == 1


def test_timing_dialog_draft_cancel_and_unified_save(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=740)
    root = ctx.root
    show_and_activate(ctx.widget)

    sid = ctx.bridge.createCustomSchedule("Training", "Talk", 600)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomPart(sid, rev, "Practice", 600, "", "")
    QTest.qWait(30)

    second_id = ctx.bridge.parts[1]["id"]
    timing_dlg = root.findChild(QObject, "timerScheduleTimingDialog")
    dialogs_root = timing_dlg.parent()

    # Open modal, modify fields & draft weights, then Cancel -> nothing saved!
    assert QMetaObject.invokeMethod(dialogs_root, "openTimingModal")
    QTest.qWait(30)
    assert timing_dlg.property("visible") is True
    start_input = timing_dlg.findChild(QObject, "timerWindowStartInput")
    end_input = timing_dlg.findChild(QObject, "timerWindowEndInput")
    untracked_input = timing_dlg.findChild(QObject, "timerWindowUntrackedInput")
    closing_input = timing_dlg.findChild(QObject, "timerWindowClosingInput")
    # Date field is intentionally removed — timing is pure HH:MM.
    assert timing_dlg.findChild(QObject, "timerWindowDateInput") is None
    assert all(
        item is not None
        for item in (start_input, end_input, untracked_input, closing_input)
    )

    start_input.setProperty("text", "19:30")
    end_input.setProperty("text", "20:00")
    assert QMetaObject.invokeMethod(
        timing_dlg, "setDraftWeight", Q_ARG("QVariant", second_id), Q_ARG("QVariant", 2)
    )
    assert QMetaObject.invokeMethod(timing_dlg, "close")
    QTest.qWait(30)
    assert ctx.bridge.activeCustomSchedule["pacingWindow"]["configured"] is False
    assert ctx.bridge.parts[1]["adjustmentWeight"] == 0

    # Removing a newly entered window must not silently discard an explicit estimate.
    assert QMetaObject.invokeMethod(dialogs_root, "openTimingModal")
    QTest.qWait(30)
    start_input.setProperty("text", "19:30")
    end_input.setProperty("text", "20:00")
    untracked_input.setProperty("text", "0")
    start_input.setProperty("text", "")
    end_input.setProperty("text", "")
    assert QMetaObject.invokeMethod(timing_dlg, "submitTimingSettings")
    assert timing_dlg.property("visible") is True
    assert timing_dlg.property("timingLocalError")
    assert untracked_input.property("text") == "0"
    assert timing_dlg.findChild(QObject, "timerClearWindowButton").property("visible") is True
    assert QMetaObject.invokeMethod(timing_dlg, "clearDraftTimingWindow")
    assert untracked_input.property("text") == ""
    assert QMetaObject.invokeMethod(timing_dlg, "close")
    QTest.qWait(30)

    # Re-open, configure window + weight + breaks, then Save -> all committed together!
    assert QMetaObject.invokeMethod(dialogs_root, "openTimingModal")
    QTest.qWait(30)
    start_input.setProperty("text", "19:30")
    end_input.setProperty("text", "20:00")
    untracked_input.setProperty("text", "60")
    closing_input.setProperty("text", "30")
    assert QMetaObject.invokeMethod(
        timing_dlg, "setDraftWeight", Q_ARG("QVariant", second_id), Q_ARG("QVariant", 2)
    )
    assert QMetaObject.invokeMethod(
        timing_dlg, "setDraftBefore", Q_ARG("QVariant", second_id), Q_ARG("QVariant", "45")
    )
    assert QMetaObject.invokeMethod(timing_dlg, "submitTimingSettings")
    QTest.qWait(40)

    assert timing_dlg.property("visible") is False
    saved_window = ctx.bridge.activeCustomSchedule["pacingWindow"]
    assert saved_window["configured"] is True
    assert saved_window["startTime"] == "19:30"
    assert saved_window["endTime"] == "20:00"
    assert saved_window["untrackedSeconds"] == 60
    assert saved_window["closingSeconds"] == 30
    assert saved_window["beforeParts"].get(second_id) == 45
    assert ctx.bridge.parts[0]["adjustmentWeight"] == 0
    assert ctx.bridge.parts[1]["adjustmentWeight"] == 2
    assert ctx.widget.errors() == []


def test_schedule_editor_drag_and_drop_reorders_without_vanishing(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=740)
    root = ctx.root
    show_and_activate(ctx.widget)
    show_and_activate(ctx.widget)

    sid = ctx.bridge.createCustomSchedule("Training", "First", 300)
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomPart(sid, rev, "Second", 300, "", "")
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSectionStyled(sid, rev, "Practice", "default", "", "")
    QTest.qWait(40)

    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(60)
    editor = _find_visual(root, "timerScheduleEditor")
    assert editor is not None and editor.isVisible()

    part_rows = [row for row in _find_visuals(editor, "timerEditorPartRow") if row.isVisible()]
    assert len(part_rows) == 2
    first_row, second_row = part_rows[0], part_rows[1]

    # Grip sits on the left edge of the card (leftMargin=8 + grip width 26 -> x ~ 20)
    start_pos = first_row.mapToItem(root, QPointF(20, first_row.height() / 2)).toPoint()
    mid_pos = second_row.mapToItem(root, QPointF(20, second_row.height() * 0.85)).toPoint()

    last_pos = start_pos
    released = False
    mouse_press(ctx.widget, Qt.MouseButton.LeftButton, pos=start_pos)
    try:
        QTest.qWait(20)
        # Cross 5px drag threshold, then drag past "Second"
        last_pos = start_pos + QPointF(0, 12).toPoint()
        mouse_move(ctx.widget, last_pos)
        QTest.qWait(30)
        assert editor.property("dragActive") is True
        last_pos = mid_pos
        mouse_move(ctx.widget, last_pos)
        QTest.qWait(40)
        # Still actively dragging — must NOT have been canceled when hiding the dragged card!
        assert editor.property("dragActive") is True
        mouse_release(ctx.widget, Qt.MouseButton.LeftButton, pos=last_pos)
        released = True
    finally:
        if not released:
            mouse_release(ctx.widget, Qt.MouseButton.LeftButton, pos=last_pos)
    QTest.qWait(60)

    assert editor.property("dragActive") is False
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(40)
    assert [p["title"] for p in ctx.bridge.parts] == ["Second", "First"]

    # Also verify dragging a root part directly INTO the empty section!
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(60)
    part_rows = [row for row in _find_visuals(editor, "timerEditorPartRow") if row.isVisible()]
    sec_cards = [card for card in _find_visuals(editor, "timerEditorSectionCard") if card.isVisible()]
    assert len(part_rows) == 2 and len(sec_cards) == 1
    second_card = part_rows[0]
    sec_card = sec_cards[0]
    grab_pt = second_card.mapToItem(root, QPointF(60, second_card.height() / 2)).toPoint()
    drop_in_sec = sec_card.mapToItem(root, QPointF(60, sec_card.height() * 0.65)).toPoint()

    last_sec_pos = grab_pt
    released_sec = False
    mouse_press(ctx.widget, Qt.MouseButton.LeftButton, pos=grab_pt)
    try:
        QTest.qWait(20)
        last_sec_pos = grab_pt + QPointF(0, 12).toPoint()
        mouse_move(ctx.widget, last_sec_pos)
        QTest.qWait(30)
        assert editor.property("dragActive") is True
        last_sec_pos = drop_in_sec
        mouse_move(ctx.widget, last_sec_pos)
        QTest.qWait(40)
        assert editor.property("dropMode") == "section"
        last_sec_pos += QPointF(25, 0).toPoint()
        mouse_move(ctx.widget, last_sec_pos)
        assert editor.property("dropMode") == "section"
        mouse_release(ctx.widget, Qt.MouseButton.LeftButton, pos=last_sec_pos)
        released_sec = True
    finally:
        if not released_sec:
            mouse_release(ctx.widget, Qt.MouseButton.LeftButton, pos=last_sec_pos)
    QTest.qWait(50)

    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(40)
    moved_row = next(row for row in ctx.bridge.parts if row["title"] == "Second")
    assert moved_row["sectionTitle"] == "Practice"

    # Verify icon tiles place "No icon" first, then "Default", with visual labels
    appearance_dlg = root.findChild(QObject, "timerSectionIconPickerDialog")
    tiles = appearance_dlg.parent().property("appearanceIconTiles")
    if hasattr(tiles, "toVariant"):
        tiles = tiles.toVariant()
    assert tiles[0]["id"] == "none"
    assert tiles[1]["id"] == "default"

    # Verify Up/Down arrows step parts smoothly INTO and OUT OF sections (not jumping over them!)
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(50)
    first_id = next(p["id"] for p in ctx.bridge.parts if p["title"] == "First")
    # Current order: root [First], section Practice [Second]
    # Moving First DOWN enters Practice at index 0 -> Practice [First, Second]
    assert QMetaObject.invokeMethod(
        editor, "movePartStep", Q_ARG("QVariant", first_id), Q_ARG("QVariant", 1)
    )
    # Moving First DOWN again swaps inside Practice -> Practice [Second, First]
    assert QMetaObject.invokeMethod(
        editor, "movePartStep", Q_ARG("QVariant", first_id), Q_ARG("QVariant", 1)
    )
    # Moving First DOWN once more exits Practice to root AFTER Practice!
    assert QMetaObject.invokeMethod(
        editor, "movePartStep", Q_ARG("QVariant", first_id), Q_ARG("QVariant", 1)
    )
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(40)
    entries = ctx.bridge.activeCustomSchedule["entries"]
    assert entries[0]["type"] == "section"
    assert [p["title"] for p in entries[0]["parts"]] == ["Second"]
    assert entries[1]["type"] == "part" and entries[1]["title"] == "First"

    # Moving First UP steps back INTO Practice at the bottom, and Second UP exits to root BEFORE Practice!
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(50)
    second_id = next(p["id"] for p in ctx.bridge.parts if p["title"] == "Second")
    assert QMetaObject.invokeMethod(
        editor, "movePartStep", Q_ARG("QVariant", first_id), Q_ARG("QVariant", -1)
    )
    assert QMetaObject.invokeMethod(
        editor, "movePartStep", Q_ARG("QVariant", second_id), Q_ARG("QVariant", -1)
    )
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(40)
    entries = ctx.bridge.activeCustomSchedule["entries"]
    assert entries[0]["type"] == "part" and entries[0]["title"] == "Second"
    assert entries[1]["type"] == "section"
    assert [p["title"] for p in entries[1]["parts"]] == ["First"]
    assert ctx.widget.errors() == []


def test_freeze_allows_part_switching_shows_reset_clock_scrolls_and_nav_aligns(
    timer_ui_factory,
) -> None:
    ctx = timer_ui_factory(width=1040, height=720)
    root = ctx.root
    show_and_activate(ctx.widget)

    # 1. Meetings week nav aligns horizontally with the schedule list; segment aligns with clock panel.
    week_nav = _find_visual(root, "timerWeekNavigationRow")
    sched_list = _find_visual(root, "timerWideScheduleList")
    meeting_seg = _find_visual(root, "timerMeetingTypeSegment")
    clock_panel = _find_visual(root, "timerWideClockPanel")
    assert all(x is not None for x in (week_nav, sched_list, meeting_seg, clock_panel))
    assert week_nav.width() == pytest.approx(sched_list.width(), abs=1.5)
    assert meeting_seg.width() == pytest.approx(clock_panel.width(), abs=1.5)

    # 2. Idle part rows have rounded hover corners (radius == 8).
    part_rows = [row for row in _find_visuals(sched_list, "timerPartRow") if row.isVisible()]
    assert len(part_rows) >= 2
    assert all(row.property("radius") == 8 for row in part_rows[:3])

    # 3. Stopping a part keeps it frozen on monitor (live.active=True, state='stopped'),
    #    yet immediately swaps Adjust -> Reset and lets the user click other parts freely!
    first_id = ctx.bridge.parts[0]["id"]
    second_id = ctx.bridge.parts[1]["id"]
    ctx.bridge.startPart(first_id)
    ctx.clock.now += 10
    ctx.bridge.stopPart(first_id)
    # Keep the real QTimer deterministic while asserting frozen part-switching behavior.
    assert ctx.engine._revert_timer.isActive() is True
    ctx.engine._revert_timer.stop()
    QTest.qWait(40)

    assert ctx.bridge.liveState["active"] is True
    assert ctx.bridge.liveState["state"] == "stopped"
    assert root.property("isRunning") is False

    adjust_btn = _find_visual(clock_panel, "timerAdjustToggleButton")
    reset_btn = _find_visual(clock_panel, "timerResetSelectedPartButton")
    active_title = _find_visual(clock_panel, "timerClockActiveTitle")
    assert adjust_btn is not None and not adjust_btn.isVisible()
    assert reset_btn is not None and reset_btn.isVisible()
    assert active_title.property("text") == ctx.bridge.parts[0]["title"]

    # Click the second part while the first is still frozen -> panel switches immediately!
    part_rows = [row for row in _find_visuals(sched_list, "timerPartRow") if row.isVisible()]
    second_row = part_rows[1]
    click_pt = second_row.mapToItem(root, QPointF(60, second_row.height() / 2)).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=click_pt)
    QTest.qWait(30)
    assert root.property("selectedPartId") == second_id
    assert active_title.property("text") == ctx.bridge.parts[1]["title"]

    # Click back on the first (still-frozen) part -> shows its frozen state with Reset ready!
    part_rows = [row for row in _find_visuals(sched_list, "timerPartRow") if row.isVisible()]
    first_row = part_rows[0]
    click_first = first_row.mapToItem(root, QPointF(60, first_row.height() / 2)).toPoint()
    mouse_click(ctx.widget, Qt.MouseButton.LeftButton, pos=click_first)
    QTest.qWait(30)
    assert root.property("selectedPartId") == first_id
    assert clock_panel.property("isFrozenSelected") is True
    assert reset_btn.isVisible()

    # Delivering the freeze timeout at a controlled step clears the frozen live readout cleanly.
    ctx.engine._on_revert_timeout()
    QTest.qWait(20)
    assert ctx.bridge.liveState["active"] is False
    assert clock_panel.property("isFrozenSelected") is False

    # 4. Short window vertically caps the clock panel and activates vertical scrolling!
    ctx.widget.resize(1040, 340)
    wait_until(lambda: abs(root.height() - 340) <= 1.0, description="short vertical resize")
    QTest.qWait(40)
    clock_flick = _find_visual(clock_panel, "timerClockFlickable")
    assert clock_flick is not None
    assert clock_panel.height() <= clock_panel.parentItem().height()
    assert clock_flick.property("contentHeight") > clock_flick.height()
    assert ctx.widget.errors() == []


@pytest.fixture
def two_custom_schedules(timer_ui_factory):
    ctx = timer_ui_factory(width=1040, height=720)
    show_and_activate(ctx.widget)
    sid_a = ctx.bridge.createCustomSchedule("Schedule A", "Part A1", 600)
    sid_b = ctx.bridge.createCustomSchedule("Schedule B", "Part B1", 600)
    assert sid_a and sid_b
    assert ctx.bridge.selectCustomSchedule(sid_a)
    return ctx, sid_a, sid_b


def test_escape_cancels_drag_autoscroll_with_mouse_pressed(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=500)
    show_and_activate(ctx.widget)
    schedule_id = ctx.bridge.createCustomSchedule("Training", "Intro", 120)
    for index in range(20):
        revision = ctx.bridge.activeCustomSchedule["revision"]
        assert ctx.bridge.addCustomPart(schedule_id, revision, f"Part {index + 1}", 120, "", "")
    ctx.root.setProperty("editingCustomSchedule", True)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    flick = _find_visual(editor, "timerEditorFlickable")
    first_row = _find_visual(editor, "timerEditorPartRow")
    start = first_row.mapToItem(ctx.root, QPointF(20, first_row.height() / 2)).toPoint()
    edge = flick.mapToItem(ctx.root, QPointF(30, flick.height() - 20)).toPoint()

    last_pos = start
    mouse_press(ctx.widget, Qt.MouseButton.LeftButton, pos=start)
    try:
        last_pos += QPointF(0, 15).toPoint()
        mouse_move(ctx.widget, last_pos)
        wait_until(lambda: editor.property("dragActive"), description="drag starts")
        QTest.keyClick(ctx.widget, Qt.Key.Key_Escape)
        assert not editor.property("dragActive")
        offset = flick.property("contentY")
        last_pos = edge
        mouse_move(ctx.widget, last_pos)
        QTest.qWait(90)
        assert flick.property("contentY") == pytest.approx(offset)
    finally:
        mouse_release(ctx.widget, Qt.MouseButton.LeftButton, pos=last_pos)
    QTest.qWait(70)
    assert flick.property("contentY") == pytest.approx(offset)


def test_keyboard_focus_reveals_clipped_timer_controls(timer_ui_factory) -> None:
    ctx = timer_ui_factory(width=1040, height=330)
    show_and_activate(ctx.widget)
    schedule_id = ctx.bridge.createCustomSchedule("Training", "Intro", 120)
    for index in range(10):
        revision = ctx.bridge.activeCustomSchedule["revision"]
        assert ctx.bridge.addCustomSectionStyled(schedule_id, revision, f"Section {index + 1}", "default", "", "")

    list_flick = _find_visual(ctx.root, "timerScheduleFlickable")
    sections = [item for item in _find_visuals(ctx.root, "timerSectionHeader") if item.isVisible()]
    assert len(sections) == 10
    section = sections[-1]
    wait_until(
        lambda: section.width() > 0 and section.mapToItem(list_flick, QPointF()).y() > list_flick.height(),
        description="last section laid out below the viewport",
    )
    assert section.mapToItem(list_flick, section.boundingRect().bottomRight()).y() > list_flick.height()
    section.forceActiveFocus()
    wait_until(
        lambda: section.mapToItem(list_flick, section.boundingRect().bottomRight()).y() <= list_flick.height() + 1,
        description="section with keyboard focus is visible",
    )

    part_id = ctx.bridge.parts[0]["id"]
    ctx.bridge.startPart(part_id)
    ctx.clock.now += 10
    ctx.bridge.stopPart(part_id)
    clock_flick = _find_visual(ctx.root, "timerClockFlickable")
    reset = _find_visual(ctx.root, "timerResetAllCompletedButton")
    wait_until(lambda: reset.isVisible() and reset.width() > 0, description="reset action laid out")
    assert reset.mapToItem(clock_flick, reset.boundingRect().bottomRight()).y() > clock_flick.height()
    reset.forceActiveFocus()
    wait_until(
        lambda: reset.mapToItem(clock_flick, reset.boundingRect().bottomRight()).y() <= clock_flick.height() + 1,
        description="clock control with keyboard focus is visible",
    )


def test_organizer_suspends_draft_when_counting_starts(two_custom_schedules) -> None:
    ctx, sid_a, _ = two_custom_schedules
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    ctx.root.setProperty("editingCustomSchedule", True)
    revision = ctx.bridge.activeCustomSchedule["revision"]
    assert QMetaObject.invokeMethod(
        editor, "applyDraftAddPart", Q_ARG("QVariant", "Draft"), Q_ARG("QVariant", 120), Q_ARG("QVariant", ""),
    )
    part_id = ctx.bridge.parts[0]["id"]

    assert ctx.operations.start_part(part_id)
    assert not editor.isVisible()
    ctx.clock.now += 5
    assert ctx.operations.stop_part(part_id)
    ctx.root.setProperty("editingCustomSchedule", True)

    assert editor.isVisible()
    assert editor.property("hasUnsavedChanges")
    assert editor.property("draftScheduleId") == sid_a
    assert editor.property("draftRevision") == revision
    assert [entry["title"] for entry in editor.property("draftEntries").toVariant()] == ["Part A1", "Draft"]
    assert _find_visual(ctx.root, "timerFeedbackReloadButton").isVisible()
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    assert ctx.bridge.feedback["code"] == "schedule_stale"
    assert QMetaObject.invokeMethod(_find_visual(ctx.root, "timerFeedbackReloadButton"), "clicked")
    ctx.root.setProperty("editingCustomSchedule", True)
    assert not editor.property("hasUnsavedChanges")
    assert [entry["title"] for entry in editor.property("draftEntries").toVariant()] == ["Part A1"]


@pytest.mark.parametrize("entry_kind", ["Part", "Section"])
def test_organizer_keeps_invalid_name_open_for_correction(two_custom_schedules, entry_kind) -> None:
    ctx, _, _ = two_custom_schedules
    ctx.root.setProperty("editingCustomSchedule", True)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    assert QMetaObject.invokeMethod(_find_visual(editor, f"timerEditorAdd{entry_kind}Button"), "clicked")
    dialog = ctx.root.findChild(QObject, f"timerQuickAdd{entry_kind}Dialog")
    title = ctx.root.findChild(QObject, f"timerAdd{entry_kind}TitleInput")
    confirm = ctx.root.findChild(QObject, f"timerAdd{entry_kind}ConfirmButton")

    invalid_title = "x" * (models.MAX_TIMER_TITLE_LENGTH + 1)
    title.setProperty("text", invalid_title)
    assert QMetaObject.invokeMethod(confirm, "clicked")
    assert dialog.property("visible") is True
    assert dialog.property("errorMessage")
    assert title.property("text") == invalid_title
    assert not editor.property("hasUnsavedChanges")

    title.setProperty("text", "Valid name")
    assert QMetaObject.invokeMethod(confirm, "clicked")
    assert editor.property("hasUnsavedChanges")
    assert editor.property("draftEntries").toVariant()[-1]["title"] == "Valid name"


def test_section_appearance_invalid_color_does_not_save_icon(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.addCustomSectionStyled(sid_a, rev, "Sec A", "default", "", "")
    sec_a = ctx.bridge.activeCustomSchedule["entries"][1]["id"]
    rev_before = ctx.bridge.activeCustomSchedule["revision"]
    assert not ctx.bridge.saveCustomSectionAppearance(sid_a, rev_before, sec_a, "book", "not-hex")
    assert ctx.bridge.activeCustomSchedule["entries"][1]["icon"] == "default"
    assert ctx.bridge.activeCustomSchedule["revision"] == rev_before


def test_timing_save_updates_window_and_weights_atomically(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    rev_before = ctx.bridge.activeCustomSchedule["revision"]
    part_a1_id = ctx.bridge.parts[0]["id"]
    assert ctx.bridge.saveCustomTimingSettings(
        sid_a,
        rev_before,
        {
            "startTime": "19:30",
            "endTime": "20:10",
            "untrackedSeconds": 0,
            "closingSeconds": 1800,
            "weights": {part_a1_id: 50},
            "beforeParts": {},
        },
    )
    rev_after_wide = ctx.bridge.activeCustomSchedule["revision"]
    assert rev_after_wide == rev_before + 1
    assert ctx.bridge.saveCustomTimingSettings(
        sid_a,
        rev_after_wide,
        {
            "startTime": "19:30",
            "endTime": "19:55",
            "untrackedSeconds": 0,
            "closingSeconds": 300,
            "weights": {part_a1_id: 70},
            "beforeParts": {},
        },
    )
    rev_after_shrunk = ctx.bridge.activeCustomSchedule["revision"]
    assert rev_after_shrunk == rev_after_wide + 1
    previous_window = dict(ctx.bridge.activeCustomSchedule["pacingWindow"])
    assert previous_window["closingSeconds"] == 300
    assert not ctx.bridge.saveCustomTimingSettings(
        sid_a,
        rev_after_shrunk,
        {
            "startTime": "19:30",
            "endTime": "20:00",
            "untrackedSeconds": 0,
            "closingSeconds": 1801,
            "weights": {part_a1_id: 90},
            "beforeParts": {},
        },
    )
    assert ctx.bridge.activeCustomSchedule["revision"] == rev_after_shrunk
    assert ctx.bridge.activeCustomSchedule["pacingWindow"] == previous_window
    assert ctx.bridge.parts[0]["adjustmentWeight"] == 70
    assert not ctx.bridge.saveCustomTimingSettings(
        sid_a, rev_after_shrunk,
        {"startTime": "19:30", "endTime": "20:00", "closingSeconds": "9" * 5000},
    )
    assert ctx.bridge.activeCustomSchedule["revision"] == rev_after_shrunk
    assert ctx.bridge.activeCustomSchedule["pacingWindow"] == previous_window
    rev_b = ctx.operations.get_custom_schedule(sid_b).revision
    assert not ctx.bridge.saveCustomTimingSettings(
        sid_b, rev_b, {"startTime": "", "endTime": "", "untrackedSeconds": 60}
    )


def test_organizer_draft_stays_bound_to_original_schedule(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    root.setProperty("editingCustomSchedule", True)
    QTest.qWait(40)
    editor = _find_visual(root, "timerScheduleEditor")
    assert QMetaObject.invokeMethod(
        editor, "applyDraftAddPart", Q_ARG("QVariant", "Only for A"), Q_ARG("QVariant", 120), Q_ARG("QVariant", ""),
    )
    # Switch active custom schedule externally to B while A's draft is open!
    ctx.operations.select_custom_schedule(sid_b)
    QTest.qWait(30)
    assert ctx.bridge.activeCustomSchedule["id"] == sid_b
    assert QMetaObject.invokeMethod(editor, "saveDraft")
    QTest.qWait(30)
    # B must NOT be overwritten with A's draft; A receives its own draft!
    assert [p.title for p in ctx.operations.get_custom_schedule(sid_b).iter_parts()] == ["Part B1"]
    assert "Only for A" in [p.title for p in ctx.operations.get_custom_schedule(sid_a).iter_parts()]


def test_failed_delete_restores_selection_and_preserves_confirmation(two_custom_schedules, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    assert ctx.bridge.selectCustomSchedule(sid_a)
    QTest.qWait(20)
    dialogs_obj = root.findChild(QObject, "timerDeleteConfirmButton")
    delete_dlg = root.findChild(QObject, "timerDeleteScheduleDialog")
    assert dialogs_obj is not None and delete_dlg is not None
    dialogs_item = next(
        child for child in root.children() if child.metaObject().className().startswith("TimerCustomScheduleDialogs")
    )
    rev_a_del = ctx.bridge.activeCustomSchedule["revision"]
    assert QMetaObject.invokeMethod(
        dialogs_item,
        "openDeleteConfirm",
        Q_ARG("QVariant", sid_a),
        Q_ARG("QVariant", "Schedule A"),
        Q_ARG("QVariant", rev_a_del),
    )
    assert delete_dlg.property("opened") is True

    def fail_delete(_sid: str) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(ctx.custom, "delete_schedule", fail_delete)
    assert QMetaObject.invokeMethod(dialogs_obj, "clicked")
    QTest.qWait(20)
    assert ctx.bridge.activeCustomSchedule["id"] == sid_a
    assert delete_dlg.property("opened") is True
    assert ctx.bridge.feedback["code"] == "failed"
    assert ctx.bridge.feedback["message"]
    assert delete_dlg.property("errorMessage") == ctx.bridge.feedback["message"]
    assert delete_dlg.property("targetId") == sid_a
    assert delete_dlg.property("targetRevision") == rev_a_del
    assert QMetaObject.invokeMethod(delete_dlg, "close")


def test_running_selection_follows_next_part_and_reset_requires_confirmation(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    for title in ("Part A2", "Part A3"):
        assert ctx.bridge.addCustomPart(
            sid_a, ctx.bridge.activeCustomSchedule["revision"], title, 600, "", "",
        )
    part_ids = [p["id"] for p in ctx.bridge.parts]
    ctx.bridge.startPart(part_ids[0])
    QTest.qWait(20)
    assert root.property("selectedPartId") == part_ids[0]
    ctx.bridge.startNextPart()
    QTest.qWait(20)
    assert root.property("selectedPartId") == part_ids[1]
    ctx.bridge.startNextPart()
    QTest.qWait(20)
    assert root.property("selectedPartId") == part_ids[2]
    ctx.bridge.stopPart(part_ids[2])
    ctx.engine._revert_timer.stop()
    QTest.qWait(30)

    reset_all_btn = _find_visual(root, "timerResetAllCompletedButton")
    reset_dlg = root.findChild(QObject, "timerResetResultsDialog")
    assert reset_all_btn is not None and reset_all_btn.isVisible()
    assert QMetaObject.invokeMethod(reset_all_btn, "clicked")
    QTest.qWait(20)
    assert reset_dlg.property("opened") is True
    # Results are NOT cleared until confirmed!
    assert all(p["state"] == "stopped" for p in ctx.bridge.parts)
    assert QMetaObject.invokeMethod(reset_dlg, "close")


def test_timing_untracked_estimate_can_be_preserved_or_cleared(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.saveCustomTimingSettings(
        sid_a, rev, {"startTime": "19:30", "endTime": "20:00", "untrackedSeconds": 120},
    )
    assert ctx.bridge.activeCustomSchedule["pacingWindow"]["untrackedSeconds"] == 120
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.saveCustomTimingSettings(
        sid_a, rev, {"startTime": "19:30", "endTime": "20:05"},
    )
    assert ctx.bridge.activeCustomSchedule["pacingWindow"]["untrackedSeconds"] == 120
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.saveCustomTimingSettings(
        sid_a, rev, {"startTime": "19:30", "endTime": "20:05", "untrackedSeconds": -1},
    )
    assert ctx.bridge.activeCustomSchedule["pacingWindow"]["untrackedSeconds"] == -1


def test_timing_dialog_snapshots_original_schedule_parts(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    original_total = ctx.bridge.activeCustomSchedule["plannedTotalLabel"]
    assert ctx.bridge.selectCustomSchedule(sid_b)
    assert ctx.operations.set_part_seconds(ctx.bridge.parts[0]["id"], 300)
    assert ctx.bridge.activeCustomSchedule["plannedTotalLabel"] != original_total
    assert ctx.bridge.selectCustomSchedule(sid_a)
    part_a1_id = ctx.bridge.parts[0]["id"]
    dialogs_item = next(
        child for child in root.children() if child.metaObject().className().startswith("TimerCustomScheduleDialogs")
    )
    assert QMetaObject.invokeMethod(dialogs_item, "openTimingModal")
    QTest.qWait(30)
    timing_dlg = root.findChild(QObject, "timerScheduleTimingDialog")
    assert timing_dlg.property("timingDraftScheduleId") == sid_a
    assert timing_dlg.property("timingDraftTotalLabel") == original_total
    draft_parts_before = [p["id"] for p in timing_dlg.property("timingDraftParts").toVariant()]
    assert draft_parts_before == [part_a1_id]
    ctx.operations.select_custom_schedule(sid_b)
    QTest.qWait(20)
    assert [p["id"] for p in timing_dlg.property("timingDraftParts").toVariant()] == [part_a1_id]
    assert timing_dlg.property("timingDraftTotalLabel") == original_total
    assert timing_dlg is not None and QMetaObject.invokeMethod(timing_dlg, "close")


def test_stale_delete_preserves_schedule(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    assert ctx.bridge.selectCustomSchedule(sid_a)
    stale_rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.renameCustomSchedule(sid_a, stale_rev, "Schedule A Edited")
    assert not ctx.bridge.deleteCustomSchedule(sid_a, stale_rev)
    assert ctx.bridge.feedback["code"] == "schedule_stale"
    assert ctx.operations.get_custom_schedule(sid_a) is not None


def test_reset_confirmation_rejects_external_schedule_switch(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    dialogs_item = next(
        child for child in root.children()
        if child.metaObject().className().startswith("TimerCustomScheduleDialogs")
    )
    part_a1_id = ctx.bridge.parts[0]["id"]
    ctx.bridge.startPart(part_a1_id)
    ctx.bridge.stopPart(part_a1_id)
    ctx.engine._revert_timer.stop()
    QTest.qWait(20)
    part_b1_id = ctx.operations.get_custom_schedule(sid_b).parts[0].id
    assert ctx.bridge.selectCustomSchedule(sid_b)
    ctx.bridge.startPart(part_b1_id)
    ctx.bridge.stopPart(part_b1_id)
    ctx.engine._revert_timer.stop()
    assert ctx.bridge.selectCustomSchedule(sid_a)
    QTest.qWait(20)
    sum_a = ctx.bridge.scheduleSummary
    assert QMetaObject.invokeMethod(
        dialogs_item,
        "openResetConfirm",
        Q_ARG("QVariant", sum_a["scheduleId"]),
        Q_ARG("QVariant", sum_a["scheduleRevision"]),
    )
    ctx.operations.select_custom_schedule(sid_b)
    QTest.qWait(20)
    reset_confirm = root.findChild(QObject, "timerResetConfirmButton")
    assert QMetaObject.invokeMethod(reset_confirm, "clicked")
    QTest.qWait(20)
    assert ctx.bridge.feedback["code"] == "schedule_stale"
    assert ctx.bridge.parts[0]["state"] == "stopped"
    reset_dlg = root.findChild(QObject, "timerResetResultsDialog")
    assert QMetaObject.invokeMethod(reset_dlg, "close")


def test_schedule_actions_close_on_external_context_switch(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    assert ctx.operations.select_custom_schedule(sid_b)
    part_b = ctx.bridge.parts[0]["id"]
    ctx.bridge.startPart(part_b)
    ctx.clock.now += 10
    ctx.bridge.stopPart(part_b)
    assert ctx.operations.select_custom_schedule(sid_a)
    menu = ctx.root.findChild(QObject, "timerScheduleMoreMenu")
    more = _find_visual(ctx.root, "timerScheduleMoreButton")
    assert QMetaObject.invokeMethod(more, "clicked")
    wait_until(lambda: menu.property("opened"), description="schedule A menu opened")

    assert ctx.operations.select_custom_schedule(sid_b)
    wait_until(lambda: not menu.property("visible"), description="stale menu closed")
    action = ctx.root.findChild(QObject, "timerResetAllMenuItem")
    assert QMetaObject.invokeMethod(action, "clicked")
    reset = ctx.root.findChild(QObject, "timerResetResultsDialog")
    assert not reset.property("visible")
    assert ctx.bridge.parts[0]["state"] == "stopped"


def test_schedule_selector_invalidates_open_menu_when_options_shrink(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    root = ctx.root
    select_item = _find_visual(root, "timerCustomScheduleSelect")
    assert select_item is not None
    assert QMetaObject.invokeMethod(select_item, "toggleMenu")
    popup = select_item.findChild(QObject, "timerSelectPopup")
    assert popup is not None
    wait_until(lambda: popup.property("opened") is True, description="select popup opened")
    selections = QSignalSpy(select_item.picked)
    popup.setProperty("highlightedIndex", 1)
    rev_b_cur = ctx.operations.get_custom_schedule(sid_b).revision
    assert ctx.bridge.deleteCustomSchedule(sid_b, rev_b_cur)
    assert popup.property("acceptingSelection") is False
    assert popup.property("highlightedIndex") == -1
    assert QMetaObject.invokeMethod(popup, "pick", Q_ARG("QVariant", 0))
    assert selections.count() == 0
    assert ctx.bridge.activeCustomSchedule["id"] == sid_a


def test_part_context_menu_is_dismissed_when_schedule_changes(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    schedule_list = _find_visual(ctx.root, "timerWideScheduleList")
    assert schedule_list is not None and schedule_list.isVisible()
    row = _find_visual(schedule_list, "timerPartRow")
    menu = schedule_list.findChild(QObject, "timerRowContextMenu")
    assert row is not None and row.isVisible() and menu is not None
    row.forceActiveFocus()
    QTest.keyClick(ctx.widget, Qt.Key.Key_Menu)
    wait_until(lambda: menu.property("opened") is True, description="part context menu opened")

    assert ctx.bridge.selectCustomSchedule(sid_b)
    assert menu.property("opened") is False
    assert ctx.bridge.activeCustomSchedule["id"] == sid_b
    assert [part["title"] for part in ctx.bridge.parts] == ["Part B1"]


def test_schedule_selector_invalidates_changed_keys_even_when_labels_match(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    select_item = _find_visual(ctx.root, "timerCustomScheduleSelect")
    assert select_item is not None
    assert QMetaObject.invokeMethod(select_item, "toggleMenu")
    popup = select_item.findChild(QObject, "timerSelectPopup")
    assert popup is not None
    wait_until(lambda: popup.property("opened") is True, description="select popup opened")
    keys = list(select_item.property("optionKeys").toVariant())
    assert set(keys) == {sid_a, sid_b}
    selections = QSignalSpy(select_item.picked)

    select_item.setProperty("optionKeys", keys)
    assert popup.property("acceptingSelection") is True
    select_item.setProperty("optionKeys", list(reversed(keys)))
    assert popup.property("acceptingSelection") is False
    assert popup.property("highlightedIndex") == -1
    assert QMetaObject.invokeMethod(popup, "pick", Q_ARG("QVariant", 0))
    assert selections.count() == 0
    assert ctx.bridge.activeCustomSchedule["id"] == sid_a


def test_custom_catalog_failed_refresh_retains_snapshot_and_recovers(two_custom_schedules, monkeypatch) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    previous = list(ctx.bridge.customSchedules)
    assert {item["id"] for item in previous} == {sid_a, sid_b}
    assert ctx.bridge.customCatalogAvailable
    list_schedules = ctx.operations.list_custom_schedules

    def fail_read():
        raise OSError("catalog unavailable")

    monkeypatch.setattr(ctx.operations, "list_custom_schedules", fail_read)
    ctx.operations.custom_catalog_changed.emit()
    assert not ctx.bridge.customCatalogAvailable
    assert ctx.bridge.customSchedules == previous
    assert not ctx.bridge.refreshCustomCatalog()
    assert ctx.bridge.customSchedules == previous

    monkeypatch.setattr(ctx.operations, "list_custom_schedules", list_schedules)
    assert ctx.bridge.refreshCustomCatalog()
    assert ctx.bridge.customCatalogAvailable
    assert ctx.bridge.customSchedules == previous


def test_restored_running_timer_remains_visible_when_catalog_read_fails(timer_ui_factory, monkeypatch) -> None:
    ctx = timer_ui_factory(profile="restored")
    other = ctx.bridge.createCustomSchedule("Other", "Other part", 120)
    active = ctx.bridge.createCustomSchedule("Active", "Active part", 120)
    ctx.bridge.startPart(ctx.bridge.parts[0]["id"])
    load_schedule = JsonCustomTimerRepository.load_schedule

    def load(repository, schedule_id):
        if schedule_id == other:
            raise PermissionError("injected catalog read failure")
        return load_schedule(repository, schedule_id)

    monkeypatch.setattr(JsonCustomTimerRepository, "load_schedule", load)
    restored = timer_ui_factory(profile="restored")
    assert not restored.bridge.customCatalogAvailable
    assert restored.bridge.activeCustomSchedule["id"] == active
    assert restored.bridge.isRunning
    assert not _find_visual(restored.root, "timerEmptyStateCard").isVisible()
    assert _find_visual(restored.root, "timerCatalogFeedbackBanner").isVisible()
    selector = _find_visual(restored.root, "timerCustomScheduleSelect")
    assert selector.isVisible()
    assert selector.property("currentLabel") == "Active"
    primary = _find_visual(restored.root, "timerPrimaryActionButton")
    assert primary.isVisible()
    assert QMetaObject.invokeMethod(primary, "clicked")
    assert not restored.bridge.isRunning


def test_selector_uses_active_title_when_selection_is_missing_from_catalog(two_custom_schedules, monkeypatch) -> None:
    ctx, sid_a, _ = two_custom_schedules

    def fail_read():
        raise OSError("injected catalog read failure")

    monkeypatch.setattr(ctx.operations, "list_custom_schedules", fail_read)
    active = ctx.bridge.createCustomSchedule("Outside catalog", "Part", 120)
    assert ctx.bridge.activeCustomSchedule["id"] == active
    selector = _find_visual(ctx.root, "timerCustomScheduleSelect")
    assert selector.property("currentLabel") == "Outside catalog"
    selector.forceActiveFocus()
    QTest.keyClick(ctx.widget, Qt.Key.Key_Down)
    assert ctx.bridge.activeCustomSchedule["id"] == sid_a


def test_failed_creation_rollback_exposes_remaining_schedule(two_custom_schedules, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    ctx, _, _ = two_custom_schedules

    def fail_selection(*_args, **_kwargs):
        raise OSError("disk full")

    def fail_rollback_delete(*_args, **_kwargs):
        raise OSError("cannot delete")

    monkeypatch.setattr(ctx.custom, "save_selection", fail_selection)
    monkeypatch.setattr(ctx.custom, "delete_schedule", fail_rollback_delete)
    with caplog.at_level("WARNING"):
        created = ctx.bridge.createCustomSchedule("Broken Event", "First", 300)
    assert created == ""
    assert any("Broken Event" == item["title"] for item in ctx.bridge.customSchedules)
    assert ctx.bridge.feedback["code"] == "created_without_selection"
    assert any("roll back created custom timer schedule" in r.message for r in caplog.records)
    assert ctx.widget.errors() == []


def test_part_editor_rejects_external_schedule_switch(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    root = ctx.root
    sid_a = ctx.bridge.createCustomSchedule("Schedule A", "Part A", 300)
    sid_b = ctx.bridge.createCustomSchedule("Schedule B", "Part B", 300)
    assert ctx.bridge.selectCustomSchedule(sid_a)
    before_a = ctx.custom.load_schedule(sid_a)
    before_b = ctx.custom.load_schedule(sid_b)
    dialogs = next(
        child for child in root.children()
        if child.metaObject().className().startswith("TimerCustomScheduleDialogs")
    )
    assert QMetaObject.invokeMethod(
        dialogs, "openEditPart", Q_ARG("QVariant", ctx.bridge.parts[0]),
    )
    title = root.findChild(QObject, "timerEditPartTitleInput")
    title.setProperty("text", "Edited")
    ctx.operations.select_custom_schedule(sid_b)
    save = root.findChild(QObject, "timerEditPartSaveButton")
    assert QMetaObject.invokeMethod(save, "clicked")
    assert ctx.bridge.feedback["code"] == "schedule_stale"
    assert ctx.custom.load_schedule(sid_a) == before_a
    assert ctx.custom.load_schedule(sid_b) == before_b


def test_failed_delete_and_selection_restore_keeps_the_running_context(
    timer_ui_factory, monkeypatch, caplog,
) -> None:
    ctx = timer_ui_factory()
    sid_a = ctx.bridge.createCustomSchedule("Schedule A", "Part A", 300)
    sid_b = ctx.bridge.createCustomSchedule("Schedule B", "Part B", 300)
    assert ctx.bridge.selectCustomSchedule(sid_a)
    revision = ctx.bridge.activeCustomSchedule["revision"]
    save_selection = ctx.custom.save_selection

    def fail_delete(*_args):
        raise OSError("cannot delete")

    def fail_restore(schedule_id):
        if schedule_id == sid_a:
            raise OSError("cannot restore selection")
        return save_selection(schedule_id)

    monkeypatch.setattr(ctx.custom, "delete_schedule", fail_delete)
    monkeypatch.setattr(ctx.custom, "save_selection", fail_restore)
    assert not ctx.bridge.deleteCustomSchedule(sid_a, revision)
    assert ctx.bridge.feedback["code"] == "selection_restore_failed"
    assert ctx.bridge.activeCustomSchedule["id"] == sid_a
    assert ctx.custom.load_selection() == sid_b
    assert ctx.custom.load_schedule(sid_a) is not None
    assert any("restore the previous custom timer selection" in r.message for r in caplog.records)


@pytest.mark.parametrize("lookup", ["load_schedule", "list_schedules"])
def test_failed_delete_lookup_preserves_selection(two_custom_schedules, monkeypatch, lookup) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    assert ctx.bridge.selectCustomSchedule(sid_a)
    target_id = sid_b if lookup == "load_schedule" else sid_a
    revision = ctx.custom.load_schedule(target_id).revision

    def fail_lookup(*_args):
        raise OSError("cannot read schedules")

    with monkeypatch.context() as failure:
        failure.setattr(ctx.custom, lookup, fail_lookup)
        assert not ctx.bridge.deleteCustomSchedule(target_id, revision)
    assert ctx.bridge.feedback["code"] == "failed"
    assert ctx.custom.load_selection() == sid_a
    assert ctx.custom.load_schedule(sid_a) is not None
    assert ctx.custom.load_schedule(sid_b) is not None


def test_external_meeting_selection_updates_desktop_tab(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    assert ctx.bridge.createCustomSchedule("Event", "Part", 300)
    assert ctx.operations.select_meeting_schedule()
    assert ctx.bridge.activeScheduleTab == "meetings"
    assert len(ctx.bridge.parts) == len(ctx.session.schedule.parts) > 0


def test_custom_catalog_invalidates_only_on_catalog_mutation(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    sid = ctx.bridge.createCustomSchedule("Event", "Part", 300)
    assert sid
    assert ctx.bridge.selectScheduleTab("meetings")
    changes = QSignalSpy(ctx.bridge.customSchedulesChanged)
    assert ctx.bridge.selectCustomSchedule(sid)
    assert changes.count() == 0
    rev = ctx.bridge.activeCustomSchedule["revision"]
    assert ctx.bridge.renameCustomSchedule(sid, rev, "Renamed")
    assert changes.count() == 1
    assert ctx.bridge.customSchedules[0]["title"] == "Renamed"


def test_custom_catalog_tracks_inactive_edits_and_timing_data(two_custom_schedules) -> None:
    ctx, sid_a, sid_b = two_custom_schedules
    changes = QSignalSpy(ctx.bridge.customSchedulesChanged)
    revision_b = ctx.operations.get_custom_schedule(sid_b).revision
    assert ctx.operations.edit_custom_schedule(sid_b, revision_b, "rename_schedule", title="Inactive edit")
    assert changes.count() == 1
    assert next(item for item in ctx.bridge.customSchedules if item["id"] == sid_b)["title"] == "Inactive edit"
    part_id = ctx.bridge.parts[0]["id"]
    assert ctx.operations.start_part(part_id)
    assert next(item for item in ctx.bridge.customSchedules if item["id"] == sid_a)["hasTimingData"]
    assert changes.count() > 1
    assert ctx.operations.stop_part(part_id)
    assert ctx.operations.reset_all()
    assert not next(item for item in ctx.bridge.customSchedules if item["id"] == sid_a)["hasTimingData"]
    count = changes.count()
    assert ctx.operations.select_meeting_schedule()
    assert ctx.operations.start_part(ctx.bridge.parts[0]["id"])
    assert ctx.operations.stop_part()
    assert changes.count() == count


def test_draft_move_rejects_missing_destination(two_custom_schedules) -> None:
    ctx, _, _ = two_custom_schedules
    ctx.root.setProperty("editingCustomSchedule", True)
    QTest.qWait(30)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    part_id = ctx.bridge.parts[0]["id"]
    original = editor.property("draftEntries").toVariant()
    assert QMetaObject.invokeMethod(
        editor, "applyDraftMove", Q_ARG("QVariant", "part"), Q_ARG("QVariant", part_id),
        Q_ARG("QVariant", "missing-section"), Q_ARG("QVariant", ""),
    )
    assert editor.property("draftEntries").toVariant() == original
    assert not editor.property("hasUnsavedChanges")


@pytest.mark.parametrize("color", ["", "#183b50"])
def test_draft_section_uses_saved_section_palette(two_custom_schedules, color) -> None:
    ctx, _, _ = two_custom_schedules
    ctx.root.setProperty("editingCustomSchedule", True)
    QTest.qWait(30)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    assert QMetaObject.invokeMethod(
        editor, "applyDraftAddSection", Q_ARG("QVariant", "Section"),
        Q_ARG("QVariant", "default"), Q_ARG("QVariant", color),
    )
    entry = editor.property("draftEntries").toVariant()[-1]
    palette = section_palette(entry["id"], color or None)
    assert entry["accentColor"] == palette["accent"]
    assert entry["textColor"] == palette["text"]
    assert entry["badgeBg"] == palette["badge"]


def test_external_meeting_reselection_leaves_empty_custom_view(timer_ui_factory) -> None:
    ctx = timer_ui_factory()
    assert ctx.bridge.selectScheduleTab("custom")
    assert ctx.bridge.activeScheduleTab == "custom"
    assert not ctx.bridge.parts
    changes = QSignalSpy(ctx.operations.changed)
    catalog = QSignalSpy(ctx.bridge.customSchedulesChanged)
    assert not ctx.operations.select_meeting_schedule()
    assert ctx.bridge.activeScheduleTab == "meetings"
    assert ctx.bridge.parts
    assert changes.count() == 0
    assert catalog.count() == 0


def test_draft_section_total_tracks_part_mutations(two_custom_schedules) -> None:
    ctx, _, _ = two_custom_schedules
    ctx.root.setProperty("editingCustomSchedule", True)
    QTest.qWait(30)
    editor = _find_visual(ctx.root, "timerScheduleEditor")
    assert QMetaObject.invokeMethod(
        editor, "applyDraftAddSection", Q_ARG("QVariant", "Section"),
        Q_ARG("QVariant", "default"), Q_ARG("QVariant", ""),
    )
    section_id = editor.property("draftEntries").toVariant()[-1]["id"]
    assert QMetaObject.invokeMethod(
        editor, "applyDraftAddPart", Q_ARG("QVariant", "New part"),
        Q_ARG("QVariant", 120), Q_ARG("QVariant", section_id),
    )
    section = editor.property("draftEntries").toVariant()[-1]
    assert section["totalLabel"] == "2 min"
    part_id = ctx.bridge.parts[0]["id"]
    assert QMetaObject.invokeMethod(
        editor, "applyDraftMove", Q_ARG("QVariant", "part"), Q_ARG("QVariant", part_id),
        Q_ARG("QVariant", section_id), Q_ARG("QVariant", ""),
    )
    section = editor.property("draftEntries").toVariant()[-1]
    assert section["totalLabel"] == "12 min"
    assert QMetaObject.invokeMethod(editor, "deleteDraftPart", Q_ARG("QVariant", part_id))
    assert editor.property("draftEntries").toVariant()[-1]["totalLabel"] == "2 min"
