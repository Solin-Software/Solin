from __future__ import annotations

from datetime import date
import json
import os
from dataclasses import FrozenInstanceError

import pytest
from PySide6.QtCore import QObject

from solin.bootstrap import application as bootstrap_application
from solin.bootstrap.config import AppConfig
from solin.bootstrap.container import initialize_application_container
from solin.bootstrap.lifecycle import ApplicationLifecycle
from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from solin.core.meetings import meeting_weeks
from solin.core.meetings.tree_store import MeetingTreeStore


def test_app_config_is_immutable_and_applies_qt_identity() -> None:
    events = []
    config = AppConfig(
        display_name="Solin",
        qt_application_name="SolinDev",
        qt_organization_name="SolinDev",
        version="1.2.3",
    )

    class _App:
        def setApplicationName(self, value):
            events.append(("name", value))

        def setOrganizationName(self, value):
            events.append(("org", value))

        def setApplicationVersion(self, value):
            events.append(("version", value))

    config.apply_to(_App())

    assert events == [
        ("name", "SolinDev"),
        ("org", "SolinDev"),
        ("version", "1.2.3"),
    ]
    with pytest.raises(FrozenInstanceError):
        config.version = "2.0.0"  # type: ignore[misc]


def test_runtime_paths_snapshot_is_immutable_and_creates_runtime_dirs(
    tmp_path,
) -> None:
    runtime_paths = RuntimePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
    )
    runtime_paths.ensure_dirs()

    assert runtime_paths.data_dir == tmp_path / "data"
    assert runtime_paths.log_dir == tmp_path / "data" / "logs"
    assert runtime_paths.media_cache_dir == tmp_path / "cache" / "media"
    assert runtime_paths.jwpub_cache_dir == tmp_path / "cache" / "jwpub"
    assert not hasattr(runtime_paths, "playlists_file")
    assert not hasattr(runtime_paths, "images_dir")
    assert not hasattr(runtime_paths, "embedded_dir")
    assert all(
        directory.is_dir()
        for directory in (
            runtime_paths.data_dir,
            runtime_paths.log_dir,
            runtime_paths.cache_dir,
            runtime_paths.media_cache_dir,
            runtime_paths.thumb_cache_dir,
            runtime_paths.meeting_thumb_cache_dir,
            runtime_paths.jwpub_cache_dir,
            runtime_paths.pdf_pages_dir,
            runtime_paths.pptx_pages_dir,
            runtime_paths.docx_pages_dir,
        )
    )
    with pytest.raises(FrozenInstanceError):
        runtime_paths.data_dir = tmp_path / "other"  # type: ignore[misc]


def test_profile_paths_snapshot_is_immutable_and_creates_profile_cache_dirs(
    tmp_path,
) -> None:
    profile_paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    profile_paths.ensure_dirs()

    assert profile_paths.profile_dir == tmp_path / "data" / "profiles" / "main"
    assert profile_paths.profile_cache_dir == tmp_path / "cache" / "profiles" / "main"
    assert all(
        directory.is_dir()
        for directory in (
            profile_paths.profile_dir,
            profile_paths.images_dir,
            profile_paths.embedded_dir,
            profile_paths.talk_theme_assets_dir,
            profile_paths.profile_cache_dir,
            profile_paths.thumb_cache_dir,
            profile_paths.meeting_thumb_cache_dir,
            profile_paths.pdf_pages_dir,
            profile_paths.pptx_pages_dir,
            profile_paths.docx_pages_dir,
        )
    )
    with pytest.raises(FrozenInstanceError):
        profile_paths.profile_dir = tmp_path / "other"  # type: ignore[misc]


def test_profile_preparation_prunes_only_expired_meeting_tree_records(
    tmp_path,
    monkeypatch,
) -> None:
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    paths.ensure_dirs()
    store = MeetingTreeStore(paths.meeting_trees_file)
    old_tree = "mwb:2026-05-25:T:20260500"
    boundary_tree = "mwb:2026-06-01:T:20260500"
    future_tree = "wt:2027-01-04:T:20270100"
    old_thumbnail = paths.meeting_thumb_cache_dir / "old-media.jpg"
    old_thumbnail.write_bytes(b"keep cached artifact")
    store.save(
        old_tree,
        [
            {
                "id": "old-media",
                "type": "media",
                "children": [],
                "thumbnail_local_path": str(old_thumbnail),
            }
        ],
        "old",
    )
    store.save(boundary_tree, [], "boundary")
    store.save(future_tree, [], "future")
    main_window_class = object()
    monkeypatch.setattr(
        bootstrap_application,
        "_import_main_window_class",
        lambda: main_window_class,
    )
    monkeypatch.setattr(
        meeting_weeks,
        "meeting_week_bounds",
        lambda: (date(2026, 6, 1), date(2026, 7, 20)),
    )

    prepared = bootstrap_application._prepare_profile_main_window(paths)

    assert prepared is main_window_class
    persisted = json.loads(paths.meeting_trees_file.read_text(encoding="utf-8"))
    assert set(persisted["trees"]) == {boundary_tree, future_tree}
    assert old_thumbnail.read_bytes() == b"keep cached artifact"


def test_application_lifecycle_runs_cleanup_callbacks_once_in_reverse_order() -> None:
    events = []

    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _App(QObject):
        def __init__(self):
            super().__init__()
            self.aboutToQuit = _Signal()

    app = _App()
    lifecycle = ApplicationLifecycle(app)
    lifecycle.install()
    lifecycle.install()
    lifecycle.register_cleanup(lambda: events.append("first"))
    lifecycle.register_cleanup(lambda: events.append("second"))

    assert app.aboutToQuit.callbacks == [lifecycle.shutdown]

    lifecycle.shutdown()
    lifecycle.shutdown()

    assert events == ["second", "first"]


def test_application_container_initializes_runtime_services(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_paths = RuntimePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
    )
    events = []

    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _App(QObject):
        def __init__(self):
            super().__init__()
            self.aboutToQuit = _Signal()

    class _ProfileService:
        def __init__(
            self,
            data_dir,
            cache_dir=None,
            *,
            global_settings,
            profile_app_settings_for,
        ):
            self.constructor_args = (data_dir, cache_dir)
            self.global_settings = global_settings
            self.profile_app_settings_for = profile_app_settings_for

    constructed = []

    import solin.core.foundation.logging_config as logging_config
    import solin.core.profiles.infrastructure as profile_infrastructure

    monkeypatch.setattr(
        RuntimePaths,
        "from_standard_locations",
        classmethod(lambda cls: runtime_paths),
    )
    monkeypatch.setattr(
        logging_config,
        "configure_logging",
        lambda log_dir: events.append(("logging", log_dir)),
    )
    monkeypatch.setattr(
        profile_infrastructure,
        "create_local_profile_service",
        lambda data_dir, cache_dir=None, *, global_settings, profile_app_settings_for: (
            constructed.append(
                _ProfileService(
                    data_dir,
                    cache_dir,
                    global_settings=global_settings,
                    profile_app_settings_for=profile_app_settings_for,
                )
            )
            or constructed[-1]
        ),
    )

    app = _App()
    config = AppConfig(
        display_name="Solin",
        qt_application_name="SolinDev",
        qt_organization_name="SolinDev",
        version="1.2.3",
    )

    container = initialize_application_container(app, config)

    assert events == [("logging", os.fspath(tmp_path / "data" / "logs"))]
    assert constructed[0].constructor_args == (
        os.fspath(tmp_path / "data"),
        os.fspath(tmp_path / "cache"),
    )
    assert container.config is config
    assert container.app is app
    assert container.profile_service is constructed[0]
    assert callable(constructed[0].profile_app_settings_for)
    assert container.profile_runtime is not None
    assert container.onboarding_service is not None
    assert container.global_settings is not None
    assert container.talk_theme_output_settings is not None
    assert container.media.cache_manager.parent() is app
    assert not hasattr(container, "media_cache_manager")
    assert container.jwpub_checksum_store is not None
    assert container.runtime_paths.data_dir == tmp_path / "data"
    assert container.window_ref == [None]
    assert app.aboutToQuit.callbacks == [container.lifecycle.shutdown]
