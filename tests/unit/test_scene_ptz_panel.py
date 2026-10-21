from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QImage, QKeyEvent
import pytest

from solin.controllers.scene_runtime_controller import SceneRuntimeController
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.scenes.model import (
    DEFAULT_CAMERA_SOURCE_ID,
    BusId,
    CameraPreset,
    LocalCameraConfig,
    OnvifPtzBinding,
    SceneLayer,
    SourceDefinition,
    SourceKind,
    ViscaIpPtzBinding,
)
from solin.core.scenes.presets import CAMERA_SCENE_ID, SceneSeedNames
from solin.core.scenes.ptz import (
    PtzControlKind,
    PtzControlResult,
    PtzRecallResult,
    PtzRecallStatus,
)
from solin.widgets.scenes.scene_window import SceneWindowManager
from tests.unit.test_scene_control_popup import _Projection


class _Ptz:
    """A PTZ executor whose commands the test completes by hand."""

    def __init__(self) -> None:
        self.moves: list[tuple[str, float, float, float]] = []
        self.stops: list[str] = []
        self.recalls: list[tuple[str, Future[PtzRecallResult]]] = []
        self.stores: list[tuple[CameraPreset, Future[PtzControlResult]]] = []
        self.creates: list[tuple[str, Future[PtzControlResult]]] = []

    @staticmethod
    def _done(camera_source_id: str, kind: PtzControlKind) -> Future[PtzControlResult]:
        future: Future[PtzControlResult] = Future()
        future.set_result(PtzControlResult(kind.value, camera_source_id, kind, PtzRecallStatus.SUCCEEDED))
        return future

    def move(self, *, camera_source_id, binding, pan=0.0, tilt=0.0, zoom=0.0, **_timing):
        self.moves.append((camera_source_id, pan, tilt, zoom))
        return self._done(camera_source_id, PtzControlKind.MOVE)

    def stop(self, *, camera_source_id, binding, **_timing):
        self.stops.append(camera_source_id)
        return self._done(camera_source_id, PtzControlKind.STOP)

    def recall(self, *, camera_source_id, binding, preset, timeout_ms):
        future: Future[PtzRecallResult] = Future()
        self.recalls.append((preset.id, future))
        return future

    def store_preset(self, *, camera_source_id, binding, preset, timeout_ms=3000):
        future: Future[PtzControlResult] = Future()
        self.stores.append((preset, future))
        return future

    def create_preset(self, *, camera_source_id, binding, name, timeout_ms=3000):
        future: Future[PtzControlResult] = Future()
        self.creates.append((name, future))
        return future

    def cancel(self, future) -> None:
        pass

    def close(self) -> None:
        pass


def _recalled(preset_id: str, status=PtzRecallStatus.SUCCEEDED, error_code="") -> PtzRecallResult:
    return PtzRecallResult("recall", DEFAULT_CAMERA_SOURCE_ID, preset_id, status, error_code)


def _stored(token: str = "", status=PtzRecallStatus.SUCCEEDED, error_code="") -> PtzControlResult:
    return PtzControlResult(
        "store", DEFAULT_CAMERA_SOURCE_ID, PtzControlKind.STORE_PRESET, status, error_code,
        preset_token=token,
    )


class _Confirm:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, _parent, _title: str, text: str) -> bool:
        self.asked.append(text)
        return self.answer


@pytest.fixture
def setup(request, tmp_path: Path, scene_workspace_factory):
    def create(*, binding=None, confirm: _Confirm | None = None):
        paths = ProfilePaths.from_roots(
            data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", profile_id="ptz-panel-test",
        )
        paths.ensure_dirs()
        workspace = scene_workspace_factory(paths, seed_names=SceneSeedNames(
            content_source="Current content", default_camera_source="Default camera",
            no_signal_source="No signal background", content_scene="Content",
            camera_scene="Camera", content_camera_pip_scene="Content and camera",
            no_signal_scene="No signal", content_layer="Content", camera_layer="Camera",
            background_layer="Background",
        ))
        camera = SourceDefinition(
            id=DEFAULT_CAMERA_SOURCE_ID, kind=SourceKind.LOCAL_CAMERA, name="Stage",
            configuration=LocalCameraConfig(
                ptz_binding=binding or OnvifPtzBinding(
                    endpoint="http://camera.test/onvif/ptz_service", profile_token="main",
                ),
            ),
        )
        workspace.upsert_camera(camera, add_to_active_document=True)
        if not any(
            layer.source_id == DEFAULT_CAMERA_SOURCE_ID
            for layer in workspace.documents.document.scene(CAMERA_SCENE_ID).layers
        ):
            workspace.documents.add_layer(CAMERA_SCENE_ID, SceneLayer(
                id="stage-layer", source_id=DEFAULT_CAMERA_SOURCE_ID, name="Stage",
            ))
        ptz = _Ptz()
        controller = SceneRuntimeController(workspace, _Projection(), ptz=ptz)
        # Close, then let the conftest's DeferredDelete pass destroy it on the main
        # thread rather than cyclic GC on a later test's worker thread.
        request.addfinalizer(controller.deleteLater)
        request.addfinalizer(controller.close)
        controller.set_thumbnail_egress = lambda *args, **kwargs: True
        confirm = confirm or _Confirm()
        manager = SceneWindowManager(controller, confirm=confirm)
        request.addfinalizer(manager.close_all)
        return controller, ptz, manager, confirm

    return create


def _preset(controller, preset_id: str, name: str, token: str = "1") -> CameraPreset:
    preset = CameraPreset(id=preset_id, camera_source_id=DEFAULT_CAMERA_SOURCE_ID, name=name, remote_token=token)
    controller.workspace.create_camera_preset(preset)
    return preset


def _process() -> None:
    for _ in range(3):
        QCoreApplication.processEvents()


def _key(window, key: Qt.Key, *, release: bool = False, auto_repeat: bool = False) -> None:
    event = QKeyEvent(
        QEvent.Type.KeyRelease if release else QEvent.Type.KeyPress,
        key, Qt.KeyboardModifier.NoModifier, "", auto_repeat,
    )
    QCoreApplication.sendEvent(window, event)


def _second_ptz_camera(controller) -> None:
    controller.workspace.upsert_camera(SourceDefinition(
        id="audience", kind=SourceKind.LOCAL_CAMERA, name="Audience",
        configuration=LocalCameraConfig(
            device_id="audience-device",
            ptz_binding=ViscaIpPtzBinding(host="192.0.2.9"),
        ),
    ), add_to_active_document=True)
    controller.documents.add_layer(CAMERA_SCENE_ID, SceneLayer(
        id="audience-layer", source_id="audience", name="Audience",
    ))


def test_panel_shows_only_for_scenes_with_a_ptz_camera(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    other = next(
        scene.id for scene in controller.document.scenes
        if not any(layer.source_id == DEFAULT_CAMERA_SOURCE_ID for layer in scene.layers)
    )

    with_ptz = manager.show_scene(CAMERA_SCENE_ID)
    without = manager.show_scene(other)

    assert with_ptz is not None and with_ptz.ptz_panel.isVisibleTo(with_ptz)
    assert with_ptz.ptz_panel.camera_ids == (DEFAULT_CAMERA_SOURCE_ID,)
    assert without is not None and not without.ptz_panel.isVisibleTo(without)


def test_holding_a_direction_moves_until_release(setup) -> None:
    _controller, ptz, manager, _confirm = setup()
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel

    panel._pad_buttons["left"].pressed.emit()
    panel._send_motion()  # what the repeat timer does while held
    panel._pad_buttons["left"].released.emit()

    # The direction scaled by the camera's speed (5 of 10 by default).
    assert ptz.moves == [(DEFAULT_CAMERA_SOURCE_ID, -0.5, 0.0, 0.0)] * 2
    assert ptz.stops == [DEFAULT_CAMERA_SOURCE_ID]
    assert not panel._repeat.isActive()


def test_window_keys_move_zoom_stop_and_recall(setup) -> None:
    controller, ptz, manager, _confirm = setup()
    first = _preset(controller, "wide", "Wide", "1")
    _preset(controller, "pulpit", "Pulpit", "2")
    window = manager.show_scene(CAMERA_SCENE_ID)
    _process()

    _key(window, Qt.Key.Key_Up)
    _key(window, Qt.Key.Key_Up, auto_repeat=True)  # held: no new move per repeat
    _key(window, Qt.Key.Key_Up, release=True)
    _key(window, Qt.Key.Key_Plus)
    _key(window, Qt.Key.Key_Space)
    _key(window, Qt.Key.Key_2)
    _key(window, Qt.Key.Key_9)  # no ninth preset: nothing

    assert ptz.moves == [
        (DEFAULT_CAMERA_SOURCE_ID, 0.0, 0.5, 0.0),
        (DEFAULT_CAMERA_SOURCE_ID, 0.0, 0.0, 0.5),
    ]
    assert ptz.stops == [DEFAULT_CAMERA_SOURCE_ID, DEFAULT_CAMERA_SOURCE_ID]
    assert [preset_id for preset_id, _future in ptz.recalls] == ["pulpit"]
    assert first.id != "pulpit"


def test_tab_switches_camera_and_each_camera_keeps_its_speed(setup) -> None:
    controller, ptz, manager, _confirm = setup()
    _second_ptz_camera(controller)
    window = manager.show_scene(CAMERA_SCENE_ID)
    panel = window.ptz_panel
    _process()
    assert panel.camera_ids == (DEFAULT_CAMERA_SOURCE_ID, "audience")

    panel._speed.setValue(9)
    window.focusNextPrevChild(True)
    assert panel.camera_id == "audience"
    assert panel._speed.value() == 5  # its own default
    window.focusNextPrevChild(False)
    assert panel.camera_id == DEFAULT_CAMERA_SOURCE_ID
    assert panel._speed.value() == 9

    # Speeds follow the camera into any other window that shows it.
    other = next(scene.id for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID)
    controller.documents.add_layer(other, SceneLayer(
        id="stage-elsewhere", source_id=DEFAULT_CAMERA_SOURCE_ID, name="Stage",
    ))
    assert manager.show_scene(other).ptz_panel._speed.value() == 9


def test_recall_feedback_moves_then_confirms_or_explains(setup) -> None:
    controller, ptz, manager, _confirm = setup()
    _preset(controller, "wide", "Wide", "1")
    _preset(controller, "pulpit", "Pulpit", "2")
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.recall_preset("wide")
    assert panel.preset_feedback("wide") == ("moving", "Moving…")
    ptz.recalls[-1][1].set_result(_recalled("wide"))
    _process()
    assert panel.preset_feedback("wide") == ("done", "✓")

    panel.recall_preset("pulpit")
    ptz.recalls[-1][1].set_result(
        _recalled("pulpit", PtzRecallStatus.TIMED_OUT, "ptz_recall_timeout")
    )
    _process()
    assert panel.preset_feedback("pulpit") == ("error", "Timed out")
    assert "did not finish moving" in panel.status_text


def test_recalling_on_a_live_camera_asks_every_time(setup) -> None:
    confirm = _Confirm(answer=False)
    controller, ptz, manager, _confirm = setup(confirm=confirm)
    _preset(controller, "wide", "Wide", "1")
    controller.set_output_enabled(BusId.VIRTUAL_CAMERA, True)
    controller.select_scene(BusId.VIRTUAL_CAMERA, CAMERA_SCENE_ID)
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.recall_preset("wide")
    assert ptz.recalls == [] and len(confirm.asked) == 1
    assert "virtual camera" in confirm.asked[0]

    confirm.answer = True
    panel.recall_preset("wide")
    panel.recall_preset("wide")
    assert len(ptz.recalls) == 2 and len(confirm.asked) == 3


def test_manual_moves_on_a_live_camera_ask_once_until_it_leaves_the_air(setup) -> None:
    confirm = _Confirm(answer=True)
    controller, ptz, manager, _confirm = setup(confirm=confirm)
    other = next(
        scene.id for scene in controller.document.scenes
        if not any(layer.source_id == DEFAULT_CAMERA_SOURCE_ID for layer in scene.layers)
    )
    # Live through *another* scene that nests this camera's scene still counts.
    controller.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.start_motion(1.0, 0.0, 0.0)
    panel.stop_motion()
    panel.start_motion(-1.0, 0.0, 0.0)
    panel.stop_motion()
    assert len(confirm.asked) == 1 and len(ptz.moves) == 2
    assert "projection" in confirm.asked[0]
    assert "unlocked" in panel._live.text()

    controller.select_scene(BusId.MEDIA_WINDOWS, other)
    _process()
    controller.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    _process()
    panel.start_motion(1.0, 0.0, 0.0)
    assert len(confirm.asked) == 2  # off air re-locked it


def test_declined_live_move_does_not_move(setup) -> None:
    confirm = _Confirm(answer=False)
    controller, ptz, manager, _confirm = setup(confirm=confirm)
    controller.select_scene(BusId.MEDIA_WINDOWS, CAMERA_SCENE_ID)
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.start_motion(0.0, 1.0, 0.0)

    assert ptz.moves == [] and len(confirm.asked) == 1


def test_save_current_adds_the_preset_only_once_the_camera_stored_it(setup) -> None:
    controller, ptz, manager, _confirm = setup()
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.save_current_position()
    name, future = ptz.creates[-1]  # ONVIF: the camera creates it and picks the token
    assert name == "Preset 1" and ptz.stores == []
    assert panel.presets() == ()  # not before the camera confirms
    future.set_result(_stored(token="cam-17"))
    _process()

    (saved,) = panel.presets()
    assert (saved.name, saved.remote_token) == ("Preset 1", "cam-17")
    assert "Saved" in panel.status_text

    panel.save_current_position()
    ptz.creates[-1][1].set_result(_stored(status=PtzRecallStatus.FAILED, error_code="ptz_network_unavailable"))
    _process()
    assert len(panel.presets()) == 1  # a refused store leaves nothing behind
    assert "did not answer" in panel.status_text


def test_save_current_on_visca_takes_the_lowest_free_preset_number(setup) -> None:
    controller, ptz, manager, _confirm = setup(binding=ViscaIpPtzBinding(host="192.0.2.8"))
    _preset(controller, "one", "One", "1")
    _preset(controller, "three", "Three", "3")
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.save_current_position()
    preset, future = ptz.stores[-1]
    future.set_result(_stored(token=preset.remote_token))
    _process()

    assert preset.remote_token == "2"
    assert [item.remote_token for item in panel.presets()] == ["1", "3", "2"]
    assert panel.presets()[-1].name == "Preset 3"


def test_presets_can_be_renamed_and_deleted(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    _preset(controller, "wide", "Wide", "1")
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    assert panel.rename_preset("wide", "  Whole stage ")
    assert panel.presets()[0].name == "Whole stage"
    row = panel._preset_rows["wide"]
    # The visible label may be shortened to the tile's width (font metrics differ
    # per platform); the full name stays on the tile.
    assert row.accessibleName() == "1  Whole stage"
    assert row.label.toolTip() == "1  Whole stage"
    assert row.label.text().startswith("1  Wh")
    assert panel.delete_preset("wide")
    assert panel.presets() == ()


def test_also_in_lists_other_scenes_showing_the_camera(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    other = next(scene for scene in controller.document.scenes if scene.id != CAMERA_SCENE_ID)
    controller.documents.add_layer(other.id, SceneLayer(
        id="stage-elsewhere", source_id=DEFAULT_CAMERA_SOURCE_ID, name="Stage",
    ))
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    assert other.name in panel._also_in.text()


def test_closing_or_leaving_the_window_never_leaves_a_camera_moving(setup) -> None:
    _controller, ptz, manager, _confirm = setup()
    window = manager.show_scene(CAMERA_SCENE_ID)
    panel = window.ptz_panel

    panel.start_motion(1.0, 0.0, 0.0)
    QCoreApplication.sendEvent(window, QEvent(QEvent.Type.FocusOut))
    assert ptz.stops == [DEFAULT_CAMERA_SOURCE_ID]

    panel.start_motion(1.0, 0.0, 0.0)
    window.close()
    _process()
    assert ptz.stops == [DEFAULT_CAMERA_SOURCE_ID] * 2


def _green(width: int = 320, height: int = 180) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)
    return image


def _finish_capture(panel, preset_id: str, image: QImage | None) -> None:
    capture = panel._captures[preset_id]
    if image is not None:
        capture._on_frame(capture.camera_source_id, image)
    capture._finish()
    _process()


def test_a_saved_preset_gets_a_picture_from_its_camera(setup) -> None:
    controller, ptz, manager, _confirm = setup()
    requests: list[str] = []
    controller.set_thumbnail_egress = lambda *args, channel_id="", **kwargs: requests.append(channel_id) or True
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.save_current_position()
    ptz.creates[-1][1].set_result(_stored(token="cam-1"))
    _process()
    (preset,) = panel.presets()

    assert preset.id in panel._captures
    assert f"camera-still:{DEFAULT_CAMERA_SOURCE_ID}" in requests
    assert panel._preset_rows[preset.id].image.text() == "Taking picture…"

    _finish_capture(panel, preset.id, _green())

    stored = controller.workspace.preset_thumbnails.load(preset.id)
    assert stored and QImage.fromData(stored).pixelColor(5, 5).green() > 200
    assert not panel._preset_rows[preset.id].image.pixmap().isNull()
    assert preset.id not in panel._captures


def test_preset_pictures_load_when_the_window_opens_and_go_with_the_preset(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    _preset(controller, "wide", "Wide", "1")
    _preset(controller, "pulpit", "Pulpit", "2")
    store = controller.workspace.preset_thumbnails
    buffer_image = _green()
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    buffer_image.save(buffer, "JPG")
    assert store.save("wide", bytes(data.data()))
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    assert not panel._preset_rows["wide"].image.pixmap().isNull()
    assert panel._preset_rows["pulpit"].image.text() == "No picture"

    assert panel.delete_preset("wide")
    assert store.load("wide") is None


def test_refreshing_a_picture_replaces_it_and_a_failed_one_says_so(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    _preset(controller, "wide", "Wide", "1")
    panel = manager.show_scene(CAMERA_SCENE_ID).ptz_panel
    _process()

    panel.capture_thumbnail("wide")
    _finish_capture(panel, "wide", None)
    assert "No picture of “Wide” could be taken" in panel.status_text
    assert controller.workspace.preset_thumbnails.load("wide") is None

    panel.capture_thumbnail("wide")
    _finish_capture(panel, "wide", _green())
    assert controller.workspace.preset_thumbnails.load("wide")


def test_closing_the_window_cancels_a_picture_in_progress(setup) -> None:
    controller, _ptz, manager, _confirm = setup()
    _preset(controller, "wide", "Wide", "1")
    requests: list[tuple[str, bool]] = []
    controller.set_thumbnail_egress = (
        lambda descriptor, *args, channel_id="", **kwargs: requests.append((channel_id, descriptor is not None)) or True
    )
    window = manager.show_scene(CAMERA_SCENE_ID)
    window.ptz_panel.capture_thumbnail("wide")

    window.close()
    _process()

    still = f"camera-still:{DEFAULT_CAMERA_SOURCE_ID}"
    assert (still, True) in requests and requests[-1] != (still, True)
    assert (still, False) in requests
