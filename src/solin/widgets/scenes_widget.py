"""Scenes panel — configures how the virtual camera follows the projector.

Edits a :class:`VcamSceneConfig` (meeting mode + per-mode rule table + PiP
corner) and reports every change through an ``on_change`` callback, which the
shell wires to persistence + the live :class:`VcamDirector`. This is the
*virtual-camera* scene config, distinct from the OBS-remote "scenes".
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..core.media.vcam_model import (
    SCENE_PRESETS,
    ContentGroup,
    MeetingMode,
    PipCorner,
    VcamSceneConfig,
    preset_for_id,
    preset_id_for,
)

# Rows of the rule table, in display order.
_GROUP_ORDER = (
    ContentGroup.IDLE,
    ContentGroup.PICTURE,
    ContentGroup.VIDEO,
    ContentGroup.PROJECTED_CAMERA,
    ContentGroup.STREAM,
)


class ScenesWidget(QWidget):
    """Left-nav page: the virtual-camera follow-the-projector configuration."""

    def __init__(
        self,
        config: VcamSceneConfig,
        on_change: Callable[[VcamSceneConfig], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._on_change = on_change
        self._loading = False
        self._rule_combos: dict[ContentGroup, QComboBox] = {}
        self._build_ui()
        self._reload()

    # ── construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel(self.tr("Virtual Camera Scenes"))
        title.setObjectName("scenesTitle")
        subtitle = QLabel(
            self.tr(
                "Choose what the virtual camera sends to the meeting for each "
                "kind of projected content."
            )
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("scenesSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        # Meeting mode
        mode_row = QFormLayout()
        self._mode_combo = QComboBox()
        for mode in MeetingMode:
            self._mode_combo.addItem(self._mode_label(mode), mode)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addRow(self.tr("Meeting mode"), self._mode_combo)
        root.addLayout(mode_row)

        root.addWidget(self._divider())

        # Rule table: one row per content group
        rules_caption = QLabel(self.tr("When the projector shows…"))
        rules_caption.setObjectName("scenesSectionCaption")
        root.addWidget(rules_caption)
        rules = QFormLayout()
        rules.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for group in _GROUP_ORDER:
            combo = QComboBox()
            for preset_id, _comp in SCENE_PRESETS:
                combo.addItem(self._preset_label(preset_id), preset_id)
            combo.currentIndexChanged.connect(
                lambda _idx, g=group: self._on_rule_changed(g)
            )
            self._rule_combos[group] = combo
            rules.addRow(self._group_label(group), combo)
        root.addLayout(rules)

        root.addWidget(self._divider())

        # PiP placement
        pip_row = QFormLayout()
        self._corner_combo = QComboBox()
        for corner in PipCorner:
            self._corner_combo.addItem(self._corner_label(corner), corner)
        self._corner_combo.currentIndexChanged.connect(self._on_corner_changed)
        pip_row.addRow(self.tr("Camera picture-in-picture corner"), self._corner_combo)
        root.addLayout(pip_row)

        root.addStretch(1)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ── state sync ─────────────────────────────────────────────────────────

    def set_config(self, config: VcamSceneConfig) -> None:
        """Replace the shown config (e.g. after an external change) and refresh."""
        self._config = config
        self._reload()

    def _reload(self) -> None:
        """Populate every control from the current config (no callbacks)."""
        self._loading = True
        try:
            self._select_data(self._mode_combo, self._config.mode)
            self._reload_rules()
            self._select_data(self._corner_combo, self._config.pip_corner)
        finally:
            self._loading = False

    def _reload_rules(self) -> None:
        mode = self._config.mode
        for group, combo in self._rule_combos.items():
            preset_id = preset_id_for(self._config.rule(mode, group))
            self._select_data(combo, preset_id, block=True)

    @staticmethod
    def _select_data(combo: QComboBox, data: object, *, block: bool = False) -> None:
        index = combo.findData(data)
        if index < 0:
            return
        if block:
            was = combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(was)
        else:
            combo.setCurrentIndex(index)

    # ── edits ──────────────────────────────────────────────────────────────

    def _on_mode_changed(self) -> None:
        if self._loading:
            return
        mode = self._mode_combo.currentData()
        if mode is not None:
            self._config.mode = mode
            self._reload_rules()  # the table now reflects the new mode's rules
            self._emit()

    def _on_rule_changed(self, group: ContentGroup) -> None:
        if self._loading:
            return
        preset_id = self._rule_combos[group].currentData()
        if preset_id is not None:
            self._config.set_rule(self._config.mode, group, preset_for_id(preset_id))
            self._emit()

    def _on_corner_changed(self) -> None:
        if self._loading:
            return
        corner = self._corner_combo.currentData()
        if corner is not None:
            self._config.pip_corner = corner
            self._emit()

    def _emit(self) -> None:
        self._on_change(self._config)

    # ── labels (translatable) ──────────────────────────────────────────────

    def _mode_label(self, mode: MeetingMode) -> str:
        return {
            MeetingMode.REGULAR: self.tr("Regular"),
            MeetingMode.SIGN_LANGUAGE: self.tr("Sign language"),
        }[mode]

    def _group_label(self, group: ContentGroup) -> str:
        return {
            ContentGroup.IDLE: self.tr("Nothing / yeartext"),
            ContentGroup.PICTURE: self.tr("A picture"),
            ContentGroup.VIDEO: self.tr("A video"),
            ContentGroup.PROJECTED_CAMERA: self.tr("The camera"),
            ContentGroup.STREAM: self.tr("A stream (browser / OBS)"),
        }[group]

    def _preset_label(self, preset_id: str) -> str:
        return {
            "camera_full": self.tr("Camera, full screen"),
            "media_full": self.tr("Projected content, full screen"),
            "media_with_camera": self.tr("Content + camera (picture-in-picture)"),
            "logo_only": self.tr("Solin logo"),
        }.get(preset_id, preset_id)

    def _corner_label(self, corner: PipCorner) -> str:
        return {
            PipCorner.BOTTOM_RIGHT: self.tr("Bottom right"),
            PipCorner.BOTTOM_LEFT: self.tr("Bottom left"),
            PipCorner.TOP_RIGHT: self.tr("Top right"),
            PipCorner.TOP_LEFT: self.tr("Top left"),
        }[corner]

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate()
        super().changeEvent(event)

    def _retranslate(self) -> None:
        # Rebuild item texts in place (data/order unchanged) on a language switch.
        self._loading = True
        try:
            for i, mode in enumerate(MeetingMode):
                self._mode_combo.setItemText(i, self._mode_label(mode))
            for combo in self._rule_combos.values():
                for i, (preset_id, _c) in enumerate(SCENE_PRESETS):
                    combo.setItemText(i, self._preset_label(preset_id))
            for i, corner in enumerate(PipCorner):
                self._corner_combo.setItemText(i, self._corner_label(corner))
        finally:
            self._loading = False


__all__ = ["ScenesWidget"]
