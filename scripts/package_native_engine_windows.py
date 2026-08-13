from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE_EXECUTABLE = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "media-engine-gstreamer"
    / "Release"
    / "solin-media-engine.exe"
)
DEFAULT_VIRTUAL_CAMERA_FILTER_X64 = (
    DEFAULT_ENGINE_EXECUTABLE.parent / "solin-virtual-camera.dll"
)
DEFAULT_VIRTUAL_CAMERA_FILTER_X86 = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "directshow-filter-x86"
    / "Release"
    / "solin-virtual-camera.dll"
)
DEFAULT_GSTREAMER_ROOT = (
    REPOSITORY_ROOT / "build" / "dependencies" / "gstreamer-runtime" / "msvc_x86_64"
)
DEFAULT_GSTREAMER_LICENSES = (
    REPOSITORY_ROOT
    / "build"
    / "dependencies"
    / "gstreamer"
    / "msvc_x86_64"
    / "share"
    / "licenses"
)
DEFAULT_APPLICATION_DIRECTORY = REPOSITORY_ROOT / "build" / "main.dist"
GSTREAMER_PLUGIN_FILENAMES = (
    "gstapp.dll",
    "gstautodetect.dll",
    "gstcoreelements.dll",
    "gstd3d11.dll",
    "gstjpeg.dll",
    "gstlibav.dll",
    "gstmediafoundation.dll",
    "gstplayback.dll",
    "gstrawparse.dll",
    "gstrtp.dll",
    "gstrtpmanager.dll",
    "gstrtsp.dll",
    "gsttcp.dll",
    "gsttypefindfunctions.dll",
    "gstudp.dll",
    "gstvideoconvertscale.dll",
    "gstvideocrop.dll",
    "gstvideoparsersbad.dll",
    "gstvideotestsrc.dll",
)
GSTREAMER_RUNTIME_DLL_SEEDS = ("gstreamer-1.0-0.dll",)
GSTREAMER_RUNTIME_EXECUTABLES = (
    "gspawn-win64-helper-console.exe",
    "gspawn-win64-helper.exe",
)
GSTREAMER_PLUGIN_SCANNER = "libexec/gstreamer-1.0/gst-plugin-scanner.exe"
FORBIDDEN_GSTREAMER_FILENAMES = {
    "dca-0.dll",
    "dvdnav-4.dll",
    "dvdread-8.dll",
    "gsta52dec.dll",
    "gstdtsdec.dll",
    "gstdvdread.dll",
    "gstresindvd.dll",
    "gstx264.dll",
    "gstx265.dll",
    "x264-164.dll",
    "x265.dll",
}
FORBIDDEN_DEVELOPMENT_SUFFIXES = {".h", ".hpp", ".lib", ".pc"}
ALLOWED_FILTER_IMPORTS = {
    "advapi32.dll",
    "bcrypt.dll",
    "gdi32.dll",
    "kernel32.dll",
    "ole32.dll",
    "oleaut32.dll",
    "user32.dll",
}
WINDOWS_RENAME_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4)
PE_MACHINE_X86 = 0x014C
PE_MACHINE_X64 = 0x8664


class NativeEnginePackagingError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage the native media engine and private GStreamer runtime."
    )
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE_EXECUTABLE)
    parser.add_argument(
        "--virtual-camera-filter-x64",
        type=Path,
        help="x64 DirectShow filter; defaults to the directory containing --engine.",
    )
    parser.add_argument(
        "--virtual-camera-filter-x86",
        type=Path,
        help=(
            "x86 DirectShow filter; required with a custom --engine to prevent "
            "mixing build configurations."
        ),
    )
    parser.add_argument("--gstreamer-root", type=Path, default=DEFAULT_GSTREAMER_ROOT)
    parser.add_argument("--gstreamer-licenses", type=Path, default=DEFAULT_GSTREAMER_LICENSES)
    parser.add_argument("--application-dir", type=Path, default=DEFAULT_APPLICATION_DIRECTORY)
    parser.add_argument("--skip-verification", action="store_true")
    return parser


def _resolve_filter_sources(options: argparse.Namespace) -> tuple[Path, Path]:
    filter_x64 = options.virtual_camera_filter_x64 or options.engine.with_name(
        DEFAULT_VIRTUAL_CAMERA_FILTER_X64.name
    )
    filter_x86 = options.virtual_camera_filter_x86
    if filter_x86 is None:
        if options.engine != DEFAULT_ENGINE_EXECUTABLE:
            raise NativeEnginePackagingError(
                "A custom --engine requires --virtual-camera-filter-x86 from "
                "the matching build configuration"
            )
        filter_x86 = DEFAULT_VIRTUAL_CAMERA_FILTER_X86
    return filter_x64, filter_x86


def _validate_inputs(
    engine: Path,
    virtual_camera_filter_x64: Path,
    virtual_camera_filter_x86: Path,
    runtime_root: Path,
    licenses_root: Path,
    application_dir: Path,
) -> None:
    if sys.platform != "win32":
        raise NativeEnginePackagingError("Windows packaging must run on Windows")
    if not engine.is_file():
        raise NativeEnginePackagingError(f"Native engine executable is missing: {engine}")
    if not virtual_camera_filter_x64.is_file():
        raise NativeEnginePackagingError(
            f"Native x64 virtual-camera filter is missing: {virtual_camera_filter_x64}"
        )
    if not virtual_camera_filter_x86.is_file():
        raise NativeEnginePackagingError(
            f"Native x86 virtual-camera filter is missing: {virtual_camera_filter_x86}"
        )
    if _pe_machine(virtual_camera_filter_x64) != PE_MACHINE_X64:
        raise NativeEnginePackagingError(
            f"Virtual-camera x64 filter has the wrong PE architecture: {virtual_camera_filter_x64}"
        )
    if _pe_machine(virtual_camera_filter_x86) != PE_MACHINE_X86:
        raise NativeEnginePackagingError(
            f"Virtual-camera x86 filter has the wrong PE architecture: {virtual_camera_filter_x86}"
        )
    if not application_dir.is_dir() or not (application_dir / "Solin.exe").is_file():
        raise NativeEnginePackagingError(
            f"Nuitka application directory is incomplete: {application_dir}"
        )
    for relative_path in ("bin", "lib/gstreamer-1.0"):
        if not (runtime_root / relative_path).is_dir():
            raise NativeEnginePackagingError(
                f"GStreamer runtime directory is missing: {runtime_root / relative_path}"
            )
    if not licenses_root.is_dir():
        raise NativeEnginePackagingError(
            f"GStreamer redistribution notices are missing: {licenses_root}"
        )


def _read_c_string(data: bytes, offset: int) -> str | None:
    if offset < 0 or offset >= len(data):
        return None
    terminator = data.find(b"\0", offset)
    if terminator < 0:
        return None
    try:
        return data[offset:terminator].decode("ascii")
    except UnicodeDecodeError:
        return None


def _pe_machine(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if len(data) < 0x86 or data[:2] != b"MZ":
        return 0
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 6 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return 0
    return struct.unpack_from("<H", data, pe_offset + 4)[0]


def _pe_imports(path: Path) -> set[str]:
    """Return imported DLL names without depending on a Visual Studio installation."""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return set()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return set()
    coff_offset = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff_offset + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff_offset + 16)[0]
    optional_offset = coff_offset + 20
    if optional_offset + optional_size > len(data):
        return set()
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, optional_offset + 24)[0]
        directory_count_offset = optional_offset + 108
        directory_offset = optional_offset + 112
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", data, optional_offset + 28)[0]
        directory_count_offset = optional_offset + 92
        directory_offset = optional_offset + 96
    else:
        return set()
    if directory_count_offset + 4 > len(data):
        return set()
    directory_count = struct.unpack_from("<I", data, directory_count_offset)[0]
    section_offset = optional_offset + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            return set()
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def rva_to_offset(rva: int) -> int | None:
        for virtual_address, span, raw_offset, raw_size in sections:
            if virtual_address <= rva < virtual_address + span:
                relative = rva - virtual_address
                if relative >= raw_size:
                    return None
                result = raw_offset + relative
                return result if result < len(data) else None
        return rva if rva < len(data) else None

    def data_directory(index: int) -> tuple[int, int]:
        if directory_count <= index or directory_offset + (index + 1) * 8 > len(data):
            return (0, 0)
        return struct.unpack_from("<II", data, directory_offset + index * 8)

    imports: set[str] = set()
    import_rva, import_size = data_directory(1)
    import_offset = rva_to_offset(import_rva) if import_rva else None
    if import_offset is not None:
        end = min(len(data), import_offset + import_size) if import_size else len(data)
        cursor = import_offset
        while cursor + 20 <= end:
            descriptor = struct.unpack_from("<IIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0):
                break
            name_offset = rva_to_offset(descriptor[3])
            name = _read_c_string(data, name_offset) if name_offset is not None else None
            if name:
                imports.add(name)
            cursor += 20

    delay_rva, delay_size = data_directory(13)
    delay_offset = rva_to_offset(delay_rva) if delay_rva else None
    if delay_offset is not None:
        end = min(len(data), delay_offset + delay_size) if delay_size else len(data)
        cursor = delay_offset
        while cursor + 32 <= end:
            descriptor = struct.unpack_from("<IIIIIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0, 0, 0, 0):
                break
            attributes, name_address = descriptor[:2]
            name_rva = name_address if attributes & 1 else name_address - image_base
            name_offset = rva_to_offset(name_rva) if name_rva >= 0 else None
            name = _read_c_string(data, name_offset) if name_offset is not None else None
            if name:
                imports.add(name)
            cursor += 32
    return imports


def _validate_filter_imports(path: Path, architecture: str) -> None:
    imports = _pe_imports(path)
    if not imports:
        raise NativeEnginePackagingError(
            f"The {architecture} virtual-camera filter PE import table could not "
            "be audited"
        )
    unexpected = sorted(
        imported
        for imported in imports
        if imported.casefold() not in ALLOWED_FILTER_IMPORTS
    )
    if unexpected:
        raise NativeEnginePackagingError(
            f"The {architecture} virtual-camera filter imports dependencies outside "
            f"the production allowlist: {', '.join(unexpected)}"
        )


def _copy_required_runtime_dlls(
    runtime_root: Path,
    destination: Path,
    dependency_roots: Sequence[Path],
) -> tuple[str, ...]:
    runtime_bin = runtime_root / "bin"
    available = {
        path.name.casefold(): path for path in runtime_bin.iterdir() if path.is_file()
    }
    pending = list(dependency_roots)
    copied: dict[str, Path] = {}
    for filename in GSTREAMER_RUNTIME_DLL_SEEDS:
        dependency = available.get(filename.casefold())
        if dependency is None:
            raise NativeEnginePackagingError(
                f"Required GStreamer runtime DLL is missing: {runtime_bin / filename}"
            )
        pending.append(dependency)
    while pending:
        binary = pending.pop()
        for imported_name in _pe_imports(binary):
            key = imported_name.casefold()
            dependency = available.get(key)
            if dependency is not None and key not in copied:
                copied[key] = dependency
                pending.append(dependency)
        key = binary.name.casefold()
        if binary.parent == runtime_bin and key not in copied:
            copied[key] = binary
    destination.mkdir(parents=True, exist_ok=True)
    for dependency in sorted(copied.values(), key=lambda item: item.name.casefold()):
        shutil.copy2(dependency, destination / dependency.name)
    return tuple(path.name for path in sorted(copied.values(), key=lambda item: item.name.casefold()))


def _audit_plugin_licenses(runtime_root: Path, plugins: Sequence[Path]) -> dict[str, str]:
    inspector = runtime_root / "bin" / "gst-inspect-1.0.exe"
    if not inspector.is_file():
        raise NativeEnginePackagingError(
            f"GStreamer license inspector is missing: {inspector}"
        )
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        [str(runtime_root / "bin"), environment.get("PATH", "")]
    )
    environment["GST_PLUGIN_PATH_1_0"] = ""
    environment["GST_PLUGIN_SYSTEM_PATH_1_0"] = ""
    audited: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="solin-gstreamer-license-audit-") as temporary:
        environment["GST_REGISTRY_1_0"] = str(Path(temporary) / "registry.bin")
        for plugin in plugins:
            result = subprocess.run(
                [str(inspector), str(plugin)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                raise NativeEnginePackagingError(
                    f"Could not inspect GStreamer plugin license: {plugin.name}"
                )
            license_line = next(
                (
                    line.split(None, 1)[1].strip()
                    for line in result.stdout.splitlines()
                    if line.strip().startswith("License ")
                    and len(line.split(None, 1)) == 2
                ),
                None,
            )
            if license_line is None or not license_line.upper().startswith("LGPL"):
                raise NativeEnginePackagingError(
                    "Non-LGPL GStreamer plugin rejected: "
                    f"{plugin.name} ({license_line or 'unknown'})"
                )
            audited[plugin.name] = license_line
    return audited


def _copy_runtime(
    runtime_root: Path,
    destination: Path,
    engine: Path,
) -> dict[str, object]:
    plugin_root = runtime_root / "lib" / "gstreamer-1.0"
    plugins: list[Path] = []
    destination_plugins = destination / "lib" / "gstreamer-1.0"
    destination_plugins.mkdir(parents=True)
    for filename in GSTREAMER_PLUGIN_FILENAMES:
        plugin = plugin_root / filename
        if not plugin.is_file():
            raise NativeEnginePackagingError(f"Required GStreamer plugin is missing: {plugin}")
        plugins.append(plugin)
        shutil.copy2(plugin, destination_plugins / filename)
    licenses = _audit_plugin_licenses(runtime_root, plugins)

    scanner = runtime_root / GSTREAMER_PLUGIN_SCANNER
    if not scanner.is_file():
        raise NativeEnginePackagingError(f"GStreamer plugin scanner is missing: {scanner}")
    destination_scanner = destination / GSTREAMER_PLUGIN_SCANNER
    destination_scanner.parent.mkdir(parents=True)
    shutil.copy2(scanner, destination_scanner)

    dependency_roots = [engine, scanner, *plugins]
    runtime_bin = runtime_root / "bin"
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    for filename in GSTREAMER_RUNTIME_EXECUTABLES:
        executable = runtime_bin / filename
        if executable.is_file():
            shutil.copy2(executable, destination / "bin" / filename)
            dependency_roots.append(executable)
    runtime_dlls = _copy_required_runtime_dlls(
        runtime_root, destination / "bin", dependency_roots
    )
    return {
        "plugins": licenses,
        "runtime_dlls": runtime_dlls,
        "policy": "curated-lgpl-runtime",
    }


def _assert_release_payload_is_curated(runtime_root: Path) -> None:
    for path in runtime_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.casefold() in FORBIDDEN_GSTREAMER_FILENAMES:
            raise NativeEnginePackagingError(
                f"GPL GStreamer component entered the release payload: {path}"
            )
        if path.suffix.casefold() in FORBIDDEN_DEVELOPMENT_SUFFIXES:
            raise NativeEnginePackagingError(
                f"Development GStreamer artifact entered the release payload: {path}"
            )


def _replace_with_retry(source: Path, destination: Path) -> None:
    for delay in (*WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _remove_backup_with_retry(path: Path) -> None:
    for delay in (*WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if delay is None:
                print(
                    f"warning: obsolete native-engine backup could not be removed: {path}",
                    file=sys.stderr,
                )
                return
            time.sleep(delay)


def _replace_staging_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(
        f".{destination.name}-{uuid.uuid4().hex}.backup"
    )
    had_destination = destination.exists()
    if had_destination:
        _replace_with_retry(destination, backup)
    try:
        _replace_with_retry(staging, destination)
    except Exception as replacement_error:  # noqa: BLE001 - transactional rollback boundary
        if had_destination and backup.exists():
            try:
                _replace_with_retry(backup, destination)
            except Exception as rollback_error:  # noqa: BLE001 - rollback failure boundary
                raise NativeEnginePackagingError(
                    "Native engine replacement failed and the previous payload "
                    "could not be restored"
                ) from rollback_error
        raise replacement_error
    if backup.exists():
        _remove_backup_with_retry(backup)


def _verify_staged_engine(engine_root: Path) -> None:
    runtime_root = engine_root / "gstreamer"
    environment = os.environ.copy()
    search_directories = [runtime_root / "bin"]
    proxy_directory = runtime_root / "lib" / "libproxy"
    if proxy_directory.is_dir():
        search_directories.append(proxy_directory)
    environment["PATH"] = os.pathsep.join(
        [*(str(path) for path in search_directories), environment.get("PATH", "")]
    )
    environment["GST_PLUGIN_PATH_1_0"] = ""
    environment["GST_PLUGIN_SYSTEM_PATH_1_0"] = str(
        runtime_root / "lib" / "gstreamer-1.0"
    )
    scanner = runtime_root / "libexec" / "gstreamer-1.0" / "gst-plugin-scanner.exe"
    if scanner.is_file():
        environment["GST_PLUGIN_SCANNER_1_0"] = str(scanner)
    with tempfile.TemporaryDirectory(prefix="solin-native-engine-") as temporary_directory:
        environment["GST_REGISTRY_1_0"] = str(Path(temporary_directory) / "registry.bin")
        result = subprocess.run(
            [str(engine_root / "solin-media-engine.exe"), "--self-test"],
            env=environment,
            check=False,
            timeout=30,
        )
    if result.returncode != 0:
        raise NativeEnginePackagingError(
            f"Staged native engine self-test failed with exit code {result.returncode}"
        )


def package_native_engine(
    *,
    engine: Path,
    virtual_camera_filter_x64: Path,
    virtual_camera_filter_x86: Path,
    runtime_root: Path,
    licenses_root: Path,
    application_dir: Path,
    verify: bool,
) -> Path:
    engine = engine.resolve()
    virtual_camera_filter_x64 = virtual_camera_filter_x64.resolve()
    virtual_camera_filter_x86 = virtual_camera_filter_x86.resolve()
    runtime_root = runtime_root.resolve()
    licenses_root = licenses_root.resolve()
    application_dir = application_dir.resolve()
    _validate_inputs(
        engine,
        virtual_camera_filter_x64,
        virtual_camera_filter_x86,
        runtime_root,
        licenses_root,
        application_dir,
    )
    _validate_filter_imports(virtual_camera_filter_x64, "x64")
    _validate_filter_imports(virtual_camera_filter_x86, "x86")

    native_directory = application_dir / "native"
    native_directory.mkdir(parents=True, exist_ok=True)
    destination = native_directory / "media-engine"
    staging = Path(tempfile.mkdtemp(prefix="media-engine-staging-", dir=native_directory))
    try:
        shutil.copy2(engine, staging / "solin-media-engine.exe")
        virtual_camera_directory = staging / "virtual-camera"
        virtual_camera_directory.mkdir()
        (virtual_camera_directory / "x64").mkdir()
        (virtual_camera_directory / "x86").mkdir()
        shutil.copy2(
            virtual_camera_filter_x64,
            virtual_camera_directory / "x64" / "solin-virtual-camera.dll",
        )
        shutil.copy2(
            virtual_camera_filter_x86,
            virtual_camera_directory / "x86" / "solin-virtual-camera.dll",
        )
        runtime_manifest = _copy_runtime(
            runtime_root,
            staging / "gstreamer",
            engine,
        )
        shutil.copytree(licenses_root, staging / "gstreamer" / "share" / "licenses")
        _assert_release_payload_is_curated(staging / "gstreamer")
        (staging / "gstreamer" / "runtime-manifest.json").write_text(
            json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        license_directory = staging / "licenses" / "solin-media-engine"
        license_directory.mkdir(parents=True)
        shutil.copy2(
            REPOSITORY_ROOT
            / "native"
            / "media_engine"
            / "third_party"
            / "nlohmann-json.LICENSE.MIT",
            license_directory / "nlohmann-json.LICENSE.MIT",
        )
        shutil.copy2(
            REPOSITORY_ROOT
            / "native"
            / "media_engine"
            / "third_party"
            / "directshow-baseclasses.LICENSE",
            license_directory / "directshow-baseclasses.LICENSE",
        )
        shutil.copy2(
            REPOSITORY_ROOT
            / "native"
            / "media_engine"
            / "third_party"
            / "libyuv.LICENSE",
            license_directory / "libyuv.LICENSE",
        )
        if verify:
            _verify_staged_engine(staging)
        _replace_staging_directory(staging, destination)
    except Exception:  # noqa: BLE001 - incomplete staging cleanup boundary
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        virtual_camera_filter_x64, virtual_camera_filter_x86 = (
            _resolve_filter_sources(options)
        )
        destination = package_native_engine(
            engine=options.engine,
            virtual_camera_filter_x64=virtual_camera_filter_x64,
            virtual_camera_filter_x86=virtual_camera_filter_x86,
            runtime_root=options.gstreamer_root,
            licenses_root=options.gstreamer_licenses,
            application_dir=options.application_dir,
            verify=not options.skip_verification,
        )
    except (NativeEnginePackagingError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Packaged native media engine: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
