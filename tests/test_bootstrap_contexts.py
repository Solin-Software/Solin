from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from solin.bootstrap.config import AppConfig
from solin.bootstrap.container import initialize_application_container
from solin.bootstrap.lifecycle import ApplicationLifecycle
from solin.core.foundation import paths as legacy_paths
from solin.core.foundation.runtime_paths import RuntimePaths


def _set_legacy_paths(monkeypatch, tmp_path) -> dict[str, str]:
    values = {
        "DATA_DIR": str(tmp_path / "data"),
        "PLAYLISTS_FILE": str(tmp_path / "data" / "playlists.json"),
        "PENDING_DEL_FILE": str(tmp_path / "data" / "pending_cleanup.json"),
        "IMAGES_DIR": str(tmp_path / "data" / "images"),
        "EMBEDDED_DIR": str(tmp_path / "data" / "embedded"),
        "LOG_DIR": str(tmp_path / "data" / "logs"),
        "CACHE_DIR": str(tmp_path / "cache"),
        "MEDIA_CACHE_DIR": str(tmp_path / "cache" / "media"),
        "THUMB_CACHE_DIR": str(tmp_path / "cache" / "thumbs"),
        "MEETING_THUMB_CACHE_DIR": str(tmp_path / "cache" / "meeting_thumbs"),
        "PDF_PAGES_DIR": str(tmp_path / "cache" / "pdf_pages"),
        "PPTX_PAGES_DIR": str(tmp_path / "cache" / "pptx_pages"),
        "DOCX_PAGES_DIR": str(tmp_path / "cache" / "docx_pages"),
    }
    for name, value in values.items():
        monkeypatch.setattr(legacy_paths, name, value)
    return values


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
    monkeypatch,
) -> None:
    values = _set_legacy_paths(monkeypatch, tmp_path)

    runtime_paths = RuntimePaths.from_legacy_globals()
    runtime_paths.ensure_dirs()

    assert runtime_paths.data_dir == tmp_path / "data"
    assert runtime_paths.log_dir == tmp_path / "data" / "logs"
    assert runtime_paths.media_cache_dir == tmp_path / "cache" / "media"
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
            runtime_paths.pdf_pages_dir,
            runtime_paths.pptx_pages_dir,
            runtime_paths.docx_pages_dir,
        )
    )
    with pytest.raises(FrozenInstanceError):
        runtime_paths.data_dir = values["CACHE_DIR"]  # type: ignore[misc]


def test_application_lifecycle_runs_cleanup_callbacks_once_in_reverse_order() -> None:
    events = []

    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _App:
        def __init__(self):
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
    _set_legacy_paths(monkeypatch, tmp_path)
    events = []

    class _Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _App:
        def __init__(self):
            self.aboutToQuit = _Signal()

    class _ProfileManager:
        def __init__(self, data_dir, cache_dir=None):
            self.constructor_args = (data_dir, cache_dir)

    constructed = []

    import solin.core.foundation.logging_config as logging_config
    import solin.core.profiles.manager as profile_manager_module

    monkeypatch.setattr(legacy_paths, "init", lambda: events.append("paths.init"))
    monkeypatch.setattr(
        logging_config,
        "configure_logging",
        lambda log_dir: events.append(("logging", log_dir)),
    )
    monkeypatch.setattr(
        profile_manager_module,
        "ProfileManager",
        lambda data_dir, cache_dir=None: (
            constructed.append(_ProfileManager(data_dir, cache_dir))
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

    assert events == [
        "paths.init",
        ("logging", os.fspath(tmp_path / "data" / "logs")),
    ]
    assert constructed[0].constructor_args == (
        os.fspath(tmp_path / "data"),
        os.fspath(tmp_path / "cache"),
    )
    assert container.config is config
    assert container.app is app
    assert container.profile_manager is constructed[0]
    assert container.runtime_paths.data_dir == tmp_path / "data"
    assert container.window_ref == [None]
    assert app.aboutToQuit.callbacks == [container.lifecycle.shutdown]
