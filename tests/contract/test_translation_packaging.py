"""Translation sources enter sdists; generated Qt catalogs enter delivery artifacts."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from setuptools import Distribution

from scripts.setuptools_commands import BuildPy
from solin.core.i18n import compiler
from tests._paths import REPO_ROOT


RECIPES = (
    ("scripts/build_solin.bat", "%TRANSLATIONS_DIR%"),
    ("scripts/build_solin.sh", "${TRANSLATIONS_DIR}"),
    (".github/workflows/build-solin-windows.yml", "$env:BUILD_DIR/translations/windows"),
    (".github/workflows/build-solin-macos.yml", "${BUILD_DIR}/translations/macos"),
)


@pytest.mark.parametrize(("recipe", "output"), RECIPES)
def test_nuitka_compiles_catalogs_before_packaging_generated_outputs(recipe, output):
    text = (REPO_ROOT / recipe).read_text(encoding="utf-8").replace("\\", "/")
    preparation = text.index("compile_translations.py")
    nuitka = text.index("-m nuitka", preparation)
    assert preparation < nuitka
    assert f'--output-dir "{output}"' in text[:nuitka]
    assert f'--include-data-files="{output}/*.qm=solin/resources/translations/"' in text[nuitka:]
    assert "src/solin/resources/translations/*.qm" not in text
    assert "src/solin/resources/translations/*.ts" not in text
    assert "src/solin/resources/translations/locales=solin/resources/translations/locales" in text


def test_isolated_build_has_the_pinned_qt_compiler_and_excludes_source_catalogs():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "PySide6-Essentials==6.11.1" in project["build-system"]["requires"]
    excluded = project["tool"]["setuptools"]["exclude-package-data"]["solin"]
    assert "resources/translations/*.ts" in excluded
    assert "resources/translations/*.qm" in excluded


def test_sdist_carries_sources_and_build_adapters_without_generated_catalogs():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "include setup.py" in manifest
    assert "include scripts/__init__.py scripts/setuptools_commands.py" in manifest
    assert "include scripts/compile_translations.py" in manifest
    assert "recursive-include src/solin/resources/translations *.ts" in manifest
    assert "recursive-exclude src/solin/resources/translations *.qm" in manifest


@pytest.fixture
def build_command(tmp_path):
    """Exercise setuptools copying with source and output trees owned by this test."""
    source = tmp_path / "src" / "solin"
    translations = source / "resources" / "translations"
    locales = translations / "locales"
    locales.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (translations / "solin_en.ts").write_text("source", encoding="utf-8")
    (translations / "solin_en.qm").write_bytes(b"stale source-tree binary")
    (locales / "en.json").write_text("{}", encoding="utf-8")
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    config = metadata["tool"]["setuptools"]
    distribution = Distribution({
        "name": "translation-build-contract",
        "packages": ["solin"],
        "package_dir": {"": str(tmp_path / "src")},
        "package_data": config["package-data"],
        "exclude_package_data": config["exclude-package-data"],
        "include_package_data": False,
    })
    distribution.script_name = str(tmp_path / "setup.py")
    distribution.get_command_obj("build").build_base = str(tmp_path / "build")
    command = BuildPy(distribution)
    command.build_lib = str(tmp_path / "lib")
    command.ensure_finalized()
    return command, translations, tmp_path / "lib" / "solin" / "resources" / "translations"


def test_wheel_build_stages_catalogs_outside_delivery_tree_and_reports_copied_outputs(
    build_command, monkeypatch,
):
    command, source, output = build_command
    # A previous build must not leak excluded inputs into the next wheel.
    output.mkdir(parents=True)
    (output / "solin_deleted.ts").write_text("stale build source", encoding="utf-8")
    (output / "solin_deleted.qm").write_bytes(b"stale build binary")
    (output / ".solin-translations.json").write_text("old manifest", encoding="utf-8")
    (output / ".solin-translations.lock").write_bytes(b"old lock")
    abandoned = output / ".solin-translations-abandoned"
    abandoned.mkdir()
    (abandoned / "solin_deleted.qm").write_bytes(b"interrupted build output")
    compiled = output / "solin_en.qm"
    staging = Path(command.get_finalized_command("build").build_base) / "translations" / "wheel"
    calls = []

    def compile_catalogs(source_dir, output_dir):
        calls.append((source_dir, output_dir))
        assert output_dir == staging
        assert not compiled.exists(), "A tracked binary bypassed compilation"
        output_dir.mkdir(parents=True)
        (output_dir / compiled.name).write_bytes(b"current binary")
        (output_dir / ".solin-translations.json").write_text("{}", encoding="utf-8")
        (output_dir / ".solin-translations.lock").write_bytes(b"\0")
        return [output_dir / compiled.name]

    monkeypatch.setattr(compiler, "compile_catalogs", compile_catalogs)
    command.run()

    assert calls == [(source, staging)]
    assert compiled.read_bytes() == b"current binary"
    assert (output / "locales" / "en.json").is_file()
    assert sorted(path.name for path in output.iterdir()) == ["locales", "solin_en.qm"]
    assert (staging / ".solin-translations.json").is_file()
    assert (staging / ".solin-translations.lock").is_file()
    assert str(compiled) in command.get_outputs()
    assert str(staging / compiled.name) not in command.get_outputs()
    assert len(command.get_outputs()) == len(set(command.get_outputs()))


def test_repeated_build_preserves_staging_cache_and_replaces_future_dated_delivery(
    build_command, monkeypatch,
):
    command, _, output = build_command
    staging = Path(command.get_finalized_command("build").build_base) / "translations" / "wheel"
    calls = []

    def compile_catalogs(source_dir, output_dir):
        calls.append(output_dir)
        manifest = output_dir / ".solin-translations.json"
        lock = output_dir / ".solin-translations.lock"
        catalog = output_dir / "solin_en.qm"
        if len(calls) == 1:
            output_dir.mkdir(parents=True)
            manifest.write_text("cached fingerprint", encoding="utf-8")
            lock.write_bytes(b"stable lock")
            catalog.write_bytes(b"current translation")
        else:
            assert manifest.read_text(encoding="utf-8") == "cached fingerprint"
            assert lock.read_bytes() == b"stable lock"
        return [catalog]

    monkeypatch.setattr(compiler, "compile_catalogs", compile_catalogs)
    command.run()
    delivered = output / "solin_en.qm"
    delivered.write_bytes(b"future dated stale translation")
    os.utime(delivered, (2_000_000_000, 2_000_000_000))
    previous_force = command.force

    command.run()

    assert calls == [staging, staging]
    assert delivered.read_bytes() == b"current translation"
    assert command.force == previous_force


@pytest.mark.parametrize("mode", ["editable_mode", "dry_run"])
def test_editable_and_dry_run_builds_never_invoke_the_compiler(build_command, monkeypatch, mode):
    command, _, output = build_command
    setattr(command, mode, True)

    def unexpected(*args, **kwargs):
        pytest.fail("Non-delivery builds must not prepare translation catalogs")

    monkeypatch.setattr(compiler, "compile_catalogs", unexpected)
    command.run()
    assert not list(output.glob("*.qm"))


def test_wheel_build_propagates_compilation_failure(build_command, monkeypatch):
    command, _, _ = build_command

    def fail(source_dir: Path, output_dir: Path):
        raise compiler.TranslationCompilationError("invalid source catalog")

    monkeypatch.setattr(compiler, "compile_catalogs", fail)
    with pytest.raises(compiler.TranslationCompilationError, match="invalid source catalog"):
        command.run()
