from __future__ import annotations

import ast
import re
import struct
import uuid

import pytest

from scripts import manage_virtual_camera_directshow
from solin.core.scenes import windows_vcam_transport as wire
from tests._paths import REPO_ROOT

NATIVE_ROOT = REPO_ROOT / "native" / "virtual_camera"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _native_constant(source: str, name: str) -> int:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(0x[0-9A-Fa-f]+|\d+)U", source)
    assert match is not None, name
    return int(match.group(1), 0)


def _native_magic(source: str, name: str) -> bytes:
    match = re.search(rf"\b{re.escape(name)}\s*\{{([^}}]+)\}}", source)
    assert match is not None, name
    return "".join(re.findall(r"'([^'])'", match.group(1))).encode("ascii")


def test_native_camera_build_is_limited_to_transport_and_directshow() -> None:
    cmake = _read("native/virtual_camera/CMakeLists.txt")
    assert "cxx_std_20" in cmake and "/W4 /WX /permissive-" in cmake
    assert "SOLIN_VIRTUAL_CAMERA_BUILD_TESTS" in cmake
    assert "add_library(solin_virtual_camera_transport STATIC" in cmake
    for component in (
        "video_frame",
        "shared_video_frame_channel",
        "virtual_camera_broker_protocol",
    ):
        assert component in cmake
        assert (NATIVE_ROOT / "tests" / f"{component}_tests.cpp").is_file()
    for obsolete in ("GSTREAMER", "FILTER_ONLY", "nlohmann", "solin-media-engine", "libobs"):
        assert obsolete.casefold() not in cmake.casefold()
    for path in (NATIVE_ROOT / "include").rglob("*.hpp"):
        assert "namespace solin::virtual_camera" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "native_name,python_name",
    [
        ("kVirtualCameraBrokerProtocolVersion", "BROKER_PROTOCOL_VERSION"),
        ("kVirtualCameraBrokerNonceSize", "NONCE_SIZE"),
        ("kVirtualCameraBrokerRequestSize", "REQUEST_SIZE"),
        ("kVirtualCameraBrokerResponseSize", "RESPONSE_SIZE"),
        ("kVirtualCameraBrokerMaximumPathBytes", "MAX_PATH_BYTES"),
        ("kVirtualCameraBrokerPresenceMarker", "PRESENCE_MARKER"),
    ],
)
def test_python_and_native_broker_constants_match(native_name: str, python_name: str) -> None:
    header = _read(
        "native/virtual_camera/include/solin/virtual_camera/virtual_camera_broker_protocol.hpp"
    )
    assert _native_constant(header, native_name) == getattr(wire, python_name)


def test_python_and_native_broker_magic_and_enums_match() -> None:
    source = _read("native/virtual_camera/src/virtual_camera_broker_protocol.cpp")
    assert _native_magic(source, "kRequestMagic") == wire.REQUEST_MAGIC
    assert _native_magic(source, "kResponseMagic") == wire.RESPONSE_MAGIC
    header = _read(
        "native/virtual_camera/include/solin/virtual_camera/virtual_camera_broker_protocol.hpp"
    )
    for status in wire.BrokerStatus:
        assert re.search(rf"\b{status.name.lower()}\s*=\s*{int(status)}U", header)
    pixels = _read("native/virtual_camera/include/solin/virtual_camera/video_frame.hpp")
    for pixel in wire.PixelFormat:
        assert re.search(rf"\b{pixel.name.lower()}\s*=\s*{int(pixel)}U", pixels)


def test_broker_frames_match_native_field_offsets_and_padding_byte_for_byte() -> None:
    source = re.sub(
        r"\s+", "", _read("native/virtual_camera/src/virtual_camera_broker_protocol.cpp")
    )
    layout = wire.nv12_layout(16, 8)
    nonce = bytes(range(wire.NONCE_SIZE))
    path = "Solin.VirtualCamera.é.frames".encode("utf-8")
    expected = bytearray(wire.RESPONSE_SIZE)
    expected[:8] = _native_magic(source, "kResponseMagic")
    expected[16:32] = nonce
    fields = [
        ("H", "kVirtualCameraBrokerProtocolVersion", wire.BROKER_PROTOCOL_VERSION),
        ("H", "static_cast<std::uint16_t>(kVirtualCameraBrokerResponseSize)", wire.RESPONSE_SIZE),
        ("I", "static_cast<std::uint32_t>(response.status)", int(wire.BrokerStatus.OK)),
        ("Q", "response.mapping_size", wire.mapping_size_for(layout)),
        ("Q", "response.generation", 0x1122334455667788),
        ("Q", "response.layout.payload_size", layout.payload_size),
        ("I", "response.layout.width", layout.width),
        ("I", "response.layout.height", layout.height),
        ("I", "static_cast<std::uint32_t>(response.layout.pixel_format)", int(layout.pixel_format)),
        ("I", "response.layout.plane_count", layout.plane_count),
        ("I", "response.layout.plane_strides[0]", layout.plane_strides[0]),
        ("I", "response.layout.plane_strides[1]", layout.plane_strides[1]),
        ("Q", "response.layout.plane_offsets[1]", layout.plane_offsets[1]),
        ("I", "response.fps_numerator", 30000),
        ("I", "response.fps_denominator", 1001),
        ("H", "static_cast<std::uint16_t>(response.mapping_file_path_utf8.size())", len(path)),
    ]
    for fmt, expression, value in fields:
        match = re.search(
            rf"write_little_endian\(result,(\d+)U,{re.escape(re.sub(r'\s+', '', expression))}\)",
            source,
        )
        assert match is not None, expression
        struct.pack_into("<" + fmt, expected, int(match.group(1)), value)
    path_offset = re.search(
        r"response.mapping_file_path_utf8.end\(\),result.begin\(\)\+(\d+)U", source
    )
    assert path_offset is not None
    offset = int(path_offset.group(1))
    expected[offset : offset + len(path)] = path
    actual = wire.encode_response(
        status=wire.BrokerStatus.OK,
        nonce=nonce,
        mapping_file_path_utf8=path.decode("utf-8"),
        mapping_size=wire.mapping_size_for(layout),
        generation=0x1122334455667788,
        layout=layout,
        fps_numerator=30000,
        fps_denominator=1001,
    )
    assert actual == expected
    request_source = source.split("encode_virtual_camera_broker_request", 1)[1].split(
        "decode_virtual_camera_broker_request", 1
    )[0]
    assert re.search(
        r"write_little_endian\(result,8U,kVirtualCameraBrokerProtocolVersion\)", request_source
    )
    assert re.search(
        r"write_little_endian\(result,10U,static_cast<std::uint16_t>\(kVirtualCameraBrokerRequestSize\)\)",
        request_source,
    )
    assert "result.begin()+16U" in request_source
    assert wire.encode_request(nonce) == struct.pack(
        "<8sHHI16s", wire.REQUEST_MAGIC, wire.BROKER_PROTOCOL_VERSION, wire.REQUEST_SIZE, 0, nonce
    )
    for status in wire.BrokerStatus:
        if status != wire.BrokerStatus.OK:
            assert wire.encode_response(status=status, nonce=nonce) == struct.pack(
                "<8sHHI16s",
                wire.RESPONSE_MAGIC,
                wire.BROKER_PROTOCOL_VERSION,
                wire.RESPONSE_SIZE,
                int(status),
                nonce,
            ) + bytes(wire.RESPONSE_SIZE - 32)


@pytest.mark.parametrize(
    "native_name,python_name",
    [
        ("kVersion", "RING_VERSION"),
        ("kHeaderSize", "RING_HEADER_SIZE"),
        ("kSlotHeaderSize", "RING_SLOT_HEADER_SIZE"),
        ("kSlotCount", "RING_SLOT_COUNT"),
    ],
)
def test_python_and_native_ring_constants_match(native_name: str, python_name: str) -> None:
    source = _read("native/virtual_camera/src/shared_video_frame_channel.cpp")
    assert _native_constant(source, native_name) == getattr(wire, python_name)
    assert _native_magic(source, "kMagic") == wire.RING_MAGIC


def test_ring_header_and_slot_match_native_field_offsets_byte_for_byte() -> None:
    native_source = _read("native/virtual_camera/src/shared_video_frame_channel.cpp")
    source = re.sub(r"\s+", "", native_source)
    layout = wire.nv12_layout(16, 8)
    slot_size = wire.aligned_slot_size(layout.payload_size)
    mapping_size = wire.mapping_size_for(layout)
    assert slot_size % 64 == 0
    buf = bytearray(mapping_size)
    writer = wire.SharedFrameRingWriter(buf, layout, generation=0x1122334455667788)
    expected = bytearray(wire.RING_HEADER_SIZE)
    expected[:8] = _native_magic(source, "kMagic")
    fields = [
        ("H", "kVersion", wire.RING_VERSION),
        ("H", "static_cast<std::uint16_t>(kHeaderSize)", wire.RING_HEADER_SIZE),
        ("I", "kSlotCount", wire.RING_SLOT_COUNT),
        ("I", "configuration.layout.width", layout.width),
        ("I", "configuration.layout.height", layout.height),
        (
            "I",
            "static_cast<std::uint32_t>(configuration.layout.pixel_format)",
            int(layout.pixel_format),
        ),
        ("I", "configuration.layout.plane_count", layout.plane_count),
        ("I", "configuration.layout.plane_strides[0]", layout.plane_strides[0]),
        ("I", "configuration.layout.plane_strides[1]", layout.plane_strides[1]),
        ("Q", "configuration.layout.plane_offsets[1]", layout.plane_offsets[1]),
        ("Q", "configuration.generation", 0x1122334455667788),
        ("Q", "configuration.layout.payload_size", layout.payload_size),
        ("Q", "slot_size", slot_size),
        ("Q", "mapping_size", mapping_size),
    ]
    for fmt, expression, value in fields:
        native_type = {"H": "uint16_t", "I": "uint32_t", "Q": "uint64_t"}[fmt]
        match = re.search(
            rf"write_value<std::{native_type}>\(bytes,(\d+)U,{re.escape(re.sub(r'\s+', '', expression))}\)",
            source,
        )
        assert match is not None, expression
        struct.pack_into("<" + fmt, expected, int(match.group(1)), value)
    heartbeat_offset = _native_constant(native_source, "kHeartbeatOffset")
    published_offset = _native_constant(native_source, "kPublishedSequenceOffset")
    struct.pack_into("<Q", expected, heartbeat_offset, 1)
    assert buf[: wire.RING_HEADER_SIZE] == expected
    payload = bytes(range(layout.payload_size))
    writer.write(
        payload,
        presentation_timestamp_ns=123,
        duration_ns=456,
        produced_monotonic_ns=789,
        discontinuity=True,
    )
    expected_slot = bytearray(slot_size)
    struct.pack_into("<Q", expected_slot, 0, 2)
    for expression, fmt, value in [
        ("sequence", "Q", 1),
        ("frame.presentation_timestamp_ns", "Q", 123),
        ("frame.duration_ns", "Q", 456),
        ("frame.produced_monotonic_ns", "Q", 789),
        ("configuration_.layout.payload_size", "Q", layout.payload_size),
        ("frame.discontinuity ? 1U : 0U", "I", 1),
    ]:
        native_type = "uint64_t" if fmt == "Q" else "uint32_t"
        match = re.search(
            rf"write_value<std::{native_type}>\(bytes\(\),static_cast<std::size_t>\(slot_offset\+(\d+)U\),{re.escape(re.sub(r'\s+', '', expression))}\)",
            source,
        )
        assert match is not None, expression
        struct.pack_into("<" + fmt, expected_slot, int(match.group(1)), value)
    expected_slot[wire.RING_SLOT_HEADER_SIZE : wire.RING_SLOT_HEADER_SIZE + len(payload)] = payload
    assert buf[wire.RING_HEADER_SIZE : wire.RING_HEADER_SIZE + slot_size] == expected_slot
    assert struct.unpack_from("<Q", buf, published_offset) == (1,)
    assert struct.unpack_from("<Q", buf, heartbeat_offset) == (2,)
    assert not any(buf[wire.RING_HEADER_SIZE + slot_size :])


def test_camera_dependency_downloads_are_versioned_and_digest_pinned() -> None:
    cmake = _read("native/virtual_camera/cmake/SolinVirtualCameraFilter.cmake")
    assert "Windows-classic-samples/archive/d59e5f1" in cmake
    assert "libyuv/archive/eb6e7bb6" in cmake
    assert cmake.count("URL_HASH SHA256=") == 2


def test_windows_builds_and_installer_package_the_same_filter_pair() -> None:
    workflow = _read(".github/workflows/build-solin-windows.yml")
    local = _read("scripts/build_solin.bat")
    installer = _read("packaging/windows/installer/setup.iss")
    for recipe in (workflow, local):
        assert "scripts\\build_virtual_camera.py" in recipe
        assert "scripts\\package_virtual_camera_windows.py" in recipe
        for architecture in ("x64", "x86"):
            assert f"native\\virtual-camera\\{architecture}\\solin-virtual-camera.dll" in recipe
        assert "package_libobs_runtime.py" in recipe
        assert "gstreamer" not in recipe.casefold()
    for architecture in ("x64", "x86"):
        assert f"native\\virtual-camera\\{architecture}\\solin-virtual-camera.dll" in installer
        assert (
            f"build\\native\\virtual-camera-{architecture}\\Release\\solin-virtual-camera-filter-tests.exe"
            in workflow
        )
    quality = _read(".github/workflows/quality.yml")
    assert "native/virtual_camera" in quality and "build/native/virtual-camera-ci" in quality
    assert "SOLIN_VIRTUAL_CAMERA_BUILD_TESTS=ON" in quality


def test_retired_engine_sources_scripts_and_qt_multimedia_imports_are_absent() -> None:
    for path in (
        "native/media_engine",
        "native/qt_media_bridge",
        "native/media_transport",
        "src/solin/core/scenes/frame_channel.py",
        "src/solin/controllers/scene_frame_egress_controller.py",
        "src/solin/core/scenes/native_engine.py",
        "scripts/build_native_engine.py",
        "scripts/package_native_engine_windows.py",
        "scripts/build_qt_media_bridge.py",
        "scripts/install_qt_bridge_sdk_windows.ps1",
        "scripts/install_gstreamer_windows.ps1",
        "scripts/benchmark_native_media_route.py",
        "docs/native-media-performance.md",
        "docs/native-scenes-engine-implementation-plan.md",
    ):
        assert not (REPO_ROOT / path).exists(), path
    violations = []
    for path in (REPO_ROOT / "src" / "solin").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""] + [alias.name for alias in node.names]
            else:
                continue
            if any(
                "QtMultimedia" in module or "solin_qt_media_bridge" in module for module in modules
            ):
                violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == []
    bootstrap = _read("src/solin/bootstrap/application.py")
    assert "create_libobs_scene_engine(" in bootstrap
    assert "create_native_scene_engine" not in bootstrap
    assert "libobs_scene_engine_selected" not in bootstrap


def test_windows_virtual_camera_registration_contract_is_consistent() -> None:
    contract = _read(
        "native/virtual_camera/include/solin/virtual_camera/windows_virtual_camera_contract.hpp"
    )
    installer = _read("packaging/windows/installer/setup.iss")
    registration_coordinator = _read("packaging/windows/installer/virtual_camera_registration.iss")
    development_registration = _read("scripts/manage_virtual_camera_directshow.py")
    source_exports = _read(
        "native/virtual_camera/windows/directshow_virtual_camera/solin_virtual_camera.def"
    )
    local_build = _read("scripts/build_solin.bat")

    guid_body = re.search(r"kDirectShowFilterClassId\s*\{(.*?)\};", contract, re.S)
    assert guid_body is not None
    parts = [int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]+)", guid_body.group(1))]
    assert len(parts) == 11
    native_guid = uuid.UUID(
        fields=(
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            int.from_bytes(bytes(parts[5:]), "big"),
        )
    )
    assert native_guid == uuid.UUID(manage_virtual_camera_directshow.FILTER_CLSID)
    assert str(native_guid).upper() in development_registration
    assert "ArchitecturesAllowed=x64os" in installer
    for setup_script in (installer,):
        assert "InstallCameraRegistrationTransaction();" in setup_script
        assert 'DestName: "solin-virtual-camera-x64.dll"; Flags: dontcopy' in setup_script
        assert 'DestName: "solin-virtual-camera-x86.dll"; Flags: dontcopy' in setup_script
        assert 'include "uninstall.iss"' in setup_script
        install_step = setup_script.split("if CurStep = ssInstall then", 1)[1]
        install_step = install_step.split("if CurStep = ssPostInstall then", 1)[0]
        assert "InstallCameraRegistrationTransaction();" in install_step
    assert "regserver" not in installer
    assert "MinVersion=10.0.17763" in installer
    assert "function ConfirmCloseRunningSolin(): Boolean;" in installer
    assert "'Close it and continue?'" in installer
    assert "Result := ConfirmCloseRunningSolin();" in installer
    assert "TerminateProcess" not in installer
    assert "CleanupObsoleteCameraVersions" in installer
    assert "{commoncf64}" not in installer
    assert "FrameServer" not in installer
    assert "SignedUninstaller=yes" not in installer
    assert "SignTool=" not in installer
    assert "LOCALAPPDATA" in development_registration
    assert "_stage_filters" in development_registration
    assert "System32" in development_registration
    assert "SysWOW64" in development_registration
    assert "DllUnregisterServer" in source_exports
    assert "DllInstall" in source_exports
    assert "regsvr32.exe" in registration_coordinator
    assert "{commonpf64}\\Solin\\VirtualCamera" in registration_coordinator
    assert "{localappdata}\\Solin\\VirtualCamera" in registration_coordinator
    assert "HKLM64" in registration_coordinator
    assert "HKCU64" in registration_coordinator
    assert "'/n /i:'" in registration_coordinator
    assert "RegisterCameraPair" in registration_coordinator
    assert "ExecAndWaitResponsive" not in registration_coordinator
    assert "RollbackCameraRegistrationTransaction" in registration_coordinator
    assert 'include "virtual_camera_registration.iss"' in installer
    assert 'command.append("/u")' in development_registration
    assert "scripts\\build_virtual_camera.py" in local_build
    assert "scripts\\package_virtual_camera_windows.py" in local_build
