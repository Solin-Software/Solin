from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import (
    Q_ARG,
    QCoreApplication,
    Property,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    QTimer,
    QTranslator,
    Signal,
    Slot,
    Qt,
)
from PySide6.QtGui import QColor, QPixmap, QWheelEvent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.timer_engine import TimerEngine
from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_weekday_names,
)
from solin.core.timer.models import ClockConfig
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.playlist.visuals import PlaylistIconProvider, PlaylistThumbnailProvider
from solin.ui.qml.timer_output import ClockRenderBridge


_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])
elif not isinstance(_APP, QApplication):
    pytest.skip(
        "QML widget host tests require QApplication before QCoreApplication.",
        allow_module_level=True,
    )


class _CursorProbe(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.exited = 0

    @Slot()
    def pointerEnter(self) -> None:  # noqa: N802
        self.entered += 1

    @Slot()
    def pointerExit(self) -> None:  # noqa: N802
        self.exited += 1


class _MediaCountdownProbe(QObject):
    mediaCountdownChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0
        self.presentation_updates: list[int] = []
        self.enabled_updates: list[bool] = []
        self.lead_updates: list[int] = []
        self.time_requests: list[tuple[int, int]] = []
        self.duration_requests: list[int] = []
        self.schedule_requests = 0
        self.model = {
            "presentationIndex": 0,
            "activeProjection": {
                "active": False,
                "origin": "",
                "targetTime": "",
            },
            "manualSuggestion": {
                "hour": 19,
                "minute": 30,
                "fromMeeting": True,
            },
            "automation": {
                "enabled": True,
                "leadSeconds": 600,
                "status": "ready",
                "activeAutomatic": False,
                "blockingReason": "",
            },
            "schedule": {
                "configuredCount": 2,
                "next": {
                    "slotId": "midweek:2026-06-08:19:30",
                    "kind": "midweek",
                    "label": "Midweek meeting",
                    "weekdayLabel": "Monday",
                    "startTime": "19:30",
                    "triggerTime": "19:20:00",
                },
            },
        }

    @Property("QVariant", notify=mediaCountdownChanged)
    def mediaCountdown(self):  # noqa: N802 - QML API
        return self.model

    @Slot()
    def refreshMediaCountdownPage(self) -> None:  # noqa: N802 - QML API
        self.refreshes += 1
        self.mediaCountdownChanged.emit()

    @Slot(bool)
    def setAutomaticCountdownEnabled(self, enabled: bool) -> None:  # noqa: N802
        self.enabled_updates.append(enabled)
        self.model["automation"]["enabled"] = enabled
        self.mediaCountdownChanged.emit()

    @Slot(int)
    def setAutomaticCountdownLeadSeconds(self, seconds: int) -> None:  # noqa: N802
        self.lead_updates.append(seconds)
        self.model["automation"]["leadSeconds"] = seconds
        self.mediaCountdownChanged.emit()

    @Slot(int)
    def setMediaCountdownPresentation(self, index: int) -> None:  # noqa: N802
        self.presentation_updates.append(index)
        self.model["presentationIndex"] = index
        self.mediaCountdownChanged.emit()

    @Slot()
    def configureMeetingSchedule(self) -> None:  # noqa: N802 - QML API
        self.schedule_requests += 1

    @Slot(int, int)
    def startCountdownToTime(self, hour: int, minute: int) -> None:  # noqa: N802
        self.time_requests.append((hour, minute))

    @Slot(int)
    def startCountdownDuration(self, seconds: int) -> None:  # noqa: N802
        self.duration_requests.append(seconds)

    @Slot()
    def pointerEnter(self) -> None:  # noqa: N802 - QML API
        pass

    @Slot()
    def pointerExit(self) -> None:  # noqa: N802 - QML API
        pass


class _PlaybackProtectionProbe(QObject):
    enabledChanged = Signal()
    lockedChanged = Signal()

    def __init__(self, *, enabled: bool, locked: bool = False) -> None:
        super().__init__()
        self._enabled = enabled
        self._locked = locked

    @Property(bool, notify=enabledChanged)
    def enabled(self) -> bool:
        return self._enabled

    @Property(bool, notify=lockedChanged)
    def locked(self) -> bool:
        return self._locked

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.enabledChanged.emit()

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.lockedChanged.emit()


@pytest.fixture
def pt_br_translator():
    translator = QTranslator()
    assert translator.load(
        str(application_translation_root() / "solin_pt_BR.qm")
    )
    assert _APP.installTranslator(translator)
    yield translator
    assert _APP.removeTranslator(translator)


class _PlaylistTreeControllerProbe(QObject):
    stateChanged = Signal()
    mediaChanged = Signal(str, str, str, str)
    imageFramingChanged = Signal(str, object)
    mediaInserted = Signal(str, int, "QVariant")
    nodesInserted = Signal(str, int, "QVariant")
    nodeReplaced = Signal(str, "QVariant")
    nodeMoved = Signal(str, str, int)
    sectionChanged = Signal(str, str, str, str, str, int)
    sectionCollapseChanged = Signal(str, bool)
    sectionCountsChanged = Signal(object)
    markerEditRequested = Signal(str)
    cloudChanged = Signal(str, bool, bool, float, str)

    def __init__(self, nodes: list[dict]) -> None:
        super().__init__()
        self._nodes = nodes
        self.projected: list[str] = []
        self.moves: list[tuple[str, str, int]] = []

    @Property(object, notify=stateChanged)
    def playlistData(self):  # noqa: N802 - QML API
        return self._nodes

    @Slot(str)
    def projectItem(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.projected.append(item_id)

    @Slot(result=float)
    def imageFramingAspectRatio(self) -> float:  # noqa: N802 - QML API
        return 16 / 9

    @Slot(str, result=float)
    def imageFramingSourceAspectRatio(self, _item_id: str) -> float:  # noqa: N802
        return 16 / 9

    @Slot(str, str, int, result=bool)
    def moveNode(  # noqa: N802 - QML API
        self,
        node_id: str,
        target_list_id: str,
        insert_index: int,
    ) -> bool:
        self.moves.append((node_id, target_list_id, insert_index))
        return True


def _playlist_media_node(item_id: str, title: str) -> dict:
    return {
        "type": "media",
        "id": item_id,
        "title": title,
        "duration": "1:00",
        "thumbSource": "",
        "cloudVisible": False,
        "cloudActive": False,
        "cloudProgress": -1.0,
        "cloudTooltip": "",
        "isMissing": False,
        "imageFraming": None,
        "mediaType": "video",
        "badge": "Video",
    }


def _playlist_section_node(
    section_id: str,
    children: list[dict],
) -> dict:
    return {
        "type": "section",
        "id": section_id,
        "title": "Section",
        "color": "#4f8cff",
        "textColor": "#ffffff",
        "badgeBg": "#26466f",
        "itemCount": len(children),
        "collapsed": False,
        "children": children,
    }


def _playlist_tree_host(
    nodes: list[dict],
    *,
    height: int = 260,
) -> tuple[
    QQuickWidget,
    _PlaylistTreeControllerProbe,
    QObject,
    _PlaybackProtectionProbe,
]:
    protection = _PlaybackProtectionProbe(enabled=False)
    controller = _PlaylistTreeControllerProbe(nodes)
    widget = QQuickWidget()
    widget.resize(520, height)
    configure_qml_host(
        widget,
        type_name="PlaylistTreeView",
        clear_color="#000000",
        image_providers={"playlisticons": PlaylistIconProvider()},
        context_properties={"playbackProtection": protection},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("playlistController", controller)
    root.setProperty("playlistNodes", nodes)
    root.setProperty("hasItems", True)
    root_playlist = root.findChild(QObject, "rootPlaylist")
    assert root_playlist is not None
    root_playlist.scheduleRebuild(nodes)
    widget.show()
    QTest.qWait(40)
    assert root_playlist.property("rebuildQueued") is False
    return widget, controller, root_playlist, protection


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    texts: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        texts.append(text)
    for child in item.childItems():
        texts.extend(_visible_texts(child, parent_visible=visible))
    return texts


def test_deleting_untracked_moved_delegate_is_exact_and_does_not_rebuild() -> None:
    nodes = [
        _playlist_media_node("keep", "Keep"),
        _playlist_media_node("remove", "Remove"),
    ]
    widget, controller, root_playlist, _protection = _playlist_tree_host(
        nodes,
        height=220,
    )

    remove_card = next(
        item
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId") == "remove"
    )
    root_playlist.removeNode(remove_card)

    controller._nodes = [nodes[0]]
    controller.nodeReplaced.emit("remove", [])
    assert root_playlist.property("rebuildQueued") is False
    QTest.qWait(30)

    visual_ids = [
        item.property("nodeId")
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeType") == "media"
    ]
    assert "keep" in visual_ids
    assert "remove" not in visual_ids

    widget.deleteLater()


def test_move_from_section_to_root_then_delete_stays_incremental() -> None:
    moved = _playlist_media_node("moved", "Moved")
    section = _playlist_section_node("section", [moved])
    nodes = [section]
    widget, controller, root_playlist, _protection = _playlist_tree_host(nodes)

    section_without_media = _playlist_section_node("section", [])
    controller._nodes = [section_without_media, moved]
    controller.nodeMoved.emit("moved", "root", 1)
    assert root_playlist.property("rebuildQueued") is False

    controller._nodes = [section_without_media]
    controller.nodeReplaced.emit("moved", [])
    assert root_playlist.property("rebuildQueued") is False
    QTest.qWait(20)

    visual_ids = [
        item.property("nodeId")
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId")
    ]
    assert "section" in visual_ids
    assert "moved" not in visual_ids

    widget.deleteLater()


def test_move_from_root_to_section_then_delete_stays_incremental() -> None:
    moved = _playlist_media_node("moved", "Moved")
    empty_section = _playlist_section_node("section", [])
    nodes = [moved, empty_section]
    widget, controller, root_playlist, _protection = _playlist_tree_host(nodes)
    target_list = root_playlist.findList("section:section")
    assert target_list is not None
    assert target_list.property("collapsed") is False
    assert target_list.property("rebuildQueued") is False
    assert target_list.property("pendingIndex") == 0

    section_with_media = _playlist_section_node("section", [moved])
    controller._nodes = [section_with_media]
    controller.nodeMoved.emit("moved", "section:section", 0)
    assert root_playlist.property("rebuildQueued") is False

    controller._nodes = [_playlist_section_node("section", [])]
    controller.nodeReplaced.emit("moved", [])
    assert root_playlist.property("rebuildQueued") is False
    QTest.qWait(20)

    visual_ids = [
        item.property("nodeId")
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId")
    ]
    assert "section" in visual_ids
    assert "moved" not in visual_ids

    widget.deleteLater()


def test_move_between_sections_then_delete_stays_incremental() -> None:
    moved = _playlist_media_node("moved", "Moved")
    source_section = _playlist_section_node("source", [moved])
    target_section = _playlist_section_node("target", [])
    nodes = [source_section, target_section]
    widget, controller, root_playlist, _protection = _playlist_tree_host(nodes)

    moved_card = next(
        item
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId") == "moved"
    )
    target_list = root_playlist.findList("section:target")
    assert target_list is not None
    drag_manager = widget.rootObject().findChild(QObject, "dragManager")
    assert drag_manager is not None
    placeholder = widget.rootObject().findChild(QObject, "dragPlaceholder")
    assert placeholder is not None

    drag_manager.startDrag(moved_card)
    target_list.appendNode(placeholder)
    drag_manager.endDrag()
    assert controller.moves == [("moved", "section:target", 0)]
    assert root_playlist.property("rebuildQueued") is False

    source_without_media = _playlist_section_node("source", [])
    controller._nodes = [
        source_without_media,
        _playlist_section_node("target", []),
    ]
    controller.nodeReplaced.emit("moved", [])
    assert root_playlist.property("rebuildQueued") is False
    QTest.qWait(20)

    visual_ids = [
        item.property("nodeId")
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId")
    ]
    assert "source" in visual_ids
    assert "target" in visual_ids
    assert "moved" not in visual_ids

    widget.deleteLater()


def test_first_reorder_after_section_insertion_stays_incremental() -> None:
    existing = _playlist_media_node("existing", "Existing")
    section = _playlist_section_node("section", [existing])
    nodes = [section]
    widget, controller, root_playlist, _protection = _playlist_tree_host(nodes)

    inserted = _playlist_media_node("inserted", "Inserted")
    controller._nodes = [
        _playlist_section_node("section", [existing, inserted]),
    ]
    controller.mediaInserted.emit("section:section", 2**31 - 1, [inserted])
    assert root_playlist.property("rebuildQueued") is False

    target_list = root_playlist.findList("section:section")
    assert target_list is not None
    existing_card = next(
        item
        for item in target_list.findChildren(QObject)
        if item.property("nodeId") == "existing"
    )
    inserted_card = next(
        item
        for item in target_list.findChildren(QObject)
        if item.property("nodeId") == "inserted"
    )
    drag_manager = widget.rootObject().findChild(QObject, "dragManager")
    placeholder = widget.rootObject().findChild(QObject, "dragPlaceholder")
    assert drag_manager is not None
    assert placeholder is not None

    drag_manager.startDrag(inserted_card)
    target_list.insertBeforeNode(placeholder, existing_card)
    drag_manager.endDrag()

    assert controller.moves == [("inserted", "section:section", 0)]
    assert root_playlist.property("rebuildQueued") is False
    assert target_list.indexOfNode(inserted_card) == 0
    assert target_list.indexOfNode(existing_card) == 1

    widget.deleteLater()


def test_media_countdown_page_renders_context_and_applies_meeting_suggestion() -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(640, 820)
    configure_qml_host(
        widget,
        type_name="MediaCountdownPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()

    assert QMetaObject.invokeMethod(root, "enterPage")
    QTest.qWait(40)

    assert timer.refreshes == 1
    assert root.property("hourValue") == 19
    assert root.property("minuteValue") == 30
    visible_text = _visible_texts(root)
    assert "Projection appearance" in visible_text
    assert "Circular" in visible_text
    assert "Annual text" in visible_text
    assert "Start manually" in visible_text
    assert "Meeting time" not in visible_text
    assert "Before meetings" in visible_text
    manual_card = root.findChild(QObject, "manualCountdownCard")
    automatic_card = root.findChild(QObject, "automaticCountdownCard")
    appearance_row = root.findChild(QObject, "countdownAppearanceRow")
    presentation_selector = root.findChild(QObject, "presentationSelector")
    automation_panel = root.findChild(QObject, "automationSettingsPanel")
    assert appearance_row is not None
    assert presentation_selector is not None
    assert manual_card is not None
    assert automatic_card is not None
    assert automation_panel is not None
    assert appearance_row.property("y") < manual_card.property("y")
    assert manual_card.property("y") < automatic_card.property("y")
    assert appearance_row.property("height") <= 64
    assert presentation_selector.property("height") == 42
    assert automation_panel.property("visible") is False

    root.setProperty("automationExpanded", True)
    QTest.qWait(20)
    assert automation_panel.property("visible") is True
    assert "How long before the meeting should the countdown start?" in _visible_texts(root)

    status_description = root.findChild(QObject, "countdownStatusDescription")
    assert status_description is not None
    timer.model["automation"].update({"status": "active", "activeAutomatic": False})
    timer.mediaCountdownChanged.emit()
    QTest.qWait(20)
    assert status_description.property("text") == (
        "Next: Monday at 19:30 · starts automatically at 19:20:00"
    )

    timer.model["automation"].update({
        "status": "waiting_for_projection",
        "blockingReason": "automation_unavailable",
    })
    timer.mediaCountdownChanged.emit()
    QTest.qWait(20)
    assert status_description.property("text") == (
        "Automatic projection is temporarily unavailable. "
        "Solin will keep trying until the meeting starts."
    )
    widget.deleteLater()


@pytest.mark.parametrize(
    ("type_name", "signal_name"),
    [("TimerButton", "clicked"), ("TimerToggle", "toggled")],
)
def test_timer_pointer_controls_do_not_keep_focus_after_mouse_click(
    type_name: str,
    signal_name: str,
) -> None:
    widget = QQuickWidget()
    widget.resize(120, 60)
    configure_qml_host(
        widget,
        type_name=type_name,
        clear_color="#000000",
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    QTest.qWait(20)

    activation = QSignalSpy(getattr(root, signal_name))
    QTest.mouseClick(
        widget,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(widget.width() // 2, widget.height() // 2),
    )

    assert activation.count() == 1
    assert root.property("activeFocus") is False

    root.forceActiveFocus()
    assert root.property("activeFocus") is True
    QTest.keyClick(widget, Qt.Key.Key_Space)
    assert activation.count() == 2
    widget.deleteLater()


def test_media_countdown_manual_duration_requires_confirmation() -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(640, 820)
    configure_qml_host(
        widget,
        type_name="MediaCountdownPage",
        clear_color="#000000",
        context_properties={"timer": timer},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    assert QMetaObject.invokeMethod(root, "enterPage")
    QTest.qWait(30)

    root.setProperty("manualMode", 1)
    start_button = root.findChild(QObject, "startDurationButton")
    custom_editor = root.findChild(QObject, "customDurationEditor")
    presentation_selector = root.findChild(QObject, "presentationSelector")
    assert start_button is not None
    assert custom_editor is not None
    assert presentation_selector is not None

    root.setProperty("selectedDurationSeconds", 900)
    root.setProperty("customDurationVisible", False)
    QTest.qWait(10)
    assert root.property("selectedDurationSeconds") == 900
    assert timer.duration_requests == []

    assert QMetaObject.invokeMethod(start_button, "clicked")
    assert timer.duration_requests == [900]

    root.setProperty("customDurationVisible", True)
    root.setProperty("selectedDurationSeconds", 3661)
    QTest.qWait(10)
    assert custom_editor.property("visible") is True
    assert custom_editor.property("value") == 3661

    assert QMetaObject.invokeMethod(
        presentation_selector,
        "picked",
        Q_ARG(int, 1),
    )
    assert timer.presentation_updates == [1]
    assert QMetaObject.invokeMethod(
        presentation_selector,
        "picked",
        Q_ARG(int, 0),
    )
    assert timer.presentation_updates == [1, 0]
    widget.deleteLater()


def test_media_countdown_does_not_duplicate_live_projection_status() -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(640, 820)
    configure_qml_host(
        widget,
        type_name="MediaCountdownPage",
        clear_color="#000000",
        context_properties={"timer": timer},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    assert QMetaObject.invokeMethod(root, "enterPage")
    QTest.qWait(30)

    automation_toggle = root.findChild(QObject, "automationToggle")
    assert automation_toggle is not None
    assert root.findChild(QObject, "activeCountdownBanner") is None

    timer.model["activeProjection"] = {
        "active": True,
        "origin": "manual",
        "targetTime": "19:30",
    }
    timer.model["automation"].update({
        "status": "active",
        "activeAutomatic": False,
    })
    timer.mediaCountdownChanged.emit()
    QTest.qWait(20)
    visible_text = _visible_texts(root)
    assert "Manual countdown in progress" not in visible_text
    assert "Automatic countdown in progress" not in visible_text
    assert "Next: Monday at 19:30 · starts automatically at 19:20:00" in visible_text
    assert "Scheduled" not in visible_text
    assert "Countdown active" not in visible_text

    root.setProperty("automationExpanded", False)
    assert QMetaObject.invokeMethod(
        automation_toggle,
        "toggled",
        Q_ARG(bool, True),
    )
    QTest.qWait(10)
    assert timer.enabled_updates == [True]
    assert root.property("automationExpanded") is True
    widget.deleteLater()


@pytest.mark.parametrize("width", [415, 560, 640, 900])
def test_media_countdown_page_is_responsive_without_horizontal_overflow(
    width: int,
) -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(width, 760)
    configure_qml_host(
        widget,
        type_name="MediaCountdownPage",
        clear_color="#000000",
        context_properties={"timer": timer},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    assert QMetaObject.invokeMethod(root, "enterPage")
    QTest.qWait(30)

    for object_name in (
        "countdownAppearanceRow",
        "manualCountdownCard",
        "automaticCountdownCard",
    ):
        card = root.findChild(QObject, object_name)
        assert card is not None
        assert 0 < card.property("width") <= width - 32

    widget.deleteLater()


@pytest.mark.parametrize(
    ("width", "compact"),
    [(500, True), (640, False)],
)
def test_timer_view_moves_mode_selector_below_header_when_compact(
    width: int,
    compact: bool,
) -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(width, 760)
    configure_qml_host(
        widget,
        type_name="TimerView",
        clear_color="#000000",
        context_properties={"timer": timer},
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("mode", 1)
    widget.show()
    QTest.qWait(30)

    wide_selector = root.findChild(QObject, "timerModeSelectorWide")
    compact_selector = root.findChild(QObject, "timerModeSelectorCompact")
    assert wide_selector is not None
    assert compact_selector is not None
    assert root.property("compactHeader") is compact
    assert compact_selector.property("visible") is compact
    assert wide_selector.property("visible") is not compact
    if compact:
        assert compact_selector.property("width") <= width - 40

    widget.deleteLater()


def test_media_countdown_portuguese_catalog_covers_ui_and_runtime_feedback(
    pt_br_translator,
) -> None:
    timer = _MediaCountdownProbe()
    widget = QQuickWidget()
    widget.resize(640, 820)
    configure_qml_host(
        widget,
        type_name="MediaCountdownPage",
        clear_color="#000000",
        context_properties={"timer": timer},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    assert QMetaObject.invokeMethod(root, "enterPage")
    QTest.qWait(40)

    visible_text = _visible_texts(root)
    assert "Aparência da projeção" in visible_text
    assert "Horário da reunião" not in visible_text
    assert "Antes das reuniões" in visible_text
    root.setProperty("automationExpanded", True)
    QTest.qWait(20)
    visible_text = _visible_texts(root)
    assert "Quanto tempo antes da reunião o cronômetro deve começar?" in visible_text
    assert meeting_weekday_names()[0] == "Segunda-feira"
    assert meeting_kind_label("midweek") == "Reunião do meio de semana"
    assert QCoreApplication.translate(
        "MediaCountdownPage",
        (
            "Automatic projection is temporarily unavailable. Solin will keep "
            "trying until the meeting starts."
        ),
    ) == (
        "A projeção automática está temporariamente indisponível. O Solin "
        "continuará tentando até o início da reunião."
    )
    assert QCoreApplication.translate(
        "MediaCountdownAutomation",
        "Countdown started automatically for {time}.",
    ) == (
        "Contagem regressiva iniciada automaticamente. Chegará a zero às {time}."
    )
    assert QCoreApplication.translate(
        "MediaCountdownAutomation",
        "The countdown did not start because no media window was available.",
    ) == (
        "A contagem regressiva não foi iniciada porque nenhuma janela de mídia "
        "estava disponível."
    )
    assert QCoreApplication.translate(
        "MediaCountdownAutomation",
        "The countdown did not start because automatic projection remained unavailable.",
    ) == (
        "A contagem regressiva não foi iniciada porque a projeção automática "
        "permaneceu indisponível."
    )
    widget.deleteLater()


def test_qml_host_renders_clock_face_content() -> None:
    engine = TimerEngine()
    bridge = ClockRenderBridge(engine, lambda: ClockConfig())
    widget = QQuickWidget()
    widget.resize(800, 450)

    configure_qml_host(
        widget,
        type_name="ClockFace",
        clear_color="#000000",
        context_properties={
            "clock": bridge,
            "timerDigitFontFamily": "Arial",
        },
    )
    widget.show()

    QTimer.singleShot(50, _APP.quit)
    _APP.exec()

    root = widget.rootObject()
    assert widget.errors() == []
    assert root is not None
    assert root.width() == 800
    assert root.height() == 450
    assert any(":" in text for text in _visible_texts(root))


def test_timer_pointer_area_enters_and_exits_native_cursor_state() -> None:
    probe = _CursorProbe()
    widget = QQuickWidget()
    widget.resize(160, 48)

    configure_qml_host(
        widget,
        type_name="TimerButton",
        clear_color="#000000",
        context_properties={"timer": probe},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("text", "Start")
    widget.show()
    _APP.processEvents()

    QTest.mouseMove(widget, QPoint(12, 12))
    QTest.qWait(30)

    assert probe.entered == 1
    assert probe.exited == 0

    root.setProperty("enabled", False)
    QTest.qWait(30)

    assert probe.entered == 1
    assert probe.exited == 1


def test_shared_playlist_tree_requires_explicit_play_when_protection_is_enabled(
    pt_br_translator,
) -> None:
    nodes = [{
        "type": "media",
        "id": "media-1",
        "title": "Protected media",
        "duration": "1:00",
        "thumbSource": "",
        "cloudVisible": False,
        "cloudActive": False,
        "cloudProgress": -1.0,
        "cloudTooltip": "",
        "isMissing": False,
        "imageFraming": None,
        "mediaType": "video",
        "badge": "Video",
    }]
    protection = _PlaybackProtectionProbe(enabled=True)
    controller = _PlaylistTreeControllerProbe(nodes)
    widget = QQuickWidget()
    widget.resize(520, 180)

    configure_qml_host(
        widget,
        type_name="PlaylistTreeView",
        clear_color="#000000",
        image_providers={
            "playlisticons": PlaylistIconProvider(),
        },
        context_properties={"playbackProtection": protection},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("playlistController", controller)
    root.setProperty("playlistNodes", nodes)
    root.setProperty("hasItems", True)
    root_playlist = root.findChild(QObject, "rootPlaylist")
    assert root_playlist is not None
    root_playlist.scheduleRebuild(nodes)
    widget.show()
    QTest.qWait(30)

    media_card = next(
        item
        for item in root_playlist.findChildren(QObject)
        if item.property("nodeId") == "media-1"
    )
    play_button = media_card.findChild(QObject, "protectedPlayButton")
    download_button = media_card.findChild(QObject, "mediaDownloadButton")
    more_button = media_card.findChild(QObject, "mediaMoreButton")
    item_menu = media_card.findChild(QObject, "mediaItemMenu")
    card_hit_area = media_card.findChild(QObject, "mediaCardHitArea")
    framing_thumbnail = media_card.findChild(QObject, "imageFramingThumbnail")
    assert play_button is not None
    assert download_button is not None
    assert more_button is not None
    assert item_menu is not None
    assert card_hit_area is not None
    assert framing_thumbnail is not None
    assert play_button.property("visible") is True
    assert play_button.property("enabled") is True
    assert download_button.property("x") < play_button.property("x")
    assert play_button.property("x") < more_button.property("x")
    assert card_hit_area.property("cursorShape") == Qt.CursorShape.ArrowCursor
    assert framing_thumbnail.property("clickActionEnabled") is False
    protected_menu_count = item_menu.property("count")
    assert media_card.findChild(QObject, "mediaItemPlayAction") is None
    trim_action = media_card.findChild(QObject, "mediaItemTrimAction")
    assert trim_action is not None
    assert trim_action.property("text") == "Tempos de início e fim"

    assert root.findChild(QObject, "mediaTrimDialog") is None
    root.openMediaTrim(nodes[0])
    QTest.qWait(1)
    trim_dialog = root.findChild(QObject, "mediaTrimDialog")
    assert trim_dialog is not None
    trim_timeline = trim_dialog.findChild(QObject, "trimTimeline")
    trim_start_handle = trim_dialog.findChild(QObject, "trimStartHandle")
    trim_end_handle = trim_dialog.findChild(QObject, "trimEndHandle")
    trim_start_drag_area = trim_dialog.findChild(QObject, "trimStartDragArea")
    trim_end_drag_area = trim_dialog.findChild(QObject, "trimEndDragArea")
    trim_selected_range = trim_dialog.findChild(QObject, "trimSelectedRange")
    trim_start_bubble = trim_dialog.findChild(QObject, "trimStartBubble")
    trim_end_bubble = trim_dialog.findChild(QObject, "trimEndBubble")
    trim_audio_preview_label = trim_dialog.findChild(QObject, "trimAudioPreviewLabel")
    trim_muted_preview_label = trim_dialog.findChild(QObject, "trimMutedPreviewLabel")
    assert trim_timeline is not None
    assert trim_start_handle is not None
    assert trim_end_handle is not None
    assert trim_start_drag_area is not None
    assert trim_end_drag_area is not None
    assert trim_selected_range is not None
    assert trim_start_bubble is not None
    assert trim_end_bubble is not None
    assert trim_audio_preview_label is not None
    assert trim_muted_preview_label is not None
    assert trim_audio_preview_label.property("color").name() == "#cbd5e1"
    assert trim_muted_preview_label.property("color").name() == "#cbd5e1"

    trim_dialog.setProperty("durationMs", 1_000_000)
    trim_dialog.setProperty("startMs", 499_950)
    trim_dialog.setProperty("endMs", 500_050)
    QTest.qWait(1)

    start_inner_edge = (
        trim_start_handle.property("x") + trim_start_handle.property("width")
    )
    end_inner_edge = trim_end_handle.property("x")
    selected_start = trim_selected_range.property("x")
    selected_end = selected_start + trim_selected_range.property("width")
    assert start_inner_edge == pytest.approx(selected_start)
    assert end_inner_edge == pytest.approx(selected_end)
    assert start_inner_edge <= end_inner_edge
    assert (
        trim_start_drag_area.property("x")
        + trim_start_drag_area.property("width")
        <= trim_start_handle.property("width")
    )
    assert trim_end_drag_area.property("x") >= 0
    assert trim_start_bubble.property("y") <= -10
    assert trim_end_bubble.property("y") <= -10

    media_card.clicked.emit()
    assert controller.projected == []

    play_button.clicked.emit()
    assert controller.projected == ["media-1"]

    protection.set_locked(True)
    QTest.qWait(1)
    assert play_button.property("enabled") is False

    protection.set_enabled(False)
    QTest.qWait(1)
    assert play_button.property("visible") is False
    assert card_hit_area.property("cursorShape") == Qt.CursorShape.PointingHandCursor
    assert framing_thumbnail.property("clickActionEnabled") is True
    assert item_menu.property("count") == protected_menu_count
    assert media_card.findChild(QObject, "mediaItemPlayAction") is None
    media_card.clicked.emit()
    assert controller.projected == ["media-1", "media-1"]

    unprotected_video_menu_count = item_menu.property("count")
    media_card.setProperty("mediaType", "image")
    QTest.qWait(1)
    assert item_menu.property("count") == unprotected_video_menu_count - 1
    assert media_card.findChild(QObject, "mediaItemTrimAction") is None


def _send_thumbnail_wheel(widget: QQuickWidget, modifiers) -> None:
    position = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        position,
        widget.mapToGlobal(position.toPoint()).toPointF(),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget.quickWindow(), event)


def test_image_framing_thumbnail_handles_click_zoom_pan_and_reset() -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
        image_providers={
            "playlistthumbs": PlaylistThumbnailProvider({"portrait": portrait}),
        },
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("imageSource", "image://playlistthumbs/portrait/0")
    root.setProperty("projectionAspectRatio", 16 / 9)
    root.setProperty("sourceAspectRatio", 9 / 16)
    widget.show()
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True
    assert root.property("sourceWidth") == pytest.approx(9 / 16)
    assert root.property("sourceHeight") == 1.0
    interaction_area = root.findChild(QObject, "imageFramingInteractionArea")
    assert interaction_area is not None
    assert interaction_area.property("cursorShape") == Qt.CursorShape.PointingHandCursor

    clicked = QSignalSpy(root.clicked)
    edited = QSignalSpy(root.framingEdited)
    reset = QSignalSpy(root.framingReset)

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(53, 28), delay=5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(53, 28))
    assert clicked.count() == 1
    assert edited.count() == 0

    root.setProperty("clickActionEnabled", False)
    assert interaction_area.property("cursorShape") == Qt.CursorShape.ArrowCursor
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    assert clicked.count() == 1
    root.setProperty("clickActionEnabled", True)

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(50, 44), delay=5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 44))
    assert clicked.count() == 1
    assert edited.count() == 0
    assert root.property("framingActive") is False
    assert root.property("panAvailable") is False

    _send_thumbnail_wheel(widget, Qt.KeyboardModifier.NoModifier)
    assert root.property("framingZoom") == 1.0
    assert edited.count() == 0

    _send_thumbnail_wheel(widget, Qt.KeyboardModifier.ControlModifier)
    assert root.property("framingZoom") > 1.0
    assert edited.count() == 1
    assert edited.at(0)[5] is True
    assert root.property("framingActive") is True
    assert root.property("panAvailable") is True

    zoom_before_pan = float(root.property("framingZoom"))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(50, 80), delay=5)
    assert root.property("panning") is True
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
    assert clicked.count() == 1
    assert edited.count() > 1
    assert edited.at(edited.count() - 1)[5] is False
    assert root.property("framingZoom") == pytest.approx(zoom_before_pan)
    assert root.property("panning") is False

    cover_zoom = float(root.coverZoom())
    for _step in range(20):
        if float(root.property("framingZoom")) >= cover_zoom:
            break
        _send_thumbnail_wheel(widget, Qt.KeyboardModifier.ControlModifier)
    assert root.property("framingZoom") == pytest.approx(cover_zoom)
    assert root.maxPanX() == pytest.approx(0.0, abs=1e-9)

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(86, 42))
    assert reset.count() == 1
    assert root.property("framingActive") is False


def test_image_framing_thumbnail_loads_a_local_file_source(tmp_path) -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    source_path = tmp_path / "portrait.png"
    assert portrait.save(str(source_path), "PNG")

    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("imageSource", source_path.as_uri())
    root.setProperty("sourceAspectRatio", 9 / 16)
    widget.show()

    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)

    assert root.property("imageReady") is True


def test_image_framing_thumbnail_projection_frame_covers_fixed_viewport() -> None:
    landscape = QPixmap(160, 90)
    landscape.fill(QColor("red"))
    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
        image_providers={
            "playlistthumbs": PlaylistThumbnailProvider({
                "landscape": landscape,
            }),
        },
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("imageSource", "image://playlistthumbs/landscape/0")
    root.setProperty("sourceAspectRatio", 16 / 9)
    widget.show()
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True

    root.setProperty("projectionAspectRatio", 5 / 4)
    _APP.processEvents()

    assert root.property("frameWidth") == pytest.approx(100.0)
    assert root.property("frameHeight") == pytest.approx(80.0)
    assert root.property("frameX") == pytest.approx(0.0)
    assert root.property("frameY") == pytest.approx(-12.0)
    assert root.property("drawnWidth") == pytest.approx(56 * 16 / 9)
    assert root.property("drawnHeight") == pytest.approx(56.0)

    root.setProperty("projectionAspectRatio", 21 / 9)
    _APP.processEvents()

    assert root.property("frameWidth") == pytest.approx(56 * 21 / 9)
    assert root.property("frameHeight") == pytest.approx(56.0)
    assert root.property("frameX") < 0.0
    assert root.property("frameY") == pytest.approx(0.0)


def test_image_framing_thumbnail_fits_portrait_in_fixed_viewport() -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
        image_providers={
            "playlistthumbs": PlaylistThumbnailProvider({
                "portrait": portrait,
            }),
        },
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("projectionAspectRatio", 5 / 4)
    root.setProperty("sourceAspectRatio", 9 / 16)
    root.setProperty("imageSource", "image://playlistthumbs/portrait/0")
    widget.show()
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True

    # Projection calculations still use the virtual 5:4 frame.
    assert root.property("frameWidth") == pytest.approx(100.0)
    assert root.property("frameHeight") == pytest.approx(80.0)
    assert root.property("projectionDrawnHeight") == pytest.approx(80.0)

    # The fixed 16:9 thumbnail shows the complete portrait at identity.
    assert root.property("drawnWidth") == pytest.approx(56 * 9 / 16)
    assert root.property("drawnHeight") == pytest.approx(56.0)


def test_image_framing_thumbnail_preserves_pan_until_geometry_is_ready() -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    widget.resize(100, 56)
    configure_qml_host(
        widget,
        type_name="ImageFramingThumbnail",
        clear_color="#000000",
        image_providers={
            "playlistthumbs": PlaylistThumbnailProvider({"portrait": portrait}),
        },
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("projectionAspectRatio", 16 / 9)
    root.setProperty("framing", {
        "version": 1,
        "zoom": 4.0,
        "norm_x": 0.15,
        "norm_y": -0.2,
    })

    assert root.property("imageReady") is False
    assert root.property("framingNormX") == pytest.approx(0.15)
    assert root.property("framingNormY") == pytest.approx(-0.2)

    root.setProperty("imageSource", "image://playlistthumbs/portrait/0")
    widget.show()
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True
    QTest.qWait(20)
    assert root.property("framingNormX") < 0.15
    assert root.property("requestedNormX") == pytest.approx(0.15)

    root.setProperty("sourceAspectRatio", 0.65)
    QTest.qWait(20)

    assert root.property("framingNormX") == pytest.approx(0.15)
    assert root.property("framingNormY") == pytest.approx(-0.2)
