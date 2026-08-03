from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from ..core.integrations.camera_options import CameraOption
from ..core.foundation.constants import MEMORIZE_PRE_MEDIA_SCENE

log = logging.getLogger(__name__)


class _VcamProvisionSink(QObject):
    """Marshals the off-thread provisioning result back onto the GUI thread."""

    done = Signal(bool)


@dataclass(frozen=True, slots=True)
class LiveIntegrationContext:
    """Stable services and presentation ports for live integrations."""

    projection_session: Any
    obs_scene_session: Any
    obs_settings: Any
    camera_settings: Any
    obs_service: Any
    ndi_service: Any
    camera_service: Any
    zoom_service: Any
    notifications: Any
    projection_bar: Any
    media_controller: Any
    quick_toolbar: Callable[[], Any | None]
    projection_windows: Callable[[], list[Any]]
    translate: Callable[[str], str]
    playback_protection: Any
    platform: str = sys.platform


@dataclass(frozen=True, slots=True)
class LiveIntegrationHandlers:
    """Shell actions invoked by live integration workflows."""

    stop_projection: Callable[[], None]
    stop_browser_tab_projection: Callable[[], None]
    update_projection_status: Callable[..., None]


class LiveIntegrationController:
    """Owns OBS, NDI, camera, Zoom, and quick-toolbar integration callbacks."""

    def __init__(
        self,
        context: LiveIntegrationContext,
        handlers: LiveIntegrationHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._session = context.projection_session
        self._camera_tap_connected = False  # program-preview tap wiring (obs camera)
        #: Alive only while a privileged vcam provisioning run is in flight; also
        #: the re-entrancy guard against a second elevation prompt.
        self._vcam_provision_sink = None

    @Slot(bool)
    def on_zoom_settings_enabled_toggled(self, enabled: bool) -> None:
        context = self._context
        if context.platform != "win32":
            return
        if enabled:
            context.zoom_service.start()
        else:
            context.zoom_service.stop()
            toolbar = context.quick_toolbar()
            if toolbar is not None:
                toolbar.set_zoom_connected(False)

    @Slot(bool)
    def on_zoom_settings_parts_toggled(self, show: bool) -> None:
        context = self._context
        if context.platform != "win32":
            return
        if show:
            context.zoom_service.start_participant_polling()
        else:
            context.zoom_service.stop_participant_polling()
            self.on_zoom_participants_updated(0, [])

    def on_zoom_connection_changed(self, connected: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_connected(connected)

    def on_zoom_participants_updated(self, count: int, names: list) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_participants(count, names)

    def on_zoom_sharing_state_changed(self, sharing: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_zoom_sharing(sharing)

    def on_zoom_share_error(self, message: str) -> None:
        context = self._context
        context.notifications.error(
            message,
            title=context.translate("Zoom sharing failed"),
            dedupe_key=f"zoom-share:{message}",
        )

    def on_obs_state_changed(self, _state, _message: str) -> None:
        self.refresh_obs_btn_availability()
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_connected(self._context.obs_service.is_connected)

    def on_obs_scene_changed(self, scene_name: str) -> None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_current_scene(scene_name)
        media_scene = context.obs_settings.media_window_scene()
        if not media_scene or media_scene.startswith("—"):
            return
        context.projection_bar.set_obs_scene_is_media(scene_name == media_scene)

    def on_obs_scenes_updated(self, scenes: list) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_scenes(scenes)

    def refresh_obs_btn_availability(self) -> None:
        context = self._context
        connected = context.obs_service.is_connected
        available = connected and context.obs_settings.has_media_window_scene()
        context.projection_bar.set_obs_btn_available(available)

    def refresh_obs_stream_availability(self) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is None:
            return
        available = self._context.obs_settings.ndi_stream_configured()
        toolbar.set_obs_stream_available(available)
        toolbar.set_obs_stream_active(self._session.state_type == "obs_stream")
        self.refresh_obs_camera_stream_availability()

    def set_obs_stream_active(self, active: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_obs_stream_active(active)

    def selected_camera_option(self) -> CameraOption | None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is not None:
            opt = toolbar.current_camera_option()
            if opt is not None:
                return opt
        backend = context.camera_settings.backend()
        name = context.camera_settings.device_name()
        return context.camera_service.find_saved(backend, name)

    def refresh_obs_camera_stream_availability(self) -> None:
        context = self._context
        toolbar = context.quick_toolbar()
        if toolbar is None:
            return
        ndi_enabled = context.obs_settings.ndi_enabled()
        camera_enabled = context.camera_settings.is_enabled()
        if not camera_enabled or ndi_enabled:
            toolbar.set_obs_camera_stream_available(False)
            return
        opt = self.selected_camera_option()
        toolbar.set_obs_camera_stream_available(bool(opt and opt.is_virtual))

    def set_camera_stream_active(self, active: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_camera_stream_active(active)
        if not active:
            # Stop feeding the operator preview once the camera stream ends (the
            # v4l2 source is released by the program's crossfade-away).
            self._stop_camera_preview_tap()

    def on_camera_settings_enabled_toggled(self, enabled: bool) -> None:
        toolbar = self._context.quick_toolbar()
        if toolbar is not None:
            toolbar.set_camera_enabled(enabled)
        self.refresh_obs_camera_stream_availability()
        if not enabled and self._session.state_type == "camera_stream":
            self._handlers.stop_projection()

    def on_camera_selection_changed(self, option) -> None:
        self.refresh_obs_camera_stream_availability()
        self._sync_vcam_camera(option)

    def on_vcam_scene_override(self, comp) -> None:
        """Force the virtual camera to a specific scene for the current projected
        content (``comp`` a VcamComposition), or resume following the projector
        (``comp`` is None). No-op off the libobs engine."""
        from ..projection.window import obs_media_engine_active

        if not obs_media_engine_active():
            return
        try:
            from ..core.media.vcam_director import vcam_director

            director = vcam_director()
            if comp is None:
                director.clear_override()
            else:
                director.set_override(comp)
        except Exception:  # noqa: BLE001 - override must not crash the toolbar
            log.debug("Virtual-camera scene override failed", exc_info=True)

    # ── virtual camera (obs engine) ─────────────────────────────────────────

    def start_virtual_camera(self) -> None:
        """Bring the virtual camera up at launch and follow the projector.

        No-op off the libobs engine. On Linux the sink is a ``v4l2loopback``
        device whose branded name needs a one-time, polkit-authorised setup
        (:mod:`vcam_provision`): if the module is missing the operator is guided to
        install it; if it is unloaded or wrongly labelled Solin provisions it (off
        the GUI thread) before starting; otherwise it starts straight away.
        """
        from ..projection.window import obs_media_engine_active

        active = obs_media_engine_active()
        log.info("Virtual camera: startup requested (obs engine active=%s)", active)
        if not active:
            return
        if not self._vcam_autostart_enabled():
            log.info("Virtual camera: start skipped — disabled in Settings")
            return
        from ..core.media import vcam_provision as prov

        state = prov.detect()
        log.info("Virtual camera: provision state = %s", state.value)
        if state is prov.VcamProvisionState.UNSUPPORTED:
            # macOS has no vcam sink yet. Falling through would try to start it
            # and warn the operator at every launch about something they cannot fix.
            return
        if state is prov.VcamProvisionState.MODULE_MISSING:
            self._context.notifications.warning(
                prov.install_hint(),
                title=self._context.translate("Virtual camera unavailable"),
                dedupe_key="vcam-module-missing",
            )
            return
        if state in (
            prov.VcamProvisionState.NOT_LOADED,
            prov.VcamProvisionState.WRONG_LABEL,
        ):
            self._provision_vcam_then_start()
            return
        self._start_virtual_camera_now()

    def _vcam_autostart_enabled(self) -> bool:
        """Whether the operator wants the camera up when Solin opens.

        Fails open: a settings read that raises must not silently strip the
        camera out of every meeting app.
        """
        try:
            return bool(self._context.camera_settings.vcam_autostart())
        except Exception:  # noqa: BLE001 - settings must not gate the camera
            log.debug("Could not read the vcam autostart setting", exc_info=True)
            return True

    def on_vcam_autostart_toggled(self, enabled: bool) -> None:
        """Apply the operator's choice immediately.

        This doubles as the only manual control: turning it on starts the camera
        now, turning it off stops it. Without that, switching the setting off
        would leave no way to switch the camera back on without restarting.
        """
        if enabled:
            self.start_virtual_camera()
        else:
            self.stop_virtual_camera()

    def stop_virtual_camera(self) -> None:
        """Take the virtual camera down (idempotent).

        The device itself stays registered — meeting apps keep listing it and see
        the filter's own placeholder, exactly as they do before Solin starts.
        """
        try:
            from ..core.media.obs_virtual_camera import virtual_camera
            from ..core.media.vcam_director import vcam_director

            vcam_director().stop_following()
            virtual_camera().stop()
        except Exception:  # noqa: BLE001 - teardown must not break the UI
            log.debug("Virtual camera stop failed", exc_info=True)
        toolbar = self._context.quick_toolbar()
        if toolbar is not None and hasattr(toolbar, "set_scene_override_available"):
            toolbar.set_scene_override_available(False)

    def _provision_vcam_then_start(self) -> None:
        """Run the privileged one-time setup off the GUI thread, then start."""
        from PySide6.QtCore import QRunnable, QThreadPool

        # Re-entrancy guard: the toggle can call this while a run is in flight,
        # and a second privileged worker means a second UAC prompt.
        if self._vcam_provision_sink is not None:
            log.debug("Virtual camera: provisioning already in flight")
            return

        sink = _VcamProvisionSink()
        sink.done.connect(self._on_vcam_provisioned)
        self._vcam_provision_sink = sink  # keep alive until the signal fires

        class _Worker(QRunnable):
            def run(self) -> None:  # runs on a QThreadPool worker thread
                from ..core.media import vcam_provision as prov

                sink.done.emit(prov.provision())

        QThreadPool.globalInstance().start(_Worker())

    def _on_vcam_provisioned(self, ok: bool) -> None:
        # Start regardless: on success the sink is now "Solin Virtual Camera"; on
        # failure/cancel it still works on whatever loopback exists (start() logs
        # the real name). NOT_LOADED with a failed setup simply has no device and
        # start() reports that.
        self._vcam_provision_sink = None
        if not ok:
            log.info("Virtual camera: continuing without the branded setup.")
        self._start_virtual_camera_now()

    def _start_virtual_camera_now(self) -> None:
        from ..core.media.obs_virtual_camera import virtual_camera
        from ..core.media.vcam_director import vcam_director

        if not virtual_camera().start():
            # Tell the operator. Otherwise the camera is simply absent from every
            # meeting app, the Scenes override never appears, and nothing on
            # screen explains why.
            reason = virtual_camera().last_error
            self._context.notifications.warning(
                reason or self._context.translate("The virtual camera could not start."),
                title=self._context.translate("Virtual camera unavailable"),
                dedupe_key="vcam-start-failed",
            )
            return
        self._load_vcam_scene_config()
        self._sync_vcam_camera(self.selected_camera_option())
        vcam_director().start_following()
        toolbar = self._context.quick_toolbar()
        if toolbar is not None and hasattr(toolbar, "set_scene_override_available"):
            toolbar.set_scene_override_available(True)

    def _load_vcam_scene_config(self) -> None:
        """Seed the director with the operator's persisted Scenes config, just
        before it starts following (so a saved config applies from the start)."""
        try:
            from ..core.media.vcam_director import vcam_director
            from ..core.media.vcam_settings import VcamSettingsStore

            store = VcamSettingsStore(self._context.camera_settings.settings)
            vcam_director().set_config(store.scene_config())
        except Exception:  # noqa: BLE001 - config load must not block the vcam
            log.debug("Could not load virtual-camera scene config", exc_info=True)

    def _sync_vcam_camera(self, option) -> None:
        """Feed the operator's selected camera to the virtual camera (obs only)."""
        from ..projection.window import obs_media_engine_active

        if not obs_media_engine_active():
            return
        from ..core.media.camera_source import camera_target
        from ..core.media.obs_virtual_camera import virtual_camera

        path, name = camera_target(option)
        virtual_camera().set_meeting_camera(path, name)

    def on_quick_obs_scene_change(self, scene_name: str) -> None:
        self._context.obs_service.request_scene_change(scene_name)

    def on_quick_obs_return_scene_change(self, scene_name: str) -> None:
        context = self._context
        scene_name = (scene_name or "").strip()
        media_scene = context.obs_settings.media_window_scene()
        current = context.obs_service.current_scene or ""
        if (
            not scene_name
            or scene_name == current
            or not media_scene
            or media_scene.startswith("—")
            or current != media_scene
        ):
            return
        context.obs_scene_session.remember(scene_name)

    def project_obs_ndi_stream(self) -> None:
        context = self._context
        if self._session.state_type == "obs_stream":
            self._handlers.stop_projection()
            return
        if not context.playback_protection.allow_manual_projection_change():
            return

        source = context.obs_settings.ndi_source()
        if not context.obs_settings.ndi_stream_configured():
            context.notifications.warning(
                context.translate("OBS stream is not configured.")
            )
            self.refresh_obs_stream_availability()
            return

        self._session.set_tab_projection_active(False)
        try:
            self._handlers.stop_browser_tab_projection()
        except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
            log.debug("Failed to stop browser tab projection before OBS stream", exc_info=True)
        context.media_controller.stop()
        context.camera_service.stop()
        context.projection_bar.set_playlist([])
        for win in context.projection_windows():
            win.clear()
        title = context.translate("OBS Program Stream")
        context.projection_bar.activate_live_stream(
            title,
            keep_expanded=context.projection_bar.is_expanded(),
        )
        self._session.set_state({"type": "obs_stream", "title": title})
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_obs_stream_active(True)
        context.ndi_service.start(source, max_fps=30)

    def project_camera_stream(self) -> None:
        context = self._context
        if self._session.state_type == "camera_stream":
            self._handlers.stop_projection()
            return
        if not context.playback_protection.allow_manual_projection_change():
            return

        if not context.camera_settings.is_enabled():
            context.notifications.warning(context.translate("Camera is not enabled."))
            return

        option = self.selected_camera_option()
        if option is None:
            context.notifications.warning(context.translate("No camera selected."))
            return

        self._session.set_tab_projection_active(False)
        try:
            self._handlers.stop_browser_tab_projection()
        except Exception:  # noqa: BLE001 - native browser projection cleanup boundary
            log.debug("Failed to stop browser tab projection before camera stream", exc_info=True)
        context.media_controller.stop()
        context.ndi_service.stop()
        context.camera_service.stop()
        context.projection_bar.set_playlist([])
        for win in context.projection_windows():
            win.clear()
        title = context.translate("Camera")
        context.projection_bar.activate_live_stream(
            title,
            keep_expanded=context.projection_bar.is_expanded(),
        )
        self._session.set_state({"type": "camera_stream", "title": title})
        self._handlers.update_projection_status(
            True,
            title,
            auto_keys_media=False,
            sync_obs=False,
        )
        self.set_camera_stream_active(True)

        # Native libobs camera: a v4l2_input source libobs owns, decodes and
        # crossfades — no QImage round-trip, no Qt QCamera on the device. The
        # operator preview is fed from a channel-0 tap (libobs holds the device,
        # so the Qt camera service can't). Falls back to the Qt camera path if the
        # source can't be created (non-obs engine, or no v4l2 device path).
        from ..projection.window import obs_media_engine_active

        if obs_media_engine_active() and option.device_path:
            shown = False
            for win in context.projection_windows():
                if hasattr(win, "show_camera") and win.show_camera(
                    option.device_path, option.name
                ):
                    shown = True
            if shown:
                self._start_camera_preview_tap()
                return

        context.camera_service.start(option)

    def _start_camera_preview_tap(self) -> None:
        from ..projection.program_preview import program_preview_tap

        tap = program_preview_tap()
        if not self._camera_tap_connected:
            tap.frame_ready.connect(self._on_camera_preview_frame)
            self._camera_tap_connected = True
        tap.set_enabled(True)

    def _stop_camera_preview_tap(self) -> None:
        from ..projection.program_preview import program_preview_tap

        program_preview_tap().set_enabled(False)

    def _on_camera_preview_frame(self, image) -> None:
        # Only feed the preview while the camera is the projected content (the tap
        # is a shared channel-0 grab; state gates it so it never fights another
        # source's preview).
        if self._session.state_type == "camera_stream":
            self._context.projection_bar.update_tab_live_preview(image)

    @Slot(QImage)
    def on_camera_frame(self, frame: QImage) -> None:
        if self._session.state_type != "camera_stream":
            return
        context = self._context
        for win in context.projection_windows():
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        context.projection_bar.update_tab_live_preview(frame)

    def on_camera_error(self, message: str) -> None:
        if self._session.state_type == "camera_stream":
            context = self._context
            context.notifications.error(
                message,
                title=context.translate("Camera error"),
                dedupe_key=f"camera:{message}",
            )
            self._handlers.stop_projection()

    def on_camera_stopped(self) -> None:
        self.set_camera_stream_active(False)

    @Slot(QImage)
    def on_obs_ndi_frame(self, frame: QImage) -> None:
        if self._session.state_type != "obs_stream":
            return
        context = self._context
        for win in context.projection_windows():
            # Push the ctypes-decoded NDI frame into a libobs async source so the
            # program composites + crossfades it (vcam/recording-capturable),
            # instead of painting the QImage on the Qt page with the surface
            # hidden. Falls back to the Qt per-frame path when unavailable.
            if hasattr(win, "show_ndi_frame"):
                try:
                    if win.show_ndi_frame(frame):
                        continue
                except Exception:  # noqa: BLE001 - libobs/render boundary
                    log.debug("libobs NDI frame failed; using Qt frames", exc_info=True)
            if hasattr(win, "show_image_from_qimage"):
                win.show_image_from_qimage(frame, cache_pixmap=False)
        # The operator preview keeps the raw frame (already available in Python;
        # no device-exclusivity constraint like the camera).
        context.projection_bar.update_tab_live_preview(frame)

    def on_obs_ndi_error(self, message: str) -> None:
        if self._session.state_type == "obs_stream":
            context = self._context
            context.notifications.error(
                message,
                title=context.translate("OBS stream error"),
                dedupe_key=f"obs-stream:{message}",
            )
            self._handlers.stop_projection()

    def on_obs_ndi_stopped(self) -> None:
        if not self._context.ndi_service.is_running:
            self.set_obs_stream_active(False)

    def on_obs_scene_toggle(self) -> None:
        context = self._context
        if not context.obs_service.is_connected:
            return

        media_scene = context.obs_settings.media_window_scene()
        if not media_scene or media_scene.startswith("—"):
            return

        if context.projection_bar.is_obs_scene_media():
            if MEMORIZE_PRE_MEDIA_SCENE:
                target = (
                    context.obs_scene_session.pre_media_scene
                    or context.obs_settings.default_scene()
                )
            else:
                target = context.obs_settings.default_scene()
            if target and not target.startswith("—"):
                context.obs_service.request_scene_change(target)
                context.projection_bar.set_obs_scene_is_media(False)
        else:
            if MEMORIZE_PRE_MEDIA_SCENE:
                current = context.obs_service.current_scene or ""
                if current and current != media_scene:
                    context.obs_scene_session.remember(current)
            context.obs_service.request_scene_change(media_scene)
            context.projection_bar.set_obs_scene_is_media(True)
