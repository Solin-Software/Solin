from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from ..core.media.playback_state import SolinPlaybackState
from ..core.i18n.meeting_sections import display_meeting_section_title
from ..core.i18n.remote_control import remote_control_localization
from ..core.media.formats import MediaKind
from ..core.meetings.thumbnails import meeting_thumb_storage_id
from ..core.media.thumbnail_identity import thumbnail_storage_id
from ..core.remote_control.catalog import (
    CatalogBuild,
    CatalogResolutionCode,
    CatalogResolutionError,
    MediaAvailability,
    RemoteCatalog,
    ResolvedMeetingPlay,
    ResolvedPlaylistPlay,
)
from ..core.remote_control.contracts import (
    CatalogKind,
    CommandError,
    CommandErrorCode,
    JsonObject,
    NextCommand,
    PauseCommand,
    PlaybackCapabilities,
    PlaybackState,
    PlayCommand,
    PreviousCommand,
    ProjectionError,
    ProjectionOrigin,
    ProjectionQueueItem,
    ProjectionSnapshot,
    ProjectionSource,
    RemoteCommand,
    RemoteMediaKind,
    ResumeCommand,
    SeekCommand,
    SetVolumeCommand,
    StopCommand,
)
from ..core.remote_control.network import (
    LanInterface,
    discover_lan_interfaces,
    resolve_selected_interface,
)
from ..core.remote_control.sanitization import public_reason, public_title
from ..core.remote_control.security import InMemorySessionStore, LoginRateLimiter
from ..core.remote_control.settings import (
    REMOTE_CONTROL_PORT,
    NetworkInterfaceSelection,
)
from ..core.remote_control.state import (
    ProjectionCommandSession,
    RemoteControlStateStore,
)
from ..core.remote_control.thumbnails import (
    private_media_thumbnail_source,
    render_image_thumbnail_bytes,
    render_local_image_thumbnail,
    render_media_placeholder_thumbnail,
)


log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..core.remote_control.certificates import TLSIdentity
    from ..core.remote_control.server import RemoteControlServer

_FALLBACK_THUMBNAIL_TTL_SECONDS = 15 * 60
_MAX_FALLBACK_THUMBNAILS = 256

ThumbnailRequestKey = tuple[int, str, str, str]


@dataclass(frozen=True, slots=True)
class RemoteControlDependencies:
    runtime_paths: Any
    active_profile_id: str
    active_profile_name: str
    active_profile_locale: Callable[[], str]
    settings: Any
    credentials: Any
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
    settings_widget: Any


@dataclass(slots=True)
class _CommandEnvelope:
    command: RemoteCommand
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[CommandError | None]
    cancelled: threading.Event


class _QtCommandBridge(QObject):
    requested = Signal(object)

    def __init__(
        self,
        handler: Callable[[RemoteCommand], CommandError | None],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self.requested.connect(self._dispatch)

    async def execute(self, command: RemoteCommand) -> CommandError | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CommandError | None] = loop.create_future()
        envelope = _CommandEnvelope(command, loop, future, threading.Event())
        self.requested.emit(envelope)
        try:
            return await future
        except asyncio.CancelledError:
            envelope.cancelled.set()
            raise

    @Slot(object)
    def _dispatch(self, envelope: _CommandEnvelope) -> None:
        if envelope.cancelled.is_set():
            return
        try:
            result = self._handler(envelope.command)
        except Exception:  # noqa: BLE001 - Qt command boundary
            log.exception("Remote-control command dispatch failed")
            result = CommandError(
                CommandErrorCode.FAILED,
                "The command could not be completed.",
            )

        def complete() -> None:
            if not envelope.future.done():
                envelope.future.set_result(result)

        envelope.loop.call_soon_threadsafe(complete)


class RemoteControlController(QObject):
    """Main-thread coordinator for the secure local remote-control vertical."""

    catalog_invalidated = Signal()
    playback_invalidated = Signal()
    session_inventory_reported = Signal(object)
    runtime_status_reported = Signal(bool, bool, str)
    session_revocation_reported = Signal(str, bool)
    _catalog_build_completed = Signal(int, object, object)

    def __init__(self, dependencies: RemoteControlDependencies, parent: QObject) -> None:
        super().__init__(parent)
        self._dependencies = dependencies
        self._catalog_publication_lock = threading.RLock()
        self._state = RemoteControlStateStore(
            publication_lock=self._catalog_publication_lock,
        )
        self._sessions = InMemorySessionStore()
        self._rate_limiter = LoginRateLimiter()
        self._tls_store: Any | None = None
        self._server: RemoteControlServer | None = None
        self._tls_identity: TLSIdentity | None = None
        self._next_tls_renewal_attempt_at = 0.0
        self._thumbnail_tasks: dict[
            ThumbnailRequestKey,
            asyncio.Task[bytes | None],
        ] = {}
        self._fallback_thumbnails: OrderedDict[
            ThumbnailRequestKey,
            tuple[float, bytes],
        ] = OrderedDict()
        self._last_media_error = ""
        self._localization = remote_control_localization(dependencies.active_profile_locale())
        self._unsubscribers: list[Callable[[], None]] = []
        self._bridge = _QtCommandBridge(self._execute_command, self)
        self._catalog = RemoteCatalog(
            dependencies.playlist_repository,
            dependencies.meeting_tree_store,
            linked_playlist_source=self._load_linked_playlists,
            availability_resolver=self._media_availability,
            thumbnail_id_resolver=self._thumbnail_id,
            meeting_group_title_resolver=display_meeting_section_title,
            publication_lock=self._catalog_publication_lock,
        )
        self._catalog_refresh_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="solin-remote-catalog",
        )
        self._catalog_refresh_generation = 0
        self._catalog_refresh_inflight: tuple[int, Future[Any]] | None = None
        self._catalog_refresh_pending = False
        self._catalog_refresh_shutdown = False
        self._catalog_ready = False
        self._catalog_dirty = True
        self._runtime_waiting_for_catalog = False
        self._started = False
        self._catalog_refresh_timer = QTimer(self)
        self._catalog_refresh_timer.setSingleShot(True)
        self._catalog_refresh_timer.setInterval(750)
        self._catalog_refresh_timer.timeout.connect(self._start_catalog_refresh)
        self._catalog_build_completed.connect(
            self._on_catalog_build_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._playback_timer = QTimer(self)
        self._playback_timer.setSingleShot(True)
        self._playback_timer.setInterval(160)
        self._playback_timer.timeout.connect(self._refresh_playback)
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(15_000)
        self._runtime_timer.timeout.connect(self._reconcile_runtime)

        self.catalog_invalidated.connect(self._schedule_catalog_refresh)
        self.playback_invalidated.connect(self._schedule_playback_refresh)
        self._connect_publishers()

    @property
    def state(self) -> RemoteControlStateStore:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._refresh_playback()
        self.reconfigure()
        self._runtime_timer.start()

    def stop(self) -> None:
        self._catalog_refresh_shutdown = True
        self._catalog_refresh_timer.stop()
        future = (
            self._catalog_refresh_inflight[1]
            if self._catalog_refresh_inflight is not None
            else None
        )
        if future is not None:
            future.cancel()
        self._catalog_refresh_executor.shutdown(wait=False, cancel_futures=True)
        self._catalog_refresh_inflight = None
        self._catalog_refresh_pending = False
        self._runtime_timer.stop()
        self._playback_timer.stop()
        self._sessions.revoke_all()
        self._stop_server()
        self.session_inventory_reported.emit(())
        self._dependencies.media_thumbnail_extractor.shutdown()
        while self._unsubscribers:
            self._unsubscribers.pop()()

    @Slot()
    def reconfigure(self) -> None:
        self._sessions.revoke_all()
        if not self._stop_server():
            self._show_runtime_status(
                running=self.is_running,
                message=self.tr("Remote control could not stop cleanly."),
            )
            return
        self.session_inventory_reported.emit(())
        settings = self._dependencies.settings
        if not settings.enabled():
            self._show_runtime_status(
                running=False,
                message=self.tr("Remote control is disabled."),
                status="pending",
            )
            return
        if not self._dependencies.credentials.has_credentials():
            self._show_runtime_status(
                running=False,
                message=self.tr("Save access credentials to start remote control."),
            )
            return
        selection = settings.network_selection()
        if selection is None:
            self._show_runtime_status(
                running=False,
                message=self.tr("Select a private network to start remote control."),
            )
            return
        interface = self._resolve_selected_interface(selection)
        if interface is None:
            self._show_runtime_status(
                running=False,
                message=self.tr("The selected network is not currently available."),
            )
            return

        if not self._catalog_ready or self._catalog_dirty:
            self._runtime_waiting_for_catalog = True
            self._ensure_initial_catalog_refresh()
            self._show_runtime_status(
                running=False,
                message=self.tr("Remote control is preparing the media catalog."),
                status="pending",
            )
            return

        try:
            identity = self._tls_certificate_store().load_or_create(interface.ipv4_address)
            server = self._start_server(identity)
        except Exception:  # noqa: BLE001 - service lifecycle boundary
            log.exception("Could not start local remote control")
            self._show_runtime_status(
                running=False,
                message=self.tr("Remote control could not start on the selected network."),
            )
            return
        self._server = server
        self._tls_identity = identity
        self._next_tls_renewal_attempt_at = 0.0
        self._show_runtime_status(
            running=True,
            message=self.tr("Secure remote control is running."),
        )

    def _start_server(self, identity: TLSIdentity) -> RemoteControlServer:
        from ..core.foundation.resources import application_resource_path
        from ..core.remote_control.server import (
            RemoteControlServer,
            RemoteControlServerBinding,
            RemoteControlServerDependencies,
        )

        self._state.rotate_server_instance()
        server = RemoteControlServer(
            RemoteControlServerDependencies(
                state=self._state,
                command_session=ProjectionCommandSession(self._state),
                credentials=self._dependencies.credentials,
                sessions=self._sessions,
                rate_limiter=self._rate_limiter,
                assets_directory=application_resource_path("remote_control"),
                command_handler=self._bridge.execute,
                setup_provider=lambda current=identity: self._setup_payload(current),
                thumbnail_handler=self._load_thumbnail,
                collection_thumbnail_handler=self._load_collection_thumbnail,
                session_inventory_changed=self.session_inventory_reported.emit,
                profile_id=self._dependencies.active_profile_id,
                profile_name=self._dependencies.active_profile_name,
            )
        )
        server.start(
            RemoteControlServerBinding(
                host=str(identity.ipv4_address),
                port=REMOTE_CONTROL_PORT,
                tls_identity=identity,
            )
        )
        return server

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
                message=self.tr(
                    "Secure remote control is running. Certificate renewal will retry "
                    "automatically."
                ),
            )
            return
        self._tls_identity = renewed
        self._next_tls_renewal_attempt_at = 0.0
        self._show_runtime_status(
            running=True,
            message=self.tr("Secure remote control is running."),
        )

    def _setup_payload(self, identity: TLSIdentity) -> JsonObject:
        return {
            **self._localization_payload(),
            "tls": {
                "installationId": identity.installation_id,
                "authoritySha256": identity.trust_anchor_fingerprint_sha256,
                "verificationCode": self._verification_code(
                    identity.trust_anchor_fingerprint_sha256
                ),
                "authorityCertificateUrl": "/remote/trust-certificate.cer",
                "authorityNotValidAfter": identity.root_not_valid_after.isoformat(),
            },
        }

    @Slot()
    def _reconcile_runtime(self) -> None:
        settings = self._dependencies.settings
        if not settings.enabled() or not self._dependencies.credentials.has_credentials():
            if self.is_running:
                self.reconfigure()
            return
        selection = settings.network_selection()
        interface_available = (
            selection is not None and self._resolve_selected_interface(selection) is not None
        )
        if interface_available != self.is_running:
            self.reconfigure()
            return
        if (
            interface_available
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
    def credentials_changed(self) -> None:
        self.reconfigure()

    @Slot()
    def revoke_sessions(self) -> None:
        server = self._server
        if server is None:
            self._sessions.revoke_all()
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

    def _connect_publishers(self) -> None:
        dependencies = self._dependencies
        self._unsubscribers.append(
            dependencies.playlist_repository.subscribe(self.catalog_invalidated.emit)
        )
        self._unsubscribers.append(
            dependencies.meeting_tree_store.subscribe(self.catalog_invalidated.emit)
        )
        self._unsubscribers.append(
            dependencies.watched_folder_playlist_store.subscribe(self.catalog_invalidated.emit)
        )
        self._unsubscribers.append(
            dependencies.projection_session.subscribe(self.playback_invalidated.emit)
        )
        media = dependencies.media_controller
        media.state_changed.connect(self._on_media_state)
        media.duration_changed.connect(lambda _value: self.playback_invalidated.emit())
        media.position_changed.connect(lambda _value: self.playback_invalidated.emit())
        media.error_occurred.connect(self._on_media_error)
        dependencies.projection_bar.volume_changed.connect(
            lambda _value: self.playback_invalidated.emit()
        )
        dependencies.playback_protection.lockedChanged.connect(self.playback_invalidated.emit)
        dependencies.settings_widget.remote_control_settings_changed.connect(self.reconfigure)
        dependencies.settings_widget.remote_control_credentials_changed.connect(
            self.credentials_changed
        )
        dependencies.settings_widget.watched_folder_changed.connect(self._on_watched_folder_changed)

    @Slot(str)
    def _on_watched_folder_changed(self, _path: str) -> None:
        """Invalidate the remote catalog without forwarding the path payload."""

        self.catalog_invalidated.emit()

    @Slot()
    def _refresh_catalog(self) -> None:
        """Synchronously refresh for startup and explicit local operations."""

        self._catalog_refresh_generation += 1
        self._catalog_dirty = True
        self._catalog_refresh_pending = False
        self._catalog_refresh_timer.stop()
        try:
            build = self._catalog.build()
        except (CatalogResolutionError, TypeError, ValueError):
            log.warning("Could not refresh remote-control catalog", exc_info=True)
            return
        self._commit_catalog_build(build)

    @Slot()
    def _schedule_catalog_refresh(self) -> None:
        """Coalesce repository invalidations without blocking the Qt thread."""

        if self._catalog_refresh_shutdown:
            return
        self._catalog_refresh_generation += 1
        self._catalog_dirty = True
        if not self._dependencies.settings.enabled():
            return
        self._catalog_refresh_timer.start()

    @Slot()
    def _start_catalog_refresh(self) -> None:
        if (
            self._catalog_refresh_shutdown
            or not self._dependencies.settings.enabled()
        ):
            self._catalog_refresh_pending = False
            return
        if self._catalog_refresh_inflight is not None:
            self._catalog_refresh_pending = True
            return
        self._catalog_refresh_pending = False
        generation = self._catalog_refresh_generation
        future = self._catalog_refresh_executor.submit(self._catalog.build)
        self._catalog_refresh_inflight = (generation, future)
        future.add_done_callback(
            lambda completed, current_generation=generation: self._emit_catalog_build_completed(
                current_generation,
                completed,
            )
        )

    def _emit_catalog_build_completed(
        self,
        generation: int,
        future: Future[Any],
    ) -> None:
        try:
            build = future.result()
        except BaseException as error:  # noqa: BLE001 - worker boundary
            try:
                self._catalog_build_completed.emit(generation, None, error)
            except RuntimeError:
                pass
        else:
            try:
                self._catalog_build_completed.emit(generation, build, None)
            except RuntimeError:
                pass

    @Slot(int, object, object)
    def _on_catalog_build_completed(
        self,
        generation: int,
        build: object,
        error: BaseException | None,
    ) -> None:
        inflight = self._catalog_refresh_inflight
        if inflight is None or inflight[0] != generation:
            return
        self._catalog_refresh_inflight = None
        stale = generation != self._catalog_refresh_generation
        if error is not None:
            if not isinstance(error, (CatalogResolutionError, TypeError, ValueError)):
                log.warning(
                    "Could not refresh remote-control catalog",
                    exc_info=(type(error), error, error.__traceback__),
                )
            else:
                log.warning(
                    "Could not refresh remote-control catalog: %s",
                    error,
                )
        elif not stale and isinstance(build, CatalogBuild):
            self._commit_catalog_build(build)

        if self._catalog_refresh_pending or stale:
            self._catalog_refresh_pending = False
            if not self._dependencies.settings.enabled():
                return
            if self._catalog_refresh_timer.isActive():
                return
            QTimer.singleShot(0, self._start_catalog_refresh)

    def _commit_catalog_build(self, build: CatalogBuild) -> None:
        """Commit public and private catalog representations at one revision."""

        with self._catalog_publication_lock:
            previous_revision = self._state.catalog_revision
            published = self._state.update_catalog(
                build.collections,
                change_token=build.fingerprint,
            )
            private_snapshot = self._catalog.publish(
                build,
                revision=published.catalog_revision,
            )
            if private_snapshot != published:
                raise RuntimeError("Public and private catalog snapshots diverged")
        if published.catalog_revision != previous_revision:
            self._publish_snapshot()
        self._catalog_ready = True
        self._catalog_dirty = False
        if self._runtime_waiting_for_catalog:
            self._runtime_waiting_for_catalog = False
            self.reconfigure()

    @Slot(str)
    def on_language_changed(self, locale_code: str) -> None:
        """Publish the newly installed Qt locale and rebuild localized catalog titles."""

        self._localization = remote_control_localization(locale_code)
        if self._server is not None:
            self._server.publish_profile()
        self._schedule_catalog_refresh()

    def _localization_payload(self) -> JsonObject:
        messages = self._localization.get("messages")
        return {
            "locale": str(self._localization.get("locale") or "en"),
            "messages": dict(messages) if isinstance(messages, dict) else {},
        }

    @Slot()
    def _schedule_playback_refresh(self) -> None:
        if not self._playback_timer.isActive():
            self._playback_timer.start()

    @Slot(object)
    def _on_media_state(self, state) -> None:
        if state in (SolinPlaybackState.PLAYING, SolinPlaybackState.PAUSED):
            self._last_media_error = ""
        self._refresh_playback()

    @Slot(str)
    def _on_media_error(self, message: str) -> None:
        raw_message = str(message or "").strip()
        if raw_message:
            log.warning("Local media playback error: %s", raw_message)
        self._last_media_error = (
            public_reason(
                raw_message,
                fallback="The media could not be played.",
            )
            or "The media could not be played."
        )
        self._refresh_playback()

    @Slot()
    def _refresh_playback(self) -> None:
        previous_revision = self._state.playback_revision
        self._state.update_playback(self._build_playback_snapshot())
        if self._state.playback_revision != previous_revision:
            self._publish_snapshot()

    def _build_playback_snapshot(self) -> ProjectionSnapshot:
        dependencies = self._dependencies
        projection_session = dependencies.projection_session
        state_type = projection_session.state_type
        if state_type == "idle":
            return ProjectionSnapshot.idle()

        state = projection_session.state
        media_controller = dependencies.media_controller
        projection_bar = dependencies.projection_bar
        is_video = state_type == "video"
        media_playing = bool(media_controller.is_playing)
        media_paused = bool(media_controller.is_paused)
        error = None
        if is_video and self._last_media_error:
            playback_state = PlaybackState.ERROR
            error = ProjectionError("media_error", self._last_media_error)
        elif is_video and media_playing:
            playback_state = PlaybackState.PLAYING
        elif is_video and media_paused:
            playback_state = PlaybackState.PAUSED
        elif is_video:
            playback_state = PlaybackState.LOADING
        else:
            playback_state = PlaybackState.PAUSED

        queue = self._projection_queue()
        current_index = None
        if queue:
            current_index = max(
                0,
                min(int(projection_bar.playlist_index), len(queue) - 1),
            )
        origin = (
            queue[current_index].origin
            if current_index is not None
            else self._origin_from_state(state.get("origin"))
        )
        duration = max(0, int(media_controller.duration)) if is_video else None
        if duration == 0:
            duration = None
        position = max(0, int(media_controller.position)) if is_video else 0
        if duration is not None:
            position = min(position, duration)
        media_kind = self._media_kind_for_state(state_type, state)
        navigation_unlocked = not dependencies.playback_protection.locked
        capabilities = PlaybackCapabilities(
            can_pause=is_video and media_playing,
            can_resume=is_video and media_paused,
            can_seek=is_video and duration is not None and navigation_unlocked,
            can_set_volume=is_video,
            can_previous=projection_bar.can_navigate_previous(),
            can_next=projection_bar.can_navigate_next(),
            can_stop=True,
        )
        return ProjectionSnapshot(
            playback_revision=0,
            state=playback_state,
            playback_session_id=(
                f"projection-{projection_session.session_id}-media-{media_controller.session_id}"
            ),
            origin=origin,
            title=public_title(
                state.get("title")
                or projection_bar.current_media_title()
                or (queue[current_index].title if current_index is not None else ""),
                "Media",
            ),
            media_kind=media_kind,
            queue=queue,
            current_index=current_index,
            position_ms=position,
            duration_ms=duration,
            volume=projection_bar.volume_percent,
            capabilities=capabilities,
            error=error,
        )

    def _projection_queue(self) -> tuple[ProjectionQueueItem, ...]:
        queue: list[ProjectionQueueItem] = []
        for index, item in enumerate(self._dependencies.projection_bar.playlist_items()):
            origin = self._origin_from_item(item, index)
            queue.append(
                ProjectionQueueItem(
                    origin=origin,
                    title=public_title(item.get("title"), "Media"),
                    media_kind=self._media_kind(str(item.get("type") or "video")),
                    thumbnail_id=self._catalog_thumbnail_id(origin),
                )
            )
        return tuple(queue)

    def _catalog_thumbnail_id(self, origin: ProjectionOrigin) -> str | None:
        expected_kind = {
            ProjectionSource.PLAYLIST: CatalogKind.PLAYLIST,
            ProjectionSource.LINKED_FOLDER: CatalogKind.LINKED_FOLDER,
            ProjectionSource.MEETING: CatalogKind.MEETING,
        }.get(origin.source)
        if expected_kind is None or origin.collection_id is None or origin.node_id is None:
            return None
        collection = next(
            (
                value
                for value in self._state.catalog.collections
                if value.id == origin.collection_id and value.kind is expected_kind
            ),
            None,
        )
        node = self._find_catalog_node(collection.nodes if collection else (), origin.node_id)
        return node.thumbnail_id if node is not None else None

    def _origin_from_item(
        self,
        item: Mapping[str, Any],
        index: int,
    ) -> ProjectionOrigin:
        source = RemoteControlController._projection_source(
            str(item.get("origin_kind") or "temporary")
        )
        collection_id = str(item.get("origin_container_id") or "") or None
        node_id = str(item.get("origin_item_id") or item.get("id") or f"queue-{index}")
        if source is ProjectionSource.TEMPORARY:
            return self._temporary_origin(index)
        catalog_origin = self._catalog_origin(source, collection_id, node_id)
        return catalog_origin or self._temporary_origin(index)

    def _origin_from_state(self, value: object) -> ProjectionOrigin | None:
        if not isinstance(value, Mapping):
            return None
        source = RemoteControlController._projection_source(str(value.get("kind") or ""))
        collection_id = str(value.get("container_id") or "") or None
        node_id = str(value.get("item_id") or "") or None
        if source is ProjectionSource.TEMPORARY:
            return self._temporary_origin(0) if node_id else None
        return self._catalog_origin(source, collection_id, node_id)

    def _catalog_origin(
        self,
        source: ProjectionSource,
        collection_id: str | None,
        node_id: str | None,
    ) -> ProjectionOrigin | None:
        expected_kind = {
            ProjectionSource.PLAYLIST: CatalogKind.PLAYLIST,
            ProjectionSource.LINKED_FOLDER: CatalogKind.LINKED_FOLDER,
            ProjectionSource.MEETING: CatalogKind.MEETING,
        }.get(source)
        if expected_kind is None or not collection_id or not node_id:
            return None
        collection = next(
            (
                value
                for value in self._state.catalog.collections
                if value.id == collection_id and value.kind is expected_kind
            ),
            None,
        )
        if collection is None or self._find_catalog_node(collection.nodes, node_id) is None:
            return None
        return ProjectionOrigin(source, collection_id, node_id)

    @staticmethod
    def _temporary_origin(index: int) -> ProjectionOrigin:
        return ProjectionOrigin(
            ProjectionSource.TEMPORARY,
            node_id=f"queue-{index}",
        )

    @staticmethod
    def _projection_source(value: str) -> ProjectionSource:
        aliases = {"watched_folder": ProjectionSource.LINKED_FOLDER}
        if value in aliases:
            return aliases[value]
        try:
            return ProjectionSource(value)
        except ValueError:
            return ProjectionSource.TEMPORARY

    @staticmethod
    def _media_kind(value: str) -> RemoteMediaKind:
        try:
            return RemoteMediaKind(value)
        except ValueError:
            return RemoteMediaKind.UNKNOWN

    @classmethod
    def _media_kind_for_state(
        cls,
        state_type: str,
        state: Mapping[str, Any],
    ) -> RemoteMediaKind:
        if state_type == "video":
            return RemoteMediaKind.AUDIO if state.get("is_audio") else RemoteMediaKind.VIDEO
        if state_type == "image" and state.get("generated_kind") == "talk_theme":
            return RemoteMediaKind.SCREEN
        return {
            "image": RemoteMediaKind.IMAGE,
            "browser": RemoteMediaKind.BROWSER,
            "timer": RemoteMediaKind.SCREEN,
            "obs_stream": RemoteMediaKind.SCREEN,
        }.get(state_type, RemoteMediaKind.UNKNOWN)

    def _execute_command(self, command: RemoteCommand) -> CommandError | None:
        if precondition_error := self._state.validate_command_preconditions(command):
            return precondition_error
        if isinstance(command, PlayCommand):
            return self._execute_play(command)
        snapshot = self._state.playback
        if isinstance(command, StopCommand):
            if snapshot.state is not PlaybackState.IDLE:
                self._dependencies.projection_stop.stop_projection()
            self._refresh_playback()
            return None
        if snapshot.state is PlaybackState.IDLE:
            return CommandError(CommandErrorCode.BLOCKED, "Nothing is being projected.")
        if isinstance(command, PauseCommand):
            if not snapshot.capabilities.can_pause:
                return CommandError(CommandErrorCode.BLOCKED, "Playback cannot be paused now.")
            self._dependencies.media_controller.pause()
        elif isinstance(command, ResumeCommand):
            if not snapshot.capabilities.can_resume:
                return CommandError(CommandErrorCode.BLOCKED, "Playback cannot be resumed now.")
            self._dependencies.media_controller.play()
        elif isinstance(command, SeekCommand):
            if not snapshot.capabilities.can_seek:
                return CommandError(CommandErrorCode.BLOCKED, "Seeking is unavailable now.")
            if snapshot.duration_ms is not None and command.position_ms > snapshot.duration_ms:
                return CommandError(CommandErrorCode.INVALID, "Seek position is out of range.")
            if not self._dependencies.playback_protection.request_seek(command.position_ms):
                return CommandError(CommandErrorCode.BLOCKED, "Pause playback before seeking.")
        elif isinstance(command, SetVolumeCommand):
            if not snapshot.capabilities.can_set_volume:
                return CommandError(CommandErrorCode.BLOCKED, "Volume is unavailable now.")
            self._dependencies.projection_bar.set_volume_percent(command.volume)
        elif isinstance(command, PreviousCommand):
            if not self._dependencies.projection_bar.navigate_previous():
                return CommandError(CommandErrorCode.BLOCKED, "There is no previous item.")
        elif isinstance(command, NextCommand):
            if not self._dependencies.projection_bar.navigate_next():
                return CommandError(CommandErrorCode.BLOCKED, "There is no next item.")
        self._refresh_playback()
        return None

    def _execute_play(self, command: PlayCommand) -> CommandError | None:
        if not self._dependencies.playback_protection.allow_manual_projection_change(notify=False):
            return CommandError(
                CommandErrorCode.BLOCKED,
                "Pause the current playback before changing media.",
            )
        try:
            resolved = self._catalog.resolve_play(command)
        except CatalogResolutionError as error:
            return self._catalog_error(error)
        if isinstance(resolved, ResolvedPlaylistPlay):
            self._dependencies.media_projection.project_media_at_index(
                list(resolved.items),
                resolved.current_index,
            )
        elif isinstance(resolved, ResolvedMeetingPlay):
            self._dependencies.media_projection.on_meeting_media_project(resolved.media)
        if resolved.start_paused and self._dependencies.projection_session.state_type == "video":
            self._dependencies.media_controller.pause()
        self._last_media_error = ""
        self._refresh_playback()
        return None

    @staticmethod
    def _catalog_error(error: CatalogResolutionError) -> CommandError:
        if error.code is CatalogResolutionCode.STALE_CATALOG:
            code = CommandErrorCode.CATALOG_STALE
            retryable = True
        elif error.code in (
            CatalogResolutionCode.COLLECTION_NOT_FOUND,
            CatalogResolutionCode.NODE_NOT_FOUND,
        ):
            code = CommandErrorCode.NOT_FOUND
            retryable = False
        elif error.code is CatalogResolutionCode.INVALID_CATALOG:
            code = CommandErrorCode.FAILED
            retryable = False
        else:
            code = CommandErrorCode.UNAVAILABLE
            retryable = False
        return CommandError(code, str(error), retryable=retryable)

    def _load_linked_playlists(self) -> list[dict[str, Any]]:
        root = self._dependencies.watched_folder_settings.path()
        if not root:
            return []
        store = self._dependencies.watched_folder_playlist_store
        return store.load_all_playlists(root)

    @staticmethod
    def _media_availability(
        kind: CatalogKind,
        _collection_id: str,
        raw: Mapping[str, Any],
    ) -> MediaAvailability:
        if kind is CatalogKind.MEETING:
            media_ref = raw.get("media_ref")
            if not isinstance(media_ref, Mapping):
                return MediaAvailability(False, "Unavailable")
            location = str(media_ref.get("file_path") or "")
            resolvable = bool(
                media_ref.get("key_symbol")
                or media_ref.get("meps_doc_id")
                or media_ref.get("multimedia_id")
            )
        else:
            location = str(raw.get("url") or "")
            resolvable = False
        if location.startswith(("http://", "https://")):
            return MediaAvailability(True)
        if location and os.path.isfile(location):
            return MediaAvailability(True)
        if resolvable:
            return MediaAvailability(True)
        return MediaAvailability(False, "Unavailable")

    def _thumbnail_id(
        self,
        kind: CatalogKind,
        collection_id: str,
        raw: Mapping[str, Any],
    ) -> str | None:
        node_id = str(raw.get("id") or "")
        if not node_id:
            return None
        media_ref_value = raw.get("media_ref")
        media_ref = media_ref_value if isinstance(media_ref_value, Mapping) else {}
        source_key = str(
            raw.get("thumbnail_url")
            or raw.get("url")
            or raw.get("resolved_url")
            or media_ref.get("file_path")
            or ""
        )
        if kind is CatalogKind.MEETING:
            base_id = (
                meeting_thumb_storage_id(collection_id, node_id)
                if raw.get("meeting_generated")
                else node_id
            )
            store = self._dependencies.meeting_thumbnail_store
        else:
            base_id = node_id
            store = self._dependencies.playlist_thumbnail_store
        storage_id = thumbnail_storage_id(base_id, source_key)
        if store.exists(storage_id):
            return storage_id
        source = private_media_thumbnail_source(raw)
        if source is None:
            return None
        return storage_id if source.is_remote or os.path.isfile(source.location) else None

    async def _load_thumbnail(
        self,
        source: str,
        collection_id: str,
        node_id: str,
    ) -> bytes | None:
        path = self._thumbnail_path(source, collection_id, node_id)
        if path is not None:
            try:
                return await asyncio.to_thread(path.read_bytes)
            except OSError:
                pass
        catalog_revision = self._state.catalog_revision
        key = (catalog_revision, source, collection_id, node_id)
        fallback = self._fallback_thumbnail(key)
        if fallback is not None:
            return fallback
        task = self._thumbnail_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._generate_and_cache_thumbnail(
                    source,
                    collection_id,
                    node_id,
                    expected_catalog_revision=catalog_revision,
                    request_key=key,
                )
            )
            self._thumbnail_tasks[key] = task
            task.add_done_callback(
                lambda completed, current_key=key: self._forget_thumbnail_task(
                    current_key,
                    completed,
                )
            )
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._thumbnail_tasks.get(key) is task:
                self._thumbnail_tasks.pop(key, None)

    async def _load_collection_thumbnail(
        self,
        source: str,
        collection_id: str,
    ) -> bytes | None:
        if source != ProjectionSource.MEETING.value:
            return None
        cover = self._catalog.resolve_collection_thumbnail(
            ProjectionSource.MEETING,
            collection_id,
        )
        if cover is None:
            return None
        return await asyncio.to_thread(render_image_thumbnail_bytes, cover)

    def _forget_thumbnail_task(
        self,
        key: ThumbnailRequestKey,
        task: asyncio.Task[bytes | None],
    ) -> None:
        if self._thumbnail_tasks.get(key) is task:
            self._thumbnail_tasks.pop(key, None)

    async def _generate_and_cache_thumbnail(
        self,
        source: str,
        collection_id: str,
        node_id: str,
        *,
        expected_catalog_revision: int,
        request_key: ThumbnailRequestKey,
    ) -> bytes | None:
        with self._catalog_publication_lock:
            if self._state.catalog_revision != expected_catalog_revision:
                return None
            target = self._thumbnail_target(source, collection_id, node_id)
            if target is None:
                return None
            origin, store, storage_id = target
            media = self._catalog.resolve_local_media(origin)
            if media is None:
                return None
        if media.extraction_kind is MediaKind.IMAGE and not media.is_remote:
            data = await asyncio.to_thread(
                render_local_image_thumbnail,
                media.location,
            )
        else:
            data = await self._dependencies.media_thumbnail_extractor.extract(
                media.location,
                media.extraction_kind,
            )
        if self._state.catalog_revision != expected_catalog_revision:
            return None
        if not data:
            data = await asyncio.to_thread(
                render_media_placeholder_thumbnail,
                media.placeholder_kind,
            )
            if data:
                self._cache_fallback_thumbnail(request_key, data)
            return data
        try:
            await asyncio.to_thread(store.save_bytes, storage_id, data)
        except OSError:
            log.warning("Could not cache remote-control media thumbnail", exc_info=True)
        return data

    def _fallback_thumbnail(self, key: ThumbnailRequestKey) -> bytes | None:
        entry = self._fallback_thumbnails.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at <= time.monotonic():
            self._fallback_thumbnails.pop(key, None)
            return None
        self._fallback_thumbnails.move_to_end(key)
        return data

    def _cache_fallback_thumbnail(self, key: ThumbnailRequestKey, data: bytes) -> None:
        self._fallback_thumbnails[key] = (
            time.monotonic() + _FALLBACK_THUMBNAIL_TTL_SECONDS,
            data,
        )
        self._fallback_thumbnails.move_to_end(key)
        while len(self._fallback_thumbnails) > _MAX_FALLBACK_THUMBNAILS:
            self._fallback_thumbnails.popitem(last=False)

    def _thumbnail_path(
        self,
        source: str,
        collection_id: str,
        node_id: str,
    ) -> Path | None:
        target = self._thumbnail_target(source, collection_id, node_id)
        if target is None:
            return None
        _origin, store, storage_id = target
        return store.path(storage_id) if store.exists(storage_id) else None

    def _thumbnail_target(
        self,
        source: str,
        collection_id: str,
        node_id: str,
    ) -> tuple[ProjectionOrigin, Any, str] | None:
        source_and_kind = {
            ProjectionSource.PLAYLIST.value: (
                ProjectionSource.PLAYLIST,
                CatalogKind.PLAYLIST,
            ),
            ProjectionSource.LINKED_FOLDER.value: (
                ProjectionSource.LINKED_FOLDER,
                CatalogKind.LINKED_FOLDER,
            ),
            ProjectionSource.MEETING.value: (
                ProjectionSource.MEETING,
                CatalogKind.MEETING,
            ),
        }.get(source)
        if source_and_kind is None:
            return None
        projection_source, expected_kind = source_and_kind
        collection = next(
            (
                value
                for value in self._state.catalog.collections
                if value.id == collection_id and value.kind is expected_kind
            ),
            None,
        )
        node = self._find_catalog_node(collection.nodes if collection else (), node_id)
        if node is None or node.thumbnail_id is None:
            return None
        store = (
            self._dependencies.meeting_thumbnail_store
            if expected_kind is CatalogKind.MEETING
            else self._dependencies.playlist_thumbnail_store
        )
        return (
            ProjectionOrigin(projection_source, collection_id, node_id),
            store,
            node.thumbnail_id,
        )

    @classmethod
    def _find_catalog_node(cls, nodes, node_id: str):
        for node in nodes:
            if node.id == node_id:
                return node
            if found := cls._find_catalog_node(node.children, node_id):
                return found
        return None

    def _show_runtime_status(
        self,
        *,
        running: bool,
        message: str,
        status: str | None = None,
    ) -> None:
        self.runtime_status_reported.emit(
            self._dependencies.settings.enabled(),
            running,
            message,
        )
        identity = self._tls_identity
        binding = getattr(self._server, "binding", None)
        access_url = binding.url if binding is not None and running else ""
        self._dependencies.settings_widget.set_remote_control_runtime_status(
            running=running,
            message=message,
            fingerprint=identity.trust_anchor_fingerprint_sha256 if identity else "",
            access_url=access_url,
            setup_url=f"{access_url}?setup=1" if access_url else "",
            verification_code=(
                self._verification_code(identity.trust_anchor_fingerprint_sha256)
                if identity
                else ""
            ),
            certificate_der=(identity.trust_certificate_der() if identity and running else b""),
            status=status,
        )

    def _ensure_initial_catalog_refresh(self) -> None:
        if self._catalog_refresh_shutdown or self._catalog_refresh_inflight is not None:
            return
        self._catalog_refresh_generation += 1
        self._catalog_refresh_timer.stop()
        self._start_catalog_refresh()

    def _tls_certificate_store(self):
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

    def _publish_snapshot(self) -> None:
        if self._server is not None:
            self._server.publish_snapshot()

    def _stop_server(self) -> bool:
        server = self._server
        if server is None:
            self._tls_identity = None
            self._thumbnail_tasks.clear()
            self._fallback_thumbnails.clear()
            return True
        try:
            server.stop()
        except TimeoutError:
            log.error("Timed out stopping local remote control", exc_info=True)
            return False
        self._server = None
        self._tls_identity = None
        self._thumbnail_tasks.clear()
        self._fallback_thumbnails.clear()
        return True
