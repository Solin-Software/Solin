from __future__ import annotations

import sys

from ...styles.icons import ICON_CAMERA

#: Platforms with a virtual-camera sink (see vcam_provision). macOS has none yet,
#: so the option is hidden there rather than offered and silently ignored.
_VCAM_PLATFORMS = ("win32", "linux")


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

        if sys.platform.startswith(_VCAM_PLATFORMS):
            lay.addWidget(self._divider())
            (
                vcam_row,
                self._vcam_autostart_toggle,
                self._vcam_autostart_label,
                self._vcam_autostart_desc,
            ) = self._toggle_row(
                ICON_CAMERA,
                self.tr("Start the virtual camera automatically"),
                self.tr(
                    "Turns the Solin virtual camera on when Solin opens, so "
                    "meeting apps can select it straight away."
                ),
                checked=self._camera_settings.vcam_autostart(),
            )
            self._vcam_autostart_toggle.toggled.connect(self._on_vcam_autostart_toggled)
            lay.addWidget(vcam_row)
        return card

    def _on_camera_toggled(self, checked):
        self._camera_settings.set_enabled(checked)
        self.camera_enabled_toggled.emit(checked)

    def get_camera_enabled(self):
        return self._camera_settings.is_enabled()

    def _on_vcam_autostart_toggled(self, checked):
        # Persist first: the controller re-reads the setting when it starts.
        #
        # The signal also applies the change immediately, which is what makes
        # this a usable control rather than just a preference: turning it on IS
        # the manual "start the camera" action, and off is the only way to stop
        # it. Nothing else in the app can start or stop the virtual camera.
        self._camera_settings.set_vcam_autostart(checked)
        self.vcam_autostart_toggled.emit(bool(checked))

    def get_vcam_autostart(self):
        return self._camera_settings.vcam_autostart()
