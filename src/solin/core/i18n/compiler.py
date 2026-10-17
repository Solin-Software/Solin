"""Compile Qt catalogs without importing Qt or application services."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator


_MANIFEST = ".solin-translations.json"
_QM_MAGIC = bytes.fromhex("3cb86418caef9c95cd211cbf60a1bddd")


class TranslationCompilationError(RuntimeError):
    """A catalog could not be prepared for use by Qt."""


def development_catalog_directory(project_root: Path) -> Path:
    """Shared output location for source runs and the translation editor."""
    return project_root / "build" / "translations"


def qt_tool_path(name: str) -> Path:
    """Use tools belonging to this interpreter's PySide6, never another Qt on PATH."""
    spec = importlib.util.find_spec("PySide6")
    if spec is not None and spec.submodule_search_locations:
        package = Path(next(iter(spec.submodule_search_locations)))
        tool = package / (f"{name}.exe" if sys.platform == "win32" else name)
        if tool.is_file():
            return tool
    raise TranslationCompilationError(
        f"Qt tool {name!r} is unavailable for {sys.executable}. "
        "Install the project's pinned PySide6 dependencies in this Python environment."
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compiler_identity(tool: Path, options: tuple[str, ...]) -> dict:
    return {
        "schema": 1,
        "pyside6": importlib.metadata.version("PySide6-Essentials"),
        "compiler": _digest(tool.read_bytes()),
        "options": list(options),
    }


@contextmanager
def _output_lock(output_dir: Path) -> Iterator[None]:
    """OS-owned locks survive neither crashes nor terminated build processes."""
    with (output_dir / ".solin-translations.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()

            def acquire() -> None:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release() -> None:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire() -> None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release() -> None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + 120
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TranslationCompilationError(
                        f"Timed out waiting for translation compilation in {output_dir}"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def compile_catalogs(
    source_dir: Path,
    output_dir: Path,
    *,
    options: tuple[str, ...] = (),
) -> list[Path]:
    """Prepare every solin_*.ts, returning all outputs, including unchanged ones.

    Content fingerprints include the compiler, options, source and output bytes.
    Changed sources are compiled from snapshots; a concurrent edit is picked up
    on the next invocation. Failed compilations never publish partial outputs.
    """
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if source_dir == output_dir:
        raise TranslationCompilationError("Translation sources and outputs must be separate.")
    try:
        if not source_dir.is_dir():
            raise TranslationCompilationError(f"Translation source directory is missing: {source_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        with _output_lock(output_dir):
            sources = sorted(source_dir.glob("solin_*.ts"))
            if not sources:
                raise TranslationCompilationError(f"No solin_*.ts catalogs found in {source_dir}")
            locale_dir = source_dir / "locales"
            expected = {f"solin_{p.stem}.ts" for p in locale_dir.glob("*.json")}
            if expected and expected != {p.name for p in sources}:
                raise TranslationCompilationError(
                    "Translation sources do not match locale metadata: "
                    f"missing={sorted(expected - {p.name for p in sources})}, "
                    f"unexpected={sorted({p.name for p in sources} - expected)}"
                )
            tool = qt_tool_path("lrelease")
            identity = _compiler_identity(tool, options)
            previous = _read_manifest(output_dir / _MANIFEST)
            entries = previous.get("catalogs", {}) if previous.get("tool") == identity else {}
            if not isinstance(entries, dict):
                entries = {}
            records = {}
            outputs = []
            with tempfile.TemporaryDirectory(prefix=".solin-translations-", dir=output_dir) as work:
                staging = Path(work)
                changed = []
                for source in sources:
                    destination = output_dir / source.with_suffix(".qm").name
                    outputs.append(destination)
                    content = source.read_bytes()
                    source_digest = _digest(content)
                    output_digest = _digest(destination.read_bytes()) if destination.is_file() else None
                    record = {"source": source_digest, "output": output_digest}
                    if output_digest is None or entries.get(source.name) != record:
                        snapshot = staging / source.name
                        snapshot.write_bytes(content)
                        compiled = snapshot.with_suffix(".qm")
                        result = subprocess.run(
                            [os.fspath(tool), *options, os.fspath(snapshot), "-qm", os.fspath(compiled)],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=60,
                        )
                        if result.returncode != 0 or not compiled.is_file():
                            raise TranslationCompilationError(
                                f"lrelease failed for {source.name} (exit {result.returncode}):\n"
                                f"{result.stdout}\n{result.stderr}"
                            )
                        binary = compiled.read_bytes()
                        if not binary.startswith(_QM_MAGIC):
                            raise TranslationCompilationError(f"Invalid QM output for {source.name}")
                        record["output"] = _digest(binary)
                        changed.append((compiled, destination))
                    records[source.name] = record

                # Finish every compilation before publishing any changed catalog.
                for compiled, destination in changed:
                    os.replace(compiled, destination)
                for orphan in output_dir.glob("solin_*.qm"):
                    if orphan not in outputs:
                        orphan.unlink()
                manifest = {"tool": identity, "catalogs": records}
                if manifest != previous:
                    staged_manifest = staging / _MANIFEST
                    staged_manifest.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                    os.replace(staged_manifest, output_dir / _MANIFEST)
            return outputs
    except (OSError, subprocess.SubprocessError) as exc:
        raise TranslationCompilationError(f"Could not compile translations: {exc}") from exc
