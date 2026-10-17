from pathlib import Path

import pytest

from solin.bootstrap import translations
from solin.core.i18n import compiler


def test_source_checkout_prepares_explicit_dev_output(tmp_path, monkeypatch):
    package = tmp_path / "src" / "solin"
    sources = package / "resources" / "translations"
    monkeypatch.setattr(translations, "IS_DEV", True)
    monkeypatch.setattr(translations, "package_root", lambda: package)
    monkeypatch.setattr(translations, "application_translation_root", lambda: sources)
    calls = []
    monkeypatch.setattr(compiler, "compile_catalogs", lambda *args: calls.append(args))

    assert translations.prepare_application_catalogs() == tmp_path / "build" / "translations"
    assert calls == [(sources, tmp_path / "build" / "translations")]


@pytest.mark.parametrize("layout", ["wheel", "standalone"])
def test_installed_application_never_calls_compiler_or_writes(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(translations, "IS_DEV", False)
    package = tmp_path / "site-packages" / "solin"
    if layout == "standalone":
        package = tmp_path / "src" / "solin"
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    resources = package / "resources" / "translations"
    monkeypatch.setattr(translations, "package_root", lambda: package)
    monkeypatch.setattr(translations, "application_translation_root", lambda: resources)
    monkeypatch.setattr(compiler, "compile_catalogs", lambda *args: pytest.fail("compiler called"))
    assert translations.prepare_application_catalogs() == resources
    assert not (tmp_path / "build").exists()


def test_compilation_failure_propagates_instead_of_loading_stale_catalogs(tmp_path, monkeypatch):
    package = tmp_path / "src" / "solin"
    monkeypatch.setattr(translations, "IS_DEV", True)
    monkeypatch.setattr(translations, "package_root", lambda: package)
    monkeypatch.setattr(translations, "application_translation_root", lambda: Path("source"))

    def fail(*args):
        raise compiler.TranslationCompilationError("invalid catalog")

    monkeypatch.setattr(compiler, "compile_catalogs", fail)
    with pytest.raises(compiler.TranslationCompilationError, match="invalid catalog"):
        translations.prepare_application_catalogs()
