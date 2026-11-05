from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import traceback
from types import ModuleType, SimpleNamespace

import pytest

from scripts import build_obs_runtime as builder


@pytest.fixture
def core_project(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "COPYING").write_bytes(b"Synthetic upstream license\n")
    binary = tmp_path / "candidate.dll"
    binary.write_bytes(b"qualified corrected core")
    patches = tmp_path / "native" / "patches"
    patches.mkdir(parents=True)
    (patches / "monitor.patch").write_bytes(b"synthetic monitoring correction")
    (patches / "series.json").write_text(
        json.dumps(
            [
                {
                    "file": "monitor.patch",
                    "purpose": "Keep monitoring continuous",
                    "upstream": "pending",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "SOURCE_DIRECTORY", patches.parent)
    return binary, sources, runtime


@pytest.mark.parametrize("already_installed", [False, True])
def test_failed_qualification_cannot_publish_core_license_or_receipt(
    core_project,
    monkeypatch,
    already_installed,
):
    binary, sources, runtime = core_project
    destination = runtime / "obs.dll"
    receipt = runtime / builder.MANIFEST_NAME
    license_path = runtime / "data/solin-obs-runtime/COPYING"
    if already_installed:
        destination.write_bytes(b"previous working core")
        receipt.write_bytes(b"previous qualified receipt")
        license_path.parent.mkdir(parents=True)
        license_path.write_bytes(b"previous license")
    previous = {
        path: path.read_bytes() if path.exists() else None
        for path in (destination, receipt, license_path)
    }

    def reject(candidate, installed_runtime):
        assert candidate != binary and candidate.parent.parent == runtime
        assert candidate.read_bytes() == binary.read_bytes()
        assert installed_runtime == runtime
        for path, content in previous.items():
            assert (path.read_bytes() if path.exists() else None) == content
        raise builder.ObsRuntimeBuildError("native qualification rejected the core")

    monkeypatch.setattr(builder, "verify_runtime", reject)
    with pytest.raises(builder.ObsRuntimeBuildError, match="qualification rejected"):
        builder.install_runtime(binary, sources, runtime)
    for path, content in previous.items():
        assert (path.read_bytes() if path.exists() else None) == content
    assert not list(runtime.glob(".solin-obs-core-*"))


@pytest.mark.parametrize("identical", [False, True])
def test_success_publishes_qualified_digest_patch_receipts_and_license(
    core_project,
    monkeypatch,
    identical,
):
    binary, sources, runtime = core_project
    destination = runtime / "obs.dll"
    old = binary.read_bytes() if identical else b"previous core"
    destination.write_bytes(old)
    unrelated = runtime / "obs-ffmpeg.dll"
    unrelated.write_bytes(b"unchanged existing plugin")
    qualified = []

    def qualify(candidate, installed_runtime):
        assert installed_runtime == runtime
        assert destination.read_bytes() == old
        assert candidate.read_bytes() == binary.read_bytes()
        qualified.append(candidate.read_bytes())

    monkeypatch.setattr(builder, "verify_runtime", qualify)
    original_replace = builder.os.replace

    def publish(source, target):
        if Path(target) == destination:
            assert qualified, "The binary must qualify before publication"
            assert not identical, "Do not replace an identical core loaded by another process"
        return original_replace(source, target)

    monkeypatch.setattr(builder.os, "replace", publish)
    assert builder.install_runtime(binary, sources, runtime) == destination
    assert qualified
    assert destination.read_bytes() == binary.read_bytes()
    receipt = json.loads((runtime / builder.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert receipt["schema"] == 1
    assert receipt["obs_version"] == builder.obs_sources.OBS_VERSION
    assert receipt["source_sha256"] == builder.obs_sources.SOURCE_SHA256
    assert runtime / receipt["library"]["path"] == destination
    assert receipt["library"]["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert len(receipt["patches"]) == 1
    patch = receipt["patches"][0]
    patch_path = builder.SOURCE_DIRECTORY / "patches" / patch["file"]
    assert patch["sha256"] == hashlib.sha256(patch_path.read_bytes()).hexdigest()
    assert patch["purpose"] and patch["upstream"]
    assert (runtime / "data/solin-obs-runtime/COPYING").read_bytes() == (
        sources / "COPYING"
    ).read_bytes()
    assert unrelated.read_bytes() == b"unchanged existing plugin"
    assert not list(runtime.glob(".solin-obs-core-*"))


@pytest.mark.parametrize(
    "exports, rejected",
    [
        (
            {
                "installed": ["obs_startup", "obs_shutdown"],
                "candidate": ["obs_startup", "obs_shutdown", "obs_new_api"],
            },
            None,
        ),
        (
            {"installed": ["obs_startup", "obs_shutdown"], "candidate": ["obs_startup"]},
            "obs_shutdown",
        ),
        ({"installed": [], "candidate": ["obs_startup"]}, "no inspectable exports"),
        ({"installed": ["obs_startup"], "candidate": []}, "no inspectable exports"),
    ],
)
def test_export_compatibility_requires_inspectable_tables_and_preserves_existing_api(
    tmp_path,
    monkeypatch,
    exports,
    rejected,
):
    def inspect(command, **_options):
        key = Path(command[-1]).stem
        rows = [
            f"    {number} {number:X} 00001000 {name}"
            for number, name in enumerate(exports[key], start=1)
        ]
        return subprocess.CompletedProcess(command, 0, "Export table\n" + "\n".join(rows), "")

    monkeypatch.setattr(builder.subprocess, "run", inspect)
    arguments = (tmp_path / "link.exe", tmp_path / "candidate.dll", tmp_path / "installed.dll")
    if rejected is None:
        builder.verify_exports(*arguments)
    else:
        with pytest.raises(builder.ObsRuntimeBuildError, match=rejected):
            builder.verify_exports(*arguments)


@pytest.mark.parametrize(
    "returncode, stdout",
    [
        (3, "native loader diagnostic"),
        (0, "native loader diagnostic"),
        (3, "Corrected OBS core qualified\nnative loader diagnostic"),
    ],
)
def test_runtime_qualification_rejects_failures_and_preserves_native_diagnostics(
    tmp_path,
    monkeypatch,
    returncode,
    stdout,
):
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *_args, **_options: subprocess.CompletedProcess(
            [], returncode, stdout, "plugin ABI error"
        ),
    )
    with pytest.raises(builder.ObsRuntimeBuildError) as failure:
        builder.verify_runtime(tmp_path / "candidate.dll", tmp_path / "runtime")
    assert "native loader diagnostic" in str(failure.value)
    assert "plugin ABI error" in str(failure.value)
    assert str(returncode) in str(failure.value)


def test_runtime_qualification_binds_the_candidate_and_installed_plugin_data(tmp_path, monkeypatch):
    binary, runtime = tmp_path / "candidate.dll", tmp_path / "runtime"
    inherited_path = os.pathsep.join(str(tmp_path / name) for name in ("toolchain", "system"))
    monkeypatch.setenv("PATH", inherited_path)

    def qualify(_command, **options):
        assert Path(options["env"]["LIBOBS_PATH"]) == binary.resolve()
        assert Path(options["env"]["OBS_DATA_PATH"]) == runtime.resolve() / "data"
        search_paths = options["env"]["PATH"].split(os.pathsep)
        assert Path(search_paths[0]) == runtime.resolve()
        assert search_paths[1:] == inherited_path.split(os.pathsep)
        assert options["timeout"] > 0
        return subprocess.CompletedProcess([], 0, "Corrected OBS core qualified\n", "")

    monkeypatch.setattr(builder.subprocess, "run", qualify)
    builder.verify_runtime(binary, runtime)
    assert os.environ["PATH"] == inherited_path


@pytest.fixture
def qualification_runtime(tmp_path, monkeypatch):
    """Execute the child qualification script against an isolated OBS module ABI."""
    runtime = tmp_path / "selected runtime"
    modules = {
        "obs-ffmpeg": "ffmpeg_source",
        "win-wasapi": "wasapi_output_capture",
    }
    paths = {}
    for name in modules:
        directory = runtime / "core" / name
        (directory / "data").mkdir(parents=True)
        binary = directory / f"{name}.dll"
        binary.write_bytes(b"synthetic module")
        paths[name] = (binary, directory / "data")
    candidate = tmp_path / "candidate" / "obs.dll"
    state = SimpleNamespace(
        version=33 << 24,
        opened=[],
        initialized=[],
        registered=set(),
        open_error=0,
        null_handle=False,
        init_ok=True,
        omitted_source=None,
        post_loaded=False,
        context_closed=False,
    )

    class Library:
        def obs_get_version(self):
            return state.version

        def obs_open_module(self, pointer, binary, data):
            # libobs derives its module name by splitting only on '/'.
            module_name = binary.decode().rsplit("/", 1)[-1].removesuffix(".dll")
            binary, data = Path(binary.decode()), Path(data.decode())
            name = binary.stem
            assert (binary, data) == paths[name]
            assert binary.is_file() and data.is_dir()
            state.opened.append((binary, data))
            if name == "win-wasapi":
                if state.open_error:
                    return state.open_error
                if state.null_handle:
                    return 0
            pointer[0] = module_name
            return 0

        def obs_init_module(self, module):
            assert module in modules
            state.initialized.append(module)
            if module == "win-wasapi" and not state.init_ok:
                return False
            source = modules[module]
            if source != state.omitted_source:
                state.registered.add(source)
            return True

        def obs_post_load_modules(self):
            state.post_loaded = True

        # Registering paths cannot make the OBS 33 bulk loader initialize modules.
        def obs_add_module_path(self, *_args):
            pass

        def obs_load_all_modules(self):
            pass

    class Context:
        def __init__(self, *, module_config_path):
            assert Path(module_config_path).is_dir()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state.context_closed = True

        def set_audio(self, *, samples_per_sec):
            assert samples_per_sec == 48000

    def allocate(declaration):
        assert declaration == "obs_module_t **"
        return [None]

    library = Library()
    package = ModuleType("pylibobs")
    package.enum_source_types = lambda: sorted(state.registered)
    context_module = ModuleType("pylibobs.context")
    context_module.OBSContext = Context
    ffi_module = ModuleType("pylibobs._ffi")
    ffi_module.ffi = SimpleNamespace(NULL=None, new=allocate)
    ffi_module.get_lib = lambda: library
    for module in (package, context_module, ffi_module):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(os, "add_dll_directory", lambda path: object(), raising=False)
    monkeypatch.setattr(builder.obs_sources, "OBS_VERSION", "33.0.0")

    def execute(command, **options):
        assert Path(options["env"]["LIBOBS_PATH"]) == candidate
        assert candidate.parent != runtime
        stdout = io.StringIO()
        stderr = ""
        returncode = 0
        with monkeypatch.context() as child:
            child.setattr(sys, "argv", ["-c", *command[4:]])
            with redirect_stdout(stdout):
                try:
                    exec(compile(command[3], "<obs-core-qualification>", "exec"), {})
                except RuntimeError:
                    returncode = 1
                    stderr = traceback.format_exc()
        return subprocess.CompletedProcess(command, returncode, stdout.getvalue(), stderr)

    monkeypatch.setattr(builder.subprocess, "run", execute)
    return SimpleNamespace(
        state=state, paths=paths, qualify=lambda: builder.verify_runtime(candidate, runtime)
    )


def test_qualification_initializes_selected_runtime_modules_with_their_own_data(
    qualification_runtime,
):
    qualification_runtime.qualify()
    state = qualification_runtime.state
    assert set(state.opened) == set(qualification_runtime.paths.values())
    assert set(state.initialized) == set(qualification_runtime.paths)
    assert state.registered == {"ffmpeg_source", "wasapi_output_capture"}
    assert state.post_loaded and state.context_closed


def test_qualification_uses_shared_module_path_resolvers(qualification_runtime, monkeypatch):
    paths = qualification_runtime.paths
    for name, (binary, data) in list(paths.items()):
        directory = binary.parents[2] / "resolved modules" / name
        directory.mkdir(parents=True)
        resolved_binary, resolved_data = directory / binary.name, directory / "resources"
        binary.rename(resolved_binary)
        data.rename(resolved_data)
        paths[name] = (resolved_binary, resolved_data)
    monkeypatch.setattr(builder, "module_binary_path", lambda root, platform, name: paths[name][0])
    monkeypatch.setattr(builder, "module_data_path", lambda root, platform, name: paths[name][1])
    qualification_runtime.qualify()
    assert set(qualification_runtime.state.opened) == set(paths.values())


@pytest.mark.parametrize("missing", ["binary", "data"])
def test_qualification_rejects_missing_required_module_files(qualification_runtime, missing):
    binary, data = qualification_runtime.paths["win-wasapi"]
    path = binary if missing == "binary" else data
    if missing == "binary":
        path.unlink()
    else:
        path.rmdir()
    with pytest.raises(builder.ObsRuntimeBuildError) as failure:
        qualification_runtime.qualify()
    assert str(path) in str(failure.value)
    assert "win-wasapi" not in qualification_runtime.state.initialized


@pytest.mark.parametrize(
    "setting, value, diagnostic",
    [
        ("open_error", -4, "-4"),
        ("open_error", -99, "-99"),
        ("null_handle", True, "NULL"),
        ("init_ok", False, "obs_init_module"),
    ],
)
def test_qualification_rejects_module_loader_failures(
    qualification_runtime, setting, value, diagnostic
):
    setattr(qualification_runtime.state, setting, value)
    with pytest.raises(builder.ObsRuntimeBuildError) as failure:
        qualification_runtime.qualify()
    message = str(failure.value)
    assert str(qualification_runtime.paths["win-wasapi"][0]) in message
    assert diagnostic in message
    assert qualification_runtime.state.context_closed


@pytest.mark.parametrize("missing", ["ffmpeg_source", "wasapi_output_capture"])
def test_qualification_requires_source_registration_after_successful_init(
    qualification_runtime, missing
):
    qualification_runtime.state.omitted_source = missing
    with pytest.raises(builder.ObsRuntimeBuildError) as failure:
        qualification_runtime.qualify()
    assert missing in str(failure.value)
    assert set(qualification_runtime.state.initialized) == set(qualification_runtime.paths)
    assert qualification_runtime.state.context_closed


def test_qualification_rejects_wrong_core_abi_before_loading_plugins(qualification_runtime):
    qualification_runtime.state.version = 32 << 24
    with pytest.raises(builder.ObsRuntimeBuildError, match="ABI"):
        qualification_runtime.qualify()
    assert not qualification_runtime.state.opened


@pytest.mark.parametrize(
    "platform", [("linux", "x86_64"), ("macos", "arm64"), ("windows", "arm64")]
)
def test_unsupported_platform_cannot_acquire_sources_or_build(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(builder, "target", lambda: platform)

    def forbidden(*_args, **_options):
        pytest.fail("An unsupported platform must not prepare, build or install the Windows core")

    for name in ("installed_runtime", "prepare_sources", "prepare_sdk", "_run", "install_runtime"):
        monkeypatch.setattr(builder, name, forbidden)
    with pytest.raises(builder.ObsRuntimeBuildError, match="Windows x86_64"):
        builder.build_obs_runtime(
            build_directory=tmp_path / "build", cache_directory=tmp_path / "cache"
        )


def test_debug_configuration_cannot_acquire_sources_build_or_install(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "target", lambda: ("windows", "x86_64"))

    def forbidden(*_args, **_options):
        pytest.fail(
            "A debug core must be rejected before runtime acquisition, build or installation"
        )

    for name in ("installed_runtime", "prepare_sources", "prepare_sdk", "_run", "install_runtime"):
        monkeypatch.setattr(builder, name, forbidden)
    with pytest.raises(builder.ObsRuntimeBuildError, match="release runtime configuration"):
        builder.build_obs_runtime(
            build_directory=tmp_path / "build",
            cache_directory=tmp_path / "cache",
            configuration="Debug",
        )


@pytest.mark.parametrize("gate", ["contracts", "exports"])
def test_build_cannot_install_after_native_contract_or_abi_rejection(
    core_project, monkeypatch, gate
):
    _binary, sources, runtime = core_project
    build = runtime.parent / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        "CMAKE_LINKER:FILEPATH=synthetic-link.exe\n", encoding="utf-8"
    )
    installed = runtime / "obs.dll"
    installed.write_bytes(b"previous working core")
    receipt = runtime / builder.MANIFEST_NAME
    receipt.write_bytes(b"previous qualified receipt")
    monkeypatch.setattr(builder, "target", lambda: ("windows", "x86_64"))
    monkeypatch.setattr(builder, "installed_runtime", lambda *_args: runtime)
    monkeypatch.setattr(builder, "prepare_sources", lambda *_args: sources)
    monkeypatch.setattr(builder, "prepare_sdk", lambda *_args: runtime.parent / "sdk")
    monkeypatch.setattr(builder.shutil, "which", lambda _name: str(runtime.parent / "cmake.exe"))
    contracts_ran = False

    def run(command):
        nonlocal contracts_ran
        if "--test-dir" in command:
            contracts_ran = True
            if gate == "contracts":
                raise subprocess.CalledProcessError(1, command, output="native contract rejected")

    def verify(*_args):
        assert contracts_ran, "Native contracts must qualify before checking ABI compatibility"
        assert gate == "exports", "Failed native contracts must stop the publication pipeline"
        raise builder.ObsRuntimeBuildError("native exports rejected")

    monkeypatch.setattr(builder, "_run", run)
    monkeypatch.setattr(builder, "verify_exports", verify)
    monkeypatch.setattr(
        builder, "install_runtime", lambda *_args: pytest.fail("Unqualified install")
    )
    rejected = (
        subprocess.CalledProcessError if gate == "contracts" else builder.ObsRuntimeBuildError
    )
    with pytest.raises(rejected):
        builder.build_obs_runtime(build_directory=build, cache_directory=runtime.parent / "cache")
    assert installed.read_bytes() == b"previous working core"
    assert receipt.read_bytes() == b"previous qualified receipt"
