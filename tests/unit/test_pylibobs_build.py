"""Immutable binding handoff and packaged runtime qualification contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import build_pylibobs as builder


def _source(tmp_path):
    source = tmp_path / "pylibobs"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname="pylibobs"\nversion="32.2.2"\n')
    (source / "scripts").mkdir()
    (source / "scripts/fetch_libs.py").write_text("# Runtime recipe owned by the binding\n")

    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=source, text=True).strip()

    git("init", "--quiet")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return source, git("rev-parse", "HEAD")


def _wheel(path, architecture="x86_64", *, target="macos", missing=None):
    root = f"pylibobs/_libs/{target}/{architecture}/"
    layout = builder._LAYOUTS[target]
    entries = {
        name.replace("*", ""): b"native artifact"
        for name in (
            layout["library"],
            "data/libobs/default.effect",
            *layout["required"],
            *(
                f"obs-plugins/{name}{layout['suffix']}"
                for name in builder._CORE_PLUGINS + layout["capture"]
            ),
        )
    }
    entries["runtime.json"] = json.dumps(
        {"obs_version": "32.2.2", "sha256": "a" * 64, "macos_deployment_target": "13.0"}
    ).encode()
    entries.pop(missing, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if missing != "surface bridge":
            archive.writestr("pylibobs/native/macos_surfaces.dylib", b"surface bridge")
        for name, data in entries.items():
            archive.writestr(root + name, data)
    return path


def test_source_revision_requires_exact_clean_checkout(tmp_path):
    source, revision = _source(tmp_path)
    builder._validate_revision(source, revision)
    with pytest.raises(builder.BindingBuildError, match="full immutable"):
        builder._validate_revision(source, "main")
    with pytest.raises(builder.BindingBuildError, match="expected"):
        builder._validate_revision(source, "0" * 40)
    (source / "pyproject.toml").write_text("dirty")
    with pytest.raises(builder.BindingBuildError, match="Commit tracked source changes"):
        builder._validate_revision(source, revision)


def test_git_export_uses_committed_source_and_verifies_version(tmp_path):
    source, revision = _source(tmp_path)
    (source / "untracked-runtime.bin").write_bytes(b"not part of the source")
    exported = tmp_path / "exported"
    builder._export_source(source, revision, exported)
    assert (exported / "scripts/fetch_libs.py").read_bytes() == (
        source / "scripts/fetch_libs.py"
    ).read_bytes()
    assert not (exported / "untracked-runtime.bin").exists()
    assert not (exported / ".git").exists()


@pytest.mark.parametrize(
    "target, architecture",
    [
        ("macos", "x86_64"),
        ("macos", "arm64"),
        ("windows", "x86_64"),
        ("linux", "x86_64"),
    ],
)
def test_wheel_qualification_requires_runtime_manifest_and_native_modules(
    tmp_path, target, architecture
):
    wheel = _wheel(tmp_path / "binding.whl", architecture, target=target)
    assert builder._verify_built_wheel(wheel, target, architecture)["obs_version"] == "32.2.2"
    _wheel(
        wheel,
        architecture,
        target=target,
        missing=f"obs-plugins/obs-ffmpeg{builder._LAYOUTS[target]['suffix']}",
    )
    with pytest.raises(builder.BindingBuildError, match="obs-ffmpeg"):
        builder._verify_built_wheel(wheel, target, architecture)


def test_wheel_requires_the_binding_native_presenter(tmp_path):
    wheel = _wheel(tmp_path / "binding.whl", missing="surface bridge")
    with pytest.raises(builder.BindingBuildError, match="macos_surfaces.dylib"):
        builder._verify_built_wheel(wheel, "macos", "x86_64")


def test_local_build_delegates_runtime_packaging_and_records_provenance(tmp_path, monkeypatch):
    source, revision = _source(tmp_path)
    original = builder._run
    events = []

    def run(command, **options):
        if command[0] == "git":
            return original(command, **options)
        if "scripts/fetch_libs.py" in command:
            events.append("fetch")
            assert Path(options["cwd"]) != source
        elif "wheel" in command:
            events.append("build")
            destination = Path(command[command.index("--wheel-dir") + 1])
            _wheel(destination / "pylibobs-32.2.2-py3-none-macosx_13_0_x86_64.whl")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, "_run", run)
    monkeypatch.setattr(builder, "_verify_source_runtime", lambda *_: events.append("smoke"))
    wheel = builder.build_wheel(
        tmp_path / "wheels",
        tmp_path / "cache",
        "macos",
        "x86_64",
        source_dir=source,
        source_ref=revision,
    )
    assert events == ["fetch", "smoke", "build"]
    manifest = json.loads(wheel.with_suffix(".build.json").read_text())
    assert manifest["source_revision"] == revision
    assert manifest["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert manifest["runtime"]["obs_version"] == builder.OBS_VERSION
    assert not (source / "pylibobs/_libs").exists()


@pytest.mark.parametrize(
    "system, target, architecture",
    [
        ("darwin", "macos", "x86_64"),
        ("darwin", "macos", "arm64"),
        ("linux", "linux", "x86_64"),
        ("win32", "windows", "x86_64"),
    ],
)
def test_bootstrap_builds_the_same_immutable_binding_on_every_supported_host(
    monkeypatch,
    system,
    target,
    architecture,
    tmp_path,
):
    calls = []
    monkeypatch.setattr(builder.sys, "platform", system)
    monkeypatch.setattr(builder.platform, "machine", lambda: architecture)
    monkeypatch.setattr(
        builder,
        "build_wheel",
        lambda *args, **kwargs: calls.append((args, kwargs)) or tmp_path / "binding.whl",
    )
    assert builder.main(["--source-ref", "a" * 40]) == 0
    assert calls[0][0][2:] == (target, architecture)
    assert calls[0][1]["source_ref"] == "a" * 40


def test_unpublished_revision_requires_explicit_local_source_or_remote_pin(monkeypatch, capsys):
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.delenv("PYLIBOBS_SOURCE_REF", raising=False)
    with pytest.raises(SystemExit):
        builder.main([])
    assert "unpublished checkout" in capsys.readouterr().err
