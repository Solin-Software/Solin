from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import (
    Q_ARG,
    QCoreApplication,
    QEvent,
    Property,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    QSize,
    QTimer,
    QTranslator,
    QUrl,
    Signal,
    Slot,
    Qt,
)
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPixmap, QWheelEvent
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from solin.controllers.timer_engine import TimerEngine
from solin.core.foundation.resources import application_translation_root
from solin.core.i18n.meeting_schedule import (
    meeting_kind_label,
    meeting_weekday_names,
)
from solin.core.meetings.tree_editing import move_tree_node
from solin.core.timer.models import ClockConfig
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.media_tree.model import MediaTreeSource
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)
from solin.ui.qml.playlist.bridge import PlaylistEditBridge
from solin.ui.qml.playlist.visuals import PlaylistIconProvider, PlaylistThumbnailProvider
from solin.ui.qml.timer_output import ClockRenderBridge
from solin.ui.qml.timer_icons import TimerIconProvider
from solin.ui.helpers import (
    begin_qml_pointer_cursor,
    end_qml_pointer_cursor,
    set_qml_pointer_cursor,
)
from tests._paths import REPO_ROOT
from tests._qt import (
    dispose_widget, mouse_click, mouse_move, show_and_activate, wait_for_geometry, wait_until,
)


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


class _AdvancedTimerProbe(QObject):
    scheduleChanged = Signal()
    liveStateChanged = Signal()
    clockConfigChanged = Signal()
    monitorsChanged = Signal()
    weekChanged = Signal()
    weekShift = Signal(int)

    def __init__(self, *, part_state: str = "idle") -> None:
        super().__init__()
        self._clock_config = {
            "mode": "digital",
            "analog_style": "signature",
            "hour_format_24h": True,
            "show_seconds": True,
            "show_ampm": False,
            "part_timer_display": "timer",
            "direction": "down",
            "freeze_seconds": 4,
            "text_scale_pct": 70,
        }
        self._parts = [
            {
                "id": "part-1",
                "section": "ministry",
                "sectionKey": "ministry",
                "sectionColor": "#5a9cf8",
                "sectionTextColor": "#9fc5ff",
                "sectionBadgeBg": "#172a45",
                "sectionBorderColor": "#315b91",
                "title": "Initial Call",
                "displayNumber": 1,
                "plannedLabel": "03:00",
                "state": part_state,
                "firstOfSection": True,
                "showSectionHeader": True,
                "configurableCount": True,
                "sectionCount": 3,
                "sectionTotalLabel": "12 min",
                "startedLabel": "19:30:00" if part_state != "idle" else "",
                "resultLabel": "02:54" if part_state == "stopped" else "",
            }
        ]
        self._live_state = {
            "active": part_state == "running",
            "active_part_id": "part-1" if part_state == "running" else "",
            "overrun": False,
            "display_seconds": 96,
        }

    @Property("QVariant", notify=clockConfigChanged)
    def clockConfig(self):  # noqa: N802 - QML API
        return self._clock_config

    @Property("QVariant", constant=True)
    def clockModes(self):  # noqa: N802 - QML API
        return ["digital", "analog", "analog_digital"]

    @Property("QVariant", constant=True)
    def analogClockStyles(self):  # noqa: N802 - QML API
        return ["signature", "classic"]

    @Property("QVariant", constant=True)
    def partTimerDisplays(self):  # noqa: N802 - QML API
        return ["timer", "clock", "clock_timer"]

    @Property(bool, notify=monitorsChanged)
    def timerVisible(self):  # noqa: N802 - QML API
        return True

    @Property("QVariant", notify=monitorsChanged)
    def monitors(self):
        return [
            {
                "index": 1,
                "name": "Secondary display",
                "resolution": "1920 × 1080",
                "reserved": False,
            }
        ]

    @Property(str, notify=weekChanged)
    def weekLabel(self):  # noqa: N802 - QML API
        return "8–14 June 2026"

    @Property(bool, notify=weekChanged)
    def isCurrentWeek(self):  # noqa: N802 - QML API
        return True

    @Property(str, notify=weekChanged)
    def meetingType(self):  # noqa: N802 - QML API
        return "midweek"

    @Property("QVariant", notify=scheduleChanged)
    def parts(self):
        return self._parts

    @Property("QVariant", notify=liveStateChanged)
    def liveState(self):  # noqa: N802 - QML API
        return self._live_state

    @Slot(str, "QVariant")
    def updateClock(self, key: str, value) -> None:  # noqa: N802 - QML API
        self._clock_config[key] = value
        self.clockConfigChanged.emit()

    @Slot(bool)
    def setTimerVisible(self, _visible: bool) -> None:  # noqa: N802 - QML API
        pass

    @Slot(str)
    def setMeetingType(self, _meeting_type: str) -> None:  # noqa: N802 - QML API
        pass

    @Slot()
    def previousWeek(self) -> None:  # noqa: N802 - QML API
        pass

    @Slot()
    def nextWeek(self) -> None:  # noqa: N802 - QML API
        pass

    @Slot()
    def goToCurrentWeek(self) -> None:  # noqa: N802 - QML API
        pass

    @Slot()
    def exportSchedulePdf(self) -> None:  # noqa: N802 - QML API
        pass

    def _set_part_state(self, state: str) -> None:
        started_label = "19:30:00" if state != "idle" else ""
        result_label = "02:54" if state == "stopped" else ""
        self._parts = [
            {
                **self._parts[0],
                "state": state,
                "startedLabel": started_label,
                "resultLabel": result_label,
            }
        ]
        self._live_state = {
            **self._live_state,
            "active": state == "running",
            "active_part_id": "part-1" if state == "running" else "",
        }
        self.scheduleChanged.emit()
        self.liveStateChanged.emit()

    @Slot(str)
    def startPart(self, _part_id: str) -> None:  # noqa: N802 - QML API
        self._set_part_state("running")

    @Slot(str)
    def stopPart(self, _part_id: str) -> None:  # noqa: N802 - QML API
        self._set_part_state("stopped")

    @Slot(str)
    def resetPart(self, _part_id: str) -> None:  # noqa: N802 - QML API
        self._set_part_state("idle")


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
    markerEditRequested = Signal(str)
    pointerEntered = Signal()
    pointerCursorEntered = Signal(str, int)
    pointerCursorChanged = Signal(str, int)
    pointerCursorExited = Signal(str)
    pointerExited = Signal()

    def __init__(self, nodes: list[dict]) -> None:
        super().__init__()
        self._nodes = nodes
        self.projected: list[str] = []
        self.destination_requests: list[str] = []
        self.idle_requests: list[str] = []
        self.moves: list[tuple[str, str, int, int]] = []
        self.collapsed: list[str] = []

    @Slot(str)
    def projectItem(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.projected.append(item_id)

    @Slot(str)
    def addToDestination(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.destination_requests.append(item_id)

    @Slot(str)
    def setAsIdle(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.idle_requests.append(item_id)

    @Slot(str)
    def toggleCollapse(self, section_id: str) -> None:  # noqa: N802 - QML API
        self.collapsed.append(section_id)

    @Slot(result=float)
    def imageFramingAspectRatio(self) -> float:  # noqa: N802 - QML API
        return 16 / 9

    @Slot(str, result=float)
    def imageFramingSourceAspectRatio(self, _item_id: str) -> float:  # noqa: N802
        return 16 / 9

    @Slot(result=int)
    def treeStructureRevision(self) -> int:  # noqa: N802 - QML API
        return 1

    @Slot(str, str, str, result=bool)
    def canDrop(  # noqa: N802 - QML API
        self,
        _node_id: str,
        _node_type: str,
        _target_list_id: str,
    ) -> bool:
        return True

    @Slot(str, str, int, str, int, result=bool)
    def moveNode(  # noqa: N802 - QML API
        self,
        node_id: str,
        target_list_id: str,
        insert_index: int,
        tree_id: str,
        structure_revision: int,
    ) -> bool:
        if tree_id != "qml-test":
            return False
        if not move_tree_node(self._nodes, node_id, target_list_id, insert_index):
            return False
        self.moves.append(
            (node_id, target_list_id, insert_index, structure_revision)
        )
        return True

    @Slot()
    def pointerEnter(self) -> None:  # noqa: N802 - QML API
        self.pointerEntered.emit()

    @Slot(str, int)
    def pointerCursorEnter(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ) -> None:
        self.pointerCursorEntered.emit(cursor_source, cursor_shape)

    @Slot(str, int)
    def pointerCursorChange(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ) -> None:
        self.pointerCursorChanged.emit(cursor_source, cursor_shape)

    @Slot(str)
    def pointerCursorExit(self, cursor_source: str) -> None:  # noqa: N802
        self.pointerCursorExited.emit(cursor_source)

    @Slot()
    def pointerExit(self) -> None:  # noqa: N802 - QML API
        self.pointerExited.emit()


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
        "canDrag": True,
        "canEdit": True,
        "canProject": True,
        "canSetAsIdle": True,
        "canRemove": True,
    }


def _playlist_tree_host(
    nodes: list[dict],
    *,
    height: int = 260,
    register_cleanup,
) -> tuple[
    QQuickWidget,
    _PlaylistTreeControllerProbe,
    MediaTreeSource,
    _PlaybackProtectionProbe,
]:
    protection = _PlaybackProtectionProbe(enabled=False)
    controller = _PlaylistTreeControllerProbe(nodes)
    widget = QQuickWidget()
    register_cleanup(widget)
    protection.setParent(widget)
    controller.setParent(widget)
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
    model = MediaTreeSource("qml-test", widget)
    model.activate_snapshot(
        MediaTreeSnapshot.create(
            "qml-test",
            1,
            tuple(_snapshot_node(node) for node in nodes),
        )
    )
    root.setProperty("playlistController", controller)
    root.setProperty("treeSource", model)
    root.setProperty("hasItems", True)
    widget.show()
    QTest.qWait(40)
    return widget, controller, model, protection


@pytest.fixture
def playlist_tree_host(request):
    def register_cleanup(widget):
        def close_widget():
            if shiboken6.isValid(widget):
                dispose_widget(widget)

        request.addfinalizer(close_widget)

    def create(nodes, *, height=260):
        return _playlist_tree_host(nodes, height=height, register_cleanup=register_cleanup)

    return create


def _dispose_qml_host(
    widget: QQuickWidget,
    popup: QObject | None = None,
) -> None:
    try:
        if popup is not None:
            QMetaObject.invokeMethod(popup, "close", Qt.ConnectionType.DirectConnection)
    finally:
        dispose_widget(widget)


def _snapshot_node(node: dict) -> MediaTreeNodeSnapshot:
    roles = {
        key: value
        for key, value in node.items()
        if key not in {"id", "type", "children"}
    }
    return MediaTreeNodeSnapshot.create(
        str(node["id"]),
        MediaTreeNodeType(str(node["type"])),
        roles=roles,
        children=tuple(_snapshot_node(child) for child in node.get("children", [])),
    )


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    texts: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        texts.append(text)
    for child in item.childItems():
        texts.extend(_visible_texts(child, parent_visible=visible))
    return texts


def _find_visual(item, object_name: str):
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        if match := _find_visual(child, object_name):
            return match
    return None


def _find_visuals(item, object_name: str):
    matches = [item] if item.objectName() == object_name else []
    for child in item.childItems():
        matches.extend(_find_visuals(child, object_name))
    return matches


def _wait_until(predicate, *, timeout_seconds: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the QML state change")
        QTest.qWait(10)


def test_playlist_tree_reconciles_complete_snapshots_without_recreating_host(playlist_tree_host) -> None:
    keep = _playlist_media_node("keep", "Keep")
    remove = _playlist_media_node("remove", "Remove")
    widget, _controller, model, _protection = playlist_tree_host(
        [keep, remove],
        height=220,
    )
    root = widget.rootObject()
    assert root is not None
    keep_card = _find_visual(root, "mediaCard-keep")
    assert keep_card is not None
    assert _find_visual(root, "mediaCard-remove") is not None

    renamed_keep = {
        **keep,
        "title": "Renamed without rebuilding",
        "thumbSource": "image://playlistthumbs/keep/2",
    }
    model.publish_snapshot(
        MediaTreeSnapshot.create("qml-test", 2, (_snapshot_node(renamed_keep),))
    )
    QTest.qWait(30)

    assert all(node["id"] != "remove" for node in model.treeData)
    assert _find_visual(root, "mediaCard-keep") is keep_card
    assert "Renamed without rebuilding" in _visible_texts(keep_card)
    assert keep_card.property("thumbSource") == "image://playlistthumbs/keep/2"

    same_id_in_another_tree = {
        **keep,
        "title": "Same ID in another playlist",
    }
    model.activate_snapshot(
        MediaTreeSnapshot.create(
            "qml-other",
            1,
            (_snapshot_node(same_id_in_another_tree),),
        )
    )
    QTest.qWait(30)
    assert _find_visual(root, "mediaCard-keep") is keep_card
    assert "Same ID in another playlist" in _visible_texts(keep_card)

    same_tree_update = {
        **keep,
        "title": "Updated after same-tree reactivation",
    }
    model.begin_transition("qml-other")
    model.activate_snapshot(
        MediaTreeSnapshot.create(
            "qml-other",
            2,
            (_snapshot_node(same_tree_update),),
        )
    )
    QTest.qWait(30)
    assert _find_visual(root, "mediaCard-keep") is keep_card
    assert "Updated after same-tree reactivation" in _visible_texts(keep_card)


def test_playlist_tree_accepts_node_ids_reserved_by_javascript_objects(playlist_tree_host) -> None:
    nodes = [
        _playlist_media_node(node_id, node_id)
        for node_id in ("constructor", "__proto__", "toString", "safe-id")
    ]
    widget, _controller, _model, _protection = playlist_tree_host(
        nodes,
        height=360,
    )
    root = widget.rootObject()
    assert root is not None

    for node in nodes:
        assert _find_visual(root, f"mediaCard-{node['id']}") is not None



def test_playlist_tree_shows_a_retryable_state_after_snapshot_failure(playlist_tree_host) -> None:
    node = _playlist_media_node("media-1", "Media")
    widget, _controller, model, _protection = playlist_tree_host([node])
    root = widget.rootObject()
    assert root is not None
    retries: list[bool] = []
    model.retryRequested.connect(lambda: retries.append(True))

    model.begin_transition("qml-test")
    model.activate_error("qml-test", 2, "broken snapshot")
    QTest.qWait(30)

    error_state = _find_visual(root, "treeErrorState")
    retry_button = _find_visual(root, "treeRetryButton")
    assert error_state is not None and error_state.property("visible") is True
    assert retry_button is not None and retry_button.property("visible") is True

    center = retry_button.mapToScene(
        QPointF(retry_button.width() / 2, retry_button.height() / 2)
    ).toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=center)

    assert retries == [True]
    assert model.transitioning is True
    assert model.error == ""


def test_playlist_tree_preserves_card_identity_across_parent_snapshots(playlist_tree_host) -> None:
    media = _playlist_media_node("moving", "Moving media")
    first = {
        "type": "section",
        "id": "first-section",
        "title": "First",
        "color": "#4f46e5",
        "textColor": "#ffffff",
        "badgeBg": "#26235f",
        "collapsed": False,
        "itemCount": 1,
        "canDrag": True,
        "children": [media],
    }
    second = {
        **first,
        "id": "second-section",
        "title": "Second",
        "itemCount": 0,
        "children": [],
    }
    widget, _controller, model, _protection = playlist_tree_host(
        [first, second],
        height=360,
    )
    root = widget.rootObject()
    assert root is not None
    moving_card = _find_visual(root, "mediaCard-moving")
    assert moving_card is not None

    moved_first = {**first, "itemCount": 0, "children": []}
    moved_second = {**second, "itemCount": 1, "children": [media]}
    model.publish_snapshot(
        MediaTreeSnapshot.create(
            "qml-test",
            2,
            (_snapshot_node(moved_first), _snapshot_node(moved_second)),
        )
    )
    QTest.qWait(240)

    second_card = _find_visual(root, "sectionCard-second-section")
    assert second_card is not None
    assert _find_visual(root, "mediaCard-moving") is moving_card
    assert moving_card.mapToScene(QPointF()).y() > second_card.mapToScene(QPointF()).y()
    assert widget.errors() == []


def test_playlist_edit_shell_accepts_the_shared_tree_theme_contract() -> None:
    widget = QQuickWidget()
    widget.resize(640, 420)
    controller = PlaylistEditBridge(parent=widget)
    model = MediaTreeSource("playlist:shell", widget)
    configure_qml_host(
        widget,
        type_name="PlaylistEditView",
        clear_color="#000000",
        image_providers={
            "playlisticons": PlaylistIconProvider(),
            "playlistthumbs": PlaylistThumbnailProvider({}),
        },
        context_properties={
            "controller": controller,
            "playlistTreeSource": model,
            "playbackProtection": None,
        },
        mouse_tracking=True,
    )

    assert widget.rootObject() is not None
    assert widget.errors() == []
    widget.deleteLater()


def test_playlist_tree_preserves_nested_section_geometry(playlist_tree_host) -> None:
    child = _playlist_media_node("child", "Nested media")
    populated = {
        "type": "section",
        "id": "populated",
        "title": "Populated section",
        "color": "#4f46e5",
        "textColor": "#ffffff",
        "badgeBg": "#26235f",
        "collapsed": False,
        "itemCount": 1,
        "canDrag": True,
        "children": [child],
    }
    empty = {
        "type": "section",
        "id": "empty",
        "title": "Empty section",
        "color": "#f59e0b",
        "textColor": "#ffffff",
        "badgeBg": "#4c3510",
        "collapsed": False,
        "itemCount": 0,
        "canDrag": True,
        "children": [],
    }
    widget, _controller, _model, _protection = playlist_tree_host(
        [populated, empty],
        height=360,
    )
    root = widget.rootObject()
    assert root is not None
    populated_card = _find_visual(root, "sectionCard-populated")
    empty_card = _find_visual(root, "sectionCard-empty")
    child_card = _find_visual(root, "mediaCard-child")
    assert populated_card is not None
    assert empty_card is not None
    assert child_card is not None

    QTest.qWait(240)
    populated_scene = populated_card.mapToScene(QPointF())
    child_scene = child_card.mapToScene(QPointF())
    assert child_scene.x() - populated_scene.x() == pytest.approx(16)
    assert populated_card.height() > 48
    assert empty_card.height() == pytest.approx(118)
    assert widget.errors() == []


def test_playlist_tree_drag_keeps_placeholder_and_full_ghost_feedback(playlist_tree_host) -> None:
    first = _playlist_media_node("first", "First media")
    second = _playlist_media_node("second", "Second media")
    widget, controller, _model, _protection = playlist_tree_host(
        [first, second],
        height=220,
    )
    root = widget.rootObject()
    assert root is not None
    grip = _find_visual(root, "dragGrip-first")
    card = _find_visual(root, "mediaCard-first")
    overlay = _find_visual(root, "dragOverlay")
    placeholder = _find_visual(root, "dragPlaceholder")
    assert grip is not None
    assert card is not None
    assert overlay is not None
    assert placeholder is not None

    QTest.qWait(240)
    start = grip.mapToScene(QPointF(grip.width() / 2, grip.height() / 2)).toPoint()
    moved = QPoint(start.x() + 30, start.y() + 35)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    try:
        QTest.mouseMove(widget, moved, delay=10)
        QTest.qWait(20)
        assert card.parentItem() is overlay
        assert card.height() == pytest.approx(72)
        assert card.property("opacity") == pytest.approx(0.34)
        assert placeholder.property("visible") is True
    finally:
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=moved)
    QTest.qWait(10)
    assert placeholder.property("visible") is False
    assert controller.moves
    assert controller.moves[-1][-1] == 1
    assert widget.errors() == []


def test_playlist_tree_accepts_consecutive_down_and_back_reorders(playlist_tree_host) -> None:
    first = _playlist_media_node("first", "First media")
    second = _playlist_media_node("second", "Second media")
    widget, controller, _model, _protection = playlist_tree_host(
        [first, second],
        height=220,
    )
    root = widget.rootObject()
    assert root is not None
    first_card = _find_visual(root, "mediaCard-first")
    second_card = _find_visual(root, "mediaCard-second")
    assert first_card is not None
    assert second_card is not None
    QTest.qWait(240)

    def drag_by(item_id: str, delta_y: int) -> None:
        drag_area = _find_visual(root, f"dragMouse-{item_id}")
        assert drag_area is not None
        start = drag_area.mapToScene(
            QPointF(drag_area.width() / 2, drag_area.height() / 2)
        ).toPoint()
        destination = QPoint(start.x() + 20, start.y() + delta_y)
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
        try:
            QTest.qWait(5)
            QTest.mouseMove(widget, QPoint(start.x() + 10, start.y() + 10), delay=10)
            QTest.mouseMove(widget, destination, delay=10)
        finally:
            QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=destination)
        QTest.qWait(240)

    drag_by("first", 115)
    assert first_card.mapToScene(QPointF()).y() > second_card.mapToScene(QPointF()).y()
    assert [node["id"] for node in controller._nodes] == ["second", "first"]

    drag_by("first", -115)
    assert first_card.mapToScene(QPointF()).y() < second_card.mapToScene(QPointF()).y()
    assert [node["id"] for node in controller._nodes] == ["first", "second"]
    assert len(controller.moves) == 2
    assert widget.errors() == []


def test_playlist_section_collapse_retains_the_original_height_animation(playlist_tree_host) -> None:
    section = {
        "type": "section",
        "id": "animated",
        "title": "Animated section",
        "color": "#4f46e5",
        "textColor": "#ffffff",
        "badgeBg": "#26235f",
        "collapsed": False,
        "itemCount": 1,
        "canDrag": True,
        "children": [_playlist_media_node("child", "Nested media")],
    }
    widget, controller, _model, _protection = playlist_tree_host(
        [section],
        height=260,
    )
    try:
        root = widget.rootObject()
        assert root is not None
        card = _find_visual(root, "sectionCard-animated")
        assert card is not None
        for _ in range(100):
            if card.height() > 100:
                break
            QTest.qWait(10)
        expanded_height = card.height()
        assert expanded_height > 100

        height_samples: list[float] = []
        card.heightChanged.connect(lambda: height_samples.append(card.height()))
        assert QMetaObject.invokeMethod(card, "toggleCollapsed")
        for _ in range(100):
            if abs(card.height() - 48) < 0.01:
                break
            QTest.qWait(10)

        assert any(48 < height < expanded_height for height in height_samples)
        assert card.height() == pytest.approx(48, abs=0.01)
        assert controller.collapsed == ["animated"]
        assert widget.errors() == []
    finally:
        dispose_widget(widget)


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
    QTest.qWait(1)
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
        image_providers={"timericons": TimerIconProvider()},
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


@pytest.mark.parametrize(
    ("width", "compact_navigation"),
    [(320, True), (480, True), (768, True), (900, False), (1200, False)],
)
def test_advanced_timer_keeps_navigation_and_part_actions_inside_available_width(
    width: int,
    compact_navigation: bool,
) -> None:
    timer = _AdvancedTimerProbe()
    widget = QQuickWidget()
    widget.resize(width, 760)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    QTest.qWait(40)

    content = root.findChild(QObject, "advancedTimerContent")
    navigation = root.findChild(QObject, "timerWeekNavigationCard")
    wide_layout = root.findChild(QObject, "timerWeekNavigationWide")
    compact_layout = root.findChild(QObject, "timerWeekNavigationCompact")
    assert content is not None
    assert navigation is not None
    assert wide_layout is not None
    assert compact_layout is not None
    assert 0 < content.property("width") <= width
    assert navigation.property("width") <= content.property("width")
    assert bool(compact_layout.property("visible")) is compact_navigation
    assert bool(wide_layout.property("visible")) is not compact_navigation

    settings_buttons = [
        button
        for name in ("timerSettingsButtonWide", "timerSettingsButtonCompact")
        if (button := _find_visual(root, name)) is not None and button.isVisible()
    ]
    assert len(settings_buttons) == 1
    button_origin = settings_buttons[0].mapToItem(navigation, QPointF())
    assert button_origin.x() >= 0
    assert button_origin.x() + settings_buttons[0].property("width") <= navigation.property("width")

    part_row = _find_visual(root, "timerMeetingPartRow")
    assert part_row is not None
    assert 0 < part_row.property("width") <= content.property("width")
    assert bool(part_row.property("compactLayout")) is (part_row.property("width") < 620)

    visible_actions = [
        action
        for action in _find_visuals(part_row, "timerPartActionButton")
        if action.isVisible()
    ]
    assert len(visible_actions) == 1
    action = visible_actions[0]
    action_origin = action.mapToItem(part_row, QPointF())
    assert action_origin.x() >= 0
    assert action_origin.x() + action.property("width") <= part_row.property("width") + 0.5
    assert widget.errors() == []
    widget.deleteLater()


@pytest.mark.parametrize(("width", "height"), [(320, 400), (640, 520), (960, 760)])
def test_advanced_timer_settings_open_as_a_responsive_modal(
    request,
    width: int,
    height: int,
) -> None:
    timer = _AdvancedTimerProbe()
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    timer.setParent(widget)
    widget.resize(width, height)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    show_and_activate(widget)

    settings = root.findChild(QObject, "timerSettingsDialog")
    viewport = root.findChild(QObject, "timerSettingsViewport")
    assert settings is not None
    assert viewport is not None
    settings_button = next(
        button
        for name in ("timerSettingsButtonWide", "timerSettingsButtonCompact")
        if (button := _find_visual(root, name)) is not None and button.isVisible()
    )
    click_point = settings_button.mapToScene(
        QPointF(settings_button.width() / 2, settings_button.height() / 2)
    ).toPoint()
    mouse_click(widget, Qt.MouseButton.LeftButton, pos=click_point)
    wait_until(
        lambda: settings.property("visible") and viewport.property("width") > 0,
        description="timer settings modal layout",
    )

    assert settings.property("visible") is True
    assert 0 < settings.property("width") <= width - 32
    assert 0 < settings.property("height") <= height - 32
    assert bool(settings.property("compact")) is (settings.property("width") < 520)
    assert 0 < viewport.property("width") <= settings.property("width")
    assert viewport.property("contentWidth") == pytest.approx(viewport.property("width"))
    for card_name in (
        "timerSettingsClockCard",
        "timerSettingsPartCard",
        "timerSettingsDisplayCard",
        "timerSettingsMonitorsCard",
    ):
        card = root.findChild(QObject, card_name)
        assert card is not None
        assert 0 < card.property("width") <= viewport.property("width")

    clock_mode = root.findChild(QObject, "timerClockModeSelect")
    assert clock_mode is not None
    assert QMetaObject.invokeMethod(clock_mode, "picked", Q_ARG(int, 1))
    assert timer.clockConfig["mode"] == "analog"
    assert widget.errors() == []
    assert QMetaObject.invokeMethod(settings, "close")
    _wait_until(lambda: settings.property("visible") is False)
    assert settings_button.property("activeFocus") is True
    assert widget.errors() == []


@pytest.mark.parametrize(
    ("width", "state", "expected_action"),
    [
        (320, "running", "Stop"),
        (320, "stopped", "Reset"),
        (900, "running", "Stop"),
        (900, "stopped", "Reset"),
    ],
)
def test_advanced_timer_part_actions_remain_visible_in_live_states(
    width: int,
    state: str,
    expected_action: str,
) -> None:
    timer = _AdvancedTimerProbe(part_state=state)
    widget = QQuickWidget()
    widget.resize(width, 520)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    QTest.qWait(40)

    part_row = _find_visual(root, "timerMeetingPartRow")
    assert part_row is not None
    visible_actions = [
        action
        for action in _find_visuals(part_row, "timerPartActionButton")
        if action.isVisible()
    ]
    assert len(visible_actions) == 1
    action = visible_actions[0]
    action_origin = action.mapToItem(part_row, QPointF())
    assert action.property("text") == expected_action
    assert action_origin.x() + action.property("width") <= part_row.property("width") + 0.5
    assert widget.errors() == []
    widget.deleteLater()


@pytest.mark.parametrize("width", [320, 900])
def test_advanced_timer_part_state_changes_keep_the_row_geometry_stable(
    width: int,
) -> None:
    timer = _AdvancedTimerProbe()
    widget = QQuickWidget()
    widget.resize(width, 520)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    QTest.qWait(40)

    def visible_part_item(object_name: str):
        part_row = _find_visual(root, "timerMeetingPartRow")
        assert part_row is not None
        matches = [
            item
            for item in _find_visuals(part_row, object_name)
            if item.isVisible()
        ]
        assert len(matches) == 1
        return part_row, matches[0]

    def item_rect(item, relative_to):
        origin = item.mapToItem(relative_to, QPointF())
        return (origin.x(), origin.y(), item.property("width"), item.property("height"))

    def stable_geometry():
        part_row, action = visible_part_item("timerPartActionButton")
        region_name = (
            "timerPartCompactTimingArea"
            if bool(part_row.property("compactLayout"))
            else "timerPartPlannedControl"
        )
        _, timing_region = visible_part_item(region_name)
        action_x, action_y, action_width, action_height = item_rect(action, part_row)
        timing_x, timing_y, timing_width, timing_height = item_rect(
            timing_region,
            part_row,
        )
        return (
            part_row.property("height"),
            action_y,
            action_width,
            action_height,
            part_row.property("width") - action_x - action_width,
            timing_y,
            timing_height,
            action_x - timing_x - timing_width,
        )

    def visible_action():
        _part_row, action = visible_part_item("timerPartActionButton")
        return action

    initial_geometry = stable_geometry()
    for expected_text in ("Stop", "Reset", "Start"):
        action = visible_action()
        click_point = action.mapToScene(
            QPointF(action.width() / 2, action.height() / 2)
        ).toPoint()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=click_point)
        _wait_until(
            lambda expected=expected_text: visible_action().property("text") == expected
        )
        assert stable_geometry() == pytest.approx(initial_geometry, abs=0.5)

    assert widget.errors() == []
    widget.close()
    widget.deleteLater()


def test_advanced_timer_settings_reuse_existing_portuguese_catalog(
    pt_br_translator,
) -> None:
    timer = _AdvancedTimerProbe()
    widget = QQuickWidget()
    widget.resize(640, 760)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    QTest.qWait(30)

    settings = root.findChild(QObject, "timerSettingsDialog")
    close_button = root.findChild(QObject, "timerSettingsCloseButton")
    assert settings is not None
    assert close_button is not None
    assert QMetaObject.invokeMethod(settings, "open")
    QTest.qWait(220)

    content_item = settings.property("contentItem")
    visible_text = _visible_texts(content_item)
    assert "Cronômetro · Configurações" in visible_text
    assert "Mostrador do relógio" in visible_text
    assert "Telas" in visible_text
    assert close_button.property("tip") == "Fechar"
    assert widget.errors() == []
    assert QMetaObject.invokeMethod(settings, "close")
    _wait_until(lambda: settings.property("visible") is False)
    widget.close()
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
    assert "Quanto tempo antes da reunião a contagem regressiva deve começar?" in visible_text
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


def _timer_pointer_host(request, offset: QPoint, *, root_control: bool = False):
    initial_cursor = QCursor.pos()
    host = QWidget()
    host.resize(420, 220)
    host.move(host.screen().availableGeometry().center() - host.rect().center())
    QCursor.setPos(host.screen().availableGeometry().bottomRight())
    QGuiApplication.sync()
    host.setMouseTracking(True)
    widget = QQuickWidget(host)
    widget.setGeometry(
        offset.x() if root_control else 10, offset.y() if root_control else 10,
        160 if root_control else 400, 48 if root_control else 200,
    )
    probe = _CursorProbe()

    def cleanup(_probe=probe) -> None:
        # Retain the context bridge until Component.onDestruction finishes.
        try:
            dispose_widget(host)
        finally:
            QCursor.setPos(initial_cursor)
            QGuiApplication.sync()

    request.addfinalizer(cleanup)
    configure_qml_host(
        widget, type_name="TimerButton", clear_color="#000000",
        context_properties={"timer": probe}, mouse_tracking=True, defer_load=not root_control,
    )
    if root_control:
        button = widget.rootObject()
        assert button is not None
        button.setProperty("text", "Start")
        button.setProperty("enabled", False)
        widget.show()
        return host, widget, button, probe
    # Production timer controls live inside a page, rather than occupying the
    # whole native view. Keep view crossings outside the control's hit rectangle.
    url = QUrl.fromLocalFile(str(REPO_ROOT / "src/solin/qml/TimerInputTest.qml"))
    component = QQmlComponent(widget.engine(), widget)
    component.setData(b'''
        import QtQuick
        Item {
            property int buttonX: 40
            property int buttonY: 40
            TimerButton {
                objectName: "nativeTimerButton"
                x: parent.buttonX; y: parent.buttonY
                width: 160; height: 48; text: "Start"
                enabled: false
            }
        }
    ''', url)
    root = component.create(widget.rootContext())
    assert root is not None, [error.toString() for error in component.errors()]
    root.setProperty("buttonX", offset.x())
    root.setProperty("buttonY", offset.y())
    widget.setContent(url, component, root)
    button = root.findChild(QObject, "nativeTimerButton")
    assert button is not None
    # Construct and show the complete child view before exposing its parent.
    widget.show()
    return host, widget, button, probe


@pytest.mark.parametrize(
    ("offset", "outside", "initially_over_button"),
    [
        pytest.param(QPoint(40, 40), QPoint(20, 160), True, id="left-from-button"),
        pytest.param(QPoint(120, 80), QPoint(380, 160), False, id="right-from-outside"),
    ],
)
@pytest.mark.parametrize("root_control", [False, True], ids=["page", "root"])
def test_timer_pointer_area_enters_and_exits_native_cursor_state(
    request, offset: QPoint, outside: QPoint, initially_over_button: bool, root_control: bool,
) -> None:
    host, widget, button, probe = _timer_pointer_host(request, offset, root_control=root_control)
    crossings = []
    surface_leaves = []

    class PointerTrace(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt override
            kind = event.type()
            if kind in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseMove):
                position = (
                    event.globalPosition().toPoint() if hasattr(event, "globalPosition") else None
                )
                crossings.append(("host" if watched is host else "view", kind.name, position))
                if watched is widget and kind == QEvent.Type.Leave:
                    surface_leaves.append(kind)
            return False

    trace = PointerTrace(host)
    host.installEventFilter(trace)
    widget.installEventFilter(trace)

    def diagnostic() -> str:
        native_host = widget if widget.windowHandle() is not None else host
        window = native_host.windowHandle()
        return (
            f"timer pointer crossing: entered={probe.entered}, exited={probe.exited}, "
            f"cursor={QCursor.pos()}, offset={offset}, outside={outside}, "
            f"view={widget.geometry()}, native_host={type(native_host).__name__}, "
            f"exposed={window.isExposed() if window else None}, "
            f"active={host.isActiveWindow()}, crossings={crossings}"
        )

    show_and_activate(host)
    wait_for_geometry(widget, size=QSize(160, 48) if root_control else QSize(400, 200))
    native_window = widget.windowHandle()
    if native_window is not None:
        assert QTest.qWaitForWindowExposed(native_window, 3000), diagnostic()
    if initially_over_button:
        mouse_move(
            widget, QPoint(12, 12) if root_control else offset + QPoint(12, 12), sync_cursor=True,
        )

    # Establish delivery with the control disabled. This destination differs
    # from either ambient cursor position; same-position warps generate no input.
    surface_inside = QPoint(80, 24) if root_control else outside
    mouse_move(widget, surface_inside, sync_cursor=True)
    if root_control:
        # A root control has no neutral area inside its view. Leave it physically
        # and observe the native exit before enabling the control for qualification.
        previous_leaves = len(surface_leaves)
        mouse_move(host, outside, sync_cursor=True)
        wait_until(lambda: len(surface_leaves) > previous_leaves, description=diagnostic)
    assert (probe.entered, probe.exited) == (0, 0)
    button.setProperty("enabled", True)
    assert (probe.entered, probe.exited) == (0, 0)

    # The 30 ms observation starts after delivery, matching the control contract.
    # Preparation above never counts a native-view crossing as a button entry.
    mouse_move(widget, QPoint(12, 12) if root_control else offset + QPoint(12, 12), sync_cursor=True)
    QTest.qWait(30)
    assert (probe.entered, probe.exited) == (1, 0), diagnostic()

    button.setProperty("enabled", False)
    QTest.qWait(30)
    assert (probe.entered, probe.exited) == (1, 1), diagnostic()
    mouse_move(host if root_control else widget, outside, sync_cursor=True)
    QTest.qWait(30)
    assert (probe.entered, probe.exited) == (1, 1), diagnostic()


def test_timer_icon_only_button_centers_its_visible_content() -> None:
    widget = QQuickWidget()
    widget.resize(48, 34)
    configure_qml_host(
        widget,
        type_name="TimerButton",
        clear_color="#000000",
        image_providers={"timericons": TimerIconProvider()},
    )
    root = widget.rootObject()
    assert root is not None
    root.setProperty("iconName", "settings")
    root.setProperty("iconSize", 16)
    widget.show()
    QTest.qWait(30)

    icon = _find_visual(root, "timerButtonIcon")
    content = _find_visual(root, "timerButtonContent")
    assert icon is not None
    assert content is not None
    icon_origin = icon.mapToItem(root, QPointF())
    assert content.property("spacing") == 0
    assert icon_origin.x() + icon.property("width") / 2 == pytest.approx(
        root.width() / 2
    )
    assert widget.errors() == []
    widget.deleteLater()


def test_timer_navigation_tooltip_uses_overlay_and_fits_below_header() -> None:
    for existing_window in QApplication.topLevelWidgets():
        existing_window.close()
    _APP.processEvents()

    timer = _AdvancedTimerProbe()
    widget = QQuickWidget()
    widget.resize(640, 500)
    configure_qml_host(
        widget,
        type_name="AdvancedTimerPage",
        clear_color="#000000",
        context_properties={"timer": timer},
        image_providers={"timericons": TimerIconProvider()},
        mouse_tracking=True,
    )
    root = widget.rootObject()
    assert root is not None
    widget.show()
    widget.raise_()
    widget.activateWindow()
    widget.setFocus()
    QTest.qWait(40)

    button = _find_visual(root, "timerPreviousWeekButtonCompact")
    viewport = _find_visual(root, "advancedTimerViewport")
    assert button is not None
    assert viewport is not None
    tooltip = button.findChild(QObject, "timerButtonTooltip")
    assert tooltip is not None
    tooltip.setProperty("delay", 0)
    hover_point = button.mapToScene(
        QPointF(button.width() / 2, button.height() / 2)
    ).toPoint()
    QTest.mouseMove(widget, QPoint(widget.width() - 2, widget.height() - 2))
    QTest.qWait(20)
    QTest.mouseMove(widget, hover_point)
    QTest.qWait(60)
    assert tooltip.property("visible") is True
    tooltip_content = tooltip.property("contentItem")
    assert tooltip_content is not None
    popup_item = tooltip_content.parentItem()
    assert popup_item is not None
    ancestor = popup_item.parentItem()
    while ancestor is not None:
        assert ancestor is not viewport
        ancestor = ancestor.parentItem()
    popup_origin = popup_item.mapToScene(QPointF())
    assert popup_origin.y() >= 0
    assert popup_origin.y() + popup_item.height() <= root.height()
    assert popup_origin.y() >= button.mapToScene(QPointF()).y() + button.height()
    assert widget.errors() == []
    widget.deleteLater()


def test_shared_playlist_tree_requires_explicit_play_when_protection_is_enabled(
    playlist_tree_host,
) -> None:
    node = _playlist_media_node("media-1", "Protected media")
    widget, controller, _model, protection = playlist_tree_host([node], height=180)
    try:
        show_and_activate(widget)
        root = widget.rootObject()
        assert root is not None
        protection.set_enabled(True)

        play_button = _find_visual(root, "protectedPlayButton-media-1")
        card_hit_area = _find_visual(root, "mediaCardHitArea-media-1")
        root_playlist = _find_visual(root, "rootPlaylist")
        assert play_button is not None
        assert card_hit_area is not None
        assert root_playlist is not None
        wait_until(
            lambda: play_button.property("visible"), description="playback protection enabled"
        )
        assert play_button.property("visible") is True
        assert play_button.property("enabled") is True
        assert card_hit_area.property("cursorShape") == Qt.CursorShape.ArrowCursor

        def card_center():
            return card_hit_area.mapToScene(
                QPointF(card_hit_area.width() / 2, card_hit_area.height() / 2)
            ).toPoint()

        def clipping_chain():
            center = QPointF(card_center())
            ancestor = card_hit_area.parentItem()
            while ancestor is not None:
                if ancestor.clip():
                    yield ancestor, ancestor.mapFromScene(center)
                ancestor = ancestor.parentItem()

        def card_ready():
            # The list animates its clipping height independently of card layout.
            return (
                card_hit_area.isVisible()
                and card_hit_area.isEnabled()
                and card_hit_area.width() > 0
                and card_hit_area.height() > 0
                and root_playlist.height() >= card_hit_area.height()
                and root_playlist.height() == root_playlist.implicitHeight()
                and widget.rect().contains(card_center())
                and all(ancestor.contains(point) for ancestor, point in clipping_chain())
            )

        def wait_for_card():
            wait_until(
                card_ready,
                description=lambda: (
                    f"visible card center {card_center()}: "
                    f"playlist height={root_playlist.height()}, "
                    f"implicitHeight={root_playlist.implicitHeight()}, "
                    f"card size={(card_hit_area.width(), card_hit_area.height())}, "
                    f"clipping={[(ancestor.objectName(), ancestor.size(), point) for ancestor, point in clipping_chain()]}"
                ),
            )

        wait_for_card()
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=card_center())
        assert controller.projected == []
        assert QMetaObject.invokeMethod(play_button, "clicked")
        assert controller.projected == ["media-1"]

        protection.set_locked(True)
        wait_until(lambda: not play_button.property("enabled"), description="protected play lock")
        assert play_button.property("enabled") is False

        protection.set_locked(False)
        protection.set_enabled(False)
        wait_until(
            lambda: not play_button.property("visible"), description="playback protection disabled"
        )
        assert play_button.property("visible") is False
        assert card_hit_area.property("cursorShape") == Qt.CursorShape.PointingHandCursor
        wait_for_card()
        mouse_click(widget, Qt.MouseButton.LeftButton, pos=card_center())
        wait_until(
            lambda: controller.projected == ["media-1", "media-1"],
            description="unprotected card playback",
        )
        assert controller.projected == ["media-1", "media-1"]
    finally:
        dispose_widget(widget)


def test_playlist_tree_media_menu_forwards_add_to_destination(playlist_tree_host) -> None:
    node = _playlist_media_node("media-1", "Destination media")
    widget, controller, _model, _protection = playlist_tree_host(
        [node],
        height=180,
    )
    try:
        root = widget.rootObject()
        assert root is not None
        action = root.findChild(QObject, "mediaItemAddToDestinationAction")
        assert action is not None
        assert action.property("enabled") is True

        assert QMetaObject.invokeMethod(action, "triggered")
        assert controller.destination_requests == ["media-1"]
    finally:
        _dispose_qml_host(widget)


def test_playlist_tree_media_menu_forwards_set_as_idle(playlist_tree_host) -> None:
    node = _playlist_media_node("media-1", "Idle media")
    widget, controller, _model, _protection = playlist_tree_host(
        [node],
        height=180,
    )
    menu = None
    try:
        root = widget.rootObject()
        assert root is not None
        action = root.findChild(QObject, "mediaItemSetAsIdleAction")
        menu = root.findChild(QObject, "mediaItemMenu")
        assert action is not None
        assert menu is not None
        assert QMetaObject.invokeMethod(menu, "open")
        QTest.qWait(10)
        assert action.property("visible") is True

        assert QMetaObject.invokeMethod(action, "triggered")
        assert controller.idle_requests == ["media-1"]
    finally:
        _dispose_qml_host(widget, menu)


def test_playlist_tree_media_menu_omits_ineligible_idle_action(playlist_tree_host) -> None:
    node = {
        **_playlist_media_node("media-1", "Remote media"),
        "canSetAsIdle": False,
    }
    widget, _controller, _model, _protection = playlist_tree_host(
        [node],
        height=180,
    )
    menu = None
    try:
        root = widget.rootObject()
        assert root is not None
        menu = root.findChild(QObject, "mediaItemMenu")
        assert menu is not None
        assert QMetaObject.invokeMethod(menu, "open")
        QTest.qWait(10)

        assert root.findChild(QObject, "mediaItemSetAsIdleAction") is None
    finally:
        _dispose_qml_host(widget, menu)


def test_image_thumbnail_cursor_follows_playback_protection(playlist_tree_host) -> None:
    image = {
        **_playlist_media_node("image-1", "Protected image"),
        "mediaType": "image",
        "badge": "Image",
    }
    widget, _controller, _model, protection = playlist_tree_host(
        [image],
        height=180,
    )
    root = widget.rootObject()
    assert root is not None
    image_card = _find_visual(root, "mediaCard-image-1")
    assert image_card is not None
    framing_area = _find_visual(image_card, "imageFramingInteractionArea")
    assert framing_area is not None

    assert (
        framing_area.property("cursorShape")
        == Qt.CursorShape.PointingHandCursor
    )
    protection.set_enabled(True)
    QTest.qWait(10)
    assert framing_area.property("cursorShape") == Qt.CursorShape.ArrowCursor

    protection.set_enabled(False)
    QTest.qWait(10)
    assert (
        framing_area.property("cursorShape")
        == Qt.CursorShape.PointingHandCursor
    )


def test_playlist_tree_forwards_nested_hover_to_the_native_cursor(playlist_tree_host) -> None:
    image = {
        **_playlist_media_node("image-1", "Framed image"),
        "mediaType": "image",
        "badge": "Image",
    }
    widget, controller, _model, _protection = playlist_tree_host(
        [image],
        height=180,
    )
    root = widget.rootObject()
    assert root is not None
    # Exercise QML cursor signals without competing native pointer delivery.
    # Process the host's hide events before connecting the manual callbacks.
    widget.hide()
    _APP.processEvents()
    controller.pointerEntered.connect(lambda: begin_qml_pointer_cursor(widget))
    controller.pointerCursorEntered.connect(
        lambda _source, shape: set_qml_pointer_cursor(widget, shape)
    )
    controller.pointerCursorChanged.connect(
        lambda _source, shape: set_qml_pointer_cursor(widget, shape)
    )
    controller.pointerCursorExited.connect(
        lambda _source: end_qml_pointer_cursor(widget)
    )
    controller.pointerExited.connect(
        lambda: end_qml_pointer_cursor(widget)
    )

    drag_area = _find_visual(root, "dragMouse-image-1")
    image_card = _find_visual(root, "mediaCard-image-1")
    assert drag_area is not None
    assert image_card is not None
    framing_thumb = _find_visual(image_card, "imageFramingThumbnail")
    assert framing_thumb is not None

    drag_area.entered.emit()
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.OpenHandCursor, (
        f"visible={widget.isVisible()}, pointer={QCursor.pos()}"
    )
    assert (
        widget.quickWindow().cursor().shape()
        == Qt.CursorShape.OpenHandCursor
    )

    image_card.setProperty("dragStarted", True)
    _APP.processEvents()
    assert drag_area.property("cursorShape") == Qt.CursorShape.ClosedHandCursor
    controller.pointerCursorChange(
        "tree-drag:image-1",
        Qt.CursorShape.ClosedHandCursor.value,
    )
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert widget.quickWindow().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    image_card.setProperty("dragStarted", False)
    _APP.processEvents()
    assert drag_area.property("cursorShape") == Qt.CursorShape.OpenHandCursor
    controller.pointerCursorChange(
        "tree-drag:image-1",
        Qt.CursorShape.OpenHandCursor.value,
    )
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.OpenHandCursor

    drag_area.exited.emit()
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert widget.quickWindow().cursor().shape() == Qt.CursorShape.ArrowCursor

    framing_thumb.pointerEntered.emit(Qt.CursorShape.PointingHandCursor.value)
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert (
        widget.quickWindow().cursor().shape()
        == Qt.CursorShape.PointingHandCursor
    )

    framing_thumb.pointerCursorExited.emit()
    _APP.processEvents()
    assert widget.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert widget.quickWindow().cursor().shape() == Qt.CursorShape.ArrowCursor
    controller.pointerEntered.disconnect()
    controller.pointerCursorEntered.disconnect()
    controller.pointerCursorChanged.disconnect()
    controller.pointerCursorExited.disconnect()
    controller.pointerExited.disconnect()


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


def test_image_framing_thumbnail_handles_click_zoom_pan_and_reset(request) -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
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
    show_and_activate(widget)
    for _attempt in range(20):
        if root.property("imageReady"):
            break
        QTest.qWait(20)
    assert root.property("imageReady") is True
    assert root.property("sourceWidth") == pytest.approx(9 / 16)
    assert root.property("sourceHeight") == 1.0
    interaction_area = root.findChild(QObject, "imageFramingInteractionArea")
    assert interaction_area is not None
    assert (
        interaction_area.property("cursorShape")
        == Qt.CursorShape.PointingHandCursor
    )

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
    assert (
        interaction_area.property("cursorShape")
        == Qt.CursorShape.PointingHandCursor
    )

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
    assert interaction_area.property("cursorShape") == Qt.CursorShape.OpenHandCursor

    zoom_before_pan = float(root.property("framingZoom"))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(50, 80), delay=5)
    assert root.property("panning") is True
    assert interaction_area.property("cursorShape") == Qt.CursorShape.ClosedHandCursor
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 80))
    assert clicked.count() == 1
    assert edited.count() > 1
    assert edited.at(edited.count() - 1)[5] is False
    assert root.property("framingZoom") == pytest.approx(zoom_before_pan)
    assert root.property("panning") is False
    assert interaction_area.property("cursorShape") == Qt.CursorShape.OpenHandCursor

    cover_zoom = float(root.coverZoom())
    for _step in range(20):
        if float(root.property("framingZoom")) >= cover_zoom:
            break
        _send_thumbnail_wheel(widget, Qt.KeyboardModifier.ControlModifier)
    assert root.property("framingZoom") == pytest.approx(cover_zoom)
    assert root.maxPanX() == pytest.approx(0.0, abs=1e-9)

    reset_area = root.findChild(QObject, "imageFramingResetArea")
    assert reset_area is not None
    assert reset_area.property("cursorShape") == Qt.CursorShape.PointingHandCursor
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


def test_image_framing_thumbnail_projection_frame_covers_fixed_viewport(request) -> None:
    landscape = QPixmap(160, 90)
    landscape.fill(QColor("red"))
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
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
    show_and_activate(widget)
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


def test_image_framing_thumbnail_fits_portrait_in_fixed_viewport(request) -> None:
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    widget = QQuickWidget()
    request.addfinalizer(lambda: dispose_widget(widget))
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
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
    show_and_activate(widget)
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
