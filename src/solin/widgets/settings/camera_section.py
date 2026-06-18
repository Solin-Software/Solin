from __future__ import annotations

from ...styles.icons import ICON_CAMERA


class CameraSectionMixin:
    """Builds and manages live camera integration settings."""

    def _build_camera_card(self):
        card, lay = self._card()
        saved_camera = self._camera_settings.is_enabled()
        row, self._camera_toggle, self._camera_label, self._camera_desc = \
            self._toggle_row(
                ICON_CAMERA,
                self.tr("Camera"),
                self.tr("Shows a camera button in the live tools toolbar."),
                checked=saved_camera,
            )
        self._camera_toggle.toggled.connect(self._on_camera_toggled)
        lay.addWidget(row)
        return card

    def _on_camera_toggled(self, checked):
        self._camera_settings.set_enabled(checked)
        self.camera_enabled_toggled.emit(checked)

    def get_camera_enabled(self):
        return self._camera_settings.is_enabled()
