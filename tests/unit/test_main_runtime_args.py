from __future__ import annotations

import sys
from types import SimpleNamespace

from solin.bootstrap import application as main
from solin.bootstrap import profile_flow
from solin.bootstrap.runtime_args import parse_runtime_args


def test_parse_runtime_args_handles_profile_flags_and_existing_files(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"")

    runtime_args = parse_runtime_args([
        "solin",
        "--profile",
        "profile-1",
        "--create-profile",
        str(media_file),
        str(tmp_path / "missing.mp4"),
    ])

    assert runtime_args.requested_profile_id == "profile-1"
    assert runtime_args.create_profile is True
    assert runtime_args.media_files == (str(media_file),)


def test_parse_runtime_args_supports_profile_equals_form(tmp_path):
    csv_file = tmp_path / "poll.csv"
    csv_file.write_bytes(b"")

    runtime_args = parse_runtime_args([
        "solin",
        "--profile=profile-2",
        str(csv_file),
    ])

    assert runtime_args.requested_profile_id == "profile-2"
    assert runtime_args.create_profile is False
    assert runtime_args.media_files == (str(csv_file),)


def test_launch_main_window_schedules_startup_media_after_window_is_shown(monkeypatch):
    events = []
    file_args = ["clip.mp4", "song.mp3"]
    runtime_paths = SimpleNamespace(pending_del_file="pending.json")
    profile_paths = SimpleNamespace(
        playlists_file="playlists.json",
        meeting_trees_file="meeting_trees.json",
        embedded_dir="embedded",
        images_dir="images",
    )
    runtime_paths.thumb_cache_dir = "thumbs"
    runtime_paths.meeting_thumb_cache_dir = "meeting_thumbs"
    runtime_paths.pdf_pages_dir = "pdf_pages"
    runtime_paths.pptx_pages_dir = "pptx_pages"
    runtime_paths.docx_pages_dir = "docx_pages"
    profile_settings = object()
    media_cache_manager = object()
    media_settings = object()

    class _MediaController:
        def __init__(self):
            self.parent = None

        def setParent(self, parent):
            self.parent = parent

    created_media_controllers = [_MediaController(), _MediaController()]
    pending_media_controllers = list(created_media_controllers)
    media = SimpleNamespace(
        cache_manager=media_cache_manager,
        create_playback=lambda settings: (
            pending_media_controllers.pop(0)
            if settings is media_settings
            else None
        ),
        create_info_queue=object(),
        create_info_service=object(),
        create_browser_download_service=object(),
        create_browser_image_fetch_service=object(),
    )
    font_manager = object()
    jw_catalog_cache_paths = object()
    jw_songs_store = object()
    jwpub_checksum_store = object()
    installation_settings = object()
    timer_session = object()
    active_profile = object()

    class _MainWindow:
        def __init__(
            self,
            lang_manager,
            received_runtime_paths,
            received_profile_paths,
            received_profile_settings,
            received_media_cache_manager,
            received_media_controller,
            received_background_media_controller,
            received_media_info_queue_factory,
            received_media_info_service_factory,
            received_browser_download_service_factory,
            received_browser_image_fetch_service_factory,
            received_media_settings,
            received_font_manager,
            received_jw_catalog_service_factory,
            received_jw_catalog_thumbnail_session_factory,
            received_jw_songs_store,
            received_jwpub_checksum_store,
            received_installation_settings,
            playlist_storage_paths,
            playlist_repository,
            meeting_tree_store,
            profile_media_store,
            jwpub_import_thread_factory,
            document_conversion_service,
            clip_fetch_thread_factory,
            cache_scan_session_factory,
            qr_generation_session_factory,
            playlist_thumbnail_store,
            meeting_thumbnail_store,
            watched_folder_file_store,
            watched_folder_playlist_store,
            wifi_receive_server_factory,
            watched_folder_watcher_factory,
            playlist_cleanup_queue_factory,
            received_timer_session,
            received_active_profile,
        ):
            self.lang_manager = lang_manager
            self.runtime_paths = received_runtime_paths
            self.profile_paths = received_profile_paths
            self.profile_settings = received_profile_settings
            self.media_cache_manager = received_media_cache_manager
            self.media_controller = received_media_controller
            self.background_media_controller = received_background_media_controller
            self.media_info_queue_factory = received_media_info_queue_factory
            self.media_info_service_factory = received_media_info_service_factory
            self.browser_download_service_factory = (
                received_browser_download_service_factory
            )
            self.browser_image_fetch_service_factory = (
                received_browser_image_fetch_service_factory
            )
            self.media_settings = received_media_settings
            self.font_manager = received_font_manager
            self.jw_catalog_service_factory = received_jw_catalog_service_factory
            self.jw_catalog_thumbnail_session_factory = (
                received_jw_catalog_thumbnail_session_factory
            )
            self.jw_songs_store = received_jw_songs_store
            self.jwpub_checksum_store = received_jwpub_checksum_store
            self.installation_settings = received_installation_settings
            self.playlist_storage_paths = playlist_storage_paths
            self.playlist_repository = playlist_repository
            self.meeting_tree_store = meeting_tree_store
            self.profile_media_store = profile_media_store
            self.jwpub_import_thread_factory = jwpub_import_thread_factory
            self.document_conversion_service = document_conversion_service
            self.clip_fetch_thread_factory = clip_fetch_thread_factory
            self.cache_scan_session_factory = cache_scan_session_factory
            self.qr_generation_session_factory = qr_generation_session_factory
            self.playlist_thumbnail_store = playlist_thumbnail_store
            self.meeting_thumbnail_store = meeting_thumbnail_store
            self.watched_folder_file_store = watched_folder_file_store
            self.watched_folder_playlist_store = watched_folder_playlist_store
            self.wifi_receive_server_factory = wifi_receive_server_factory
            self.watched_folder_watcher_factory = watched_folder_watcher_factory
            self.playlist_cleanup_queue_factory = playlist_cleanup_queue_factory
            self.timer_session = received_timer_session
            self.active_profile = received_active_profile

        def show(self):
            events.append("show")

        def open_media_files(self, paths):
            events.append(("open_media_files", paths))

    monkeypatch.setitem(
        sys.modules,
        "solin.main_window",
        SimpleNamespace(MainWindow=_MainWindow),
    )
    monkeypatch.setattr(
        "solin.core.ui.titlebar.apply_titlebar_color",
        lambda window, color: events.append(("titlebar", window, color)),
    )
    monkeypatch.setattr(
        main,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: events.append(("timer", ms)) or callback()
        ),
    )
    monkeypatch.setattr(
        "solin.core.media.settings.MediaSettingsStore.for_profile_settings",
        lambda _settings: media_settings,
    )

    window = main._launch_main_window(
        app=object(),
        lang_manager="lang",
        file_args=file_args,
        runtime_paths=runtime_paths,
        profile_paths=profile_paths,
        profile_settings=profile_settings,
        media=media,
        font_manager=font_manager,
        jw_catalog_cache_paths=jw_catalog_cache_paths,
        jw_songs_store=jw_songs_store,
        jwpub_checksum_store=jwpub_checksum_store,
        installation_settings=installation_settings,
        timer_session=timer_session,
        active_profile=active_profile,
    )

    assert window.lang_manager == "lang"
    assert window.runtime_paths is runtime_paths
    assert window.profile_paths is profile_paths
    assert window.profile_settings is profile_settings
    assert window.media_cache_manager is media_cache_manager
    assert window.media_settings is media_settings
    assert window.media_info_queue_factory is media.create_info_queue
    assert window.media_info_service_factory is media.create_info_service
    assert (
        window.browser_download_service_factory
        is media.create_browser_download_service
    )
    assert (
        window.browser_image_fetch_service_factory
        is media.create_browser_image_fetch_service
    )
    assert all(controller.parent is window for controller in created_media_controllers)
    assert window.font_manager is font_manager
    assert callable(window.jw_catalog_service_factory)
    assert (
        window.jw_catalog_thumbnail_session_factory.__class__.__name__
        == "JWCatalogThumbnailSessionFactory"
    )
    assert window.jw_songs_store is jw_songs_store
    assert window.jwpub_checksum_store is jwpub_checksum_store
    assert window.installation_settings is installation_settings
    assert window.playlist_storage_paths.playlists_file == "playlists.json"
    assert window.playlist_storage_paths.pending_deletions_file == "pending.json"
    assert str(window.playlist_repository.path) == "playlists.json"
    assert str(window.meeting_tree_store.path) == profile_paths.meeting_trees_file
    assert str(window.profile_media_store.embedded_dir) == "embedded"
    assert str(window.profile_media_store.images_dir) == "images"
    assert (
        window.jwpub_import_thread_factory.__class__.__name__
        == "JwpubImportThreadFactory"
    )
    assert (
        window.document_conversion_service.__class__.__name__
        == "DocumentConversionService"
    )
    assert (
        window.clip_fetch_thread_factory.__class__.__name__
        == "ClipFetchThreadFactory"
    )
    assert (
        window.cache_scan_session_factory.__class__.__name__
        == "CacheScanSessionFactory"
    )
    assert (
        window.qr_generation_session_factory.__class__.__name__
        == "QrGenerationSessionFactory"
    )
    assert str(window.playlist_thumbnail_store.root) == "thumbs"
    assert str(window.meeting_thumbnail_store.root) == "meeting_thumbs"
    assert (
        window.watched_folder_file_store.__class__.__name__
        == "WatchedFolderFileStore"
    )
    assert (
        window.watched_folder_playlist_store.__class__.__name__
        == "WatchedFolderPlaylistStore"
    )
    assert callable(window.wifi_receive_server_factory)
    assert callable(window.watched_folder_watcher_factory)
    assert callable(window.playlist_cleanup_queue_factory)
    assert window.timer_session is timer_session
    assert window.active_profile is active_profile
    assert events == [
        "show",
        ("titlebar", window, "#1A231F"),
        ("timer", 200),
        ("open_media_files", file_args),
    ]


def test_launch_main_window_without_startup_media_does_not_schedule_open(monkeypatch):
    events = []
    runtime_paths = SimpleNamespace(pending_del_file="pending.json")
    profile_paths = SimpleNamespace(
        playlists_file="playlists.json",
        meeting_trees_file="meeting_trees.json",
        embedded_dir="embedded",
        images_dir="images",
    )
    runtime_paths.thumb_cache_dir = "thumbs"
    runtime_paths.meeting_thumb_cache_dir = "meeting_thumbs"
    runtime_paths.pdf_pages_dir = "pdf_pages"
    runtime_paths.pptx_pages_dir = "pptx_pages"
    runtime_paths.docx_pages_dir = "docx_pages"
    profile_settings = object()
    media_cache_manager = object()
    media_settings = object()

    class _MediaController:
        def setParent(self, _parent):
            pass

    media = SimpleNamespace(
        cache_manager=media_cache_manager,
        create_playback=lambda _settings: _MediaController(),
        create_info_queue=object(),
        create_info_service=object(),
        create_browser_download_service=object(),
        create_browser_image_fetch_service=object(),
    )
    font_manager = object()
    jw_catalog_cache_paths = object()
    jw_songs_store = object()
    jwpub_checksum_store = object()
    installation_settings = object()
    timer_session = object()
    active_profile = object()

    class _MainWindow:
        def __init__(
            self,
            lang_manager,
            received_runtime_paths,
            received_profile_paths,
            received_profile_settings,
            received_media_cache_manager,
            received_media_controller,
            received_background_media_controller,
            received_media_info_queue_factory,
            received_media_info_service_factory,
            received_browser_download_service_factory,
            received_browser_image_fetch_service_factory,
            received_media_settings,
            received_font_manager,
            received_jw_catalog_service_factory,
            received_jw_catalog_thumbnail_session_factory,
            received_jw_songs_store,
            received_jwpub_checksum_store,
            received_installation_settings,
            playlist_storage_paths,
            playlist_repository,
            meeting_tree_store,
            profile_media_store,
            jwpub_import_thread_factory,
            document_conversion_service,
            clip_fetch_thread_factory,
            cache_scan_session_factory,
            qr_generation_session_factory,
            playlist_thumbnail_store,
            meeting_thumbnail_store,
            watched_folder_file_store,
            watched_folder_playlist_store,
            wifi_receive_server_factory,
            watched_folder_watcher_factory,
            playlist_cleanup_queue_factory,
            received_timer_session,
            received_active_profile,
        ):
            self.lang_manager = lang_manager
            self.runtime_paths = received_runtime_paths
            self.profile_paths = received_profile_paths
            self.profile_settings = received_profile_settings
            self.media_cache_manager = received_media_cache_manager
            self.media_controller = received_media_controller
            self.background_media_controller = received_background_media_controller
            self.media_info_queue_factory = received_media_info_queue_factory
            self.media_info_service_factory = received_media_info_service_factory
            self.browser_download_service_factory = (
                received_browser_download_service_factory
            )
            self.browser_image_fetch_service_factory = (
                received_browser_image_fetch_service_factory
            )
            self.media_settings = received_media_settings
            self.font_manager = received_font_manager
            self.jw_catalog_service_factory = received_jw_catalog_service_factory
            self.jw_catalog_thumbnail_session_factory = (
                received_jw_catalog_thumbnail_session_factory
            )
            self.jw_songs_store = received_jw_songs_store
            self.jwpub_checksum_store = received_jwpub_checksum_store
            self.installation_settings = received_installation_settings
            self.playlist_storage_paths = playlist_storage_paths
            self.playlist_repository = playlist_repository
            self.meeting_tree_store = meeting_tree_store
            self.profile_media_store = profile_media_store
            self.jwpub_import_thread_factory = jwpub_import_thread_factory
            self.document_conversion_service = document_conversion_service
            self.clip_fetch_thread_factory = clip_fetch_thread_factory
            self.cache_scan_session_factory = cache_scan_session_factory
            self.qr_generation_session_factory = qr_generation_session_factory
            self.playlist_thumbnail_store = playlist_thumbnail_store
            self.meeting_thumbnail_store = meeting_thumbnail_store
            self.watched_folder_file_store = watched_folder_file_store
            self.watched_folder_playlist_store = watched_folder_playlist_store
            self.wifi_receive_server_factory = wifi_receive_server_factory
            self.watched_folder_watcher_factory = watched_folder_watcher_factory
            self.playlist_cleanup_queue_factory = playlist_cleanup_queue_factory
            self.timer_session = received_timer_session
            self.active_profile = received_active_profile

        def show(self):
            events.append("show")

    monkeypatch.setitem(
        sys.modules,
        "solin.main_window",
        SimpleNamespace(MainWindow=_MainWindow),
    )
    monkeypatch.setattr(
        "solin.core.ui.titlebar.apply_titlebar_color",
        lambda window, color: events.append(("titlebar", color)),
    )
    monkeypatch.setattr(
        main,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda ms, callback: events.append(("timer", ms)) or callback()
        ),
    )
    monkeypatch.setattr(
        "solin.core.media.settings.MediaSettingsStore.for_profile_settings",
        lambda _settings: media_settings,
    )

    main._launch_main_window(
        app=object(),
        lang_manager="lang",
        file_args=[],
        runtime_paths=runtime_paths,
        profile_paths=profile_paths,
        profile_settings=profile_settings,
        media=media,
        font_manager=font_manager,
        jw_catalog_cache_paths=jw_catalog_cache_paths,
        jw_songs_store=jw_songs_store,
        jwpub_checksum_store=jwpub_checksum_store,
        installation_settings=installation_settings,
        timer_session=timer_session,
        active_profile=active_profile,
    )

    assert events == ["show", ("titlebar", "#1A231F")]


def test_run_zoom_poll_standalone_keeps_window_alive_until_event_loop(monkeypatch):
    events = []
    window = object()

    class _App:
        def exec(self):
            events.append(("exec", getattr(self, "_solin_zoom_poll_window", None)))
            return 7

    def _launch(filepath, lang_manager):
        events.append(("launch", filepath, lang_manager))
        return window

    monkeypatch.setattr(main, "_launch_zoom_poll_window", _launch)

    app = _App()

    assert main._run_zoom_poll_standalone(app, "poll.csv", "lang") == 7
    assert app._solin_zoom_poll_window is window
    assert events == [
        ("launch", "poll.csv", "lang"),
        ("exec", window),
    ]


def test_relaunch_with_profile_persists_target_profile_starts_new_process_and_quits(
    monkeypatch,
):
    launches = []
    stopped = []

    class _ProfileService:
        def __init__(self):
            self.remembered = []

        def get_profile(self, profile_id):
            return object() if profile_id == "profile-b" else None

        def remember_profile_for_next_launch(self, profile_id):
            self.remembered.append(profile_id)

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0
            self.enabled = []
            self.shown = 0

        def close(self):
            self.closed += 1

        def setEnabled(self, enabled):
            self.enabled.append(enabled)

        def show(self):
            self.shown += 1

    app = _App()
    window = _Window()
    profile_service = _ProfileService()
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: stopped.append((app, window)) or "ipc",
    )
    monkeypatch.setattr(profile_flow.single_instance, "restart_process_ipc", lambda *args: None)
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(
            startDetached=lambda program, args, cwd: launches.append(
                (program, args, cwd)
            )
            or True
        ),
    )
    monkeypatch.setattr(profile_flow.sys, "executable", "python.exe")
    monkeypatch.delattr(profile_flow.sys, "frozen", raising=False)

    profile_flow.relaunch_with_profile(
        app,
        "profile-b",
        window,
        profile_service,
    )

    assert profile_service.remembered == ["profile-b"]
    assert stopped == [(app, window)]
    assert launches[0][0] == "python.exe"
    assert launches[0][1][-2:] == ["--profile", "profile-b"]
    assert launches[0][1][0].endswith("main.py")
    assert window.closed == 1
    assert window.enabled == []
    assert window.shown == 0
    assert app.events == ["processEvents", "quit"]


def test_relaunch_with_profile_restores_window_and_ipc_when_new_process_fails(
    monkeypatch,
):
    restored = []

    class _ProfileService:
        def __init__(self):
            self.remembered = []

        def get_profile(self, profile_id):
            return object()

        def remember_profile_for_next_launch(self, profile_id):
            self.remembered.append(profile_id)

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0
            self.enabled = []
            self.shown = 0

        def close(self):
            self.closed += 1

        def setEnabled(self, enabled):
            self.enabled.append(enabled)

        def show(self):
            self.shown += 1

    app = _App()
    window = _Window()
    profile_service = _ProfileService()
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: "ipc-server",
    )
    monkeypatch.setattr(
        profile_flow.single_instance,
        "restart_process_ipc",
        lambda app, server, window: restored.append((app, server, window)),
    )
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(startDetached=lambda *_args: False),
    )

    profile_flow.relaunch_with_profile(
        app,
        "profile-b",
        window,
        profile_service,
    )

    assert profile_service.remembered == ["profile-b"]
    assert window.enabled == [True]
    assert window.shown == 1
    assert window.closed == 0
    assert restored == [(app, "ipc-server", window)]
    assert app.events == []


def test_relaunch_with_profile_ignores_unknown_profile(monkeypatch):
    launches = []

    class _ProfileService:
        def get_profile(self, profile_id):
            return None

    profile_service = _ProfileService()
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda *_args: launches.append("stop"),
    )
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(startDetached=lambda *_args: launches.append("launch")),
    )

    profile_flow.relaunch_with_profile(
        app=object(),
        profile_id="missing",
        window=object(),
        profile_service=profile_service,
    )

    assert launches == []


def test_relaunch_to_profile_creator_persists_bootstrap_language_and_quits(
    monkeypatch,
):
    launches = []
    stopped = []

    class _ProfileService:
        def __init__(self):
            self.prepared = 0

        def prepare_profile_creation(self):
            self.prepared += 1

    class _App:
        def __init__(self):
            self.events = []

        def processEvents(self):
            self.events.append("processEvents")

        def quit(self):
            self.events.append("quit")

    class _Window:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    app = _App()
    window = _Window()
    profile_service = _ProfileService()
    monkeypatch.setattr(
        profile_flow.single_instance,
        "stop_process_ipc",
        lambda app, window: stopped.append((app, window)) or "ipc",
    )
    monkeypatch.setattr(profile_flow.single_instance, "restart_process_ipc", lambda *args: None)
    monkeypatch.setattr(
        profile_flow,
        "QProcess",
        SimpleNamespace(
            startDetached=lambda program, args, cwd: launches.append(
                (program, args, cwd)
            )
            or True
        ),
    )
    monkeypatch.setattr(profile_flow.sys, "executable", "python.exe")
    monkeypatch.delattr(profile_flow.sys, "frozen", raising=False)

    profile_flow.relaunch_to_profile_creator(app, window, profile_service)

    assert profile_service.prepared == 1
    assert stopped == [(app, window)]
    assert launches[0][0] == "python.exe"
    assert launches[0][1][-1:] == ["--create-profile"]
    assert launches[0][1][0].endswith("main.py")
    assert window.closed == 1
    assert app.events == ["processEvents", "quit"]
