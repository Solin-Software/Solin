"""Application bridge for the layered talk-theme editor."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    Property,
    QRunnable,
    Signal,
    Slot,
    QThreadPool,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import QDialog, QFileDialog, QWidget

from solin.core.projection.result import ProjectionResult, ProjectionResultStatus
from solin.core.talk_theme.models import (
    MAX_BACKGROUND_BLUR,
    MAX_BACKGROUND_OVERLAY_OPACITY,
    MAX_TEXT_LAYERS,
    MAX_USER_PRESETS,
    PROMPT_ON_UNSAVED_CHANGES,
    Background,
    TalkThemeLibrary,
    TalkThemePreset,
    TextLayer,
    ThemeDocument,
    new_identity,
    utc_now_iso,
)
from solin.core.talk_theme.presets import (
    DEFAULT_CONGREGATION_TEXT,
    DEFAULT_SPEAKER_TEXT,
    DEFAULT_TALK_TITLE,
    builtin_presets,
)
from solin.core.talk_theme.render_target import (
    ThemeRenderTarget,
    resolve_theme_render_target,
)
from solin.core.talk_theme.repository import (
    TalkThemeAssetError,
    TalkThemeAssetErrorCode,
    TalkThemeAssetStore,
    TalkThemeRepository,
)
from solin.core.talk_theme.settings import CustomColorSettings, OutputAspectSettings
from solin.core.talk_theme.snapping import LayerGeometry, SnapGuide, snap_layer
from solin.ui.dialogs.talk_theme_color import TalkThemeColorDialog
from solin.ui.qml.talk_theme_layers import TextLayerListModel


_MAX_UNDO_STATES = 100
_TEXT_EDIT_COALESCE_SECONDS = 0.8
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class _AssetImportSignals(QObject):
    succeeded = Signal(str)
    failed = Signal(str)


class _AssetImportTask(QRunnable):
    def __init__(self, store: TalkThemeAssetStore, source: str) -> None:
        super().__init__()
        self.signals = _AssetImportSignals()
        self._store = store
        self._source = source

    def run(self) -> None:
        try:
            target = self._store.import_image(self._source)
        except TalkThemeAssetError as error:
            self.signals.failed.emit(error.code.value)
            return
        except Exception:  # noqa: BLE001 - worker boundary
            self.signals.failed.emit("import_failed")
            return
        self.signals.succeeded.emit(str(target))


def _copy_document(document: ThemeDocument) -> ThemeDocument:
    return ThemeDocument.from_record(document.to_record(), fallback=document)


def _visual_record(document: ThemeDocument) -> dict[str, object]:
    record = document.to_record()
    record.pop("updated_at", None)
    return record


class TalkThemeBridge(QObject):
    """Own the transient editor state and explicit preset persistence."""

    changed = Signal()
    projectionRequested = Signal()
    exportRequested = Signal()
    editLayerRequested = Signal(str)
    saveAsRequested = Signal()
    unsavedChangesRequested = Signal(str)
    pointerEntered = Signal(str, int)
    pointerExited = Signal(str)

    def __init__(
        self,
        *,
        repository: TalkThemeRepository,
        asset_store: TalkThemeAssetStore,
        builtin_asset_dir: Path,
        notifications: Any,
        projection_session: Any,
        projection_windows: Callable[[], list[Any]],
        settings: CustomColorSettings,
        output_settings: OutputAspectSettings,
        prompt_on_unsaved_changes: bool = PROMPT_ON_UNSAVED_CHANGES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._asset_store = asset_store
        self._builtin_asset_dir = builtin_asset_dir
        self._notifications = notifications
        self._projection_session = projection_session
        self._projection_windows = projection_windows
        self._settings = settings
        self._output_settings = output_settings
        self._follow_output_aspect = output_settings.follow_output_aspect()
        self._prompt_on_unsaved_changes = prompt_on_unsaved_changes
        self._builtins = builtin_presets()
        self._library = repository.load()
        self._document = (
            self._localized_builtin_document(self._library.document)
            if not self._library.last_saved_preset_id
            and self._library.document == self._builtins[0].document
            else self._library.document
        )
        self._baseline_document = self._document
        self._active_preset_id = self._initial_preset_id()
        self._selected_layer_id = ""
        self._layers_model = TextLayerListModel(self._document.layers, self)
        self._undo: list[ThemeDocument] = []
        self._redo: list[ThemeDocument] = []
        self._gesture_before: ThemeDocument | None = None
        self._background_gesture_before: ThemeDocument | None = None
        self._snap_guides: tuple[SnapGuide, ...] = ()
        self._rendered_layer_heights: dict[str, float] = {}
        self._pending_text_center_y: dict[str, float] = {}
        self._pending_preset_id = ""
        self._rendering = False
        self._exporting = False
        self._importing = False
        self._status_message = ""
        self._last_edit_key = ""
        self._last_edit_at = 0.0
        self._import_task: _AssetImportTask | None = None
        self._closed = False

        self._guide_timer = QTimer(self)
        self._guide_timer.setSingleShot(True)
        self._guide_timer.setInterval(600)
        self._guide_timer.timeout.connect(self._clear_guides)
        self._unsubscribe_projection = projection_session.subscribe(self._on_projection_changed)

    @Property(QObject, constant=True)
    def layersModel(self) -> TextLayerListModel:
        return self._layers_model

    @Property(int, notify=changed)
    def layerCount(self) -> int:
        return len(self._document.layers)

    @Property(str, notify=changed)
    def selectedLayerId(self) -> str:
        return self._selected_layer_id

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def selectedLayer(self) -> dict[str, object]:
        layer = self._layer(self._selected_layer_id)
        return layer.to_record() if layer is not None else {}

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def background(self) -> dict[str, object]:
        return self._background_record(self._document.background)

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def builtinPresets(self) -> list[dict[str, object]]:
        return [self._preset_record(preset, builtin=True) for preset in self._builtins]

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def userPresets(self) -> list[dict[str, object]]:
        return [self._preset_record(preset, builtin=False) for preset in self._library.user_presets]

    @Property(str, notify=changed)
    def activePresetId(self) -> str:
        return self._active_preset_id

    @Property(bool, notify=changed)
    def activePresetIsBuiltin(self) -> bool:
        return any(preset.id == self._active_preset_id for preset in self._builtins)

    @Property(bool, notify=changed)
    def dirty(self) -> bool:
        return _visual_record(self._document) != _visual_record(self._baseline_document)

    @Property(bool, notify=changed)
    def canSave(self) -> bool:
        return self.dirty and self._user_preset(self._active_preset_id) is not None

    @Property(bool, notify=changed)
    def canAddLayer(self) -> bool:
        return len(self._document.layers) < MAX_TEXT_LAYERS

    @Property(bool, constant=True)
    def promptOnUnsavedChanges(self) -> bool:
        return self._prompt_on_unsaved_changes

    @Property(float, constant=True)
    def backgroundOverlayMaximum(self) -> float:
        return MAX_BACKGROUND_OVERLAY_OPACITY

    @Property(float, constant=True)
    def backgroundBlurMaximum(self) -> float:
        return MAX_BACKGROUND_BLUR

    @Property("QVariantList", notify=changed)  # type: ignore[arg-type]
    def snapGuides(self) -> list[dict[str, object]]:
        return [self._guide_record(guide) for guide in self._snap_guides]

    @Property("QStringList", constant=True)  # type: ignore[arg-type]
    def fontFamilies(self) -> list[str]:
        installed = list(QFontDatabase.families())
        preferred = ["Lato", "Arial", "Georgia", "Segoe UI", "Verdana"]
        return preferred + [family for family in installed if family not in preferred]

    @Property(bool, notify=changed)
    def canUndo(self) -> bool:
        return bool(self._undo)

    @Property(bool, notify=changed)
    def canRedo(self) -> bool:
        return bool(self._redo)

    @Property(bool, notify=changed)
    def followOutputAspect(self) -> bool:
        return self._follow_output_aspect

    @Property(float, notify=changed)
    def renderAspectRatio(self) -> float:
        return self.render_target().aspect_ratio

    @Property(int, notify=changed)
    def renderPixelHeight(self) -> int:
        return self.render_target().height

    @Property(str, notify=changed)
    def renderTargetLabel(self) -> str:
        target = self.render_target()
        prefix = "Auto · " if self._follow_output_aspect else ""
        return f"{prefix}{target.label}"

    @Property(bool, notify=changed)
    def rendering(self) -> bool:
        return self._rendering

    @Property(bool, notify=changed)
    def exporting(self) -> bool:
        return self._exporting

    @Property(bool, notify=changed)
    def importing(self) -> bool:
        return self._importing

    @Property(bool, notify=changed)
    def canRender(self) -> bool:
        return not (self._rendering or self._exporting or self._importing)

    @Property(str, notify=changed)
    def projectionStatus(self) -> str:
        state = self._projection_session.state
        if state.get("type") == "image" and state.get("generated_kind") == "talk_theme":
            return "projected" if state.get("fingerprint") == self.fingerprint() else "outdated"
        return "ready"

    @Property(str, notify=changed)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(str, notify=changed)
    def outputTitle(self) -> str:
        return self.output_title()

    def output_title(self) -> str:
        return next(
            (
                layer.text.strip()
                for layer in self._document.layers
                if layer.visible and layer.text.strip()
            ),
            self.tr("Talk theme"),
        )

    @Property(str, notify=changed)
    def suggestedExportName(self) -> str:
        return self.suggested_export_name()

    def suggested_export_name(self) -> str:
        stem = _INVALID_FILENAME.sub("-", self.output_title()).strip(" .-")
        stem = re.sub(r"\s+", " ", stem)[:80].rstrip(" .-")
        return f"{stem or self.tr('talk-theme')}.png"

    def render_target(self) -> ThemeRenderTarget:
        return resolve_theme_render_target(
            self._projection_windows(),
            follow_output=self._follow_output_aspect,
        )

    def fingerprint(self, target: ThemeRenderTarget | None = None) -> str:
        target = target or self.render_target()
        payload = {
            "document": _visual_record(self._document),
            "target": [target.width, target.height],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @Slot(str)
    def selectLayer(self, layer_id: str) -> None:
        normalized = layer_id if self._layer(layer_id) is not None else ""
        if normalized == self._selected_layer_id:
            if normalized:
                self._restore_selected_guides()
                self.changed.emit()
            return
        self._selected_layer_id = normalized
        self._restore_selected_guides()
        self.changed.emit()

    @Slot(result=str)
    def addTextLayer(self) -> str:
        if len(self._document.layers) >= MAX_TEXT_LAYERS:
            self._fail(self.tr("The maximum number of text layers has been reached."))
            return ""
        number = self._next_layer_number()
        layer = TextLayer(
            id=new_identity(),
            name=self.tr("Text %1").replace("%1", str(number)),
            text="",
            x=0.25,
            y=0.44,
            width=0.50,
            font_size=0.060,
            font_family="Arial",
            alignment="center",
            snap_x="center",
            snap_y="center",
        )
        self._replace_document(replace(self._document, layers=(layer, *self._document.layers)))
        self._selected_layer_id = layer.id
        self.changed.emit()
        self.editLayerRequested.emit(layer.id)
        return layer.id

    @Slot(str, str)
    def renameLayer(self, layer_id: str, name: str) -> None:
        layer = self._layer(layer_id)
        normalized = name.strip()[:120]
        if layer is None or not normalized or normalized == layer.name:
            return
        self._set_layer(replace(layer, name=normalized))

    @Slot(str)
    def toggleLayerVisibility(self, layer_id: str) -> None:
        layer = self._layer(layer_id)
        if layer is not None:
            self._set_layer(replace(layer, visible=not layer.visible))

    @Slot(str)
    def deleteLayer(self, layer_id: str) -> None:
        row = self._layers_model.row_for_id(layer_id)
        if row < 0:
            return
        was_selected = self._selected_layer_id == layer_id
        remaining = tuple(layer for layer in self._document.layers if layer.id != layer_id)
        cleaned = tuple(self._clear_layer_snap_references(layer, layer_id) for layer in remaining)
        self._replace_document(replace(self._document, layers=cleaned))
        if was_selected:
            neighbor = min(row, len(cleaned) - 1)
            self._selected_layer_id = cleaned[neighbor].id if neighbor >= 0 else ""
        self._restore_selected_guides()
        self.changed.emit()

    @Slot(str, int)
    def reorderLayer(self, layer_id: str, target_row: int) -> None:
        source_row = self._layers_model.row_for_id(layer_id)
        target_row = max(0, min(int(target_row), len(self._document.layers) - 1))
        if source_row < 0 or source_row == target_row:
            return
        before = self._document
        layers = list(self._document.layers)
        layer = layers.pop(source_row)
        layers.insert(target_row, layer)
        updated = replace(
            self._document,
            layers=tuple(layers),
            updated_at=utc_now_iso(),
        )
        self._push_undo(before)
        self._redo.clear()
        self._document = updated
        self._layers_model.move_layer(source_row, target_row)
        self._status_message = ""
        self.changed.emit()

    @Slot(str, str)
    def setLayerText(self, layer_id: str, text: str) -> None:
        layer = self._layer(layer_id)
        value = text[:4000]
        if layer is None or value == layer.text:
            return
        self._pending_text_center_y.setdefault(
            layer_id,
            layer.y + self._layer_geometry_height(layer) / 2,
        )
        self._set_layer(
            replace(layer, text=value),
            coalesce_key=f"text:{layer_id}",
        )

    @Slot(str, str)
    @Slot(str, str, float)
    def commitInlineText(
        self,
        layer_id: str,
        text: str,
        y: float | None = None,
    ) -> None:
        layer = self._layer(layer_id)
        value = text[:4000]
        if layer is None:
            return
        self._pending_text_center_y.pop(layer_id, None)
        next_y = layer.y
        if y is not None and isfinite(y):
            next_y = max(0.0, min(0.98, y))
        if value == layer.text and next_y == layer.y:
            return
        self._set_layer(
            replace(
                layer,
                text=value,
                y=next_y,
                snap_y="" if next_y != layer.y else layer.snap_y,
            ),
            record_undo=True,
        )

    @Slot(str, str, "QVariant")
    def setLayerProperty(self, layer_id: str, key: str, value: Any) -> None:
        layer = self._layer(layer_id)
        if layer is None:
            return
        updates: dict[str, Any] = {}
        if key in {"font_family", "font_weight", "color", "alignment"}:
            updates[key] = str(value)
        elif key in {"font_size", "width", "letter_spacing", "line_height"}:
            try:
                updates[key] = float(value)
            except (TypeError, ValueError):
                return
        elif key == "visible":
            updates[key] = bool(value)
        else:
            return
        candidate = TextLayer.from_record({**layer.to_record(), **updates})
        self._set_layer(candidate)

    @Slot(str, float, float)
    def nudgeLayer(self, layer_id: str, dx: float, dy: float) -> None:
        layer = self._layer(layer_id)
        if layer is None:
            return
        self._set_layer(
            replace(
                layer,
                x=max(-layer.width + 0.02, min(0.98, layer.x + dx)),
                y=max(-0.2, min(0.98, layer.y + dy)),
                snap_x="",
                snap_y="",
            )
        )

    @Slot(str)
    def beginLayerMove(self, layer_id: str) -> None:
        if self._layer(layer_id) is None:
            return
        self._pending_text_center_y.pop(layer_id, None)
        self.selectLayer(layer_id)
        self._gesture_before = self._document
        self._guide_timer.stop()

    @Slot(
        str,
        float,
        float,
        float,
        float,
        float,
        float,
        str,
        str,
        bool,
        str,
        float,
        float,
        result="QVariantMap",
    )
    def moveLayer(
        self,
        layer_id: str,
        x: float,
        y: float,
        width: float,
        height: float,
        viewport_width: float,
        viewport_height: float,
        active_snap_x: str,
        active_snap_y: str,
        disable_snap: bool,
        lock_axis: str,
        origin_x: float,
        origin_y: float,
    ) -> dict[str, object]:
        layer = self._layer(layer_id)
        if layer is None:
            return {}
        others = tuple(
            LayerGeometry(
                other.id,
                other.x,
                other.y,
                other.width,
                self._layer_geometry_height(other),
            )
            for other in self._document.layers
            if other.id != layer_id and other.visible
        )
        result = snap_layer(
            LayerGeometry(layer_id, x, y, width, height),
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            others=others,
            active_x=active_snap_x,
            active_y=active_snap_y,
            disabled=disable_snap,
            lock_axis=lock_axis,
            origin_x=origin_x,
            origin_y=origin_y,
        )
        self._snap_guides = result.guides
        self.changed.emit()
        return {
            "x": result.x,
            "y": result.y,
            "snap_x": result.snap_x,
            "snap_y": result.snap_y,
            "guides": [self._guide_record(guide) for guide in result.guides],
        }

    @Slot(str, float, float, str, str)
    def endLayerMove(
        self,
        layer_id: str,
        x: float,
        y: float,
        snap_x: str,
        snap_y: str,
    ) -> None:
        layer = self._layer(layer_id)
        if layer is None:
            self._gesture_before = None
            return
        before = self._gesture_before
        self._gesture_before = None
        self._set_layer(
            replace(layer, x=x, y=y, snap_x=snap_x, snap_y=snap_y),
            record_undo=False,
        )
        if before is not None and before != self._document:
            self._push_undo(before)
            self._redo.clear()
        self._restore_selected_guides()
        self.changed.emit()

    @Slot()
    def cancelLayerMove(self) -> None:
        self._gesture_before = None
        self._clear_guides()

    @Slot(str)
    def beginLayerResize(self, layer_id: str) -> None:
        self.beginLayerMove(layer_id)

    @Slot(str, float, float, float, float, float)
    def endLayerResize(
        self,
        layer_id: str,
        x: float,
        y: float,
        width: float,
        font_size: float,
        content_height: float,
    ) -> None:
        layer = self._layer(layer_id)
        if layer is None:
            self._gesture_before = None
            return
        bounded_width = max(0.08, min(1.0, width))
        bounded_font_size = max(0.012, min(0.30, font_size))
        bounded_content_height = max(0.02, min(1.0, content_height))
        bounded_x = max(0.0, min(1.0 - bounded_width, x))
        bounded_y = max(0.0, min(1.0 - bounded_content_height, y))
        before = self._gesture_before
        self._gesture_before = None
        self._set_layer(
            replace(
                layer,
                x=bounded_x,
                y=bounded_y,
                width=bounded_width,
                font_size=bounded_font_size,
                snap_x="",
                snap_y="",
            ),
            record_undo=False,
        )
        if before is not None and before != self._document:
            self._push_undo(before)
            self._redo.clear()
        self._rendered_layer_heights[layer_id] = bounded_content_height
        self._clear_guides()
        self.changed.emit()

    @Slot()
    def cancelLayerResize(self) -> None:
        self.cancelLayerMove()

    @Slot(str, float)
    def setLayerRenderedHeight(self, layer_id: str, height: float) -> None:
        layer = self._layer(layer_id)
        if layer is None:
            return
        bounded_height = max(0.02, min(1.0, height))
        self._rendered_layer_heights[layer_id] = bounded_height
        center_y = self._pending_text_center_y.pop(layer_id, None)
        if center_y is None:
            return
        next_y = max(
            0.0,
            min(max(0.0, 1.0 - bounded_height), center_y - bounded_height / 2),
        )
        if next_y == layer.y:
            return
        self._assign_layer_without_history(
            replace(layer, y=next_y, snap_y=""),
        )

    @Slot(str)
    def requestPreset(self, preset_id: str) -> None:
        if (
            self._preset(preset_id) is None
            or preset_id == self._active_preset_id
            and not self.dirty
        ):
            return
        if self.dirty and self._prompt_on_unsaved_changes:
            self._pending_preset_id = preset_id
            self.unsavedChangesRequested.emit(preset_id)
            return
        self._apply_preset(preset_id)

    @Slot(str)
    def resolveUnsavedPreset(self, action: str) -> None:
        pending = self._pending_preset_id
        if not pending:
            return
        if action == "cancel":
            self._pending_preset_id = ""
            return
        if action == "discard":
            self._pending_preset_id = ""
            self._apply_preset(pending)
            return
        if action != "save":
            return
        if self._user_preset(self._active_preset_id) is not None:
            if self.save_current_preset():
                self._pending_preset_id = ""
                self._apply_preset(pending)
        else:
            self.saveAsRequested.emit()

    @Slot()
    def cancelPendingPreset(self) -> None:
        self._pending_preset_id = ""

    @Slot(result=bool)
    def saveCurrentPreset(self) -> bool:
        return self.save_current_preset()

    def save_current_preset(self) -> bool:
        current = self._user_preset(self._active_preset_id)
        if current is None:
            self.saveAsRequested.emit()
            return False
        now = utc_now_iso()
        updated = replace(
            current,
            document=_copy_document(self._document),
            updated_at=now,
        )
        presets = tuple(
            updated if preset.id == current.id else preset for preset in self._library.user_presets
        )
        candidate = replace(
            self._library,
            document=self._document,
            user_presets=presets,
            last_saved_preset_id=current.id,
        )
        if not self._persist_library(candidate):
            return False
        self._baseline_document = self._document
        self._status_message = ""
        self.changed.emit()
        return True

    def can_update_active_preset(self) -> bool:
        return self._user_preset(self._active_preset_id) is not None

    @Slot(str, result=bool)
    def saveAsPreset(self, name: str) -> bool:
        normalized = name.strip()[:120]
        if not normalized:
            self._status_message = self.tr("Enter a name for the preset.")
            self.changed.emit()
            return False
        if len(self._library.user_presets) >= MAX_USER_PRESETS:
            self._status_message = self.tr("The maximum number of presets has been reached.")
            self.changed.emit()
            return False
        names = {
            preset.name.casefold() for preset in (*self._builtins, *self._library.user_presets)
        }
        if normalized.casefold() in names:
            self._status_message = self.tr("A preset with this name already exists.")
            self.changed.emit()
            return False
        now = utc_now_iso()
        preset = TalkThemePreset(
            id=new_identity(),
            name=normalized,
            document=_copy_document(self._document),
            created_at=now,
            updated_at=now,
        )
        candidate = replace(
            self._library,
            document=self._document,
            user_presets=(*self._library.user_presets, preset),
            last_saved_preset_id=preset.id,
        )
        if not self._persist_library(candidate):
            return False
        self._active_preset_id = preset.id
        self._baseline_document = self._document
        self._status_message = ""
        pending = self._pending_preset_id
        self._pending_preset_id = ""
        self.changed.emit()
        if pending:
            self._apply_preset(pending)
        return True

    @Slot(str)
    def deletePreset(self, preset_id: str) -> None:
        presets = tuple(preset for preset in self._library.user_presets if preset.id != preset_id)
        if len(presets) == len(self._library.user_presets):
            return
        last_saved = (
            ""
            if self._library.last_saved_preset_id == preset_id
            else self._library.last_saved_preset_id
        )
        candidate = replace(
            self._library,
            user_presets=presets,
            last_saved_preset_id=last_saved,
        )
        if not self._persist_library(candidate):
            return
        if self._active_preset_id == preset_id:
            self._apply_preset(self._builtins[0].id)
        else:
            self.changed.emit()

    @Slot()
    def chooseBackground(self) -> None:
        owner = self.parent()
        dialog_parent = owner if isinstance(owner, QWidget) else None
        source, _ = QFileDialog.getOpenFileName(
            dialog_parent,
            self.tr("Choose image"),
            "",
            self.tr("Images (*.png *.jpg *.jpeg *.webp)"),
        )
        if not source:
            return
        self._importing = True
        self._status_message = self.tr("Importing image…")
        self.changed.emit()
        task = _AssetImportTask(self._asset_store, source)
        task.signals.succeeded.connect(self._on_asset_imported)
        task.signals.failed.connect(self._on_asset_failed)
        self._import_task = task
        QThreadPool.globalInstance().start(task)

    @Slot()
    def chooseTextColor(self) -> None:
        layer = self._layer(self._selected_layer_id)
        if layer is None:
            return
        selected = self._choose_color(
            QColor(layer.color),
            self.tr("Text color"),
            show_alpha=True,
        )
        if selected is not None:
            self._set_layer(replace(layer, color=selected.name(QColor.NameFormat.HexArgb).upper()))

    @Slot()
    def chooseBackgroundColor(self) -> None:
        selected = self._choose_color(
            QColor(self._document.background.base_color),
            self.tr("Background color"),
            show_alpha=False,
        )
        if selected is not None:
            self._set_background(
                replace(
                    self._document.background,
                    base_color=selected.name(QColor.NameFormat.HexRgb).upper(),
                )
            )

    @Slot()
    def removeBackgroundImage(self) -> None:
        background = self._document.background
        if not background.source:
            return
        self._set_background(
            replace(
                background,
                kind="none",
                source="",
                fill_mode="cover",
                overlay_opacity=0.0,
                blur=0.0,
                zoom=1.0,
                norm_x=0.0,
                norm_y=0.0,
            )
        )

    def _choose_color(
        self,
        initial: QColor,
        title: str,
        *,
        show_alpha: bool,
    ) -> QColor | None:
        owner = self.parent()
        dialog_parent = owner if isinstance(owner, QWidget) else None
        if not initial.isValid():
            initial = QColor("#ffffff")
        stored_colors = self._settings.custom_colors()
        dialog = TalkThemeColorDialog(
            initial,
            stored_colors,
            title,
            dialog_parent,
            show_alpha=show_alpha,
        )
        result = dialog.exec()
        current_colors = dialog.custom_colors()
        if current_colors != stored_colors:
            self._settings.set_custom_colors(current_colors)
        selected = dialog.selectedColor()
        if result != QDialog.DialogCode.Accepted or not selected.isValid():
            return None
        return selected

    @Slot()
    def resetBackground(self) -> None:
        self._set_background(self._baseline_document.background)

    @Slot(str, "QVariant")
    def setBackgroundProperty(self, key: str, value: Any) -> None:
        record = self._document.background.to_record()
        if key in {"base_color", "fill_mode", "overlay_color"}:
            record[key] = str(value)
        elif key in {"overlay_opacity", "blur", "zoom", "norm_x", "norm_y"}:
            try:
                record[key] = float(value)
            except (TypeError, ValueError):
                return
        else:
            return
        edit_key = f"background:{key}"
        self._set_background(
            Background.from_record(record),
            coalesce_key=edit_key,
        )

    @Slot()
    def beginBackgroundMove(self) -> None:
        self._background_gesture_before = self._document

    @Slot(float, float)
    def moveBackground(self, norm_x: float, norm_y: float) -> None:
        self._set_background(
            replace(
                self._document.background,
                norm_x=max(-1.0, min(1.0, norm_x)),
                norm_y=max(-1.0, min(1.0, norm_y)),
            ),
            record_undo=False,
        )

    @Slot()
    def endBackgroundMove(self) -> None:
        before = self._background_gesture_before
        self._background_gesture_before = None
        if before is not None and before != self._document:
            self._push_undo(before)
            self._redo.clear()
            self.changed.emit()

    @Slot()
    def cancelBackgroundMove(self) -> None:
        before = self._background_gesture_before
        self._background_gesture_before = None
        if before is not None and before != self._document:
            self._assign_document(before)

    @Slot(bool)
    def setFollowOutputAspect(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._follow_output_aspect:
            return
        self._output_settings.set_follow_output_aspect(enabled)
        self._follow_output_aspect = enabled
        self.changed.emit()

    @Slot()
    def undo(self) -> None:
        if not self._undo:
            return
        self._pending_text_center_y.clear()
        current = self._document
        previous = self._undo.pop()
        self._redo.append(current)
        self._assign_document(previous)

    @Slot()
    def redo(self) -> None:
        if not self._redo:
            return
        self._pending_text_center_y.clear()
        current = self._document
        following = self._redo.pop()
        self._push_undo(current)
        self._assign_document(following)

    @Slot()
    def requestProjection(self) -> None:
        if not self.canRender:
            return
        self._rendering = True
        self._status_message = self.tr("Preparing image…")
        self.changed.emit()
        self.projectionRequested.emit()

    @Slot()
    def requestExport(self) -> None:
        if not self.canRender:
            return
        self._exporting = True
        self._status_message = self.tr("Preparing image…")
        self.changed.emit()
        self.exportRequested.emit()

    @Slot(str, int)
    def beginPointer(self, cursor_source: str, cursor_shape: int) -> None:
        self.pointerEntered.emit(cursor_source, cursor_shape)

    @Slot(str)
    def endPointer(self, cursor_source: str) -> None:
        self.pointerExited.emit(cursor_source)

    def finish_projection(self, result: ProjectionResult) -> None:
        self._rendering = False
        if result.accepted:
            self._status_message = ""
        elif result.status is ProjectionResultStatus.BLOCKED:
            self._status_message = self.tr("Projection is currently protected.")
        else:
            self._status_message = result.message or self.tr(
                "The talk theme could not be projected."
            )
            self._notify_error(self._status_message)
        self.changed.emit()

    def finish_export(self) -> None:
        self._exporting = False
        self._status_message = ""
        self.changed.emit()

    def cancel_rendering(self, message: str, *, export: bool = False) -> None:
        if export:
            self._exporting = False
        else:
            self._rendering = False
        self._status_message = message
        self.changed.emit()
        self._notify_error(message)

    def cancel_export(self) -> None:
        self._exporting = False
        self._status_message = ""
        self.changed.emit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._guide_timer.stop()
        self._unsubscribe_projection()

    @Slot(str)
    def _on_asset_imported(self, target: str) -> None:
        self._import_task = None
        self._importing = False
        self._status_message = ""
        self._set_background(
            replace(
                self._document.background,
                kind="asset",
                source=Path(target).name,
                fill_mode="cover",
                overlay_color="#101936",
                overlay_opacity=0.28,
                blur=0.0,
                zoom=1.0,
                norm_x=0.0,
                norm_y=0.0,
            )
        )

    @Slot(str)
    def _on_asset_failed(self, error_code: str) -> None:
        self._import_task = None
        self._importing = False
        messages = {
            TalkThemeAssetErrorCode.UNSUPPORTED_FORMAT.value: self.tr(
                "This image format is not supported."
            ),
            TalkThemeAssetErrorCode.ANIMATED_IMAGE.value: self.tr(
                "Animated images are not supported."
            ),
            TalkThemeAssetErrorCode.OPEN_FAILED.value: self.tr(
                "The selected image could not be opened."
            ),
            TalkThemeAssetErrorCode.INVALID_DIMENSIONS.value: self.tr(
                "The selected image has invalid dimensions."
            ),
        }
        self._fail(
            messages.get(
                error_code,
                self.tr("The selected image could not be imported."),
            )
        )

    def _replace_document(
        self,
        document: ThemeDocument,
        *,
        record_undo: bool = True,
        force_reset: bool = False,
        coalesce_key: str = "",
    ) -> None:
        if document == self._document:
            return
        if coalesce_key:
            now = monotonic()
            record_undo = (
                coalesce_key != self._last_edit_key
                or now - self._last_edit_at > _TEXT_EDIT_COALESCE_SECONDS
            )
            self._last_edit_key = coalesce_key
            self._last_edit_at = now
        else:
            self._last_edit_key = ""
            self._last_edit_at = 0.0
        current = self._document
        if record_undo:
            self._push_undo(current)
            self._redo.clear()
        updated = replace(document, updated_at=utc_now_iso())
        self._assign_document(updated, force_reset=force_reset)

    def _assign_document(
        self,
        document: ThemeDocument,
        *,
        force_reset: bool = False,
    ) -> None:
        self._document = document
        layer_ids = {layer.id for layer in document.layers}
        self._rendered_layer_heights = {
            layer_id: height
            for layer_id, height in self._rendered_layer_heights.items()
            if layer_id in layer_ids
        }
        self._pending_text_center_y = {
            layer_id: center_y
            for layer_id, center_y in self._pending_text_center_y.items()
            if layer_id in layer_ids
        }
        self._layers_model.replace_layers(document.layers, force_reset=force_reset)
        if self._selected_layer_id and self._layer(self._selected_layer_id) is None:
            self._selected_layer_id = ""
        self._status_message = ""
        self.changed.emit()

    def _set_layer(
        self,
        updated: TextLayer,
        *,
        record_undo: bool = True,
        coalesce_key: str = "",
    ) -> None:
        layers = tuple(
            updated if layer.id == updated.id else layer for layer in self._document.layers
        )
        self._replace_document(
            replace(self._document, layers=layers),
            record_undo=record_undo,
            coalesce_key=coalesce_key,
        )

    def _assign_layer_without_history(self, updated: TextLayer) -> None:
        """Complete a live edit without splitting its undo transaction."""
        layers = tuple(
            updated if layer.id == updated.id else layer for layer in self._document.layers
        )
        self._assign_document(
            replace(
                self._document,
                layers=layers,
                updated_at=utc_now_iso(),
            )
        )

    def _set_background(
        self,
        background: Background,
        *,
        record_undo: bool = True,
        coalesce_key: str = "",
    ) -> None:
        self._replace_document(
            replace(self._document, background=background),
            record_undo=record_undo,
            coalesce_key=coalesce_key,
        )

    def _apply_preset(self, preset_id: str) -> None:
        preset = self._preset(preset_id)
        if preset is None:
            return
        document = _copy_document(preset.document)
        if any(builtin.id == preset.id for builtin in self._builtins):
            document = self._localized_builtin_document(document)
        self._undo.clear()
        self._redo.clear()
        self._gesture_before = None
        self._background_gesture_before = None
        self._last_edit_key = ""
        self._last_edit_at = 0.0
        self._rendered_layer_heights.clear()
        self._pending_text_center_y.clear()
        self._pending_preset_id = ""
        self._active_preset_id = preset.id
        self._baseline_document = document
        self._selected_layer_id = ""
        self._snap_guides = ()
        self._assign_document(document, force_reset=True)

    def _persist_library(self, candidate: TalkThemeLibrary) -> bool:
        try:
            self._repository.save(candidate)
        except OSError:
            self._fail(self.tr("The preset could not be saved."))
            return False
        self._library = candidate
        self._asset_store.prune_unreferenced(self._referenced_asset_names(candidate))
        return True

    def _push_undo(self, document: ThemeDocument) -> None:
        if self._undo and self._undo[-1] == document:
            return
        self._undo.append(document)
        if len(self._undo) > _MAX_UNDO_STATES:
            del self._undo[0]

    def _layer(self, layer_id: str) -> TextLayer | None:
        return self._layers_model.layer(layer_id)

    def _preset(self, preset_id: str) -> TalkThemePreset | None:
        return next(
            (
                preset
                for preset in (*self._builtins, *self._library.user_presets)
                if preset.id == preset_id
            ),
            None,
        )

    def _user_preset(self, preset_id: str) -> TalkThemePreset | None:
        return next(
            (preset for preset in self._library.user_presets if preset.id == preset_id),
            None,
        )

    def _initial_preset_id(self) -> str:
        if self._user_preset(self._library.last_saved_preset_id) is not None:
            return self._library.last_saved_preset_id
        botanical = self._localized_builtin_document(self._builtins[0].document)
        return self._builtins[0].id if self._document == botanical else ""

    def _localized_builtin_document(self, document: ThemeDocument) -> ThemeDocument:
        source_texts = {
            "label": "PUBLIC TALK",
            "title": DEFAULT_TALK_TITLE,
            "speaker": DEFAULT_SPEAKER_TEXT,
            "congregation": DEFAULT_CONGREGATION_TEXT,
        }
        localized_texts = {
            "label": QCoreApplication.translate("MeetingSections", "PUBLIC TALK"),
            "title": QCoreApplication.translate(
                "TalkThemePresets", "Imitate Jehovah's mercy"
            ),
            "speaker": QCoreApplication.translate("TalkThemePresets", "Name"),
            "congregation": QCoreApplication.translate(
                "TalkThemePresets", "Congregation"
            ),
        }
        source_names = {
            "label": "Public talk",
            "title": "Title",
            "speaker": "Speaker",
            "congregation": "Congregation",
        }
        localized_names = {
            "label": QCoreApplication.translate("TalkThemePresets", "Public talk"),
            "title": QCoreApplication.translate("TalkThemeEditorView", "Talk title"),
            "speaker": QCoreApplication.translate("TalkThemeEditorView", "Speaker"),
            "congregation": QCoreApplication.translate(
                "TalkThemeEditorView", "Congregation"
            ),
        }
        layers = tuple(
            replace(
                layer,
                text=localized_texts.get(layer.template_key, layer.text),
                name=(
                    localized_names.get(layer.template_key, layer.name)
                    if layer.name == source_names.get(layer.template_key)
                    else layer.name
                ),
            )
            if layer.text == source_texts.get(layer.template_key)
            else layer
            for layer in document.layers
        )
        return replace(document, layers=layers)

    def _preset_record(
        self,
        preset: TalkThemePreset,
        *,
        builtin: bool,
    ) -> dict[str, object]:
        document = (
            self._localized_builtin_document(preset.document)
            if builtin
            else preset.document
        )
        visible_layers = tuple(layer for layer in document.layers if layer.visible)
        preview_layer = next(
            (layer for layer in visible_layers if layer.template_key == "title"),
            visible_layers[0] if visible_layers else None,
        )
        return {
            "id": preset.id,
            "name": self._builtin_name(preset.id, preset.name) if builtin else preset.name,
            "builtin": builtin,
            "active": preset.id == self._active_preset_id,
            "background": self._background_record(document.background),
            "preview_layer": preview_layer.to_record() if preview_layer is not None else {},
            "layer_count": len(document.layers),
        }

    def _builtin_name(self, preset_id: str, fallback: str) -> str:
        names = {
            "botanical": self.tr("Botanical"),
            "classic-blue": self.tr("Classic blue"),
            "soft-photo": self.tr("Soft photographic"),
        }
        return names.get(preset_id, fallback)

    def _background_url(self, background: Background) -> str:
        if not background.source:
            return ""
        path = (
            self._asset_store.resolve(background.source)
            if background.kind == "asset"
            else self._builtin_asset_dir / Path(background.source).name
        )
        return QUrl.fromLocalFile(str(path)).toString() if path and path.is_file() else ""

    def _background_record(self, background: Background) -> dict[str, object]:
        record = background.to_record()
        record["url"] = self._background_url(background)
        record["has_image"] = bool(background.source)
        record["missing"] = bool(background.source and not record["url"])
        return record

    def _referenced_asset_names(self, library: TalkThemeLibrary) -> set[str]:
        documents = [preset.document for preset in library.user_presets]
        if self._document.background.kind == "asset":
            documents.append(self._document)
        return {
            document.background.source
            for document in documents
            if document.background.kind == "asset" and document.background.source
        }

    def _restore_selected_guides(self) -> None:
        self._guide_timer.stop()
        layer = self._layer(self._selected_layer_id)
        if layer is None:
            self._snap_guides = ()
            return
        guides = tuple(
            guide
            for axis, snap_id in (("x", layer.snap_x), ("y", layer.snap_y))
            if (guide := self._guide_for_snap(axis, snap_id)) is not None
        )
        self._snap_guides = guides
        if guides:
            self._guide_timer.start()

    def _guide_for_snap(self, axis: str, snap_id: str) -> SnapGuide | None:
        semantic: dict[str, float] = {
            "center": 0.5,
            "middle": 0.5,
            "third-left": 1 / 3,
            "third-right": 2 / 3,
            "third-top": 1 / 3,
            "third-bottom": 2 / 3,
            "safe-left": 0.05,
            "safe-right": 0.95,
            "safe-top": 0.05,
            "safe-bottom": 0.95,
        }
        if snap_id in semantic:
            return SnapGuide(axis, semantic[snap_id], snap_id)
        if ":" not in snap_id:
            return None
        peer_id, edge = snap_id.rsplit(":", 1)
        peer = self._layer(peer_id)
        if peer is None or not peer.visible:
            return None
        if axis == "x":
            positions = {
                "left": peer.x,
                "center": peer.x + peer.width / 2,
                "right": peer.x + peer.width,
            }
        else:
            height = self._layer_geometry_height(peer)
            positions = {
                "top": peer.y,
                "center": peer.y + height / 2,
                "middle": peer.y + height / 2,
                "bottom": peer.y + height,
            }
        position = positions.get(edge)
        return SnapGuide(axis, position, snap_id) if position is not None else None

    def _layer_geometry_height(self, layer: TextLayer) -> float:
        return self._rendered_layer_heights.get(
            layer.id,
            max(layer.font_size * layer.line_height, 0.02),
        )

    def _guide_record(self, guide: SnapGuide) -> dict[str, object]:
        if guide.id in {"center", "middle"}:
            label = self.tr("Center")
        elif guide.id.startswith("third-"):
            label = self.tr("Third")
        elif guide.id.startswith("safe-"):
            label = self.tr("Safe margin")
        else:
            label = ""
        return {
            "axis": guide.axis,
            "position": guide.position,
            "id": guide.id,
            "label": label,
        }

    def _next_layer_number(self) -> int:
        prefix = self.tr("Text %1").replace("%1", "").strip().casefold()
        used: set[int] = set()
        for layer in self._document.layers:
            name = layer.name.strip().casefold()
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix) :].strip()
            if suffix.isdigit():
                used.add(int(suffix))
        number = 1
        while number in used:
            number += 1
        return number

    @staticmethod
    def _clear_layer_snap_references(layer: TextLayer, deleted_id: str) -> TextLayer:
        prefix = f"{deleted_id}:"
        return replace(
            layer,
            snap_x="" if layer.snap_x.startswith(prefix) else layer.snap_x,
            snap_y="" if layer.snap_y.startswith(prefix) else layer.snap_y,
        )

    @Slot()
    def refresh_language(self) -> None:
        self.changed.emit()

    def _clear_guides(self) -> None:
        if not self._snap_guides:
            return
        self._snap_guides = ()
        self.changed.emit()

    def _on_projection_changed(self) -> None:
        self.changed.emit()

    def _fail(self, message: str) -> None:
        self._status_message = message
        self.changed.emit()
        self._notify_error(message)

    def _notify_error(self, message: str) -> None:
        callback = getattr(self._notifications, "error", None)
        if callable(callback):
            callback(message)


__all__ = ["TalkThemeBridge"]
