from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from solin.core.scenes.process_engine import (
    SceneEngineProcessConfig,
    SubprocessSceneEngine,
)


@dataclass(frozen=True, slots=True)
class NativeSceneEngineLayout:
    executable: Path
    gstreamer_runtime_root: Path | None


def create_native_scene_engine(
    cache_dir: str | Path,
    *,
    application_dir: str | Path | None = None,
    repository_root: str | Path | None = None,
    platform: str | None = None,
) -> SubprocessSceneEngine | None:
    layout = resolve_native_scene_engine_layout(
        application_dir=application_dir,
        repository_root=repository_root,
        platform=platform,
    )
    if layout is None:
        return None
    registry_path = None
    if layout.gstreamer_runtime_root is not None:
        registry_path = Path(cache_dir) / "native-media-engine" / "gstreamer-registry.bin"
    return SubprocessSceneEngine(
        SceneEngineProcessConfig(
            executable=layout.executable,
            gstreamer_runtime_root=layout.gstreamer_runtime_root,
            gstreamer_registry_path=registry_path,
        )
    )


def resolve_native_scene_engine_layout(
    *,
    application_dir: str | Path | None = None,
    repository_root: str | Path | None = None,
    platform: str | None = None,
) -> NativeSceneEngineLayout | None:
    current_platform = platform or sys.platform
    executable_name = (
        "solin-media-engine.exe" if current_platform == "win32" else "solin-media-engine"
    )
    app_root = Path(application_dir) if application_dir is not None else _application_dir()
    repo_root = Path(repository_root) if repository_root is not None else _repository_root()
    candidates = (
        *_packaged_candidates(app_root, executable_name),
        *_development_candidates(repo_root, executable_name),
    )
    for executable, runtime_root in candidates:
        if not executable.is_file():
            continue
        if current_platform == "win32":
            if runtime_root is None or not _complete_gstreamer_runtime(runtime_root):
                continue
            return NativeSceneEngineLayout(executable.resolve(), runtime_root.resolve())
        if os.access(executable, os.X_OK):
            return NativeSceneEngineLayout(executable.resolve(), None)
    return None


def _packaged_candidates(
    application_dir: Path,
    executable_name: str,
) -> Sequence[tuple[Path, Path | None]]:
    engine_root = application_dir / "native" / "media-engine"
    return ((engine_root / executable_name, engine_root / "gstreamer"),)


def _development_candidates(
    repository_root: Path,
    executable_name: str,
) -> Sequence[tuple[Path, Path | None]]:
    build_root = repository_root / "build" / "native" / "media-engine-gstreamer"
    runtime_root = repository_root / "build" / "dependencies" / "gstreamer" / "msvc_x86_64"
    configured_builds = tuple(
        (build_root / configuration / executable_name, runtime_root)
        for configuration in ("Release", "RelWithDebInfo", "Debug")
    )
    newest_first = sorted(
        configured_builds,
        key=lambda candidate: _modification_time(candidate[0]),
        reverse=True,
    )
    return (*newest_first, (build_root / executable_name, None))


def _modification_time(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _complete_gstreamer_runtime(root: Path) -> bool:
    return (root / "bin").is_dir() and (root / "lib" / "gstreamer-1.0").is_dir()


def _application_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]
