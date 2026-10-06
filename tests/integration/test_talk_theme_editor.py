from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

import pytest

from PySide6.QtCore import (
    Q_ARG,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    QSize,
    QTranslator,
    Qt,
    qInstallMessageHandler,
)
from PySide6.QtGui import QColor, QImage
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QColorDialog, QFileDialog, QPushButton
from PySide6.QtWidgets import QSpinBox, QStyle, QStyleOptionSpinBox

from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.resources import application_translation_root
from solin.core.projection.application import ProjectionSession
from solin.core.projection.result import ProjectionResult, ProjectionResultStatus
from solin.core.talk_theme.models import (
    MAX_BACKGROUND_BLUR,
    MAX_BACKGROUND_OVERLAY_OPACITY,
    MAX_USER_PRESETS,
    TalkThemeLibrary,
    TalkThemePreset,
)
from solin.core.talk_theme.presets import builtin_presets
from solin.core.talk_theme.repository import TalkThemeAssetStore, TalkThemeRepository
from solin.styles.theme import app_stylesheet
from solin.ui.dialogs.talk_theme_color import TalkThemeColorDialog
from solin.ui.qml.talk_theme import TalkThemeBridge, TalkThemeEditorWidget
from tests._qt import dispose_widget, mouse_move, mouse_press, mouse_release, show_and_activate, wait_until


_APP = QApplication.instance() or QApplication([])


class _Notifications:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def success(self, message: str) -> None:
        self.messages.append(("success", message))

    def information(self, message: str) -> None:
        self.messages.append(("information", message))

    def warning(self, message: str) -> None:
        self.messages.append(("warning", message))

    def error(self, message: str) -> None:
        self.messages.append(("error", message))


class _RepositorySpy:
    def __init__(self, path: Path) -> None:
        self._repository = TalkThemeRepository(path)
        self.saved: list[TalkThemeLibrary] = []

    def load(self) -> TalkThemeLibrary:
        return self._repository.load()

    def save(self, library: TalkThemeLibrary) -> None:
        self.saved.append(library)
        self._repository.save(library)


class _CustomColorSettings:
    def __init__(self, colors: tuple[str, ...] = ()) -> None:
        self.colors = colors
        self.writes: list[tuple[str, ...]] = []

    def custom_colors(self) -> tuple[str, ...]:
        return self.colors

    def set_custom_colors(self, colors: tuple[str, ...]) -> None:
        self.colors = colors
        self.writes.append(colors)


class _OutputSettings:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.writes: list[bool] = []

    def follow_output_aspect(self) -> bool:
        return self.enabled

    def set_follow_output_aspect(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.writes.append(self.enabled)


def _asset_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "src" / "solin" / "resources" / "assets"


def _bridge(
    tmp_path: Path,
    *,
    repository: _RepositorySpy | TalkThemeRepository | None = None,
    prompt_on_unsaved_changes: bool = False,
    settings: _CustomColorSettings | None = None,
    output_settings: _OutputSettings | None = None,
) -> TalkThemeBridge:
    session = ProjectionSession()
    return TalkThemeBridge(
        repository=repository or TalkThemeRepository(tmp_path / "talk_theme.json"),
        asset_store=TalkThemeAssetStore(tmp_path / "assets"),
        builtin_asset_dir=_asset_dir(),
        notifications=_Notifications(),
        projection_session=session,
        projection_windows=session.all_windows,
        settings=settings or _CustomColorSettings(),
        output_settings=output_settings or _OutputSettings(),
        prompt_on_unsaved_changes=prompt_on_unsaved_changes,
    )


def _layer_records(bridge: TalkThemeBridge) -> list[dict[str, object]]:
    return [bridge.layersModel.get(row) for row in range(bridge.layerCount)]


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        sleep(0.01)
    return False


def _active_text_editor(root: QQuickItem) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        pending.extend(item.childItems())
        if "TextEdit" in item.metaObject().className() and item.hasActiveFocus():
            return item
    return None


def _quick_item_by_name(root: QQuickItem, object_name: str) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        pending.extend(item.childItems())
        if item.objectName() == object_name:
            return item
    return None


def _profile_paths(tmp_path: Path) -> ProfilePaths:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    paths.ensure_dirs()
    return paths


def test_applying_preset_replaces_the_document_and_resets_history(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    title_id = bridge.layersModel.get(1)["id"]
    bridge.commitInlineText(str(title_id), "Unsaved title")
    bridge.addTextLayer()
    bridge.setBackgroundProperty("overlay_opacity", 0.61)
    bridge.undo()
    assert bridge.canRedo

    bridge.requestPreset("classic-blue")

    expected = next(preset.document for preset in builtin_presets() if preset.id == "classic-blue")
    assert bridge._document == bridge._localized_builtin_document(expected)
    assert all(layer.text != "Unsaved title" for layer in bridge._document.layers)
    assert not bridge.canUndo
    assert not bridge.canRedo
    applied = bridge._document
    bridge.undo()
    bridge.redo()
    assert bridge._document == applied
    bridge.close()


def test_switching_to_a_user_preset_resets_undo_and_redo(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    assert bridge.saveAsPreset("First")
    first_id = bridge.activePresetId
    title_id = str(bridge.layersModel.get(1)["id"])
    bridge.commitInlineText(title_id, "Second composition")
    assert bridge.saveAsPreset("Second")
    bridge.setBackgroundProperty("overlay_opacity", 0.20)
    bridge.setBackgroundProperty("overlay_opacity", 0.35)
    bridge.undo()
    assert bridge.canUndo
    assert bridge.canRedo

    bridge.requestPreset(first_id)

    assert bridge.activePresetId == first_id
    assert not bridge.canUndo
    assert not bridge.canRedo
    bridge.close()


def test_builtin_presets_have_localized_example_content(tmp_path) -> None:
    translator = QTranslator()
    assert translator.load(str(application_translation_root() / "solin_pt_BR.qm"))
    assert _APP.installTranslator(translator)
    bridge = _bridge(tmp_path)
    try:
        assert [layer["text"] for layer in _layer_records(bridge)] == [
            "DISCURSO PÚBLICO",
            "Imite a misericórdia de Jeová",
        ]
        assert [layer["name"] for layer in _layer_records(bridge)] == [
            "Discurso Público",
            "Título do discurso",
        ]

        bridge.requestPreset("classic-blue")
        assert [layer["text"] for layer in _layer_records(bridge)] == [
            "Imite a misericórdia de Jeová",
            "Nome",
            "Congregação",
        ]

        bridge.requestPreset("soft-photo")
        assert [layer["text"] for layer in _layer_records(bridge)] == [
            "Imite a misericórdia de Jeová",
            "Nome",
            "Congregação",
        ]
        soft_photo_preview = next(
            preset for preset in bridge.builtinPresets if preset["id"] == "soft-photo"
        )["preview_layer"]
        assert soft_photo_preview["text"] == "Imite a misericórdia de Jeová"
        assert soft_photo_preview["font_family"] == "Lato"
        assert soft_photo_preview["font_weight"] == "bold"
        assert all("Ex:" not in str(layer["text"]) for layer in _layer_records(bridge))
        assert translator.translate(
            "TalkThemeEditorView",
            "Blur image",
        ) == "Desfocar imagem"
    finally:
        bridge.close()
        assert _APP.removeTranslator(translator)


def test_saved_user_preset_content_is_not_retranslated(tmp_path) -> None:
    repository = TalkThemeRepository(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    assert bridge.saveAsPreset("Keep my content")
    expected = [layer["text"] for layer in _layer_records(bridge)]
    bridge.close()

    translator = QTranslator()
    assert translator.load(str(application_translation_root() / "solin_pt_BR.qm"))
    assert _APP.installTranslator(translator)
    reopened = _bridge(tmp_path, repository=repository)
    try:
        assert [layer["text"] for layer in _layer_records(reopened)] == expected
        assert reopened.activePresetId
        assert not reopened.activePresetIsBuiltin
    finally:
        reopened.close()
        assert _APP.removeTranslator(translator)


def test_background_blur_label_is_compiled_in_every_translation_catalog() -> None:
    catalogs = sorted(application_translation_root().glob("solin_*.qm"))
    assert len(catalogs) == 18
    for catalog in catalogs:
        translator = QTranslator()
        assert translator.load(str(catalog)), catalog.name
        assert translator.translate(
            "TalkThemeEditorView",
            "Blur image",
        ), catalog.name


def test_modified_builtin_requires_save_as_and_does_not_write(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    requested: list[bool] = []
    bridge.saveAsRequested.connect(lambda: requested.append(True))
    layer_id = str(bridge.layersModel.get(0)["id"])
    bridge.renameLayer(layer_id, "Modified label")

    assert bridge.activePresetIsBuiltin
    assert bridge.dirty
    assert not bridge.saveCurrentPreset()
    assert requested == [True]
    assert repository.saved == []
    bridge.close()


def test_preset_limit_fails_without_silently_losing_the_new_preset(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    bridge._library = replace(
        bridge._library,
        user_presets=tuple(
            TalkThemePreset(
                id=f"preset-{index}",
                name=f"Preset {index}",
                document=bridge._document,
            )
            for index in range(MAX_USER_PRESETS)
        ),
    )

    assert not bridge.saveAsPreset("One too many")
    assert bridge.statusMessage == "The maximum number of presets has been reached."
    assert repository.saved == []
    bridge.close()


def test_custom_preset_saves_and_restores_the_complete_composition(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    output_settings = _OutputSettings()
    bridge = _bridge(
        tmp_path,
        repository=repository,
        output_settings=output_settings,
    )
    title_id = str(bridge.layersModel.get(1)["id"])
    bridge.commitInlineText(title_id, "A complete composition")
    added_id = bridge.addTextLayer()
    bridge.renameLayer(added_id, "Closing note")
    bridge.commitInlineText(added_id, "Remember this")
    bridge.toggleLayerVisibility(title_id)
    bridge.setBackgroundProperty("base_color", "#29435C")
    bridge.removeBackgroundImage()
    bridge.setFollowOutputAspect(False)
    assert output_settings.writes == [False]

    assert bridge.saveAsPreset("My composition")
    preset_id = bridge.activePresetId
    first_saved = bridge._document
    assert len(repository.saved) == 1

    bridge.requestPreset("soft-photo")
    bridge.requestPreset(preset_id)
    assert bridge._document == first_saved
    assert bridge.followOutputAspect is False

    bridge.commitInlineText(added_id, "Updated note")
    updated = bridge._document
    assert bridge.saveCurrentPreset()
    assert len(repository.saved) == 2
    bridge.requestPreset("classic-blue")
    bridge.requestPreset(preset_id)
    assert bridge._document == updated

    persisted = TalkThemeRepository(tmp_path / "talk_theme.json").load()
    assert persisted.last_saved_preset_id == preset_id
    assert persisted.user_presets[0].document == updated
    assert persisted.user_presets[0].document.background.kind == "none"
    assert persisted.user_presets[0].document.background.base_color == "#29435C"
    assert "follow_output_aspect" not in persisted.user_presets[0].document.to_record()
    preset_record = next(
        record for record in bridge.userPresets if record["id"] == preset_id
    )
    assert preset_record["background"]["base_color"] == "#29435C"
    assert preset_record["background"]["has_image"] is False
    bridge.close()


def test_output_aspect_is_global_transient_editor_state(tmp_path) -> None:
    output_settings = _OutputSettings(enabled=False)
    bridge = _bridge(tmp_path / "first", output_settings=output_settings)
    original_document = bridge._document
    original_fingerprint = bridge.fingerprint()

    assert bridge.followOutputAspect is False
    assert not bridge.dirty
    assert not bridge.canUndo

    bridge.setFollowOutputAspect(True)

    assert output_settings.writes == [True]
    assert bridge.followOutputAspect is True
    assert bridge._document == original_document
    assert not bridge.dirty
    assert not bridge.canUndo
    assert bridge.fingerprint() == original_fingerprint

    bridge.requestPreset("classic-blue")
    assert bridge.followOutputAspect is True
    assert bridge.saveAsPreset("Profile-independent format")
    saved_document = bridge._library.user_presets[0].document.to_record()
    assert "follow_output_aspect" not in saved_document
    bridge.close()

    reopened_from_another_profile = _bridge(
        tmp_path / "second",
        output_settings=output_settings,
    )
    assert reopened_from_another_profile.followOutputAspect is True
    reopened_from_another_profile.close()


def test_output_aspect_control_contains_its_translated_content(tmp_path) -> None:
    translator = QTranslator()
    assert translator.load(str(application_translation_root() / "solin_pt_BR.qm"))
    assert _APP.installTranslator(translator)
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    try:
        widget.resize(1400, 900)
        widget.show()
        assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
        root = widget._qml.rootObject()
        assert root is not None
        control = _quick_item_by_name(root, "talkThemeOutputAspectControl")
        content = _quick_item_by_name(root, "talkThemeOutputAspectContent")
        assert control is not None
        assert content is not None
        assert _wait_until(lambda: control.width() >= content.width() + 20)

        content_origin = content.mapToItem(control, QPointF())
        assert content_origin.x() >= 10
        assert content_origin.x() + content.width() <= control.width() - 10
    finally:
        widget.cleanup()
        widget.close()
        assert _APP.removeTranslator(translator)


def test_layer_crud_reorder_and_delete_clear_peer_snap_references(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    added_id = bridge.addTextLayer()
    target_id = str(bridge.layersModel.get(1)["id"])

    bridge.renameLayer(added_id, "Callout")
    bridge.toggleLayerVisibility(added_id)
    bridge.toggleLayerVisibility(added_id)
    bridge.beginLayerMove(added_id)
    bridge.endLayerMove(added_id, 0.3, 0.4, f"{target_id}:left", "")
    assert bridge.layersModel.get(0)["snap_x"] == f"{target_id}:left"

    bridge.selectLayer(target_id)
    bridge.deleteLayer(target_id)
    added = next(record for record in _layer_records(bridge) if record["id"] == added_id)
    assert added["name"] == "Callout"
    assert added["visible"] is True
    assert added["snap_x"] == ""
    assert bridge.selectedLayerId

    bridge.reorderLayer(added_id, bridge.layerCount - 1)
    assert bridge.layersModel.get(bridge.layerCount - 1)["id"] == added_id
    for layer_id in [str(record["id"]) for record in _layer_records(bridge)]:
        bridge.deleteLayer(layer_id)
    assert bridge.layerCount == 0
    assert bridge.selectedLayerId == ""
    bridge.close()


def test_layer_panel_reorder_previews_ghost_and_destination_slot(tmp_path, request) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    def close_widget() -> None:
        try:
            widget.cleanup()
        finally:
            dispose_widget(widget)

    request.addfinalizer(close_widget)
    # Match the child editor viewport used by the application. A standalone
    # decorated Cocoa window is constrained to the runner's screen height.
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    show_and_activate(widget, size=QSize(1400, 900))
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    sidebar = next(
        candidate
        for candidate in root.findChildren(QObject, "talkThemeSidebar")
        if isinstance(candidate, QQuickItem) and candidate.isVisible()
    )
    sidebar.setProperty("currentTab", "layers")
    assert _wait_until(
        lambda: any(
            isinstance(candidate, QQuickItem) and candidate.isVisible()
            for candidate in root.findChildren(QObject, "talkThemeLayerPanel")
        )
    )
    panel = next(
        candidate
        for candidate in root.findChildren(QObject, "talkThemeLayerPanel")
        if isinstance(candidate, QQuickItem) and candidate.isVisible()
    )

    first_id = str(widget.bridge.layersModel.get(0)["id"])
    second_id = str(widget.bridge.layersModel.get(1)["id"])
    assert _wait_until(
        lambda: _quick_item_by_name(
            panel, f"talkThemeLayerGrip-{first_id}"
        )
        is not None
    )
    first_row = _quick_item_by_name(panel, f"talkThemeLayerRow-{first_id}")
    second_row = _quick_item_by_name(panel, f"talkThemeLayerRow-{second_id}")
    grip = _quick_item_by_name(panel, f"talkThemeLayerGrip-{first_id}")
    ghost = _quick_item_by_name(panel, "talkThemeLayerDragGhost")
    placeholder = _quick_item_by_name(panel, "talkThemeLayerDropPlaceholder")
    assert first_row is not None
    assert second_row is not None
    assert grip is not None
    assert ghost is not None
    assert placeholder is not None

    QTest.qWait(180)
    first_y_before = first_row.mapToScene(QPointF()).y()
    second_y_before = second_row.mapToScene(QPointF()).y()
    undo_count = len(widget.bridge._undo)
    start = grip.mapToScene(QPointF(grip.width() / 2, grip.height() / 2)).toPoint()
    destination = QPoint(start.x(), start.y() + 45)

    mouse_press(widget._qml, Qt.MouseButton.LeftButton, pos=start)
    try:
        mouse_move(widget._qml, destination, delay=20)
        wait_until(lambda: ghost.isVisible() and placeholder.isVisible(), description="layer reorder preview")
        assert abs(ghost.mapToScene(QPointF()).y() - (first_y_before + 45)) < 2
        assert abs(placeholder.mapToScene(QPointF()).y() - second_y_before) < 2
        wait_until(
            lambda: float(first_row.property("opacity")) < 0.1
            and second_row.mapToScene(QPointF()).y() < second_y_before - 40,
            description="layer reorder destination animation",
        )
        assert str(widget.bridge.layersModel.get(0)["id"]) == first_id
        assert len(widget.bridge._undo) == undo_count
    finally:
        mouse_release(widget._qml, Qt.MouseButton.LeftButton, pos=destination)

    assert _wait_until(lambda: not ghost.isVisible() and not placeholder.isVisible())
    assert str(widget.bridge.layersModel.get(0)["id"]) == second_id
    assert str(widget.bridge.layersModel.get(1)["id"]) == first_id
    assert len(widget.bridge._undo) == undo_count + 1
    widget.bridge.undo()
    assert str(widget.bridge.layersModel.get(0)["id"]) == first_id


def test_selection_is_transient_and_empty_selection_clears_guides(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    layer_id = str(bridge.layersModel.get(0)["id"])

    bridge.selectLayer(layer_id)
    assert bridge.selectedLayerId == layer_id
    assert not bridge.dirty
    assert not bridge.canUndo
    bridge.selectLayer("")
    assert bridge.selectedLayerId == ""
    assert bridge.snapGuides == []
    assert repository.saved == []
    bridge.close()


def test_inline_edit_is_committed_as_one_undo_entry(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original = str(bridge.layersModel.get(1)["text"])

    bridge.commitInlineText(layer_id, "Edited directly on canvas")

    assert bridge.layersModel.get(1)["text"] == "Edited directly on canvas"
    assert len(bridge._undo) == 1
    bridge.undo()
    assert bridge.layersModel.get(1)["text"] == original
    assert not bridge.canUndo
    bridge.close()


def test_custom_colors_are_restored_and_persisted_when_dialog_is_cancelled(
    tmp_path,
    monkeypatch,
) -> None:
    settings = _CustomColorSettings(("#112233",))
    bridge = _bridge(tmp_path / "first", settings=settings)
    layer_id = str(bridge.layersModel.get(0)["id"])
    bridge.selectLayer(layer_id)
    original_color = str(bridge.selectedLayer["color"])
    dialog_calls = 0

    def first_dialog(dialog: QColorDialog) -> QColorDialog.DialogCode:
        nonlocal dialog_calls
        dialog_calls += 1
        assert QColorDialog.customColor(0).name(QColor.NameFormat.HexRgb).upper() == (
            "#112233"
        )
        assert QColorDialog.customColor(1).name(QColor.NameFormat.HexRgb).upper() == (
            "#FFFFFF"
        )
        assert dialog.testOption(QColorDialog.ColorDialogOption.ShowAlphaChannel)
        QColorDialog.setCustomColor(0, QColor("#445566"))
        return QColorDialog.DialogCode.Rejected

    monkeypatch.setattr(QColorDialog, "exec", first_dialog)
    bridge.chooseTextColor()

    assert dialog_calls == 1
    assert bridge.selectedLayer["color"] == original_color
    assert len(settings.writes) == 1
    assert settings.colors == ("#112233", "#445566")
    bridge.close()

    QColorDialog.setCustomColor(0, QColor("#ABCDEF"))
    reopened = _bridge(tmp_path / "reopened", settings=settings)
    reopened.selectLayer(str(reopened.layersModel.get(0)["id"]))

    def reopened_dialog(_dialog: QColorDialog) -> QColorDialog.DialogCode:
        assert QColorDialog.customColor(0).name(QColor.NameFormat.HexRgb).upper() == (
            "#112233"
        )
        assert QColorDialog.customColor(1).name(QColor.NameFormat.HexRgb).upper() == (
            "#445566"
        )
        return QColorDialog.DialogCode.Rejected

    monkeypatch.setattr(QColorDialog, "exec", reopened_dialog)
    reopened.chooseTextColor()

    assert len(settings.writes) == 1
    reopened.close()
    for index in range(QColorDialog.customCount()):
        QColorDialog.setCustomColor(index, QColor("#FFFFFF"))


def test_background_color_uses_the_shared_profile_palette_without_alpha(
    tmp_path,
    monkeypatch,
) -> None:
    settings = _CustomColorSettings(("#112233", "#445566"))
    bridge = _bridge(tmp_path, settings=settings)
    original = bridge._document

    def accept_background_color(dialog: QColorDialog) -> QColorDialog.DialogCode:
        assert not dialog.testOption(QColorDialog.ColorDialogOption.ShowAlphaChannel)
        assert QColorDialog.customColor(0).name(QColor.NameFormat.HexRgb).upper() == (
            "#112233"
        )
        assert QColorDialog.customColor(1).name(QColor.NameFormat.HexRgb).upper() == (
            "#445566"
        )
        dialog.setCurrentColor(QColor("#A24B62"))
        dialog.accept()
        return QColorDialog.DialogCode.Accepted

    monkeypatch.setattr(QColorDialog, "exec", accept_background_color)

    bridge.chooseBackgroundColor()

    assert bridge.background["base_color"] == "#A24B62"
    assert len(bridge._undo) == 1
    assert settings.writes == []
    bridge.undo()
    assert bridge._document == original
    bridge.close()


def test_talk_theme_color_spinbox_keeps_both_arrows_clickable() -> None:
    previous_stylesheet = _APP.styleSheet()
    dialog: TalkThemeColorDialog | None = None
    try:
        _APP.setStyleSheet(app_stylesheet())
        dialog = TalkThemeColorDialog(
            QColor("#667788"),
            (),
            "Text color",
        )
        dialog.show()
        _APP.processEvents()
        spin = dialog.findChild(QSpinBox)
        assert spin is not None
        spin.setValue(112)

        option = QStyleOptionSpinBox()
        spin.initStyleOption(option)
        up_rect = spin.style().subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxUp,
            spin,
        )
        down_rect = spin.style().subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxDown,
            spin,
        )
        edit_rect = spin.style().subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxEditField,
            spin,
        )

        assert not edit_rect.contains(up_rect.center())
        assert not edit_rect.contains(down_rect.center())
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up_rect.center())
        assert spin.value() == 113
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=down_rect.center())
        assert spin.value() == 112
    finally:
        if dialog is not None:
            dialog.close()
        _APP.setStyleSheet(previous_stylesheet)


def test_custom_color_dialog_reconciles_new_swatches_while_still_open() -> None:
    dialog = TalkThemeColorDialog(
        QColor("#667788"),
        ("#A24B62", "#4B8A72"),
        "Text color",
    )
    try:
        dialog.show()
        _APP.processEvents()
        add_button = next(
            button
            for button in dialog.findChildren(QPushButton)
            if "Custom" in button.text()
        )

        dialog.setCurrentColor(QColor("#3A6EA5"))
        add_button.click()
        assert _wait_until(
            lambda: QColorDialog.customColor(0).name(QColor.NameFormat.HexRgb).upper()
            == "#A24B62"
            and QColorDialog.customColor(1).name(QColor.NameFormat.HexRgb).upper()
            == "#4B8A72"
            and QColorDialog.customColor(2).name(QColor.NameFormat.HexRgb).upper()
            == "#3A6EA5"
        )

        dialog.setCurrentColor(QColor("#C17C38"))
        add_button.click()
        assert _wait_until(
            lambda: dialog.custom_colors()
            == ("#A24B62", "#4B8A72", "#3A6EA5", "#C17C38")
        )
    finally:
        dialog.close()
        for index in range(QColorDialog.customCount()):
            QColorDialog.setCustomColor(index, QColor("#FFFFFF"))


def test_text_edit_coalescing_stops_at_an_intervening_command(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original_name = str(bridge.layersModel.get(1)["name"])

    bridge.setLayerText(layer_id, "A")
    bridge.renameLayer(layer_id, "Renamed title")
    bridge.setLayerText(layer_id, "AB")
    bridge.undo()

    restored = bridge.layersModel.get(1)
    assert restored["text"] == "A"
    assert restored["name"] == "Renamed title"
    bridge.undo()
    assert bridge.layersModel.get(1)["name"] == original_name
    bridge.close()


def test_drag_preview_does_not_mutate_and_release_is_one_undo_entry(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original = bridge._document
    layer = bridge.layersModel.get(1)

    bridge.beginLayerMove(layer_id)
    preview = bridge.moveLayer(
        layer_id,
        0.41,
        0.52,
        float(layer["width"]),
        0.08,
        1200,
        675,
        "",
        "",
        True,
        "",
        float(layer["x"]),
        float(layer["y"]),
    )
    assert bridge._document == original
    assert preview["x"] == 0.41
    assert preview["y"] == 0.52

    bridge.endLayerMove(layer_id, 0.41, 0.52, "", "")
    assert len(bridge._undo) == 1
    assert bridge._document != original
    bridge.undo()
    assert bridge._document == original
    assert not bridge.canUndo
    bridge.close()


def test_cancelled_drag_discards_preview_without_an_undo_entry(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original = bridge._document
    layer = bridge.layersModel.get(1)

    bridge.beginLayerMove(layer_id)
    preview = bridge.moveLayer(
        layer_id,
        0.41,
        0.52,
        float(layer["width"]),
        0.08,
        1200,
        675,
        "",
        "",
        True,
        "",
        float(layer["x"]),
        float(layer["y"]),
    )
    assert preview["x"] == 0.41

    bridge.cancelLayerMove()

    assert bridge._document == original
    assert not bridge.canUndo
    assert bridge.snapGuides == []
    bridge.close()


def test_resize_release_is_bounded_and_recorded_as_one_undo_entry(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original = bridge._document

    bridge.beginLayerResize(layer_id)

    assert bridge._document == original
    bridge.endLayerResize(layer_id, -0.1, 0.9, 1.4, 0.5, 0.4)

    resized = bridge.layersModel.get(1)
    assert resized["x"] == 0.0
    assert resized["y"] == 0.6
    assert resized["width"] == 1.0
    assert resized["font_size"] == 0.30
    assert "height" not in resized
    assert resized["snap_x"] == ""
    assert resized["snap_y"] == ""
    assert len(bridge._undo) == 1

    bridge.undo()
    assert bridge._document == original
    assert not bridge.canUndo
    bridge.close()


def test_cancelled_resize_keeps_document_and_undo_history_unchanged(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(1)["id"])
    original = bridge._document

    bridge.beginLayerResize(layer_id)
    bridge.cancelLayerResize()

    assert bridge._document == original
    assert not bridge.canUndo
    assert bridge.snapGuides == []
    bridge.close()


def test_cancelled_background_adjustment_restores_the_original_position(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    original = bridge._document

    assert bridge.backgroundOverlayMaximum == MAX_BACKGROUND_OVERLAY_OPACITY

    bridge.beginBackgroundMove()
    bridge.moveBackground(0.45, -0.3)
    assert bridge._document != original

    bridge.cancelBackgroundMove()

    assert bridge._document == original
    assert not bridge.canUndo
    bridge.close()


def test_background_blur_is_bounded_coalesced_and_persisted(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    original = bridge._document
    original_fingerprint = bridge.fingerprint()

    assert bridge.backgroundBlurMaximum == MAX_BACKGROUND_BLUR
    bridge.setBackgroundProperty("blur", 0.25)
    bridge.setBackgroundProperty("blur", 2.0)

    assert bridge.background["blur"] == MAX_BACKGROUND_BLUR
    assert bridge.fingerprint() != original_fingerprint
    assert len(bridge._undo) == 1
    assert repository.saved == []
    bridge.undo()
    assert bridge._document == original
    bridge.redo()
    assert bridge.background["blur"] == MAX_BACKGROUND_BLUR

    assert bridge.saveAsPreset("Blurred background")
    persisted = TalkThemeRepository(tmp_path / "talk_theme.json").load()
    assert persisted.user_presets[0].document.background.blur == MAX_BACKGROUND_BLUR
    bridge.close()


def test_removing_background_image_preserves_color_and_is_one_undo_step(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    bridge.setBackgroundProperty("base_color", "#29435C")
    bridge.setBackgroundProperty("blur", 0.6)
    before_removal = bridge._document
    undo_count = len(bridge._undo)

    bridge.removeBackgroundImage()

    background = bridge.background
    assert background["kind"] == "none"
    assert background["source"] == ""
    assert background["url"] == ""
    assert background["has_image"] is False
    assert background["base_color"] == "#29435C"
    assert background["fill_mode"] == "cover"
    assert background["overlay_opacity"] == 0.0
    assert background["blur"] == 0.0
    assert background["zoom"] == 1.0
    assert background["norm_x"] == 0.0
    assert background["norm_y"] == 0.0
    assert len(bridge._undo) == undo_count + 1
    assert repository.saved == []

    bridge.undo()

    assert bridge._document == before_removal
    bridge.close()


def test_peer_snap_guide_has_no_generic_other_text_label(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    moving_id = str(bridge.layersModel.get(0)["id"])
    peer_id = str(bridge.layersModel.get(1)["id"])
    moving = bridge._layer(moving_id)
    assert moving is not None
    bridge._set_layer(replace(moving, snap_x=f"{peer_id}:left"))

    bridge.selectLayer(moving_id)

    assert bridge.snapGuides == [
        {
            "axis": "x",
            "position": bridge._layer(peer_id).x,
            "id": f"{peer_id}:left",
            "label": "",
        }
    ]
    bridge.close()


def test_live_snap_guides_are_translated_while_the_layer_is_moving(tmp_path) -> None:
    class SnapTranslator(QTranslator):
        def translate(
            self,
            context: str,
            source_text: str,
            disambiguation: str | None = None,
            n: int = -1,
        ) -> str:
            del context, disambiguation, n
            return "Centro durante o arraste" if source_text == "Center" else ""

    translator = SnapTranslator()
    assert _APP.installTranslator(translator)
    bridge = _bridge(tmp_path)
    try:
        layer_id = bridge.addTextLayer()
        layer = bridge._layer(layer_id)
        assert layer is not None
        bridge.beginLayerMove(layer_id)

        bridge.moveLayer(
            layer_id,
            0.25,
            layer.y,
            0.50,
            0.08,
            1000,
            600,
            "",
            "",
            False,
            "",
            layer.x,
            layer.y,
        )

        assert any(
            guide["label"] == "Centro durante o arraste"
            for guide in bridge.snapGuides
        )
        bridge.cancelLayerMove()
    finally:
        bridge.close()
        _APP.removeTranslator(translator)


def test_peer_snap_guide_uses_the_resized_layer_height(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    moving_id = str(bridge.layersModel.get(0)["id"])
    peer_id = str(bridge.layersModel.get(1)["id"])
    moving = bridge._layer(moving_id)
    peer = bridge._layer(peer_id)
    assert moving is not None
    assert peer is not None
    bridge.setLayerRenderedHeight(peer_id, 0.24)
    bridge._set_layer(replace(moving, snap_y=f"{peer_id}:bottom"))

    bridge.selectLayer(moving_id)

    y_guides = [guide for guide in bridge.snapGuides if guide["axis"] == "y"]
    assert y_guides == [
        {
            "axis": "y",
            "position": peer.y + 0.24,
            "id": f"{peer_id}:bottom",
            "label": "",
        }
    ]
    bridge.close()


def test_clicking_the_selected_snapped_layer_restores_its_guides(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    layer_id = str(bridge.layersModel.get(0)["id"])
    layer = bridge._layer(layer_id)
    assert layer is not None
    bridge._set_layer(replace(layer, x=0.25, width=0.50, snap_x="center"))

    bridge.selectLayer(layer_id)
    assert bridge.snapGuides
    bridge._clear_guides()
    assert bridge.snapGuides == []

    bridge.selectLayer(layer_id)
    assert bridge.snapGuides
    bridge.beginLayerMove(layer_id)
    layer = bridge.layersModel.get(0)
    bridge.endLayerMove(
        layer_id,
        float(layer["x"]),
        float(layer["y"]),
        str(layer["snap_x"]),
        str(layer["snap_y"]),
    )
    assert bridge.snapGuides
    bridge.close()


def test_list_model_emits_data_changed_and_rows_moved_without_reset(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    model = bridge.layersModel
    data_changes: list[tuple[object, ...]] = []
    moves: list[tuple[object, ...]] = []
    resets: list[bool] = []
    model.dataChanged.connect(lambda *args: data_changes.append(args))
    model.rowsMoved.connect(lambda *args: moves.append(args))
    model.modelReset.connect(lambda: resets.append(True))
    layer_id = str(model.get(0)["id"])

    bridge.renameLayer(layer_id, "Renamed")
    bridge.setLayerProperty(layer_id, "font_size", 0.10)
    bridge.reorderLayer(layer_id, 1)

    assert len(data_changes) == 2
    assert len(moves) == 1
    assert resets == []
    assert model.get(1)["name"] == "Renamed"
    assert model.get(1)["font_size"] == 0.10
    bridge.close()


def test_background_only_composition_can_project_and_export(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    for layer_id in [str(record["id"]) for record in _layer_records(bridge)]:
        bridge.deleteLayer(layer_id)
    projection_requests: list[bool] = []
    export_requests: list[bool] = []
    bridge.projectionRequested.connect(lambda: projection_requests.append(True))
    bridge.exportRequested.connect(lambda: export_requests.append(True))

    assert bridge.layerCount == 0
    assert bridge.canRender
    bridge.requestProjection()
    assert projection_requests == [True]
    bridge.finish_projection(ProjectionResult(ProjectionResultStatus.ACCEPTED))
    bridge.requestExport()
    assert export_requests == [True]
    bridge.cancel_export()
    assert bridge.canRender
    bridge.close()


def test_editing_rendering_undo_and_preset_switch_do_not_write(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    layer_id = str(bridge.layersModel.get(1)["id"])
    layer = bridge.layersModel.get(1)

    bridge.commitInlineText(layer_id, "Transient")
    bridge.beginLayerMove(layer_id)
    bridge.moveLayer(
        layer_id,
        0.4,
        0.5,
        float(layer["width"]),
        0.08,
        1200,
        675,
        "",
        "",
        True,
        "",
        float(layer["x"]),
        float(layer["y"]),
    )
    bridge.endLayerMove(layer_id, 0.4, 0.5, "", "")
    bridge.beginLayerResize(layer_id)
    bridge.endLayerResize(layer_id, 0.2, 0.3, 0.5, 0.1, 0.2)
    bridge.undo()
    bridge.redo()
    bridge.requestProjection()
    bridge.finish_projection(ProjectionResult(ProjectionResultStatus.ACCEPTED))
    bridge.requestExport()
    bridge.cancel_export()
    bridge.requestPreset("classic-blue")

    assert repository.saved == []
    bridge.close()


def test_only_save_as_update_and_delete_write_the_repository(tmp_path) -> None:
    repository = _RepositorySpy(tmp_path / "talk_theme.json")
    bridge = _bridge(tmp_path, repository=repository)
    layer_id = str(bridge.layersModel.get(1)["id"])
    bridge.commitInlineText(layer_id, "Saved")

    assert bridge.saveAsPreset("Explicit preset")
    preset_id = bridge.activePresetId
    assert len(repository.saved) == 1
    bridge.commitInlineText(layer_id, "Updated")
    assert bridge.saveCurrentPreset()
    assert len(repository.saved) == 2
    bridge.deletePreset(preset_id)
    assert len(repository.saved) == 3
    assert bridge.activePresetId == "botanical"
    bridge.close()


def test_unsaved_policy_false_discards_and_true_can_cancel_or_discard(tmp_path) -> None:
    direct = _bridge(tmp_path / "direct", prompt_on_unsaved_changes=False)
    direct.renameLayer(str(direct.layersModel.get(0)["id"]), "Changed")
    direct.requestPreset("classic-blue")
    assert direct.activePresetId == "classic-blue"
    direct.close()

    prompted = _bridge(tmp_path / "prompted", prompt_on_unsaved_changes=True)
    prompted.renameLayer(str(prompted.layersModel.get(0)["id"]), "Changed")
    requested: list[str] = []
    prompted.unsavedChangesRequested.connect(requested.append)
    prompted.requestPreset("classic-blue")
    assert requested == ["classic-blue"]
    assert prompted.activePresetId == "botanical"
    prompted.resolveUnsavedPreset("cancel")
    assert prompted.activePresetId == "botanical"
    prompted.requestPreset("classic-blue")
    prompted.resolveUnsavedPreset("discard")
    assert prompted.activePresetId == "classic-blue"
    prompted.close()


def test_unsaved_save_updates_custom_or_waits_for_native_save_as(tmp_path) -> None:
    custom_repository = _RepositorySpy(tmp_path / "custom" / "talk_theme.json")
    custom = _bridge(
        tmp_path / "custom",
        repository=custom_repository,
        prompt_on_unsaved_changes=True,
    )
    assert custom.saveAsPreset("Editable")
    custom.renameLayer(str(custom.layersModel.get(0)["id"]), "Changed")
    custom.requestPreset("classic-blue")

    custom.resolveUnsavedPreset("save")

    assert custom.activePresetId == "classic-blue"
    assert len(custom_repository.saved) == 2
    custom.close()

    native_repository = _RepositorySpy(tmp_path / "native" / "talk_theme.json")
    native = _bridge(
        tmp_path / "native",
        repository=native_repository,
        prompt_on_unsaved_changes=True,
    )
    save_as_requests: list[bool] = []
    native.saveAsRequested.connect(lambda: save_as_requests.append(True))
    native.renameLayer(str(native.layersModel.get(0)["id"]), "Changed")
    native.requestPreset("classic-blue")

    native.resolveUnsavedPreset("save")

    assert save_as_requests == [True]
    assert native.activePresetId == "botanical"
    assert native.saveAsPreset("Preserved before switching")
    assert native.activePresetId == "classic-blue"
    assert len(native_repository.saved) == 1
    native.close()


def test_asset_import_errors_are_localized_on_the_gui_thread(tmp_path) -> None:
    bridge = _bridge(tmp_path)

    bridge._on_asset_failed("unsupported_format")

    assert bridge.statusMessage == "This image format is not supported."
    bridge.close()


def test_editor_captures_and_exports_exact_clean_canvas_png(tmp_path, monkeypatch) -> None:
    session = ProjectionSession()
    notifications = _Notifications()
    captured: list[tuple[bytes, dict[str, str]]] = []
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=notifications,
        projection_session=session,
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )

    def project(_title: str, data: bytes, metadata: dict[str, str]) -> ProjectionResult:
        captured.append((data, metadata))
        return ProjectionResult(ProjectionResultStatus.ACCEPTED)

    widget.set_projection_handler(project)
    widget.resize(1400, 900)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    assert widget._qml.errors() == []
    root = widget._qml.rootObject()
    assert root is not None
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)

    widget.bridge.requestPreset("soft-photo")
    title_id = next(
        str(record["id"])
        for record in _layer_records(widget.bridge)
        if record["template_key"] == "title"
    )
    widget.bridge.commitInlineText(title_id, "How to find peace")
    widget.bridge.selectLayer(title_id)
    widget.bridge.requestProjection()

    assert _wait_until(lambda: bool(captured))
    data, metadata = captured[0]
    image = QImage()
    assert image.loadFromData(data, "PNG")
    assert (image.width(), image.height()) == (1920, 1080)
    assert metadata["generated_kind"] == "talk_theme"
    assert len(metadata["fingerprint"]) == 64
    assert canvas.property("finalOutput") is False

    export_base = tmp_path / "exported-theme"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(export_base), ""),
    )
    widget.bridge.requestExport()
    exported = export_base.with_suffix(".png")
    assert _wait_until(exported.is_file)
    exported_image = QImage(str(exported))
    assert (exported_image.width(), exported_image.height()) == (1920, 1080)
    assert not widget.bridge.exporting
    assert not list(tmp_path.glob("*.XXXXXX"))

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: ("", ""),
    )
    widget.bridge.requestExport()
    assert not widget.bridge.exporting
    assert notifications.messages == []
    widget.cleanup()
    widget.close()


def test_editor_capture_is_independent_of_qt_scale_factor(tmp_path) -> None:
    test_path = Path(__file__).resolve()
    project_root = test_path.parents[2]

    for scale_factor in ("1.25", "1.5", "1.75"):
        environment = os.environ.copy()
        environment.update(
            {
                "QT_QPA_PLATFORM": "offscreen",
                "QT_SCALE_FACTOR": scale_factor,
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{test_path}::test_editor_captures_and_exports_exact_clean_canvas_png",
                "-q",
                "--basetemp",
                str(tmp_path / f"scale-{scale_factor}"),
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 0, (
            f"Talk-theme capture failed at {scale_factor}x scaling:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def test_editor_exports_the_background_blur_effect(tmp_path, monkeypatch) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    inspector = next(
        candidate
        for candidate in root.findChildren(QObject, "talkThemeInspector")
        if isinstance(candidate, QQuickItem) and candidate.isVisible()
    )
    inspector.setProperty("currentTab", "background")
    blur_slider = _quick_item_by_name(
        inspector,
        "talkThemeBackgroundBlurSlider",
    )
    assert blur_slider is not None
    assert blur_slider.isVisible()
    assert float(blur_slider.property("to")) == MAX_BACKGROUND_BLUR

    for layer_id in [str(record["id"]) for record in _layer_records(widget.bridge)]:
        widget.bridge.deleteLayer(layer_id)
    widget.bridge.setBackgroundProperty("overlay_opacity", 0.0)

    unblurred_path = tmp_path / "unblurred.png"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(unblurred_path), ""),
    )
    widget.bridge.requestExport()
    assert _wait_until(unblurred_path.is_file)

    widget.bridge.setBackgroundProperty("blur", 1.0)
    assert _wait_until(lambda: float(blur_slider.property("value")) == 1.0)
    blurred_path = tmp_path / "blurred.png"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(blurred_path), ""),
    )
    widget.bridge.requestExport()
    assert _wait_until(blurred_path.is_file)

    unblurred = QImage(str(unblurred_path))
    blurred = QImage(str(blurred_path))
    assert (unblurred.width(), unblurred.height()) == (1920, 1080)
    assert blurred.size() == unblurred.size()

    def edge_energy(image: QImage) -> int:
        total = 0
        for y in range(8, image.height() - 8, 16):
            for x in range(8, image.width() - 8, 16):
                center = image.pixelColor(x, y)
                for neighbor in (image.pixelColor(x + 8, y), image.pixelColor(x, y + 8)):
                    total += abs(center.red() - neighbor.red())
                    total += abs(center.green() - neighbor.green())
                    total += abs(center.blue() - neighbor.blue())
        return total

    assert edge_energy(blurred) < edge_energy(unblurred) * 0.8
    widget.cleanup()
    widget.close()


def test_editor_exports_an_exact_solid_color_background(tmp_path, monkeypatch) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)

    root = widget._qml.rootObject()
    assert root is not None
    inspector = next(
        candidate
        for candidate in root.findChildren(QObject, "talkThemeInspector")
        if isinstance(candidate, QQuickItem) and candidate.isVisible()
    )
    assert isinstance(inspector, QQuickItem)
    inspector.setProperty("currentTab", "background")
    QTest.qWait(50)
    image_actions = inspector.findChild(QObject, "talkThemeImageActions")
    image_controls = inspector.findChild(QObject, "talkThemeImageControls")
    color_section = inspector.findChild(QObject, "talkThemeBackgroundColorSection")
    remove_button = inspector.findChild(QObject, "talkThemeRemoveBackgroundButton")
    assert isinstance(image_actions, QQuickItem)
    assert isinstance(image_controls, QQuickItem)
    assert isinstance(color_section, QQuickItem)
    assert isinstance(remove_button, QQuickItem)
    assert image_actions.isVisible()
    assert image_controls.isVisible()
    assert color_section.isVisible()
    image_actions_top = image_actions.mapToScene(QPointF()).y()
    image_controls_top = image_controls.mapToScene(QPointF()).y()
    color_section_top = color_section.mapToScene(QPointF()).y()
    assert image_actions_top < image_controls_top < color_section_top

    for layer_id in [str(record["id"]) for record in _layer_records(widget.bridge)]:
        widget.bridge.deleteLayer(layer_id)
    widget.bridge.setBackgroundProperty("base_color", "#A24B62")
    widget.bridge.removeBackgroundImage()

    assert not image_controls.isVisible()
    assert not remove_button.isVisible()

    exported = tmp_path / "solid-theme.png"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(exported), ""),
    )
    widget.bridge.requestExport()

    assert _wait_until(exported.is_file)
    image = QImage(str(exported))
    assert (image.width(), image.height()) == (1920, 1080)
    assert image.pixelColor(image.width() // 2, image.height() // 2) == QColor("#A24B62")
    widget.cleanup()
    widget.close()


def test_text_and_background_colors_use_the_same_picker_component(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    try:
        widget.resize(1200, 760)
        widget.show()
        assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
        root = widget._qml.rootObject()
        assert root is not None
        inspector = next(
            candidate
            for candidate in root.findChildren(QObject, "talkThemeInspector")
            if isinstance(candidate, QQuickItem) and candidate.isVisible()
        )
        layer_id = str(widget.bridge.layersModel.get(0)["id"])
        widget.bridge.selectLayer(layer_id)
        text_picker = inspector.findChild(QObject, "talkThemeTextColorPicker")
        background_picker = inspector.findChild(QObject, "talkThemeBackgroundColorPicker")
        assert isinstance(text_picker, QQuickItem)
        assert isinstance(background_picker, QQuickItem)

        def visible_picker_size(tab: str, picker: QQuickItem) -> tuple[float, float]:
            inspector.setProperty("currentTab", tab)
            # Hidden ColumnLayouts retain old geometry. Measure each component
            # only after its visible layout fills the inspector with 16 px margins.
            wait_until(
                lambda: (
                    picker.isVisible()
                    and picker.parentItem().width() == inspector.width()
                    and picker.width() == inspector.width() - 32
                    and picker.height() == 42
                ),
                description=lambda: (
                    f"{tab} color picker layout: host={widget.size()}, "
                    f"qml={widget._qml.size()}, root={(root.width(), root.height())}, "
                    f"inspector={inspector.width()}, parent={picker.parentItem().width()}, "
                    f"picker={(picker.width(), picker.height())}, visible={picker.isVisible()}"
                ),
            )
            return picker.width(), picker.height()

        text_size = visible_picker_size("text", text_picker)
        background_size = visible_picker_size("background", background_picker)
        assert text_picker.metaObject().className() == background_picker.metaObject().className()
        assert text_size == background_size
    finally:
        try:
            widget.cleanup()
        finally:
            dispose_widget(widget)


def test_pending_inline_edit_is_committed_before_close_decides_dirty_state(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    layer_id = str(widget.bridge.layersModel.get(1)["id"])

    canvas.beginInlineEdit(layer_id)
    assert _wait_until(lambda: _active_text_editor(canvas) is not None)
    editor = _active_text_editor(canvas)
    assert editor is not None
    original_text = widget.bridge.layersModel.get(1)["text"]
    editor.setProperty("text", "Pending inline title")
    assert widget.bridge.layersModel.get(1)["text"] == original_text

    assert widget.confirm_close()

    assert widget.bridge.layersModel.get(1)["text"] == "Pending inline title"
    assert widget.bridge.dirty
    assert widget.bridge.canUndo

    canvas.beginInlineEdit(layer_id)
    editor.setProperty("text", "Committed before another editor opens")
    canvas.beginInlineEdit(str(widget.bridge.layersModel.get(0)["id"]))
    assert widget.bridge.layersModel.get(1)["text"] == "Committed before another editor opens"
    canvas.cancelInlineEdit()
    widget.cleanup()
    widget.close()


@pytest.fixture
def editor(tmp_path, request):
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    request.addfinalizer(lambda: dispose_widget(widget))
    request.addfinalizer(widget.cleanup)
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    widget.resize(1200, 760)
    return widget


@pytest.fixture
def active_editor(editor):
    show_and_activate(editor)
    return editor


def test_layer_cursor_entry_survives_previous_layer_exit(editor) -> None:
    widget = editor
    bridge = widget.bridge
    bridge.beginPointer("previous-layer", Qt.CursorShape.OpenHandCursor.value)
    bridge.beginPointer("current-layer", Qt.CursorShape.IBeamCursor.value)
    bridge.endPointer("previous-layer")
    assert widget._qml.cursor().shape() == Qt.CursorShape.IBeamCursor
    assert widget._qml.quickWindow().cursor().shape() == Qt.CursorShape.IBeamCursor
    bridge.endPointer("current-layer")
    assert widget._qml.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert widget._qml.quickWindow().cursor().shape() == Qt.CursorShape.ArrowCursor


def test_builtin_label_enter_inserts_a_line_break_and_control_enter_commits(active_editor) -> None:
    widget = active_editor
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    label_id = str(widget.bridge.layersModel.get(0)["id"])
    original_text = str(widget.bridge.layersModel.get(0)["text"])
    original_layer = widget.bridge._layer(label_id)
    assert original_layer is not None
    assert _wait_until(lambda: label_id in widget.bridge._rendered_layer_heights)
    original_height = widget.bridge._rendered_layer_heights[label_id]
    original_center = original_layer.y + original_height / 2
    undo_count = len(widget.bridge._undo)
    layer_pointer = _quick_item_by_name(
        canvas,
        f"talkThemeLayerPointer-{label_id}",
    )
    assert layer_pointer is not None
    pointer_point = layer_pointer.mapToScene(
        QPointF(layer_pointer.width() / 2, layer_pointer.height() / 2)
    ).toPoint()
    mouse_move(widget._qml, pointer_point)
    assert _wait_until(lambda: bool(layer_pointer.property("containsMouse")))
    assert _wait_until(
        lambda: widget._qml.quickWindow().cursor().shape()
        == Qt.CursorShape.OpenHandCursor
    )

    canvas.beginInlineEdit(label_id)
    assert _wait_until(lambda: _active_text_editor(canvas) is not None)
    editor = _active_text_editor(canvas)
    assert editor is not None
    assert layer_pointer.property("enabled") is True
    assert layer_pointer.property("acceptedButtons") == Qt.MouseButton.NoButton
    assert layer_pointer.property("cursorShape") == Qt.CursorShape.IBeamCursor
    assert _wait_until(
        lambda: widget._qml.quickWindow().cursor().shape()
        == Qt.CursorShape.IBeamCursor
    )
    editor.setProperty("text", "First line")
    editor.setProperty("cursorPosition", len("First line"))

    QTest.keyClick(widget._qml, Qt.Key.Key_Return)
    QTest.keyClicks(widget._qml, "Second line")

    assert _active_text_editor(canvas) is editor
    assert editor.property("text") == "First line\nSecond line"
    assert widget.bridge.layersModel.get(0)["text"] == original_text
    assert _wait_until(
        lambda: widget.bridge._rendered_layer_heights[label_id] > original_height * 1.8,
    )
    layer_item = editor.parentItem()
    assert layer_item is not None
    live_center = (
        layer_item.y() + layer_item.height() / 2
    ) / canvas.height()
    assert abs(live_center - original_center) < 0.002
    assert widget.bridge._layer(label_id) == original_layer

    QTest.keyClick(
        widget._qml,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert _wait_until(lambda: _active_text_editor(canvas) is None)
    assert widget.bridge.layersModel.get(0)["text"] == "First line\nSecond line"
    assert len(widget.bridge._undo) == undo_count + 1
    committed_layer = widget.bridge._layer(label_id)
    assert committed_layer is not None
    committed_height = widget.bridge._rendered_layer_heights[label_id]
    assert committed_layer.y < original_layer.y
    assert abs(committed_layer.y + committed_height / 2 - original_center) < 0.002

    widget.bridge.undo()
    restored_layer = widget.bridge._layer(label_id)
    assert restored_layer is not None
    assert restored_layer.text == original_text
    assert restored_layer.y == original_layer.y


def test_inspector_text_edit_preserves_the_rendered_layer_center(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1400, 900)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    layer_id = widget.bridge.addTextLayer()
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    assert canvas.cancelInlineEdit()
    widget.bridge.setLayerProperty(layer_id, "font_size", 0.06)
    widget.bridge.setLayerProperty(layer_id, "width", 0.50)
    widget.bridge.commitInlineText(layer_id, "One line")
    widget.bridge.selectLayer(layer_id)

    assert _wait_until(lambda: layer_id in widget.bridge._rendered_layer_heights)
    original_layer = widget.bridge._layer(layer_id)
    assert original_layer is not None
    original_height = widget.bridge._rendered_layer_heights[layer_id]
    original_center = original_layer.y + original_height / 2
    undo_count = len(widget.bridge._undo)

    inspector = next(
        candidate
        for candidate in root.findChildren(QObject, "talkThemeInspector")
        if isinstance(candidate, QQuickItem) and candidate.isVisible()
    )
    inspector.setProperty("currentTab", "text")
    content_field = _quick_item_by_name(inspector, "talkThemeContentField")
    assert content_field is not None
    content_field.forceActiveFocus()
    content_field.setProperty("text", "One\nTwo\nThree")

    assert _wait_until(
        lambda: widget.bridge.layersModel.get(0)["text"] == "One\nTwo\nThree"
    )
    assert _wait_until(
        lambda: widget.bridge._rendered_layer_heights[layer_id] > original_height * 2
    )
    updated_layer = widget.bridge._layer(layer_id)
    assert updated_layer is not None
    updated_height = widget.bridge._rendered_layer_heights[layer_id]
    assert updated_layer.y < original_layer.y
    assert abs(updated_layer.y + updated_height / 2 - original_center) < 0.002
    assert len(widget.bridge._undo) == undo_count + 1

    widget.bridge.undo()
    restored_layer = widget.bridge._layer(layer_id)
    assert restored_layer is not None
    assert restored_layer.text == original_layer.text
    assert restored_layer.y == original_layer.y
    widget.cleanup()
    widget.close()


def test_font_size_can_switch_to_direct_pixel_entry(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1400, 900)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    layer_id = str(widget.bridge.layersModel.get(1)["id"])
    widget.bridge.selectLayer(layer_id)
    inspectors = root.findChildren(QObject, "talkThemeInspector")
    inspector = next(item for item in inspectors if item.property("visible"))

    assert QMetaObject.invokeMethod(inspector, "beginFontSizeEdit")
    assert inspector.property("fontSizeEditing") is True
    assert QMetaObject.invokeMethod(
        inspector,
        "applyFontPixels",
        Q_ARG("QVariant", 96),
    )

    assert abs(float(widget.bridge.selectedLayer["font_size"]) - 96 / 1080) < 1e-9
    widget.cleanup()
    widget.close()


def test_canvas_height_tracks_content_and_proportional_text_scale(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    layer_id = widget.bridge.addTextLayer()
    assert canvas.cancelInlineEdit()
    widget.bridge.setLayerProperty(layer_id, "font_size", 0.08)
    widget.bridge.setLayerProperty(layer_id, "width", 0.50)

    widget.bridge.commitInlineText(layer_id, "One line")
    assert _wait_until(
        lambda: 0.07 < widget.bridge._rendered_layer_heights.get(layer_id, 0) < 0.15,
    )
    single_line_height = widget.bridge._rendered_layer_heights[layer_id]
    widget.bridge.commitInlineText(layer_id, "One\nTwo\nThree")
    assert _wait_until(
        lambda: widget.bridge._rendered_layer_heights[layer_id] > single_line_height * 2,
    )
    multiline_height = widget.bridge._rendered_layer_heights[layer_id]
    layer = widget.bridge._layer(layer_id)
    assert layer is not None
    scale = 1.12

    assert QMetaObject.invokeMethod(
        canvas,
        "setGeometryOverride",
        Q_ARG("QVariant", layer_id),
        Q_ARG("QVariant", layer.x),
        Q_ARG("QVariant", layer.y),
        Q_ARG("QVariant", layer.width * scale),
        Q_ARG("QVariant", layer.font_size * scale),
    )
    assert _wait_until(
        lambda: widget.bridge._rendered_layer_heights[layer_id] > multiline_height,
    )
    scaled_height = widget.bridge._rendered_layer_heights[layer_id]

    assert abs(scaled_height / multiline_height - scale) < 0.08
    assert "height" not in layer.to_record()
    widget.cleanup()
    widget.close()


def test_vertical_resize_handle_scales_text_and_width_in_one_undo_step(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    layer_id = widget.bridge.addTextLayer()
    assert canvas.cancelInlineEdit()
    widget.bridge.setLayerProperty(layer_id, "font_size", 0.08)
    widget.bridge.setLayerProperty(layer_id, "width", 0.50)
    widget.bridge.commitInlineText(layer_id, "One\nTwo\nThree")
    widget.bridge.selectLayer(layer_id)
    assert _wait_until(lambda: layer_id in widget.bridge._rendered_layer_heights)
    handle = _quick_item_by_name(
        canvas,
        f"talkThemeResizeHandle-{layer_id}-0-1",
    )
    assert handle is not None
    start = handle.mapToScene(
        QPointF(handle.width() / 2, handle.height() / 2),
    ).toPoint()
    before_layer = widget.bridge._layer(layer_id)
    assert before_layer is not None
    before = before_layer.to_record()
    undo_count = len(widget.bridge._undo)

    QTest.mousePress(widget._qml, Qt.MouseButton.LeftButton, pos=start)
    destination = QPoint(start.x(), start.y() + 30)
    QTest.mouseMove(widget._qml, destination, delay=20)
    QTest.mouseRelease(widget._qml, Qt.MouseButton.LeftButton, pos=destination)
    QTest.qWait(50)

    after_layer = widget.bridge._layer(layer_id)
    assert after_layer is not None
    after = after_layer.to_record()
    assert float(after["font_size"]) > float(before["font_size"])
    assert float(after["width"]) > float(before["width"])
    assert "height" not in after
    assert len(widget.bridge._undo) == undo_count + 1
    widget.bridge.undo()
    restored = widget.bridge._layer(layer_id)
    assert restored is not None
    assert restored.font_size == before["font_size"]
    assert restored.width == before["width"]
    widget.cleanup()
    widget.close()


def test_background_adjustment_owns_drags_above_visible_text_layers(tmp_path) -> None:
    widget = TalkThemeEditorWidget(
        None,
        profile_paths=_profile_paths(tmp_path),
        notifications=_Notifications(),
        projection_session=ProjectionSession(),
        settings=_CustomColorSettings(),
        output_settings=_OutputSettings(),
    )
    widget.resize(1200, 760)
    widget.show()
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    workspace = root.findChild(QObject, "talkThemeWorkspace")
    assert isinstance(workspace, QQuickItem)
    canvas = root.findChild(QObject, "talkThemeCanvas")
    assert isinstance(canvas, QQuickItem)
    background_pointer = root.findChild(QObject, "talkThemeBackgroundPointer")
    assert isinstance(background_pointer, QQuickItem)
    layer_id = str(widget.bridge.layersModel.get(1)["id"])
    widget.bridge.commitInlineText(layer_id, "Visible title")
    widget.bridge.selectLayer(layer_id)
    assert _wait_until(lambda: layer_id in widget.bridge._rendered_layer_heights)
    layer_before = dict(widget.bridge.layersModel.get(1))
    background_before = dict(widget.bridge.background)
    undo_count = len(widget.bridge._undo)
    start = canvas.mapToScene(
        QPointF(
            (float(layer_before["x"]) + float(layer_before["width"]) / 2)
            * canvas.width(),
            (
                float(layer_before["y"])
                + widget.bridge._rendered_layer_heights[layer_id] / 2
            )
            * canvas.height(),
        ),
    ).toPoint()

    assert QMetaObject.invokeMethod(workspace, "toggleBackgroundAdjustment")
    assert workspace.property("adjustBackground") is True
    assert widget.bridge.selectedLayerId == ""
    assert float(background_pointer.z()) > 1000

    destination = QPoint(start.x() + 36, start.y() + 24)
    QTest.mousePress(widget._qml, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget._qml, destination, delay=20)
    QTest.mouseRelease(widget._qml, Qt.MouseButton.LeftButton, pos=destination)
    QTest.qWait(50)

    layer_after = widget.bridge.layersModel.get(1)
    background_after = widget.bridge.background
    assert layer_after["x"] == layer_before["x"]
    assert layer_after["y"] == layer_before["y"]
    assert float(background_after["norm_x"]) > float(background_before["norm_x"])
    assert float(background_after["norm_y"]) > float(background_before["norm_y"])
    assert len(widget.bridge._undo) == undo_count + 1
    widget.bridge.undo()
    assert widget.bridge.background["norm_x"] == background_before["norm_x"]
    assert widget.bridge.background["norm_y"] == background_before["norm_y"]
    widget.bridge.removeBackgroundImage()
    assert _wait_until(lambda: workspace.property("adjustBackground") is False)
    widget.cleanup()
    widget.close()


def test_responsive_navigation_uses_an_animated_drawer_and_single_mobile_tabs(
    editor,
) -> None:
    widget = editor
    show_and_activate(widget, size=QSize(1000, 760))
    assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
    root = widget._qml.rootObject()
    assert root is not None
    workspace = root.findChild(QObject, "talkThemeWorkspace")
    drawer = root.findChild(QObject, "talkThemeDrawer")
    scrim = root.findChild(QObject, "talkThemeDrawerScrim")
    toggle = root.findChild(QObject, "talkThemeDrawerToggle")
    toggle_icon = root.findChild(QObject, "talkThemeDrawerToggleIcon")
    mobile_navigation = root.findChild(QObject, "talkThemeMobileNavigation")
    assert isinstance(workspace, QQuickItem)
    assert isinstance(drawer, QQuickItem)
    assert isinstance(scrim, QQuickItem)
    assert isinstance(toggle, QQuickItem)
    assert isinstance(toggle_icon, QQuickItem)
    assert isinstance(mobile_navigation, QQuickItem)
    outside_toggle = QPoint(widget._qml.width() - 20, widget._qml.height() - 20)
    mouse_move(widget._qml, outside_toggle)
    wait_until(lambda: drawer.x() < -drawer.width(), description="closed navigation drawer")
    wait_until(
        lambda: QColor(toggle.property("color")).alpha() == 0,
        description="closed navigation toggle without hover",
    )
    closed_x = drawer.x()
    closed_toggle_color = QColor(toggle.property("color"))
    assert closed_x < -drawer.width()
    assert not mobile_navigation.isVisible()
    assert closed_toggle_color.alpha() == 0
    assert "panel-left" in str(toggle_icon.property("source"))

    toggle_center = toggle.mapToScene(QPointF(toggle.width() / 2, toggle.height() / 2)).toPoint()
    mouse_move(widget._qml, toggle_center)
    wait_until(
        lambda: QColor(toggle.property("color")).alpha() == 255,
        description="closed navigation toggle hover color",
    )
    assert workspace.property("drawerOpen") is False
    mouse_move(widget._qml, outside_toggle)
    wait_until(
        lambda: QColor(toggle.property("color")).alpha() == 0,
        description="navigation toggle hover exit",
    )

    workspace.setProperty("drawerOpen", True)
    assert _wait_until(lambda: closed_x < drawer.x() < -1, timeout=0.16)
    assert toggle.property("open") is True
    assert _wait_until(lambda: QColor(toggle.property("color")).alpha() > 0)
    assert "panel-left" in str(toggle_icon.property("source"))
    assert _wait_until(lambda: abs(drawer.x()) < 0.5)
    assert abs(scrim.opacity() - 0.35) < 0.02

    QTest.keyClick(widget._qml, Qt.Key.Key_Escape)
    assert _wait_until(lambda: workspace.property("drawerOpen") is False)
    assert _wait_until(lambda: drawer.x() < -drawer.width())
    workspace.setProperty("drawerOpen", True)
    assert _wait_until(lambda: abs(drawer.x()) < 0.5)

    widget.resize(700, 760)
    assert _wait_until(lambda: workspace.property("narrow") is True)
    assert workspace.property("drawerOpen") is False
    workspace.setProperty("mobilePage", "edit")
    workspace.setProperty("mobileSection", "styles")
    _APP.processEvents()
    assert mobile_navigation.isVisible()
    visible_sidebars = [
        item
        for item in root.findChildren(QObject, "talkThemeSidebar")
        if isinstance(item, QQuickItem) and item.isVisible()
    ]
    assert len(visible_sidebars) == 1
    mobile_sidebar = visible_sidebars[0]
    assert mobile_sidebar.property("navigationVisible") is False
    sidebar_navigation = mobile_sidebar.findChild(
        QObject,
        "talkThemeSidebarNavigation",
    )
    assert isinstance(sidebar_navigation, QQuickItem)
    assert not sidebar_navigation.isVisible()

    workspace.setProperty("mobileSection", "text")
    _APP.processEvents()
    visible_inspectors = [
        item
        for item in root.findChildren(QObject, "talkThemeInspector")
        if isinstance(item, QQuickItem) and item.isVisible()
    ]
    assert len(visible_inspectors) == 1
    mobile_inspector = visible_inspectors[0]
    assert mobile_inspector.property("navigationVisible") is False
    inspector_navigation = mobile_inspector.findChild(
        QObject,
        "talkThemeInspectorNavigation",
    )
    assert isinstance(inspector_navigation, QQuickItem)
    assert not inspector_navigation.isVisible()


def test_editor_cleanup_does_not_leave_qml_lifecycle_errors(tmp_path) -> None:
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    widget: TalkThemeEditorWidget | None = None
    try:
        session = ProjectionSession()
        widget = TalkThemeEditorWidget(
            None,
            profile_paths=_profile_paths(tmp_path),
            notifications=_Notifications(),
            projection_session=session,
            settings=_CustomColorSettings(),
            output_settings=_OutputSettings(),
        )
        widget.resize(1200, 760)
        widget.show()
        assert _wait_until(lambda: widget._qml.status() is QQuickWidget.Status.Ready)
        widget.cleanup()
        _APP.processEvents()
    finally:
        qInstallMessageHandler(previous)
        if widget is not None:
            widget.close()

    lifecycle_errors = [
        message
        for message in messages
        if any(
            marker in message
            for marker in (
                "ReferenceError",
                "TypeError",
                "Binding loop",
                "talkTheme is null",
                "talkTheme == null",
            )
        )
    ]
    assert lifecycle_errors == []
