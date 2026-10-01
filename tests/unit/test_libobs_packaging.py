from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from scripts import package_libobs_runtime as packaging
from solin.core.scenes.ipc_protocol import SceneIpcEnvelope, encode_envelope, read_envelope


def test_smoke_png_checksums_and_decoded_pixels_are_valid():
    data = packaging._smoke_png()
    Image.open(io.BytesIO(data)).verify()
    image = Image.open(io.BytesIO(data))
    image.load()
    assert image.size == (2, 2)
    assert image.mode == "RGB"
    assert [image.getpixel((x, y)) for y in range(2) for x in range(2)] == [(255, 255, 255)] * 4


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_ms": 0},
        {"position_ms": 0},
        {"state": 2},
        {"state": 7},
        {"slot": 1},
        {"path": "other.wav"},
        {"error_code": "media_open_failed"},
        {"duration_ms": True},
        {"state": True},
        {"slot": False},
    ],
)
def test_decode_confirmation_rejects_placeholders_errors_or_unrelated_media(changes):
    state = {
        "state": 1,
        "position_ms": 250,
        "duration_ms": 2000,
        "path": "smoke.wav",
        "slot": 0,
        "error_code": "",
    }
    response = SceneIpcEnvelope(
        message_type="media_playback_state",
        request_id="event-media",
        session_id="smoke",
        process_generation="smoke",
        sequence=0,
        document_revision=0,
        deadline_monotonic_ms=0,
        payload=state,
    )
    assert packaging._decoded_media_event(response, "smoke.wav")
    state.update(changes)
    assert not packaging._decoded_media_event(response, "smoke.wav")


def _touch(path: Path, value: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _runtime(package: Path, target: str) -> Path:
    root = package / "_libs" / target / "x86_64"
    names = {
        "windows": ("obs.dll", "libobs-d3d11.dll", "obs-ffmpeg-mux.exe"),
        "linux": ("libobs.so.0", "libobs-opengl.so", "obs-ffmpeg-mux"),
        "macos": ("Frameworks/libobs.dylib", "Frameworks/libobs-opengl.dylib", "obs-ffmpeg-mux"),
    }
    suffix = {"windows": ".dll", "linux": ".so", "macos": ".dylib"}[target]
    for name in names[target]:
        _touch(root / name)
    for name in ("obs-ffmpeg", "image-source", "obs-transitions"):
        _touch(root / "obs-plugins" / f"{name}{suffix}")
    _touch(root / "data/libobs/default.effect")
    _touch(root / "data/obs-plugins/obs-ffmpeg/locale/en-US.ini")
    return root


@pytest.mark.parametrize("target", ["windows", "linux", "macos"])
def test_staging_copies_only_the_host_architecture_preserves_layout_and_notices(
    tmp_path, target, monkeypatch
):
    package, app = tmp_path / "installed", tmp_path / "application"
    source = _runtime(package, target)
    _touch(package / "_libs" / target / "arm64" / "other-architecture")
    _touch(source / "plugins-extra-data.bin")
    _touch(app / "Solin.exe")
    _touch(tmp_path / "LICENSE", b"redistribution notice")
    # Fixtures are not binaries; native loader qualification is covered separately.
    monkeypatch.setattr(packaging, "_relocate_linux", lambda _root, **_kwargs: None)
    monkeypatch.setattr(packaging, "_relocate_macos", lambda _root, **_kwargs: None)
    destination = packaging.stage_runtime(
        package_dir=package,
        application_dir=app,
        target_platform=target,
        architecture="x86_64",
        license_files=[tmp_path / "LICENSE"],
    )
    assert destination == app / "pylibobs" / "_libs" / target / "x86_64"
    assert (destination / "plugins-extra-data.bin").read_bytes() == b"fixture"
    assert not (destination.parent / "arm64").exists()
    assert (app / "licenses/pylibobs/0-LICENSE").read_bytes() == b"redistribution notice"
    if target == "windows":
        assert (app / "obs-ffmpeg-mux.exe").read_bytes() == b"fixture"
    else:
        assert (app / "obs-ffmpeg-mux").read_bytes() == b"fixture"
        if os.name != "nt":
            assert (app / "obs-ffmpeg-mux").stat().st_mode & 0o111


def test_staging_replaces_old_plugins_and_never_keeps_stale_runtime_files(tmp_path):
    package, app = tmp_path / "installed", tmp_path / "application"
    _runtime(package, "windows")
    _touch(tmp_path / "LICENSE")
    _touch(app / "pylibobs/_libs/windows/x86_64/old-plugin.dll")
    destination = packaging.stage_runtime(
        package_dir=package,
        application_dir=app,
        target_platform="windows",
        architecture="x86_64",
        license_files=[tmp_path / "LICENSE"],
    )
    assert not (destination / "old-plugin.dll").exists()


def test_runtime_publish_restores_previous_payload_on_filesystem_failure(tmp_path, monkeypatch):
    staging, destination = tmp_path / "new", tmp_path / "runtime"
    _touch(staging / "new.dll")
    _touch(destination / "previous.dll", b"previous")
    replace = os.replace

    def fail_publish(source, target):
        if source == staging:
            raise OSError("publish failed")
        replace(source, target)

    monkeypatch.setattr(packaging.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        packaging._publish_runtime(staging, destination)
    assert (destination / "previous.dll").read_bytes() == b"previous"
    assert (staging / "new.dll").is_file()
    assert not list(tmp_path.glob("*.backup"))


def test_failed_runtime_rollback_keeps_recoverable_backup(tmp_path, monkeypatch):
    staging, destination = tmp_path / "new", tmp_path / "runtime"
    _touch(staging / "new.dll")
    _touch(destination / "previous.dll", b"previous")
    replace = os.replace

    def fail_publish_and_rollback(source, target):
        if target == destination:
            raise OSError("destination unavailable")
        replace(source, target)

    monkeypatch.setattr(packaging.os, "replace", fail_publish_and_rollback)
    with pytest.raises(packaging.LibobsPackagingError, match="previous runtime remains"):
        packaging._publish_runtime(staging, destination)
    (backup,) = tmp_path.glob("*.backup")
    assert (backup / "previous.dll").read_bytes() == b"previous"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violations")
def test_runtime_publish_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    staging, destination = tmp_path / "new", tmp_path / "runtime"
    _touch(staging / "new.dll")
    replace = os.replace
    attempts = []
    waits = []

    def busy_once(source, target):
        attempts.append((source, target))
        if len(attempts) == 1:
            error = PermissionError("transient sharing violation")
            error.winerror = 32
            raise error
        replace(source, target)

    monkeypatch.setattr(packaging.os, "replace", busy_once)
    monkeypatch.setattr(packaging.time, "sleep", waits.append)
    packaging._publish_runtime(staging, destination)
    assert len(attempts) == 2
    assert waits == [0.05]
    assert (destination / "new.dll").is_file()


@pytest.mark.parametrize(
    "missing",
    [
        "obs.dll",
        "obs-ffmpeg-mux.exe",
        "obs-plugins/obs-ffmpeg.dll",
        "data/libobs/default.effect",
        "data/obs-plugins/obs-ffmpeg/locale/en-US.ini",
    ],
)
def test_incomplete_runtime_fails_before_touching_an_existing_package(tmp_path, missing):
    package, app = tmp_path / "installed", tmp_path / "application"
    source = _runtime(package, "windows")
    (source / missing).unlink()
    _touch(tmp_path / "LICENSE")
    sentinel = app / "pylibobs/_libs/windows/x86_64/previous.dll"
    _touch(sentinel)
    with pytest.raises(packaging.LibobsPackagingError, match="missing"):
        packaging.stage_runtime(
            package_dir=package,
            application_dir=app,
            target_platform="windows",
            architecture="x86_64",
            license_files=[tmp_path / "LICENSE"],
        )
    assert sentinel.read_bytes() == b"fixture"


def test_staging_requires_redistribution_notice(tmp_path):
    package, app = tmp_path / "installed", tmp_path / "application"
    _runtime(package, "windows")
    app.mkdir()
    with pytest.raises(packaging.LibobsPackagingError, match="license notice"):
        packaging.stage_runtime(
            package_dir=package,
            application_dir=app,
            target_platform="windows",
            architecture="x86_64",
            license_files=[],
        )


def test_macos_host_mux_copy_resolves_private_dependencies_and_rpath_then_signs(
    tmp_path, monkeypatch
):
    application = tmp_path / "Solin.app/Contents/MacOS"
    root = application / "pylibobs/_libs/macos/x86_64"
    helper = application / "obs-ffmpeg-mux"
    magic = b"\xcf\xfa\xed\xfe"
    _touch(helper, magic)
    _touch(root / "Frameworks/libavcodec.dylib", magic)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["otool", "-L"]:
            output = b"helper:\n\t@loader_path/Frameworks/libavcodec.dylib (compatibility version 61.0.0)\n"
        elif command[:2] == ["otool", "-l"]:
            output = b"cmd LC_RPATH\npath @loader_path/Frameworks (offset 12)\n"
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(packaging.subprocess, "run", run)
    packaging._relocate_macos(root, binaries=[helper])
    assert [
        "install_name_tool",
        "-change",
        "@loader_path/Frameworks/libavcodec.dylib",
        "@loader_path/pylibobs/_libs/macos/x86_64/Frameworks/libavcodec.dylib",
        str(helper),
    ] in calls
    assert ["install_name_tool", "-delete_rpath", "@loader_path/Frameworks", str(helper)] in calls
    assert [
        "install_name_tool",
        "-add_rpath",
        "@loader_path/pylibobs/_libs/macos/x86_64/Frameworks",
        str(helper),
    ] in calls
    assert calls[-1] == ["codesign", "--force", "--sign", "-", str(helper)]


def test_linux_host_mux_copy_uses_the_private_runtime_and_checks_its_dependencies(
    tmp_path, monkeypatch
):
    application = tmp_path / "main.dist"
    root = application / "pylibobs/_libs/linux/x86_64"
    helper = application / "obs-ffmpeg-mux"
    _touch(helper, b"\x7fELF")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"libavcodec => private/runtime\n", b"")

    monkeypatch.setattr(packaging.subprocess, "run", run)
    packaging._relocate_linux(root, binaries=[helper])
    assert calls == [
        ["patchelf", "--set-rpath", "$ORIGIN:$ORIGIN/pylibobs/_libs/linux/x86_64", str(helper)],
        ["ldd", str(helper)],
    ]


def test_linux_staging_supplies_the_helper_omitted_from_the_published_wheel(
    tmp_path, monkeypatch,
):
    package, app = tmp_path / "installed", tmp_path / "application"
    source = _runtime(package, "linux")
    (source / "obs-ffmpeg-mux").unlink()
    helper = tmp_path / "official-obs/obs-ffmpeg-mux"
    _touch(helper, b"official pinned helper")
    _touch(app / "Solin.bin")
    _touch(tmp_path / "LICENSE")
    monkeypatch.setattr(packaging, "_relocate_linux", lambda _root, **_kwargs: None)
    destination = packaging.stage_runtime(
        package_dir=package, application_dir=app, target_platform="linux",
        architecture="x86_64", license_files=[tmp_path / "LICENSE"],
        linux_mux_helper=helper,
    )
    assert (destination / "obs-ffmpeg-mux").read_bytes() == helper.read_bytes()
    assert (app / "obs-ffmpeg-mux").read_bytes() == helper.read_bytes()
    assert not (source / "obs-ffmpeg-mux").exists()


def test_external_linux_helper_cannot_override_another_platform(tmp_path):
    root = _runtime(tmp_path, "windows")
    with pytest.raises(packaging.LibobsPackagingError, match="only supported for Linux"):
        packaging.validate_runtime(root, "windows", linux_mux_helper=tmp_path / "helper")


def test_linux_dependency_closure_is_bundled_with_notices_but_keeps_host_graphics(
    tmp_path, monkeypatch,
):
    root, system = tmp_path / "runtime", tmp_path / "system"
    core, codec, codec_dependency = root / "libobs.so.0", system / "libavcodec.so.60", system / "libx264.so.164"
    for library in (core, codec, codec_dependency, system / "libc.so.6", system / "libGL.so.1"):
        _touch(library, b"\x7fELF" + library.name.encode())
    notices, commands = [], []

    def dependencies(binary):
        if binary == core:
            return {"libavcodec.so.60": codec, "libc.so.6": system / "libc.so.6", "libGL.so.1": system / "libGL.so.1"}
        if binary.name == codec.name:
            return {"libx264.so.164": codec_dependency}
        return {}

    monkeypatch.setattr(packaging, "_linux_linked_libraries", dependencies)
    monkeypatch.setattr(packaging, "_copy_linux_dependency_notice", lambda library, _root: notices.append(library))
    monkeypatch.setattr(packaging.subprocess, "run", lambda command, **_kwargs: commands.append(command))
    packaging._relocate_linux(root)
    assert (root / codec.name).read_bytes() == codec.read_bytes()
    assert (root / codec_dependency.name).read_bytes() == codec_dependency.read_bytes()
    assert not (root / "libc.so.6").exists() and not (root / "libGL.so.1").exists()
    assert notices == [codec, codec_dependency]
    assert {command[-1] for command in commands} == {str(core), str(root / codec.name), str(root / codec_dependency.name)}


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [(b"libavcodec.so.60 => not found\n", b""), (b"", b"version GLIBC_2.38 not found\n")],
)
def test_linux_dependency_probe_rejects_missing_libraries_and_incompatible_abi(
    monkeypatch, stdout, stderr,
):
    monkeypatch.setattr(
        packaging.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout, stderr),
    )
    with pytest.raises(packaging.LibobsPackagingError, match="Unresolved libobs dependencies"):
        packaging._linux_linked_libraries(Path("libobs.so.0"))


def test_linux_dependency_notices_follow_the_owning_distribution_package(tmp_path, monkeypatch):
    documents = tmp_path / "doc"
    _touch(documents / "libavcodec60/copyright", b"FFmpeg redistribution notice")
    monkeypatch.setattr(packaging, "_LINUX_DOCUMENTATION_ROOT", documents)
    monkeypatch.setattr(
        packaging.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, b"libavcodec60:amd64: /usr/lib/libavcodec.so.60\n", b"",
        ),
    )
    root = tmp_path / "runtime"
    packaging._copy_linux_dependency_notice(Path("/usr/lib/libavcodec.so.60"), root)
    assert (root / "licenses/system/libavcodec60/copyright").read_bytes() == b"FFmpeg redistribution notice"


def test_linux_host_mux_cannot_silently_use_unbundled_ffmpeg(tmp_path, monkeypatch):
    helper = tmp_path / "obs-ffmpeg-mux"
    monkeypatch.setattr(packaging, "_linux_linked_libraries", lambda _binary: {"libavcodec.so.60": tmp_path / "system/libavcodec.so.60"})
    monkeypatch.setattr(packaging.subprocess, "run", lambda _command, **_kwargs: None)
    with pytest.raises(packaging.LibobsPackagingError, match="was not bundled"):
        packaging._relocate_linux(tmp_path / "runtime", binaries=[helper])


def test_linux_dependency_notices_accept_pre_usr_merge_package_records(tmp_path, monkeypatch):
    documents = tmp_path / "doc"
    _touch(documents / "libgcc-s1/copyright", b"GCC runtime notice")
    monkeypatch.setattr(packaging, "_LINUX_DOCUMENTATION_ROOT", documents)
    searched = []

    def query(command, **_kwargs):
        searched.append(command[-1])
        found = command[-1] == "/lib/x86_64-linux-gnu/libgcc_s.so.1"
        return subprocess.CompletedProcess(
            command, 0 if found else 1,
            b"libgcc-s1:amd64: /lib/x86_64-linux-gnu/libgcc_s.so.1\n" if found else b"", b"",
        )

    monkeypatch.setattr(packaging.subprocess, "run", query)
    root = tmp_path / "runtime"
    packaging._copy_linux_dependency_notice(Path("/usr/lib/x86_64-linux-gnu/libgcc_s.so.1"), root)
    assert searched == ["/usr/lib/x86_64-linux-gnu/libgcc_s.so.1", "/lib/x86_64-linux-gnu/libgcc_s.so.1"]
    assert (root / "licenses/system/libgcc-s1/copyright").read_bytes() == b"GCC runtime notice"


def _mock_sidecar(
    monkeypatch, payload=None, returncode=0, corrupt=False, rejected=None, decoded=True
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        stream = io.BytesIO(b"".join(encode_envelope(request) for request in kwargs["requests"]))
        output = b""
        while request := read_envelope(stream):
            response = SceneIpcEnvelope(
                message_type={"hello": "hello_ack", "ping": "heartbeat"}.get(
                    request.message_type, "ack"
                ),
                request_id=request.request_id,
                session_id=request.session_id,
                process_generation=request.process_generation,
                sequence=request.sequence,
                document_revision=0,
                deadline_monotonic_ms=request.deadline_monotonic_ms,
                payload=({"hardware_compositing": True} if payload is None else payload)
                if request.message_type == "hello"
                else {"monotonic_ms": 100}
                if request.message_type == "ping"
                else {"applied": request.message_type != rejected},
            )
            output += encode_envelope(response)
            if decoded and request.message_type == "open_media":
                output += encode_envelope(
                    SceneIpcEnvelope(
                        message_type="media_playback_state",
                        request_id="event-media",
                        session_id=request.session_id,
                        process_generation=request.process_generation,
                        sequence=0,
                        document_revision=0,
                        deadline_monotonic_ms=request.deadline_monotonic_ms,
                        payload={
                            "state": 1,
                            "position_ms": 250,
                            "duration_ms": 2000,
                            "path": request.payload["path"],
                            "slot": 0,
                            "error_code": "",
                        },
                    )
                )
        return subprocess.CompletedProcess(
            command, returncode, b"native log\n" if corrupt else output, b"runtime diagnostics"
        )

    monkeypatch.setattr(packaging, "_run_preview_smoke", run)
    return calls


def test_smoke_requires_real_runtime_ignores_source_overrides_and_uses_binary_ipc(
    tmp_path, monkeypatch
):
    executable = tmp_path / "application" / "Solin.exe"
    _touch(executable)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "LIBOBS_PATH",
        "OBS_DATA_PATH",
        "SOLIN_LIBOBS_SIDECAR_NO_RUNTIME",
    ):
        monkeypatch.setenv(name, "developer override")
    calls = _mock_sidecar(monkeypatch)
    packaging.verify_packaged_sidecar(executable)
    command, options = calls[0]
    assert command == [str(executable.resolve()), "--scene-engine-sidecar"]
    assert options["cwd"] != executable.parent
    assert "LIBOBS_PATH" not in options["env"]
    assert "SOLIN_LIBOBS_SIDECAR_NO_RUNTIME" not in options["env"]
    assert options["requests"][0].message_type == "hello"
    assert options["requests"][1].payload["document"]["sources"][0]["type"] == "image"


@pytest.mark.parametrize(
    "parameters, message",
    [
        ({"payload": {"hardware_compositing": False}}, "failed to initialize"),
        ({"returncode": 2}, "exited with 2"),
        ({"corrupt": True}, "Invalid packaged sidecar IPC"),
        ({"rejected": "hydrate"}, "rejected hydrate"),
        ({"rejected": "open_media"}, "rejected open_media"),
        ({"decoded": False}, "did not decode"),
    ],
)
def test_smoke_rejects_lifecycle_success_without_a_working_runtime(
    tmp_path, monkeypatch, parameters, message
):
    executable = tmp_path / "Solin.exe"
    _touch(executable)
    _mock_sidecar(monkeypatch, **parameters)
    with pytest.raises(packaging.LibobsPackagingError, match=message):
        packaging.verify_packaged_sidecar(executable)
