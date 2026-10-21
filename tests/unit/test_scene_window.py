from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSize
from PySide6.QtGui import QImage
import pytest

from solin.core.scenes.engine import scene_window_channel
from solin.core.scenes.model import (
    BusId,
)
from solin.core.scenes.presets import CAMERA_SCENE_ID
from solin.widgets.scenes.control_popup import SceneControlPopup
from solin.widgets.scenes.scene_window import SceneWindowManager, scene_feed_size
from tests._qt import wait_until
from tests.unit.test_scene_control_popup import controller_factory  # noqa: F401


class _Feeds:
    """Stands in for the controller's engine command and records each request."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple] = []

    def __call__(self, descriptor, scene_ids, width, height, *, channel_id="scene-thumbnails"):
        self.calls.append((channel_id, descriptor, scene_ids, width, height))
        return self.ready

    def last(self, channel_id: str) -> tuple | None:
        return next((call for call in reversed(self.calls) if call[0] == channel_id), None)


class _Geometry:
    def __init__(self) -> None:
        self.saved: dict[str, bytes] = {}

    def scene_window_geometry(self, scene_id: str) -> bytes:
        return self.saved.get(scene_id, b"")

    def set_scene_window_geometry(self, scene_id: str, geometry: bytes) -> None:
        self.saved[scene_id] = geometry


@pytest.fixture
def windows(controller_factory):  # noqa: F811
    created: list[SceneWindowManager] = []

    def create(*, ready: bool = True, geometry: _Geometry | None = None):
        controller = controller_factory()
        feeds = _Feeds(ready=ready)
        controller.set_thumbnail_egress = feeds
        manager = SceneWindowManager(controller, geometry_store=geometry)
        created.append(manager)
        return controller, feeds, manager

    yield create
    for manager in created:
        manager.close_all()
    QCoreApplication.processEvents()


@pytest.mark.parametrize(
    ("available", "ratio", "expected"),
    [
        (QSize(800, 450), 1.0, (800, 450)),      # exact fit
        (QSize(800, 900), 1.0, (800, 450)),      # tall area: width-limited
        (QSize(1600, 450), 1.0, (800, 450)),     # wide area: height-limited
        (QSize(800, 450), 2.0, (1600, 900)),     # device pixels on a HiDPI screen
        (QSize(3000, 2000), 1.0, (1920, 1080)),  # never above the output resolution
        (QSize(40, 20), 1.0, (160, 90)),         # a usable minimum
        (QSize(801, 451), 1.0, (800, 450)),      # even sizes
    ],
)
def test_feed_size_fits_the_output_and_never_exceeds_it(available, ratio, expected) -> None:
    assert scene_feed_size(available, ratio, 1920, 1080) == expected


def test_one_window_per_scene_and_reopening_raises_it(windows) -> None:
    controller, _feeds, manager = windows()

    first = manager.show_scene(CAMERA_SCENE_ID)
    again = manager.show_scene(CAMERA_SCENE_ID)
    other_id = next(scene.id for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID)
    other = manager.show_scene(other_id)

    assert first is not None and first is again
    assert other is not None and other is not first
    assert set(manager.windows) == {CAMERA_SCENE_ID, other_id}
    assert manager.show_scene("no-such-scene") is None


def test_window_feeds_its_scene_on_its_own_channel_and_releases_it(windows) -> None:
    controller, feeds, manager = windows()
    channel = scene_window_channel(CAMERA_SCENE_ID)

    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None
    window.resize(900, 600)
    wait_until(
        lambda: feeds.last(channel) is not None and feeds.last(channel)[1] is not None,
        description="the window's feed request",
    )
    window._sync_feed()

    _channel, descriptor, scene_ids, width, height = feeds.last(channel)
    assert descriptor.channel_id == channel
    assert scene_ids == (CAMERA_SCENE_ID,)
    # One row the size the preview draws, at the output's proportions.
    assert (descriptor.width, descriptor.height) == (width, height)
    assert abs(width / height - 16 / 9) < 0.02
    # The panel's own card atlas is never touched by a window.
    assert all(call[0] == channel for call in feeds.calls)

    window.close()
    QCoreApplication.processEvents()
    assert feeds.last(channel)[1:] == (None, (), 0, 0)
    assert CAMERA_SCENE_ID not in manager.windows


def test_window_retries_its_feed_until_the_engine_hears_it(windows) -> None:
    controller, feeds, manager = windows(ready=False)
    channel = scene_window_channel(CAMERA_SCENE_ID)
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None
    QCoreApplication.processEvents()

    window._sync_feed()
    attempts = len([call for call in feeds.calls if call[0] == channel])
    assert attempts and window._feed_sent is None
    window._sync_feed()
    assert len([call for call in feeds.calls if call[0] == channel]) > attempts

    feeds.ready = True
    window._sync_feed()
    assert window._feed_sent is not None
    sent = len(feeds.calls)
    window._sync_feed()
    assert len(feeds.calls) == sent  # delivered once, not re-sent forever

    # A restarted engine forgets every feed: once it is ready again, send again.
    feeds.ready = False
    controller.engine_ready_changed.emit(False)
    assert window._feed_sent is None
    feeds.ready = True
    controller.engine_ready_changed.emit(True)
    assert window._feed_sent is not None


def test_window_paints_the_frames_of_its_own_scene_only(windows) -> None:
    _controller, _feeds, manager = windows()
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None
    image = QImage(160, 90, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)

    window._on_frame("another-scene", image)
    assert window.preview.image is None
    window._on_frame(CAMERA_SCENE_ID, image)
    assert window.preview.image is image

    window.resize(640, 420)
    QCoreApplication.processEvents()
    painted = window.preview.grab().toImage()
    centre = painted.pixelColor(painted.width() // 2, painted.height() // 2)
    assert (centre.red(), centre.green(), centre.blue()) == (0, 255, 0)


def test_window_says_when_its_scene_is_on_air(windows) -> None:
    controller, _feeds, manager = windows()
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None
    camera, projection = window._camera_tally, window._projection_tally
    # The virtual camera button only exists while that output runs, as on cards.
    assert not camera.isVisibleTo(window) and projection.isVisibleTo(window)

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    controller.select_scene(BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID)
    other = next(scene.id for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID)
    controller.select_scene(BusId.MEDIA_WINDOWS, other)
    QCoreApplication.processEvents()
    assert camera.isVisibleTo(window) and camera.property("live") is True
    assert camera.text() == "On air · virtual camera"
    assert projection.property("live") is False and projection.text() == "Projection"

    controller.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    QCoreApplication.processEvents()
    assert projection.property("live") is True

    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, False)
    QCoreApplication.processEvents()
    assert not camera.isVisibleTo(window)


def test_window_buttons_put_the_scene_on_each_output(windows) -> None:
    controller, _feeds, manager = windows()
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    other = next(scene.id for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID)
    controller.select_scene(BusId.VIRTUAL_CAMERA, other)
    controller.select_scene(BusId.MEDIA_WINDOWS, other)
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None

    window._projection_tally.click()
    QCoreApplication.processEvents()
    # Each output independently, exactly as a card's buttons route.
    assert controller.desired_scene(BusId.MEDIA_WINDOWS) == CAMERA_SCENE_ID
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == other
    assert window._projection_tally.property("live") is True

    window._camera_tally.click()
    QCoreApplication.processEvents()
    assert controller.desired_scene(BusId.VIRTUAL_CAMERA) == CAMERA_SCENE_ID


def test_window_follows_renames_and_closes_with_its_scene(windows) -> None:
    controller, _feeds, manager = windows()
    controller.documents.create_scene("Pulpit", scene_id="pulpit")
    window = manager.show_scene("pulpit")
    assert window is not None

    controller.documents.rename_scene("pulpit", "Pulpit close-up")
    QCoreApplication.processEvents()
    assert window._name.text() == "Pulpit close-up"
    assert "Pulpit close-up" in window.windowTitle()

    controller.documents.delete_scene("pulpit")
    QCoreApplication.processEvents()
    assert "pulpit" not in manager.windows


def test_window_remembers_where_it_was_per_scene(windows) -> None:
    geometry = _Geometry()
    _controller, _feeds, manager = windows(geometry=geometry)
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None
    window.resize(700, 480)
    window.close()
    QCoreApplication.processEvents()
    assert geometry.saved[CAMERA_SCENE_ID]

    reopened = manager.show_scene(CAMERA_SCENE_ID)
    assert reopened is not None and reopened is not window
    assert (reopened.width(), reopened.height()) == (700, 480)


def test_card_expand_button_opens_that_scenes_window(controller_factory) -> None:  # noqa: F811
    controller = controller_factory()
    controller.set_thumbnail_egress = _Feeds()
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    card = popup._scene_cards[CAMERA_SCENE_ID]

    card.expand_button.click()

    assert set(popup.scene_windows.windows) == {CAMERA_SCENE_ID}
    assert "Camera" in card.expand_button.accessibleName()
    popup.scene_windows.close_all()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_card_expand_button_stays_above_a_routing_button_covering_the_card(
    controller_factory,  # noqa: F811
) -> None:
    controller = controller_factory()
    controller.set_thumbnail_egress = _Feeds()
    popup = SceneControlPopup(controller)
    QCoreApplication.processEvents()
    card = popup._scene_cards[CAMERA_SCENE_ID]

    # One visible output: its routing button is laid over the whole card.
    card.set_routing(
        on_projection=False, on_program=False,
        projection_visible=True, program_available=False,
    )
    card.resize(card.size())
    card.show()
    QCoreApplication.processEvents()

    assert card._expanded_button is card._projection
    corner = card.expand_button.geometry().center()
    assert card.childAt(corner) is card.expand_button
    popup.scene_windows.close_all()
    popup.deleteLater()
    QCoreApplication.processEvents()


def test_scene_windows_close_when_the_application_quits(windows) -> None:
    _controller, feeds, manager = windows()
    window = manager.show_scene(CAMERA_SCENE_ID)
    assert window is not None

    QCoreApplication.instance().aboutToQuit.emit()
    QCoreApplication.processEvents()

    assert manager.windows == {}
    assert feeds.last(scene_window_channel(CAMERA_SCENE_ID))[1] is None


def test_scene_window_geometry_survives_a_restart_per_scene(tmp_path) -> None:
    from PySide6.QtCore import QSettings

    from solin.core.foundation.settings_store import ProfileAppSettingsStore

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    organization = f"SolinSceneWindow{tmp_path.name}"
    settings = ProfileAppSettingsStore.for_organization(organization)
    try:
        assert settings.scene_window_geometry("pulpit") == b""
        settings.set_scene_window_geometry("pulpit", b"\x01\x02geometry")
        # A scene id never escapes its own key, whatever it contains.
        settings.set_scene_window_geometry("a/b", b"other")

        restarted = ProfileAppSettingsStore.for_organization(organization)
        assert restarted.scene_window_geometry("pulpit") == b"\x01\x02geometry"
        assert restarted.scene_window_geometry("a/b") == b"other"
        assert restarted.scene_window_geometry("a") == b""
    finally:
        settings.settings.clear()
