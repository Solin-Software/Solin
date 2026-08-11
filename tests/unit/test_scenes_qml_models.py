from __future__ import annotations

from PySide6.QtTest import QSignalSpy

from solin.ui.qml.scenes_models import SceneListModel


def _scene(scene_id: str, name: str) -> dict[str, object]:
    return {
        "id": scene_id,
        "name": name,
        "metadata": "",
        "default": False,
        "media": False,
        "live": False,
    }


def test_scene_model_moves_stable_rows_without_resetting_delegates() -> None:
    model = SceneListModel()
    model.replace_items([_scene("a", "A"), _scene("b", "B"), _scene("c", "C")])
    moved = QSignalSpy(model.rowsMoved)
    reset = QSignalSpy(model.modelReset)

    model.replace_items([_scene("b", "B"), _scene("c", "C"), _scene("a", "A")])

    assert moved.count() == 1
    assert reset.count() == 0
    assert [model.get(row)["id"] for row in range(model.rowCount())] == ["b", "c", "a"]


def test_scene_model_emits_role_changes_after_a_move() -> None:
    model = SceneListModel()
    model.replace_items([_scene("a", "A"), _scene("b", "B")])
    changed = QSignalSpy(model.dataChanged)

    model.replace_items([_scene("b", "Renamed"), _scene("a", "A")])

    assert changed.count() == 1
    assert model.get(0)["name"] == "Renamed"
