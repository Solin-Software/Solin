"""Main-thread coordinator for Solin's shared local HTTPS remote-control host."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..core.i18n.remote_control import (
    remote_media_localization,
    remote_timer_localization,
)
from ..core.remote_control.features import (
    DEFAULT_TIMER_REMOTE_FEATURE_FLAGS,
    TimerRemoteFeatureFlags,
)
from ..core.remote_control.media.state import RemoteControlStateStore
from ..core.remote_control.network import (
    LanInterface,
    discover_lan_interfaces,
    resolve_selected_interface,
)
from ..core.remote_control.security import (
    InMemorySessionStore,
    LoginRateLimiter,
    RemoteScope,
)
from ..core.remote_control.settings import (
    REMOTE_CONTROL_PORT,
    NetworkInterfaceSelection,
)
from ..core.remote_control.timer.state import RemoteTimerStateStore
from ..core.remote_control.validation import JsonObject
from ..core.timer.hand_alert import TimerHandAlert
from .remote_media_adapter import RemoteMediaAdapter, RemoteMediaAdapterDependencies
from .remote_timer_adapter import RemoteTimerAdapter, RemoteTimerAdapterDependencies


log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..core.remote_control.certificates import TLSIdentity
    from ..core.remote_control.server import RemoteControlServer


@dataclass(frozen=True, slots=True)
class RemoteControlDependencies:
    runtime_paths: Any
    active_profile_id: str
    active_profile_name: str
    active_profile_locale: Callable[[], str]
    settings: Any
    media_credentials: Any
    timer_credentials: Any
    playlist_repository: Any
    meeting_tree_store: Any
    watched_folder_settings: Any
    watched_folder_playlist_store: Any
    playlist_thumbnail_store: Any
    meeting_thumbnail_store: Any
    media_thumbnail_extractor: Any
    projection_session: Any
    projection_bar: Any
    media_controller: Any
    media_projection: Any
    projection_stop: Any
    playback_protection: Any
    timer_engine: Any
    timer_bridge: Any
    settings_widget: Any
    timer_feature_flags: TimerRemoteFeatureFlags = DEFAULT_TIMER_REMOTE_FEATURE_FLAGS


class RemoteControlController(QObject):
    """Main-thread coordinator hosting isolated Media and Timer remote web apps."""

    session_inventory_reported = Signal(object)
    public_viewers_reported = Signal(object)
    runtime_status_reported = Signal(bool, bool, str, bool, bool)
    session_revocation_reported = Signal(str, bool)

    def __init__(self, dependencies: RemoteControlDependencies, parent: QObject) -> None:
        super().__init__(parent)
        self._dependencies = dependencies
        self._media_sessions = InMemorySessionStore(scope=RemoteScope.MEDIA)
        self._timer_sessions = InMemorySessionStore(scope=RemoteScope.TIMER)
        self._media_rate_limiter = LoginRateLimiter()
        self._timer_rate_limiter = LoginRateLimiter()
        self._tls_store: Any | None = None
        self._server: RemoteControlServer | None = None
        self._tls_identity: TLSIdentity | None = None
        self._next_tls_renewal_attempt_at = 0.0
        self._started = False
        self._media_serving = False
        self._timer_serving = False
        self._runtime_message_factory: Callable[[], str] | None = None
        self._last_runtime_running = False
        self._last_runtime_status: str | None = None

        locale_code = dependencies.active_profile_locale()
        self._media_localization = remote_media_localization(locale_code)
        self._timer_localization = remote_timer_localization(locale_code)

        watched_signal = getattr(
            getattr(dependencies.settings_widget, "general", None),
            "watched_folder_changed",
            None,
        )
        self._media_adapter = RemoteMediaAdapter(
            RemoteMediaAdapterDependencies(
                active_profile_id=dependencies.active_profile_id,
                active_profile_name=dependencies.active_profile_name,
                credentials=dependencies.media_credentials,
                playlist_repository=dependencies.playlist_repository,
                meeting_tree_store=dependencies.meeting_tree_store,
                watched_folder_settings=dependencies.watched_folder_settings,
                watched_folder_playlist_store=dependencies.watched_folder_playlist_store,
                playlist_thumbnail_store=dependencies.playlist_thumbnail_store,
                meeting_thumbnail_store=dependencies.meeting_thumbnail_store,
                media_thumbnail_extractor=dependencies.media_thumbnail_extractor,
                projection_session=dependencies.projection_session,
                projection_bar=dependencies.projection_bar,
                media_controller=dependencies.media_controller,
                media_projection=dependencies.media_projection,
                projection_stop=dependencies.projection_stop,
                playback_protection=dependencies.playback_protection,
                watched_folder_changed_signal=watched_signal,
            ),
            self,
        )
        self._timer_adapter = RemoteTimerAdapter(
            RemoteTimerAdapterDependencies(
                active_profile_id=dependencies.active_profile_id,
                active_profile_name=dependencies.active_profile_name,
                credentials=dependencies.timer_credentials,
                timer_engine=dependencies.timer_engine,
                timer_bridge=dependencies.timer_bridge,
                feature_flags=dependencies.timer_feature_flags,
            ),
            self,
        )

        self._media_adapter.snapshot_updated.connect(self._on_media_snapshot_updated)
        self._media_adapter.catalog_became_ready.connect(self.reconfigure)
        self._timer_adapter.snapshot_updated.connect(self._on_timer_snapshot_updated)

        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(15_000)
        self._runtime_timer.timeout.connect(self._reconcile_runtime)

        remote_settings = dependencies.settings_widget.remote
        remote_settings.remote_control_settings_changed.connect(self.reconfigure)
        remote_settings.remote_media_credentials_changed.connect(self.media_credentials_changed)
        remote_settings.remote_timer_credentials_changed.connect(self.timer_credentials_changed)

    @property
    def media_state(self) -> RemoteControlStateStore:
        return self._media_adapter.state

    @property
    def timer_state(self) -> RemoteTimerStateStore:
        return self._timer_adapter.state

    @Slot(object)
    def set_timer_hand_alert(self, alert: TimerHandAlert) -> None:
        self._timer_adapter.set_hand_alert(alert)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    @property
    def media_active(self) -> bool:
        return self.is_running and self._media_serving

    @property
    def timer_active(self) -> bool:
        return self.is_running and self._timer_serving

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.reconfigure()
        self._runtime_timer.start()

    def stop(self) -> None:
        self._runtime_timer.stop()
        self._media_serving = False
        self._timer_serving = False
        self._media_adapter.stop()
        self._timer_adapter.stop()
        self._media_sessions.revoke_all()
        self._timer_sessions.revoke_all()
        self._stop_server()
        self.session_inventory_reported.emit(())
        self.public_viewers_reported.emit({})

    @Slot()
    def reconfigure(self) -> None:
        settings = self._dependencies.settings
        master_enabled = bool(settings.enabled())
        if not master_enabled:
            self._teardown_all()
            self._show_runtime_status(
                running=False,
                message=lambda: self.tr("Remote control is disabled."),
                status="pending",
            )
            return

        selection = settings.network_selection()
        if selection is None:
            self._teardown_all()
            self._show_runtime_status(
                running=False,
                message=lambda: self.tr("Select a private network to start remote control."),
            )
            return

        interface = self._resolve_selected_interface(selection)
        if interface is None:
            self._teardown_all()
            self._show_runtime_status(
                running=False,
                message=lambda: self.tr("The selected network is not currently available."),
            )
            return

        media_requested = bool(settings.media_enabled())
        timer_requested = bool(settings.timer_enabled())
        media_creds_ok = self._dependencies.media_credentials.has_credentials()
        timer_creds_ok = (
            self._dependencies.timer_credentials.has_credentials()
            or not self._dependencies.timer_feature_flags.require_credentials
        )

        media_eligible = media_requested and media_creds_ok
        timer_eligible = timer_requested and timer_creds_ok

        self._media_adapter.set_active(media_eligible)
        self._timer_adapter.set_active(timer_eligible)

        if not media_eligible and not timer_eligible:
            self._teardown_server_only()
            if media_requested and not media_creds_ok:
                message_fn: Callable[[], str] = lambda: self.tr(
                    "Save Media credentials to start Media remote control."
                )
                status: str | None = "error"
            elif timer_requested and not timer_creds_ok:
                message_fn = lambda: self.tr(
                    "Save Timer credentials to start Timer remote control."
                )
                status = "error"
            else:
                message_fn = lambda: self.tr(
                    "Enable Media or Timer remote control to start accepting connections."
                )
                status = "ready"
            self._show_runtime_status(running=False, message=message_fn, status=status)
            return

        media_ready = media_eligible and self._media_adapter.is_ready
        if not media_ready and not timer_eligible:
            self._teardown_server_only()
            self._show_runtime_status(
                running=False,
                message=lambda: self.tr("Remote control is preparing the media catalog."),
                status="pending",
            )
            return

        current_binding = getattr(self._server, "binding", None)
        if (
            self.is_running
            and self._server is not None
            and current_binding is not None
            and current_binding.host == str(interface.ipv4_address)
        ):
            self._media_serving = media_ready
            self._timer_serving = timer_eligible
            self._server.sync_module_states()
            if self._media_serving:
                self._server.publish_snapshot(RemoteScope.MEDIA)
            if self._timer_serving:
                self._server.publish_snapshot(RemoteScope.TIMER)
            self._show_runtime_status(
                running=True,
                message=self._running_message,
            )
            return

        if not self._teardown_server_only():
            self._show_runtime_status(
                running=self.is_running,
                message=lambda: self.tr("Remote control could not stop cleanly."),
            )
            return

        self._media_serving = media_ready
        self._timer_serving = timer_eligible
        try:
            identity = self._tls_certificate_store().load_or_create(interface.ipv4_address)
            server = self._start_server(identity)
        except Exception:  # noqa: BLE001 - service lifecycle boundary
            log.exception("Could not start local remote control")
            self._media_serving = False
            self._timer_serving = False
            self._show_runtime_status(
                running=False,
                message=lambda: self.tr(
                    "Remote control could not start on the selected network."
                ),
            )
            return

        self._server = server
        self._tls_identity = identity
        self._next_tls_renewal_attempt_at = 0.0
        self._show_runtime_status(
            running=True,
            message=self._running_message,
        )

    def _running_message(self) -> str:
        if (
            self._dependencies.settings.media_enabled()
            and self._dependencies.media_credentials.has_credentials()
            and not self._media_serving
        ):
            return self.tr("Timer remote control is running. Preparing the media catalog…")
        return self.tr("Secure remote control is running.")

    def _start_server(self, identity: TLSIdentity) -> RemoteControlServer:
        from ..core.foundation.resources import application_resource_path
        from ..core.remote_control.server import (
            RemoteControlHostDependencies,
            RemoteControlServer,
            RemoteControlServerBinding,
            TRUST_CERTIFICATE_ROUTE,
        )

        root_assets = application_resource_path("remote_control")
        shared_assets = root_assets / "shared"
        media_module = self._media_adapter.build_http_module(
            sessions=self._media_sessions,
            rate_limiter=self._media_rate_limiter,
            assets_directory=root_assets / "media",
            shared_assets_directory=shared_assets,
            setup_provider=lambda: self._setup_payload(
                self._media_localization,
                TRUST_CERTIFICATE_ROUTE,
            ),
            enabled_predicate=lambda: self._media_serving,
        )
        timer_module = self._timer_adapter.build_http_module(
            sessions=self._timer_sessions,
            rate_limiter=self._timer_rate_limiter,
            assets_directory=root_assets / "timer",
            shared_assets_directory=shared_assets,
            setup_provider=lambda: self._setup_payload(
                self._timer_localization,
                TRUST_CERTIFICATE_ROUTE,
            ),
            enabled_predicate=lambda: self._timer_serving,
        )
        server = RemoteControlServer(
            RemoteControlHostDependencies(
                modules=(media_module, timer_module),
                session_inventory_changed=self.session_inventory_reported.emit,
                public_viewer_count_changed=self.public_viewers_reported.emit,
            )
        )
        self._tls_identity = identity
        server.start(
            RemoteControlServerBinding(
                host=str(identity.ipv4_address),
                port=REMOTE_CONTROL_PORT,
                tls_identity=identity,
            )
        )
        return server

    def _setup_payload(
        self,
        localization: JsonObject,
        certificate_route: str,
    ) -> JsonObject:
        identity = self._tls_identity
        if identity is None:
            raise RuntimeError("TLS identity is not initialized")
        messages = localization.get("messages")
        return {
            "locale": str(localization.get("locale") or "en"),
            "messages": dict(messages) if isinstance(messages, dict) else {},
            "tls": {
                "installationId": identity.installation_id,
                "authoritySha256": identity.trust_anchor_fingerprint_sha256,
                "verificationCode": self._verification_code(
                    identity.trust_anchor_fingerprint_sha256
                ),
                "authorityCertificateUrl": certificate_route,
                "authorityNotValidAfter": identity.root_not_valid_after.isoformat(),
            },
        }

    def _renew_tls(self) -> None:
        previous = self._tls_identity
        server = self._server
        if previous is None or server is None:
            return
        try:
            renewed = self._tls_certificate_store().load_or_create(previous.ipv4_address)
            if renewed.leaf_fingerprint_sha256 == previous.leaf_fingerprint_sha256:
                self._next_tls_renewal_attempt_at = 0.0
                return
            server.reload_tls(renewed)
        except Exception:  # noqa: BLE001 - controlled TLS lifecycle boundary
            log.exception("Could not renew the local remote-control certificate")
            self._next_tls_renewal_attempt_at = time.monotonic() + 300.0
            self._show_runtime_status(
                running=self.is_running,
                message=lambda: self.tr(
                    "Secure remote control is running. Certificate renewal will retry "
                    "automatically."
                ),
            )
            return
        self._tls_identity = renewed
        self._next_tls_renewal_attempt_at = 0.0
        self._show_runtime_status(
            running=True,
            message=self._running_message,
        )

    @Slot()
    def _reconcile_runtime(self) -> None:
        settings = self._dependencies.settings
        if not settings.enabled():
            if self.is_running:
                self.reconfigure()
            return
        selection = settings.network_selection()
        interface = (
            self._resolve_selected_interface(selection) if selection is not None else None
        )
        current_binding = getattr(self._server, "binding", None)
        should_have_interface = interface is not None
        if not should_have_interface and self.is_running:
            self.reconfigure()
            return
        if (
            interface is not None
            and current_binding is not None
            and current_binding.host != str(interface.ipv4_address)
        ):
            self.reconfigure()
            return
        if (
            interface is not None
            and not self.is_running
            and (settings.media_enabled() or settings.timer_enabled())
        ):
            self.reconfigure()
            return
        if (
            interface is not None
            and self.is_running
            and self._tls_identity is not None
            and time.monotonic() >= self._next_tls_renewal_attempt_at
            and self._tls_certificate_store().renewal_due(self._tls_identity)
        ):
            self._renew_tls()

    @staticmethod
    def _resolve_selected_interface(
        selection: NetworkInterfaceSelection,
    ) -> LanInterface | None:
        return resolve_selected_interface(
            selection.selection_key,
            discover_lan_interfaces(),
        )

    @Slot()
    def media_credentials_changed(self) -> None:
        self._media_rate_limiter.clear()
        if self._server is not None and self.is_running:
            future = self._server.revoke_all_sessions(RemoteScope.MEDIA)
            if future is None:
                self._media_sessions.revoke_all()
        else:
            self._media_sessions.revoke_all()
        self.reconfigure()

    @Slot()
    def timer_credentials_changed(self) -> None:
        self._timer_rate_limiter.clear()
        if self._server is not None and self.is_running:
            future = self._server.revoke_all_sessions(RemoteScope.TIMER)
            if future is None:
                self._timer_sessions.revoke_all()
            self._server.publish_profile(RemoteScope.TIMER)
        else:
            self._timer_sessions.revoke_all()
        self.reconfigure()

    @Slot()
    def revoke_sessions(self) -> None:
        server = self._server
        if server is None:
            self._media_sessions.revoke_all()
            self._timer_sessions.revoke_all()
            self.session_inventory_reported.emit(())
            return
        future = server.revoke_all_sessions()
        if future is None:
            self.session_revocation_reported.emit("", False)
            return
        future.add_done_callback(lambda result: self._report_revocation("", result))

    @Slot(str)
    def revoke_session(self, management_id: str) -> None:
        server = self._server
        if server is None:
            self.session_revocation_reported.emit(management_id, False)
            return
        future = server.revoke_session(management_id)
        if future is None:
            self.session_revocation_reported.emit(management_id, False)
            return
        future.add_done_callback(
            lambda result, session_id=management_id: self._report_revocation(
                session_id,
                result,
            )
        )

    def _report_revocation(
        self,
        management_id: str,
        result: Future[bool] | Future[int],
    ) -> None:
        try:
            value = result.result()
            succeeded = bool(value) if management_id else isinstance(value, int)
        except Exception:  # noqa: BLE001 - cross-thread future boundary
            log.exception("Could not revoke remote-control session(s)")
            succeeded = False
        self.session_revocation_reported.emit(management_id, succeeded)

    @Slot(str)
    def on_language_changed(self, locale_code: str) -> None:
        self._media_localization = remote_media_localization(locale_code)
        self._timer_localization = remote_timer_localization(locale_code)
        self._media_adapter.on_language_changed()
        self._timer_adapter.on_language_changed()
        if self._server is not None:
            self._server.publish_profile()
        if self._runtime_message_factory is not None:
            self._show_runtime_status(
                running=self._last_runtime_running,
                message=self._runtime_message_factory,
                status=self._last_runtime_status,
            )

    @Slot()
    def on_theme_changed(self) -> None:
        if self._server is not None:
            self._server.publish_profile()

    @Slot()
    def _on_media_snapshot_updated(self) -> None:
        if self._server is not None and self._media_serving:
            self._server.publish_snapshot(RemoteScope.MEDIA)

    @Slot()
    def _on_timer_snapshot_updated(self) -> None:
        if self._server is not None and self._timer_serving:
            self._server.publish_snapshot(RemoteScope.TIMER)

    def _show_runtime_status(
        self,
        *,
        running: bool,
        message: str | Callable[[], str],
        status: str | None = None,
    ) -> None:
        if callable(message):
            self._runtime_message_factory = message
            resolved_message = message()
        else:
            self._runtime_message_factory = lambda text=message: text
            resolved_message = message
        self._last_runtime_running = running
        self._last_runtime_status = status

        settings = self._dependencies.settings
        master_enabled = bool(settings.enabled())
        media_on = bool(master_enabled and settings.media_enabled())
        timer_on = bool(master_enabled and settings.timer_enabled())
        toolbar_enabled = bool(master_enabled and (media_on or timer_on))
        self.runtime_status_reported.emit(
            toolbar_enabled,
            running,
            resolved_message,
            bool(running and self._media_serving),
            bool(running and self._timer_serving),
        )
        identity = self._tls_identity
        binding = getattr(self._server, "binding", None)
        media_url = (
            binding.media_url
            if binding is not None and running and self._media_serving
            else ""
        )
        timer_url = (
            binding.timer_url
            if binding is not None and running and self._timer_serving
            else ""
        )
        self._dependencies.settings_widget.remote.set_remote_control_runtime_status(
            running=running,
            message=resolved_message,
            media_running=bool(running and self._media_serving),
            timer_running=bool(running and self._timer_serving),
            fingerprint=identity.trust_anchor_fingerprint_sha256 if identity else "",
            media_access_url=media_url,
            media_setup_url=f"{media_url}?setup=1" if media_url else "",
            timer_access_url=timer_url,
            timer_setup_url=f"{timer_url}?setup=1" if timer_url else "",
            verification_code=(
                self._verification_code(identity.trust_anchor_fingerprint_sha256)
                if identity
                else ""
            ),
            certificate_der=(identity.trust_certificate_der() if identity and running else b""),
            status=status,
        )

    def _tls_certificate_store(self) -> Any:
        if self._tls_store is None:
            from ..core.remote_control.certificates import TLSCertificateStore

            self._tls_store = TLSCertificateStore(
                Path(self._dependencies.runtime_paths.data_dir) / "remote_control" / "tls"
            )
        return self._tls_store

    @staticmethod
    def _verification_code(fingerprint: str) -> str:
        from ..core.remote_control.certificates import verification_code

        return verification_code(fingerprint)

    def _teardown_all(self) -> None:
        self._media_serving = False
        self._timer_serving = False
        self._media_adapter.set_active(False)
        self._timer_adapter.set_active(False)
        self._teardown_server_only()

    def _teardown_server_only(self) -> bool:
        self._media_sessions.revoke_all()
        self._timer_sessions.revoke_all()
        stopped = self._stop_server()
        if stopped:
            self._media_serving = False
            self._timer_serving = False
            self.session_inventory_reported.emit(())
            self.public_viewers_reported.emit({})
        return stopped

    def _stop_server(self) -> bool:
        self._media_adapter.abort_pending_commands()
        self._timer_adapter.abort_pending_commands()
        server = self._server
        if server is None:
            self._tls_identity = None
            self._media_adapter.clear_runtime_caches()
            return True
        try:
            server.stop()
        except TimeoutError:
            log.error("Timed out stopping local remote control", exc_info=True)
            return False
        self._server = None
        self._tls_identity = None
        self._media_adapter.clear_runtime_caches()
        return True
