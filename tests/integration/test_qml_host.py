from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Property, QObject, QPoint, QPointF, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QColor, QPixmap, QWheelEvent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from solin.controllers.timer_engine import TimerEngine
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


class _PlaylistTreeControllerProbe(QObject):
    stateChanged = Signal()
    mediaChanged = Signal(str, str, str, str)
    imageFramingChanged = Signal(str, object)
    mediaInserted = Signal(str, int, object)
    nodesInserted = Signal(str, int, object)
    nodeReplaced = Signal(str, object)
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


def _visible_texts(item, *, parent_visible: bool = True) -> list[str]:
    visible = parent_visible and bool(item.property("visible"))
    texts: list[str] = []
    text = item.property("text")
    if visible and isinstance(text, str) and text:
        texts.append(text)
    for child in item.childItems():
        texts.extend(_visible_texts(child, parent_visible=visible))
    return texts


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
    card_hit_area = media_card.findChild(QObject, "mediaCardHitArea")
    assert play_button is not None
    assert download_button is not None
    assert more_button is not None
    assert card_hit_area is not None
    assert play_button.property("visible") is True
    assert play_button.property("enabled") is True
    assert download_button.property("x") < play_button.property("x")
    assert play_button.property("x") < more_button.property("x")
    assert card_hit_area.property("cursorShape") == Qt.CursorShape.ArrowCursor

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
    media_card.clicked.emit()
    assert controller.projected == ["media-1", "media-1"]


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

    clicked = QSignalSpy(root.clicked)
    edited = QSignalSpy(root.framingEdited)
    reset = QSignalSpy(root.framingReset)

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(50, 28))
    QTest.mouseMove(widget, QPoint(53, 28), delay=5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(53, 28))
    assert clicked.count() == 1
    assert edited.count() == 0

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
