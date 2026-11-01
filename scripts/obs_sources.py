"""Verified OBS source inputs and ordered patches shared by native builds."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import urllib.request

OBS_VERSION = "33.0.0"
SOURCE_URL = f"https://codeload.github.com/obsproject/obs-studio/tar.gz/refs/tags/{OBS_VERSION}"
SOURCE_SHA256 = "55c6767b209a00f1859b315bf977df907b19890d132723731e00fd589f6860c0"


class ObsSourceError(RuntimeError):
    """A native build input failed verification."""


def patch_series(directory: Path) -> list[dict]:
    """Read the ordered downstream changes from their single manifest."""
    manifest = directory / "series.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ObsSourceError("Invalid OBS patch series") from error
    if not isinstance(entries, list) or not entries:
        raise ObsSourceError("OBS patch series must not be empty")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key]
            for key in ("file", "purpose", "upstream")
        ):
            raise ObsSourceError("Invalid OBS patch description")
        name = entry["file"]
        if (
            PurePosixPath(name).name != name
            or "\\" in name
            or not name.endswith(".patch")
            or name in names
        ):
            raise ObsSourceError("Invalid or duplicate OBS patch path")
        prerequisites = entry.get("requires", [])
        if not isinstance(prerequisites, list) or any(
            not isinstance(required, str) or required not in names for required in prerequisites
        ):
            raise ObsSourceError(f"OBS patch prerequisites are out of order: {name}")
        names.add(name)
    return entries


def apply_patches(sources: Path, directory: Path) -> None:
    """Apply explicit unified diffs, rejecting any upstream context mismatch."""
    git = shutil.which("git")
    if git is None:
        raise ObsSourceError("Git is required to apply the audited OBS patches")
    # A source cache lives inside the Solin checkout. Git-format diffs otherwise
    # inherit that repository's prefix and may silently skip every target file.
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(sources.resolve().parent)
    for entry in patch_series(directory):
        patch = directory / entry["file"]
        if not patch.is_file():
            raise ObsSourceError(f"Missing OBS patch: {entry['file']}")
        command = [git, "apply", "--no-index", "--whitespace=nowarn"]
        for flags in (["--check"], []):
            result = subprocess.run(
                [*command, *flags, str(patch.resolve())],
                cwd=sources,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            if result.returncode:
                raise ObsSourceError(
                    f"OBS patch {entry['file']} no longer applies: {result.stderr.strip()}"
                )


def patch_receipts(directory: Path) -> list[dict]:
    return [
        {**entry, "sha256": hashlib.sha256((directory / entry["file"]).read_bytes()).hexdigest()}
        for entry in patch_series(directory)
    ]


def download_sources(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"obs-studio-{OBS_VERSION}.tar.gz"
    if archive.is_symlink():
        raise ObsSourceError("OBS source archive must not be a symbolic link")
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
                    shutil.copyfileobj(response, output)
            except BaseException:  # noqa: BLE001 - remove partial downloads even on cancellation
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            _verify_archive(temporary)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    _verify_archive(archive)
    return archive


def _verify_archive(archive: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SOURCE_SHA256:
        raise ObsSourceError(f"OBS source archive failed SHA-256 verification: {archive}")
