from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from solin.core.i18n import compiler


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    tool = tmp_path / "lrelease"
    tool.write_bytes(b"compiler-v1")
    monkeypatch.setattr(compiler, "qt_tool_path", lambda _name: tool)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        snapshot = Path(arguments[-3])
        Path(arguments[-1]).write_bytes(compiler._QM_MAGIC + snapshot.read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compiler.subprocess, "run", run)
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    (source / "solin_en.ts").write_text("source-v1", encoding="utf-8")
    return source, output, tool, calls


def test_first_compile_and_unchanged_run_preserve_output_and_manifest(toolchain):
    source, output, _, calls = toolchain
    paths = compiler.compile_catalogs(source, output)
    assert paths == [output / "solin_en.qm"]
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}

    assert compiler.compile_catalogs(source, output) == paths

    assert len(calls) == 1
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()} == before
    assert not list(source.glob("*.qm"))


def test_source_changes_invalidate_even_with_preserved_timestamp(toolchain):
    source, output, _, calls = toolchain
    compiler.compile_catalogs(source, output)
    ts = source / "solin_en.ts"
    timestamp = ts.stat()
    ts.write_text("source-v2", encoding="utf-8")
    os.utime(ts, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))

    compiler.compile_catalogs(source, output)

    assert len(calls) == 2
    assert (output / "solin_en.qm").read_bytes().endswith(b"source-v2")


@pytest.mark.parametrize("change", ["missing", "corrupted", "compiler", "options", "manifest"])
def test_changed_build_inputs_or_damaged_outputs_trigger_recompile(toolchain, change):
    source, output, tool, calls = toolchain
    compiler.compile_catalogs(source, output)
    options = ()
    if change == "missing":
        (output / "solin_en.qm").unlink()
    elif change == "corrupted":
        (output / "solin_en.qm").write_bytes(b"invalid")
    elif change == "compiler":
        tool.write_bytes(b"compiler-v2")
    elif change == "options":
        options = ("-compress",)
    else:
        (output / compiler._MANIFEST).write_text("{invalid", encoding="utf-8")

    compiler.compile_catalogs(source, output, options=options)

    assert len(calls) == 2
    assert (output / "solin_en.qm").read_bytes().startswith(compiler._QM_MAGIC)


def test_only_changed_catalog_is_recompiled_and_removed_locale_is_pruned(toolchain):
    source, output, _, calls = toolchain
    other = source / "solin_es.ts"
    other.write_text("es-v1", encoding="utf-8")
    compiler.compile_catalogs(source, output)
    other.write_text("es-v2", encoding="utf-8")

    compiler.compile_catalogs(source, output)

    assert len(calls) == 3
    assert Path(calls[-1][-3]).name == "solin_es.ts"
    other.unlink()
    unrelated = output / "unrelated.qm"
    unrelated.write_bytes(b"not ours")
    compiler.compile_catalogs(source, output)
    assert not (output / "solin_es.qm").exists()
    assert unrelated.read_bytes() == b"not ours"


@pytest.mark.parametrize("failure", ["exit", "missing_output", "invalid_output", "timeout", "oserror"])
def test_failed_compile_preserves_all_previous_outputs_and_reports_error(toolchain, monkeypatch, failure):
    source, output, _, calls = toolchain
    other = source / "solin_es.ts"
    other.write_text("es-v1", encoding="utf-8")
    compiler.compile_catalogs(source, output)
    previous = {p.name: p.read_bytes() for p in output.iterdir() if p.suffix != ".lock"}
    (source / "solin_en.ts").write_text("changed", encoding="utf-8")
    other.write_text("changed", encoding="utf-8")
    normal_run = compiler.subprocess.run

    def fail(arguments, **kwargs):
        if Path(arguments[-3]).name == "solin_en.ts":
            return normal_run(arguments, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 60)
        if failure == "oserror":
            raise OSError("cannot launch compiler")
        if failure == "invalid_output":
            Path(arguments[-1]).write_bytes(b"invalid")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="diagnostic", stderr="")

    monkeypatch.setattr(compiler.subprocess, "run", fail)
    with pytest.raises(compiler.TranslationCompilationError):
        compiler.compile_catalogs(source, output)

    assert {p.name: p.read_bytes() for p in output.iterdir() if p.suffix != ".lock"} == previous
    assert not list(output.glob(".solin-translations-*/"))
    assert len(calls) == 3

    monkeypatch.setattr(compiler.subprocess, "run", normal_run)
    assert len(compiler.compile_catalogs(source, output)) == 2
    assert len(calls) == 5


def test_source_snapshot_matches_manifest_when_source_is_edited_during_compilation(toolchain, monkeypatch):
    source, output, _, calls = toolchain
    normal_run = compiler.subprocess.run

    def edit_while_compiling(arguments, **kwargs):
        (source / "solin_en.ts").write_text("source-v2", encoding="utf-8")
        return normal_run(arguments, **kwargs)

    monkeypatch.setattr(compiler.subprocess, "run", edit_while_compiling)
    compiler.compile_catalogs(source, output)
    assert (output / "solin_en.qm").read_bytes().endswith(b"source-v1")
    compiler.compile_catalogs(source, output)
    assert (output / "solin_en.qm").read_bytes().endswith(b"source-v2")
    assert len(calls) == 2


def test_concurrent_preparation_serializes_writers_and_compiles_once(toolchain):
    source, output, _, calls = toolchain
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: compiler.compile_catalogs(source, output), range(4)))
    assert all(result == [output / "solin_en.qm"] for result in results)
    assert len(calls) == 1
    assert json.loads((output / compiler._MANIFEST).read_text())["catalogs"]["solin_en.ts"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows byte-range locking")
@pytest.mark.parametrize("initial_content", [b"", b"\0"], ids=["empty", "initialized"])
def test_preparation_waits_for_locked_file_before_writing(toolchain, monkeypatch, initial_content):
    import msvcrt

    source, output, _, calls = toolchain
    output.mkdir()
    lock_path = output / ".solin-translations.lock"
    lock_path.write_bytes(initial_content)
    attempted = Event()
    locking = msvcrt.locking

    def observe_locking(fd, mode, size):
        try:
            return locking(fd, mode, size)
        finally:
            if mode == msvcrt.LK_NBLCK:
                attempted.set()

    monkeypatch.setattr(msvcrt, "locking", observe_locking)
    with ThreadPoolExecutor(max_workers=1) as pool, lock_path.open("r+b") as owner:
        locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            pending = pool.submit(compiler.compile_catalogs, source, output)
            pending.add_done_callback(lambda _: attempted.set())
            assert attempted.wait(timeout=5), "Preparation never attempted to acquire the lock"
            if pending.done():
                pending.result()
                pytest.fail("Preparation bypassed the held lock")
            assert calls == []
        finally:
            locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        assert pending.result(timeout=5) == [output / "solin_en.qm"]
    assert len(calls) == 1


def test_metadata_inventory_missing_sources_and_shared_source_output_are_rejected(toolchain):
    source, output, _, _ = toolchain
    with pytest.raises(compiler.TranslationCompilationError, match="separate"):
        compiler.compile_catalogs(source, source)
    locales = source / "locales"
    locales.mkdir()
    (locales / "es.json").write_text("{}", encoding="utf-8")
    with pytest.raises(compiler.TranslationCompilationError, match="missing=.*solin_es.ts"):
        compiler.compile_catalogs(source, output)
    with pytest.raises(compiler.TranslationCompilationError, match="missing"):
        compiler.compile_catalogs(source / "absent", output)
    (source / "solin_en.ts").unlink()
    with pytest.raises(compiler.TranslationCompilationError, match="No solin_"):
        compiler.compile_catalogs(source, output)


def test_tool_lookup_never_uses_path(tmp_path, monkeypatch):
    package = tmp_path / "PySide6"
    package.mkdir()
    monkeypatch.setattr(
        compiler.importlib.util, "find_spec",
        lambda _: SimpleNamespace(submodule_search_locations=[str(package)]),
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "lrelease.exe").write_bytes(b"foreign Qt")
    with pytest.raises(compiler.TranslationCompilationError, match="unavailable"):
        compiler.qt_tool_path("lrelease")
    binary = package / ("lrelease.exe" if compiler.sys.platform == "win32" else "lrelease")
    binary.write_bytes(b"active Qt")
    assert compiler.qt_tool_path("lrelease") == binary
