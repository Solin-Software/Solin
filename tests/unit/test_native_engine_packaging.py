from __future__ import annotations

import struct
import subprocess
from pathlib import Path

import pytest

from scripts import package_native_engine_windows


def _touch(path: Path, content: bytes = b"test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_release_staging_uses_curated_runtime_and_preserves_notices(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(package_native_engine_windows.sys, "platform", "win32")
    engine = tmp_path / "build" / "solin-media-engine.exe"
    virtual_camera_source = tmp_path / "build" / "solin-virtual-camera-source.dll"
    application = tmp_path / "application"
    runtime = tmp_path / "runtime"
    licenses = tmp_path / "licenses"
    _touch(engine)
    _touch(virtual_camera_source)
    _touch(application / "Solin.exe")
    _touch(runtime / "bin" / "gstreamer-1.0-0.dll")
    for filename in package_native_engine_windows.GSTREAMER_PLUGIN_FILENAMES:
        _touch(runtime / "lib" / "gstreamer-1.0" / filename)
    _touch(runtime / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe")
    _touch(runtime / "lib" / "gstreamer-1.0" / "gstx264.dll")
    _touch(runtime / "lib" / "site-packages" / "development-only.h")
    _touch(licenses / "gstreamer" / "COPYING")
    monkeypatch.setattr(
        package_native_engine_windows,
        "_audit_plugin_licenses",
        lambda _runtime, plugins: {plugin.name: "LGPL" for plugin in plugins},
    )

    destination = package_native_engine_windows.package_native_engine(
        engine=engine,
        virtual_camera_source=virtual_camera_source,
        runtime_root=runtime,
        licenses_root=licenses,
        application_dir=application,
        verify=False,
    )

    assert (destination / "solin-media-engine.exe").is_file()
    assert (
        destination / "virtual-camera" / "solin-virtual-camera-source.dll"
    ).is_file()
    assert (destination / "gstreamer" / "bin" / "gstreamer-1.0-0.dll").is_file()
    assert (destination / "gstreamer" / "lib" / "gstreamer-1.0" / "gstapp.dll").is_file()
    assert not (
        destination / "gstreamer" / "lib" / "gstreamer-1.0" / "gstx264.dll"
    ).exists()
    assert (
        destination
        / "gstreamer"
        / "libexec"
        / "gstreamer-1.0"
        / "gst-plugin-scanner.exe"
    ).is_file()
    assert (destination / "gstreamer" / "share" / "licenses" / "gstreamer" / "COPYING").is_file()
    assert not (destination / "gstreamer" / "lib" / "site-packages").exists()
    assert (
        destination
        / "licenses"
        / "solin-media-engine"
        / "nlohmann-json.LICENSE.MIT"
    ).is_file()
    manifest = destination / "gstreamer" / "runtime-manifest.json"
    assert manifest.is_file()
    assert '"policy": "curated-lgpl-runtime"' in manifest.read_text(encoding="utf-8")


def test_pe_import_reader_resolves_runtime_dependencies_without_build_tools(
    tmp_path: Path,
) -> None:
    binary = bytearray(0x400)
    binary[:2] = b"MZ"
    struct.pack_into("<I", binary, 0x3C, 0x80)
    binary[0x80:0x84] = b"PE\0\0"
    coff = 0x84
    struct.pack_into("<H", binary, coff + 2, 1)
    struct.pack_into("<H", binary, coff + 16, 0xF0)
    optional = coff + 20
    struct.pack_into("<H", binary, optional, 0x20B)
    struct.pack_into("<Q", binary, optional + 24, 0x140000000)
    struct.pack_into("<I", binary, optional + 108, 16)
    struct.pack_into("<II", binary, optional + 112 + 8, 0x1000, 40)
    section = optional + 0xF0
    binary[section : section + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", binary, section + 8, 0x200, 0x1000, 0x200, 0x200)
    struct.pack_into("<IIIII", binary, 0x200, 0, 0, 0, 0x1030, 0)
    binary[0x230 : 0x230 + len(b"gstreamer-1.0-0.dll\0")] = b"gstreamer-1.0-0.dll\0"
    executable = tmp_path / "engine.exe"
    executable.write_bytes(binary)

    assert package_native_engine_windows._pe_imports(executable) == {
        "gstreamer-1.0-0.dll"
    }


def test_plugin_license_audit_rejects_gpl_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    inspector = runtime / "bin" / "gst-inspect-1.0.exe"
    plugin = runtime / "lib" / "gstreamer-1.0" / "gstunexpected.dll"
    _touch(inspector)
    _touch(plugin)
    monkeypatch.setattr(
        package_native_engine_windows.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  License                  GPL\n", stderr=""
        ),
    )

    with pytest.raises(
        package_native_engine_windows.NativeEnginePackagingError,
        match="Non-LGPL GStreamer plugin rejected",
    ):
        package_native_engine_windows._audit_plugin_licenses(runtime, [plugin])


def test_release_payload_gate_rejects_known_gpl_components(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _touch(runtime / "lib" / "gstreamer-1.0" / "gstx264.dll")

    with pytest.raises(
        package_native_engine_windows.NativeEnginePackagingError,
        match="GPL GStreamer component entered",
    ):
        package_native_engine_windows._assert_release_payload_is_curated(runtime)


def test_staging_rename_retries_a_transient_windows_file_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    original_replace = Path.replace
    attempts = 0

    def flaky_replace(path: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(package_native_engine_windows.time, "sleep", lambda _delay: None)

    package_native_engine_windows._replace_staging_directory(staging, destination)

    assert attempts == 2
    assert destination.is_dir()


def test_staging_replacement_restores_previous_payload_on_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    destination.mkdir()
    _touch(staging / "payload", b"new")
    _touch(destination / "payload", b"previous")
    original_replace = Path.replace

    def fail_new_payload(path: Path, target: Path) -> Path:
        if path == staging:
            raise OSError("simulated replacement failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_new_payload)

    try:
        package_native_engine_windows._replace_staging_directory(staging, destination)
    except OSError as error:
        assert str(error) == "simulated replacement failure"
    else:
        raise AssertionError("replacement failure was not propagated")

    assert (destination / "payload").read_bytes() == b"previous"


def test_staging_cleanup_retries_a_transient_windows_file_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    original_rmtree = package_native_engine_windows.shutil.rmtree
    attempts = 0

    def flaky_rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        original_rmtree(path)

    monkeypatch.setattr(package_native_engine_windows.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(package_native_engine_windows.time, "sleep", lambda _delay: None)

    package_native_engine_windows._remove_backup_with_retry(backup)

    assert attempts == 2
    assert not backup.exists()


def test_custom_engine_derives_virtual_camera_source_from_same_directory() -> None:
    options = package_native_engine_windows._parser().parse_args(
        ["--engine", "custom/solin-media-engine.exe"]
    )

    source = options.virtual_camera_source or options.engine.with_name(
        package_native_engine_windows.DEFAULT_VIRTUAL_CAMERA_SOURCE.name
    )

    assert source == Path("custom/solin-virtual-camera-source.dll")
