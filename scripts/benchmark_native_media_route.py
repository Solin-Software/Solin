"""Measure the current native media route without changing its production pipeline.

The harness has three explicit phases:

1. generate a deterministic H.264 fixture with the repository-pinned GStreamer;
2. write an immutable scenario configuration describing the outputs under test;
3. attach to explicit process IDs and record cumulative CPU deltas in one-second buckets.

It intentionally does not automate Solin or infer process IDs. The operator controls the
real application/consumer route, while the report records enough provenance to make results
auditable and comparable.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GSTREAMER_BOOTSTRAP = REPOSITORY_ROOT / "scripts" / "install_gstreamer_windows.ps1"
DEFAULT_GSTREAMER_ROOT = (
    REPOSITORY_ROOT / "build" / "dependencies" / "gstreamer" / "msvc_x86_64"
)
FIXTURE_SCHEMA = "solin.native-media-route-fixture.v1"
CONFIGURATION_SCHEMA = "solin.native-media-route-configuration.v1"
REPORT_SCHEMA = "solin.native-media-route-benchmark.v1"
CURRENT_ROUTE = "qt_media_to_native_program"
BUCKET_SECONDS = 1.0
MINIMUM_MEASUREMENT_BUCKETS = 60
_PLACEHOLDER = "replace-me"


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    cpu_seconds: float
    resident_bytes: int | None
    executable: str


@dataclass(frozen=True, slots=True)
class ProcessTarget:
    role: str
    pid: int


@dataclass(frozen=True, slots=True)
class ProcessBucket:
    role: str
    pid: int
    index: int
    elapsed_seconds: float
    cpu_seconds: float
    cpu_one_core_percent: float
    cpu_host_percent: float
    resident_bytes: int | None


def _write_json(path: Path, payload: object) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read JSON from {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("Percentiles require at least one sample")
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("Percentile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction))


def _summary(values: Sequence[float], *, digits: int = 4) -> dict[str, float | int]:
    if not values:
        raise ValueError("A metric summary requires samples")
    return {
        "samples": len(values),
        "p50": round(statistics.median(values), digits),
        "p95": round(_percentile(values, 0.95), digits),
        "p99": round(_percentile(values, 0.99), digits),
        "minimum": round(min(values), digits),
        "maximum": round(max(values), digits),
    }


def _pinned_gstreamer_version() -> tuple[str, str]:
    source = GSTREAMER_BOOTSTRAP.read_text(encoding="utf-8-sig")
    version_match = re.search(r'^\$version\s*=\s*"([^"]+)"', source, re.MULTILINE)
    digest_match = re.search(
        r'^\$expectedSha256\s*=\s*"([0-9a-fA-F]{64})"',
        source,
        re.MULTILINE,
    )
    if version_match is None or digest_match is None:
        raise RuntimeError("Could not read the pinned GStreamer version and digest")
    return version_match.group(1), digest_match.group(1).casefold()


def _gstreamer_tools(root: Path) -> tuple[Path, Path]:
    suffix = ".exe" if sys.platform == "win32" else ""
    launch = root / "bin" / f"gst-launch-1.0{suffix}"
    inspect = root / "bin" / f"gst-inspect-1.0{suffix}"
    if not launch.is_file() or not inspect.is_file():
        raise FileNotFoundError(
            "The pinned GStreamer development runtime is incomplete. "
            "Run scripts/install_gstreamer_windows.ps1 first."
        )
    return launch, inspect


def _reported_gstreamer_version(inspect: Path) -> str:
    result = subprocess.run(
        [str(inspect), "--version"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gst-inspect failed: {result.stderr.strip()}")
    match = re.search(r"GStreamer\s+([0-9]+(?:\.[0-9]+){2,3})", result.stdout)
    if match is None:
        raise RuntimeError("Could not parse the GStreamer runtime version")
    return match.group(1)


def _fixture_pipeline(
    *,
    output: Path,
    width: int,
    height: int,
    frames_per_second: int,
    duration_seconds: int,
    bitrate: int,
) -> list[str]:
    frame_count = frames_per_second * duration_seconds
    return [
        "videotestsrc",
        f"num-buffers={frame_count}",
        "pattern=ball",
        "is-live=false",
        "!",
        (
            "video/x-raw,format=I420,"
            f"width={width},height={height},framerate={frames_per_second}/1"
        ),
        "!",
        "openh264enc",
        f"bitrate={bitrate}",
        "rate-control=bitrate",
        "enable-frame-skip=false",
        "multi-thread=1",
        f"gop-size={frames_per_second * 2}",
        "!",
        "h264parse",
        "config-interval=-1",
        "!",
        "video/x-h264,stream-format=byte-stream,alignment=au",
        "!",
        "avimux",
        "!",
        "filesink",
        # gst-launch parses backslashes in property values as escapes. POSIX-style
        # separators preserve an absolute Windows path without losing characters.
        f"location={output.resolve().as_posix()}",
    ]


def generate_fixture(
    *,
    gstreamer_root: Path,
    output: Path,
    manifest_path: Path,
    width: int,
    height: int,
    frames_per_second: int,
    duration_seconds: int,
    bitrate: int,
) -> dict[str, Any]:
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("Fixture dimensions must be positive even integers")
    if not 1 <= frames_per_second <= 120:
        raise ValueError("Fixture frame rate must be between 1 and 120")
    if duration_seconds < 1:
        raise ValueError("Fixture duration must be positive")
    if bitrate < 100_000:
        raise ValueError("Fixture bitrate must be at least 100000 bits/s")
    if output.suffix.casefold() != ".avi":
        raise ValueError("The deterministic fixture output must use the .avi extension")

    pinned_version, installer_sha256 = _pinned_gstreamer_version()
    launch, inspect = _gstreamer_tools(gstreamer_root.resolve())
    reported_version = _reported_gstreamer_version(inspect)
    if reported_version != pinned_version:
        raise RuntimeError(
            f"GStreamer {reported_version} does not match pinned {pinned_version}"
        )

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    pipeline = _fixture_pipeline(
        output=output,
        width=width,
        height=height,
        frames_per_second=frames_per_second,
        duration_seconds=duration_seconds,
        bitrate=bitrate,
    )
    result = subprocess.run(
        [str(launch), "--quiet", "--eos-on-shutdown", *pipeline],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=max(120, duration_seconds * 4),
    )
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "GStreamer fixture generation failed. "
            f"exit={result.returncode} stderr={result.stderr.strip()}"
        )

    manifest = {
        "schema": FIXTURE_SCHEMA,
        "generator": {
            "gstreamer_version": reported_version,
            "pinned_installer_sha256": installer_sha256,
            "gst_launch_sha256": _sha256(launch),
            "pipeline": pipeline,
        },
        "asset": {
            "path": str(output),
            "sha256": _sha256(output),
            "bytes": output.stat().st_size,
            "container": "avi",
            "video_codec": "H.264 (OpenH264)",
            "width": width,
            "height": height,
            "frames_per_second": frames_per_second,
            "duration_seconds": duration_seconds,
            "frame_count": frames_per_second * duration_seconds,
            "audio": False,
        },
    }
    _write_json(manifest_path, manifest)
    return manifest


def write_configuration(
    *,
    fixture_manifest_path: Path,
    output: Path,
    native_presenters: int,
    editor_preview: bool,
    virtual_camera: bool,
    consumer_application: str | None,
    consumer_architecture: str | None,
    consumer_profile: str | None,
    decode_path: str,
) -> dict[str, Any]:
    fixture = _read_json(fixture_manifest_path)
    _validate_fixture_manifest(fixture)
    asset = fixture["asset"]
    asset_path = Path(asset["path"])
    if not asset_path.is_file() or _sha256(asset_path) != asset["sha256"]:
        raise ValueError("Fixture asset is missing or does not match its SHA-256")
    if not 0 <= native_presenters <= 3:
        raise ValueError("Native presenter count must be between zero and three")
    if virtual_camera and not all(
        value and value != _PLACEHOLDER
        for value in (consumer_application, consumer_architecture, consumer_profile)
    ):
        raise ValueError(
            "Virtual-camera runs require consumer application, architecture, and profile"
        )

    configuration = {
        "schema": CONFIGURATION_SCHEMA,
        "route": CURRENT_ROUTE,
        "fixture": fixture,
        "playback": {
            "adapter": "QtMultimedia",
            "decode_path": decode_path,
            # The default fixture is longer than warmup + the recommended run.
            # Avoiding a loop seam keeps steady-state results separate from seek/replay cost.
            "loop_fixture": False,
        },
        "outputs": {
            "native_presenters": native_presenters,
            "editor_preview": editor_preview,
            "virtual_camera": virtual_camera,
        },
        "virtual_camera_consumer": (
            {
                "application": consumer_application,
                "architecture": consumer_architecture,
                "profile": consumer_profile,
            }
            if virtual_camera
            else None
        ),
        "measurement_notes": [
            "CPU includes decode and every enabled output named above.",
            "The harness does not measure GPU engine utilization or frame latency.",
            "The DirectShow consumer must be sampled separately when enabled.",
        ],
    }
    _validate_configuration(configuration)
    _write_json(output, configuration)
    return configuration


def _validate_fixture_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != FIXTURE_SCHEMA:
        raise ValueError(f"Fixture manifest must use {FIXTURE_SCHEMA}")
    generator = manifest.get("generator")
    asset = manifest.get("asset")
    if not isinstance(generator, dict) or not isinstance(asset, dict):
        raise ValueError("Fixture manifest is missing generator or asset metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", ""))):
        raise ValueError("Fixture manifest contains an invalid asset SHA-256")
    for name in ("width", "height", "frames_per_second", "duration_seconds"):
        value = asset.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Fixture manifest contains invalid {name}")


def _validate_configuration(configuration: dict[str, Any]) -> None:
    if configuration.get("schema") != CONFIGURATION_SCHEMA:
        raise ValueError(f"Configuration must use {CONFIGURATION_SCHEMA}")
    if configuration.get("route") != CURRENT_ROUTE:
        raise ValueError(f"Configuration route must be {CURRENT_ROUTE}")
    _validate_fixture_manifest(configuration.get("fixture", {}))
    playback = configuration.get("playback")
    outputs = configuration.get("outputs")
    if not isinstance(playback, dict) or not isinstance(outputs, dict):
        raise ValueError("Configuration is missing playback or output metadata")
    if playback.get("adapter") != "QtMultimedia":
        raise ValueError("Current-route measurements require the QtMultimedia adapter")
    if playback.get("loop_fixture") is not False:
        raise ValueError("Steady-state measurements must not loop the fixture")
    if playback.get("decode_path") not in {"automatic", "hardware", "software"}:
        raise ValueError("Decode path must be automatic, hardware, or software")
    presenter_count = outputs.get("native_presenters")
    if (
        isinstance(presenter_count, bool)
        or not isinstance(presenter_count, int)
        or not 0 <= presenter_count <= 3
    ):
        raise ValueError("Configuration contains an invalid native presenter count")
    for name in ("editor_preview", "virtual_camera"):
        if not isinstance(outputs.get(name), bool):
            raise ValueError(f"Configuration contains invalid {name}")
    consumer = configuration.get("virtual_camera_consumer")
    if outputs["virtual_camera"]:
        if not isinstance(consumer, dict):
            raise ValueError("Virtual-camera configuration requires consumer metadata")
        for name in ("application", "architecture", "profile"):
            value = consumer.get(name)
            if not isinstance(value, str) or not value.strip() or value == _PLACEHOLDER:
                raise ValueError(f"Virtual-camera consumer requires a real {name}")
    elif consumer is not None:
        raise ValueError("Disabled virtual-camera configuration must not name a consumer")


def _parse_process_target(value: str) -> ProcessTarget:
    role, separator, raw_pid = value.partition("=")
    if not separator or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
        raise argparse.ArgumentTypeError("Process must use role=PID with a stable lowercase role")
    try:
        pid = int(raw_pid)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Process PID must be an integer") from error
    if pid <= 0:
        raise argparse.ArgumentTypeError("Process PID must be positive")
    return ProcessTarget(role=role, pid=pid)


def _windows_filetime_value(value: wintypes.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _windows_process_snapshot(pid: int) -> ProcessSnapshot:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    process_vm_read = 0x0010
    handle = kernel32.OpenProcess(
        process_query_limited_information | process_vm_read,
        False,
        pid,
    )
    if not handle:
        raise ProcessLookupError(pid)

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ProcessLookupError(pid)
        image = ctypes.create_unicode_buffer(32_768)
        image_size = wintypes.DWORD(len(image))
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            image,
            ctypes.byref(image_size),
        ):
            executable = f"pid-{pid}"
        else:
            executable = Path(image.value).name
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        resident = (
            int(counters.WorkingSetSize)
            if psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            )
            else None
        )
        cpu_seconds = (
            _windows_filetime_value(kernel) + _windows_filetime_value(user)
        ) / 10_000_000.0
        return ProcessSnapshot(cpu_seconds, resident, executable)
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_snapshot(pid: int) -> ProcessSnapshot:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    command_end = stat.rfind(")")
    if command_end < 0:
        raise ProcessLookupError(pid)
    fields = stat[command_end + 2 :].split()
    clock_ticks = os.sysconf("SC_CLK_TCK")
    cpu_seconds = (int(fields[11]) + int(fields[12])) / clock_ticks
    resident: int | None = None
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            resident = int(line.split()[1]) * 1024
            break
    executable = Path(os.readlink(f"/proc/{pid}/exe")).name
    return ProcessSnapshot(cpu_seconds, resident, executable)


def _parse_ps_cpu_time(value: str) -> float:
    days = 0
    time_value = value.strip()
    if "-" in time_value:
        raw_days, time_value = time_value.split("-", 1)
        days = int(raw_days)
    components = [int(component) for component in time_value.split(":")]
    if len(components) == 2:
        hours, minutes, seconds = 0, components[0], components[1]
    elif len(components) == 3:
        hours, minutes, seconds = components
    else:
        raise ValueError(f"Unsupported ps CPU time: {value}")
    return float((((days * 24) + hours) * 60 + minutes) * 60 + seconds)


def _darwin_process_snapshot(pid: int) -> ProcessSnapshot:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "time=,rss=,comm="],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ProcessLookupError(pid)
    raw_time, raw_rss, executable = result.stdout.strip().split(maxsplit=2)
    return ProcessSnapshot(
        _parse_ps_cpu_time(raw_time),
        int(raw_rss) * 1024,
        Path(executable).name,
    )


def read_process_snapshot(pid: int) -> ProcessSnapshot:
    try:
        if sys.platform == "win32":
            return _windows_process_snapshot(pid)
        if sys.platform.startswith("linux"):
            return _linux_process_snapshot(pid)
        if sys.platform == "darwin":
            return _darwin_process_snapshot(pid)
    except (FileNotFoundError, OSError, PermissionError, ValueError) as error:
        raise ProcessLookupError(pid) from error
    raise RuntimeError(f"Process sampling is unsupported on {sys.platform}")


def collect_buckets(
    targets: Sequence[ProcessTarget],
    *,
    bucket_count: int,
    bucket_seconds: float = BUCKET_SECONDS,
    snapshot_reader: Callable[[int], ProcessSnapshot] = read_process_snapshot,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.perf_counter,
) -> list[ProcessBucket]:
    if not targets:
        raise ValueError("At least one process target is required")
    if len({target.role for target in targets}) != len(targets):
        raise ValueError("Process roles must be unique")
    if len({target.pid for target in targets}) != len(targets):
        raise ValueError("Each process role must use a distinct PID")
    if bucket_count < 1 or bucket_seconds <= 0:
        raise ValueError("Bucket count and duration must be positive")
    logical_processors = os.cpu_count() or 1
    previous = {target.role: snapshot_reader(target.pid) for target in targets}
    previous_sampled_at = monotonic()
    deadline = previous_sampled_at
    buckets: list[ProcessBucket] = []

    for index in range(bucket_count):
        deadline += bucket_seconds
        sleep(max(0.0, deadline - monotonic()))
        current_by_role = {
            target.role: snapshot_reader(target.pid) for target in targets
        }
        sampled_at = monotonic()
        elapsed = sampled_at - previous_sampled_at
        if elapsed <= 0:
            raise RuntimeError("Process sampling clock did not advance")
        for target in targets:
            current = current_by_role[target.role]
            before = previous[target.role]
            if current.executable != before.executable:
                raise RuntimeError(
                    f"Process role {target.role} changed executable during measurement"
                )
            cpu_seconds = current.cpu_seconds - before.cpu_seconds
            if cpu_seconds < 0:
                raise RuntimeError(f"Process role {target.role} CPU time moved backwards")
            one_core_percent = cpu_seconds / elapsed * 100.0
            buckets.append(
                ProcessBucket(
                    role=target.role,
                    pid=target.pid,
                    index=index,
                    elapsed_seconds=elapsed,
                    cpu_seconds=cpu_seconds,
                    cpu_one_core_percent=one_core_percent,
                    cpu_host_percent=one_core_percent / logical_processors,
                    resident_bytes=current.resident_bytes,
                )
            )
            previous[target.role] = current
        previous_sampled_at = sampled_at
    return buckets


def _git_metadata() -> dict[str, Any]:
    def run(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    status = run("status", "--porcelain")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status and status != "unavailable"),
    }


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return platform.processor() or "unavailable"


def _total_memory_bytes() -> int | None:
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
        return None
    try:
        if sys.platform.startswith("linux"):
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                check=False,
                encoding="utf-8",
                timeout=5,
            )
            return int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError):
        pass
    return None


def _gpu_metadata() -> dict[str, Any]:
    if sys.platform != "win32":
        return {
            "status": "not_collected",
            "reason": "Automatic GPU inventory is currently implemented for Windows only",
        }
    command = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"status": "unavailable", "reason": result.stderr.strip() or "CIM failed"}
    try:
        controllers = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "unavailable", "reason": "CIM returned invalid JSON"}
    if isinstance(controllers, dict):
        controllers = [controllers]
    return {"status": "collected", "controllers": controllers}


def hardware_metadata() -> dict[str, Any]:
    return {
        "operating_system": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "cpu": {
            "name": _cpu_name(),
            "logical_processors": os.cpu_count() or 1,
        },
        "memory_bytes": _total_memory_bytes(),
        "gpu": _gpu_metadata(),
        "python": platform.python_version(),
    }


def _configuration_with_verified_fixture(path: Path) -> dict[str, Any]:
    configuration = _read_json(path)
    _validate_configuration(configuration)
    asset = configuration["fixture"]["asset"]
    asset_path = Path(asset["path"])
    if not asset_path.is_file():
        raise ValueError(f"Configured fixture is missing: {asset_path}")
    digest = _sha256(asset_path)
    if digest != asset["sha256"]:
        raise ValueError("Configured fixture no longer matches its recorded SHA-256")
    return configuration


def build_report(
    *,
    configuration: dict[str, Any],
    configuration_sha256: str,
    targets: Sequence[ProcessTarget],
    initial_snapshots: dict[str, ProcessSnapshot],
    buckets: Sequence[ProcessBucket],
    warmup_seconds: int,
    source: dict[str, Any],
    hardware: dict[str, Any],
) -> dict[str, Any]:
    if not buckets:
        raise ValueError("Report requires measured process buckets")
    logical_processors = int(hardware["cpu"]["logical_processors"])
    processes: dict[str, Any] = {}
    for target in targets:
        process_buckets = [bucket for bucket in buckets if bucket.role == target.role]
        if not process_buckets:
            raise ValueError(f"No buckets were recorded for {target.role}")
        host_cpu = [bucket.cpu_host_percent for bucket in process_buckets]
        one_core_cpu = [bucket.cpu_one_core_percent for bucket in process_buckets]
        resident_mib = [
            bucket.resident_bytes / (1024 * 1024)
            for bucket in process_buckets
            if bucket.resident_bytes is not None
        ]
        raw = [
            {
                "index": bucket.index,
                "elapsed_seconds": round(bucket.elapsed_seconds, 6),
                "cpu_seconds": round(bucket.cpu_seconds, 6),
                "cpu_one_core_percent": round(bucket.cpu_one_core_percent, 4),
                "cpu_host_percent": round(bucket.cpu_host_percent, 4),
                "resident_bytes": bucket.resident_bytes,
            }
            for bucket in process_buckets
        ]
        processes[target.role] = {
            "pid": target.pid,
            "executable": initial_snapshots[target.role].executable,
            "cpu_host_percent": _summary(host_cpu),
            "cpu_one_core_percent": _summary(one_core_cpu),
            "resident_mib": _summary(resident_mib) if resident_mib else None,
            "buckets": raw,
        }

    bucket_indices = sorted({bucket.index for bucket in buckets})
    aggregate_host_cpu = [
        sum(bucket.cpu_host_percent for bucket in buckets if bucket.index == index)
        for index in bucket_indices
    ]
    return {
        "schema": REPORT_SCHEMA,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source": source,
        "hardware": hardware,
        "configuration": configuration,
        "configuration_sha256": configuration_sha256,
        "sampling": {
            "warmup_seconds": warmup_seconds,
            "bucket_target_seconds": BUCKET_SECONDS,
            "measurement_buckets": len(bucket_indices),
            "cpu_normalization": {
                "cpu_one_core_percent": "100% equals one fully occupied logical processor",
                "cpu_host_percent": (
                    "cpu_one_core_percent divided by the recorded logical processor count"
                ),
                "logical_processors": logical_processors,
            },
        },
        "aggregate_sampled_processes": {
            "cpu_host_percent": _summary(aggregate_host_cpu),
            "roles": [target.role for target in targets],
            "excludes": "GPU utilization and any process whose PID was not supplied",
        },
        "processes": processes,
        "interpretation_limits": [
            "Percentiles describe one-second CPU buckets, not frame latency.",
            "CPU from the operating system, GPU driver, DWM, and unsampled consumers is excluded.",
            "A hardware decode label records operator intent; this harness cannot prove decoder selection.",
            "Compare reports only when fixture, configuration, warmup, duration, and hardware match.",
        ],
    }


def record(
    *,
    configuration_path: Path,
    output: Path,
    targets: Sequence[ProcessTarget],
    warmup_seconds: int,
    duration_seconds: int,
) -> dict[str, Any]:
    if warmup_seconds < 0:
        raise ValueError("Warmup duration cannot be negative")
    if duration_seconds < MINIMUM_MEASUREMENT_BUCKETS:
        raise ValueError(
            f"Measurement duration must provide at least {MINIMUM_MEASUREMENT_BUCKETS} "
            "one-second buckets for a report containing P99"
        )
    configuration_path = configuration_path.resolve()
    configuration = _configuration_with_verified_fixture(configuration_path)
    if not targets:
        raise ValueError("At least one explicit role=PID process is required")

    initial = {target.role: read_process_snapshot(target.pid) for target in targets}
    if warmup_seconds:
        time.sleep(warmup_seconds)
        for target in targets:
            warmed = read_process_snapshot(target.pid)
            if warmed.executable != initial[target.role].executable:
                raise RuntimeError(f"Process role {target.role} changed during warmup")
            initial[target.role] = warmed
    buckets = collect_buckets(targets, bucket_count=duration_seconds)
    report = build_report(
        configuration=configuration,
        configuration_sha256=_sha256(configuration_path),
        targets=targets,
        initial_snapshots=initial,
        buckets=buckets,
        warmup_seconds=warmup_seconds,
        source=_git_metadata(),
        hardware=hardware_metadata(),
    )
    _write_json(output, report)
    return report


def _print_fixture(manifest: dict[str, Any], manifest_path: Path) -> None:
    asset = manifest["asset"]
    print(f"Fixture: {asset['path']}")
    print(f"SHA-256: {asset['sha256']}")
    print(f"Manifest: {manifest_path.resolve()}")


def _print_report(report: dict[str, Any], output: Path) -> None:
    aggregate = report["aggregate_sampled_processes"]["cpu_host_percent"]
    print(
        "Sampled-process host CPU P50/P95/P99: "
        f"{aggregate['p50']:.4f}% / {aggregate['p95']:.4f}% / {aggregate['p99']:.4f}%"
    )
    for role, process in report["processes"].items():
        cpu = process["cpu_host_percent"]
        print(
            f"{role} ({process['pid']} {process['executable']}): "
            f"{cpu['p50']:.4f}% / {cpu['p95']:.4f}% / {cpu['p99']:.4f}%"
        )
    print(f"Report: {output.resolve()}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and measure an auditable native-media-route scenario."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fixture = subparsers.add_parser("fixture", help="generate the pinned media fixture")
    fixture.add_argument("--gstreamer-root", type=Path, default=DEFAULT_GSTREAMER_ROOT)
    fixture.add_argument("--output", type=Path, required=True)
    fixture.add_argument("--manifest", type=Path, required=True)
    fixture.add_argument("--width", type=int, default=1280)
    fixture.add_argument("--height", type=int, default=720)
    fixture.add_argument("--fps", type=int, default=30)
    fixture.add_argument("--duration-seconds", type=int, default=180)
    fixture.add_argument("--bitrate", type=int, default=4_000_000)

    configure = subparsers.add_parser(
        "configure", help="write the exact current-route configuration"
    )
    configure.add_argument("--fixture-manifest", type=Path, required=True)
    configure.add_argument("--output", type=Path, required=True)
    configure.add_argument("--native-presenters", type=int, required=True)
    configure.add_argument(
        "--editor-preview", choices=("open", "closed"), required=True
    )
    configure.add_argument("--virtual-camera", choices=("on", "off"), required=True)
    configure.add_argument("--consumer-application")
    configure.add_argument("--consumer-architecture", choices=("x86", "x64"))
    configure.add_argument("--consumer-profile")
    configure.add_argument(
        "--decode-path",
        choices=("automatic", "hardware", "software"),
        default="automatic",
    )

    recorder = subparsers.add_parser("record", help="sample explicit process IDs")
    recorder.add_argument("--configuration", type=Path, required=True)
    recorder.add_argument("--output", type=Path, required=True)
    recorder.add_argument(
        "--process",
        type=_parse_process_target,
        action="append",
        required=True,
        help="repeat role=PID for Solin, sidecar, and the camera consumer",
    )
    recorder.add_argument("--warmup-seconds", type=int, default=10)
    recorder.add_argument("--duration-seconds", type=int, default=60)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(arguments)
    if args.command == "fixture":
        manifest = generate_fixture(
            gstreamer_root=args.gstreamer_root,
            output=args.output,
            manifest_path=args.manifest,
            width=args.width,
            height=args.height,
            frames_per_second=args.fps,
            duration_seconds=args.duration_seconds,
            bitrate=args.bitrate,
        )
        _print_fixture(manifest, args.manifest)
        return 0
    if args.command == "configure":
        configuration = write_configuration(
            fixture_manifest_path=args.fixture_manifest,
            output=args.output,
            native_presenters=args.native_presenters,
            editor_preview=args.editor_preview == "open",
            virtual_camera=args.virtual_camera == "on",
            consumer_application=args.consumer_application,
            consumer_architecture=args.consumer_architecture,
            consumer_profile=args.consumer_profile,
            decode_path=args.decode_path,
        )
        print(json.dumps(configuration, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    report = record(
        configuration_path=args.configuration,
        output=args.output,
        targets=args.process,
        warmup_seconds=args.warmup_seconds,
        duration_seconds=args.duration_seconds,
    )
    _print_report(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
