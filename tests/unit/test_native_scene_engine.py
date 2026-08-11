from __future__ import annotations

import os
from pathlib import Path

from solin.core.scenes.engine import DEFAULT_ENGINE_STARTUP_DEADLINE_MS
from solin.core.scenes.native_engine import (
    create_native_scene_engine,
    resolve_native_scene_engine_layout,
)
from solin.core.scenes.process_engine import SceneEngineProcessConfig


def _windows_runtime(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib" / "gstreamer-1.0").mkdir(parents=True)
    return root


def test_process_engine_allows_cold_native_runtime_initialization() -> None:
    config = SceneEngineProcessConfig(executable=Path("solin-media-engine"))

    assert config.hello_timeout_ms == DEFAULT_ENGINE_STARTUP_DEADLINE_MS


def test_packaged_native_engine_uses_its_private_gstreamer_runtime(
    tmp_path: Path,
) -> None:
    application_dir = tmp_path / "application"
    engine_root = application_dir / "native" / "media-engine"
    executable = engine_root / "solin-media-engine.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    runtime_root = _windows_runtime(engine_root / "gstreamer")

    layout = resolve_native_scene_engine_layout(
        application_dir=application_dir,
        repository_root=tmp_path / "repository",
        platform="win32",
    )

    assert layout is not None
    assert layout.executable == executable.resolve()
    assert layout.gstreamer_runtime_root == runtime_root.resolve()


def test_development_native_engine_is_discovered_from_the_repository_build(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    executable = (
        repository_root
        / "build"
        / "native"
        / "media-engine-gstreamer"
        / "Debug"
        / "solin-media-engine.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.touch()
    runtime_root = _windows_runtime(
        repository_root / "build" / "dependencies" / "gstreamer" / "msvc_x86_64"
    )

    layout = resolve_native_scene_engine_layout(
        application_dir=tmp_path / "application",
        repository_root=repository_root,
        platform="win32",
    )
    engine = create_native_scene_engine(
        tmp_path / "cache",
        application_dir=tmp_path / "application",
        repository_root=repository_root,
        platform="win32",
    )

    assert layout is not None
    assert layout.executable == executable.resolve()
    assert layout.gstreamer_runtime_root == runtime_root.resolve()
    assert engine is not None


def test_development_discovery_prefers_the_newest_configured_build(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    build_root = repository_root / "build" / "native" / "media-engine-gstreamer"
    release = build_root / "Release" / "solin-media-engine.exe"
    debug = build_root / "Debug" / "solin-media-engine.exe"
    release.parent.mkdir(parents=True)
    debug.parent.mkdir(parents=True)
    release.touch()
    debug.touch()
    release_time = 1_700_000_000_000_000_000
    debug_time = release_time + 1_000_000_000
    os.utime(release, ns=(release_time, release_time))
    os.utime(debug, ns=(debug_time, debug_time))
    _windows_runtime(repository_root / "build" / "dependencies" / "gstreamer" / "msvc_x86_64")

    layout = resolve_native_scene_engine_layout(
        application_dir=tmp_path / "application",
        repository_root=repository_root,
        platform="win32",
    )

    assert layout is not None
    assert layout.executable == debug.resolve()


def test_incomplete_windows_runtime_does_not_expose_a_broken_engine(
    tmp_path: Path,
) -> None:
    application_dir = tmp_path / "application"
    executable = application_dir / "native" / "media-engine" / "solin-media-engine.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    assert (
        resolve_native_scene_engine_layout(
            application_dir=application_dir,
            repository_root=tmp_path / "repository",
            platform="win32",
        )
        is None
    )
