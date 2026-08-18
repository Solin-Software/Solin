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
from solin.ui.helpers import (
    begin_qml_pointer_cursor,
    end_qml_pointer_cursor,
    set_qml_pointer_cursor,
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
        self.moves: list[tuple[str, str, int, int]] = []
        self.collapsed: list[str] = []

    @Slot(str)
    def projectItem(self, item_id: str) -> None:  # noqa: N802 - QML API
        self.projected.append(item_id)

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
        "canRemove": True,
    }


def _playlist_tree_host(
    nodes: list[dict],
    *,
    height: int = 260,
) -> tuple[
    QQuickWidget,
    _PlaylistTreeControllerProbe,
    MediaTreeSource,
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


def test_playlist_tree_reconciles_complete_snapshots_without_recreating_host() -> None:
    keep = _playlist_media_node("keep", "Keep")
    remove = _playlist_media_node("remove", "Remove")
    widget, _controller, model, _protection = _playlist_tree_host(
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
    widget.deleteLater()


def test_playlist_tree_accepts_node_ids_reserved_by_javascript_objects() -> None:
    nodes = [
        _playlist_media_node(node_id, node_id)
        for node_id in ("constructor", "__proto__", "toString", "safe-id")
    ]
    widget, _controller, _model, _protection = _playlist_tree_host(
        nodes,
        height=360,
    )
    root = widget.rootObject()
    assert root is not None

    for node in nodes:
        assert _find_visual(root, f"mediaCard-{node['id']}") is not None

    widget.deleteLater()


def test_playlist_tree_shows_a_retryable_state_after_snapshot_failure() -> None:
    node = _playlist_media_node("media-1", "Media")
    widget, _controller, model, _protection = _playlist_tree_host([node])
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
    widget.deleteLater()


def test_playlist_tree_preserves_card_identity_across_parent_snapshots() -> None:
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
    widget, _controller, model, _protection = _playlist_tree_host(
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
    widget.deleteLater()


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


def test_playlist_tree_preserves_nested_section_geometry() -> None:
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
    widget, _controller, _model, _protection = _playlist_tree_host(
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
    widget.deleteLater()


def test_playlist_tree_drag_keeps_placeholder_and_full_ghost_feedback() -> None:
    first = _playlist_media_node("first", "First media")
    second = _playlist_media_node("second", "Second media")
    widget, controller, _model, _protection = _playlist_tree_host(
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
    QTest.mouseMove(widget, moved, delay=10)
    QTest.qWait(20)

    assert card.parentItem() is overlay
    assert card.height() == pytest.approx(72)
    assert card.property("opacity") == pytest.approx(0.34)
    assert placeholder.property("visible") is True

    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=moved)
    QTest.qWait(10)
    assert placeholder.property("visible") is False
    assert controller.moves
    assert controller.moves[-1][-1] == 1
    assert widget.errors() == []
    widget.deleteLater()


def test_playlist_tree_accepts_consecutive_down_and_back_reorders() -> None:
    first = _playlist_media_node("first", "First media")
    second = _playlist_media_node("second", "Second media")
    widget, controller, _model, _protection = _playlist_tree_host(
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
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
        QTest.qWait(5)
        QTest.mouseMove(widget, QPoint(start.x() + 10, start.y() + 10), delay=10)
        destination = QPoint(start.x() + 20, start.y() + delta_y)
        QTest.mouseMove(widget, destination, delay=10)
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
    widget.deleteLater()


def test_playlist_section_collapse_retains_the_original_height_animation() -> None:
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
    widget, controller, _model, _protection = _playlist_tree_host(
        [section],
        height=260,
    )
    root = widget.rootObject()
    assert root is not None
    card = _find_visual(root, "sectionCard-animated")
    header = _find_visual(root, "sectionHeaderHitArea-animated")
    assert card is not None
    assert header is not None
    QTest.qWait(240)
    expanded_height = card.height()
    assert expanded_height > 100

    point = header.mapToScene(QPointF(header.width() / 2, header.height() / 2)).toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=point)
    QTest.qWait(60)
    animated_height = card.height()

    assert 48 < animated_height < expanded_height
    QTest.qWait(220)
    assert card.height() == pytest.approx(48)
    assert controller.collapsed == ["animated"]
    assert widget.errors() == []
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


def test_shared_playlist_tree_requires_explicit_play_when_protection_is_enabled() -> None:
    node = _playlist_media_node("media-1", "Protected media")
    widget, controller, _model, protection = _playlist_tree_host([node], height=180)
    try:
        root = widget.rootObject()
        assert root is not None
        protection.set_enabled(True)
        QTest.qWait(10)

        play_button = _find_visual(root, "protectedPlayButton-media-1")
        card_hit_area = _find_visual(root, "mediaCardHitArea-media-1")
        assert play_button is not None
        assert card_hit_area is not None
        assert play_button.property("visible") is True
        assert play_button.property("enabled") is True
        assert card_hit_area.property("cursorShape") == Qt.CursorShape.ArrowCursor

        card_center = card_hit_area.mapToScene(
            QPointF(card_hit_area.width() / 2, card_hit_area.height() / 2)
        ).toPoint()
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=card_center)
        assert controller.projected == []
        assert QMetaObject.invokeMethod(play_button, "clicked")
        assert controller.projected == ["media-1"]

        protection.set_locked(True)
        QTest.qWait(1)
        assert play_button.property("enabled") is False

        protection.set_locked(False)
        protection.set_enabled(False)
        QTest.qWait(1)
        assert play_button.property("visible") is False
        assert card_hit_area.property("cursorShape") == Qt.CursorShape.PointingHandCursor
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=card_center)
        assert controller.projected == ["media-1", "media-1"]
    finally:
        widget.deleteLater()


def test_image_thumbnail_cursor_follows_playback_protection() -> None:
    image = {
        **_playlist_media_node("image-1", "Protected image"),
        "mediaType": "image",
        "badge": "Image",
    }
    widget, _controller, _model, protection = _playlist_tree_host(
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
    widget.deleteLater()


def test_playlist_tree_forwards_nested_hover_to_the_native_cursor() -> None:
    image = {
        **_playlist_media_node("image-1", "Framed image"),
        "mediaType": "image",
        "badge": "Image",
    }
    widget, controller, _model, _protection = _playlist_tree_host(
        [image],
        height=180,
    )
    root = widget.rootObject()
    assert root is not None
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
    assert widget.cursor().shape() == Qt.CursorShape.OpenHandCursor
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
    widget.deleteLater()


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
    widget.deleteLater()


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
