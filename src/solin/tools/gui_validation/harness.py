"""Core types, capability detection, and the per-check subprocess runner.

This module is import-safe on any OS and never touches libobs at import time, so
the parent process (and the unit tests) can enumerate checks, gate them by
capability, and render results without a GPU. The actual libobs work happens in
:mod:`solin.tools.gui_validation.checks`, executed inside a child process.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"          # a prerequisite (device/tool/asset) was absent
    MANUAL = "manual"      # set up on screen; needs a human to confirm
    ERROR = "error"        # the check itself crashed or timed out

    @property
    def is_problem(self) -> bool:
        return self in (CheckStatus.FAIL, CheckStatus.ERROR)


# Capability tags a check may require; a check is SKIPped when any is unmet.
CAP_DISPLAY = "display"            # a real (or Xvfb) X/Wayland display
CAP_FFPROBE = "ffprobe"           # ffprobe on PATH (recording verification)
CAP_FFMPEG = "ffmpeg"             # ffmpeg on PATH (test-asset generation)
CAP_V4L2LOOPBACK = "v4l2loopback"  # a Linux v4l2loopback sink device


@dataclass(frozen=True)
class CheckResult:
    name: str
    category: str
    status: CheckStatus
    summary: str = ""
    details: dict = field(default_factory=dict)
    duration_s: float = 0.0
    artifact: str | None = None  # path to a captured PNG, if any

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        return cls(
            name=data["name"],
            category=data.get("category", ""),
            status=CheckStatus(data["status"]),
            summary=data.get("summary", ""),
            details=data.get("details", {}) or {},
            duration_s=float(data.get("duration_s", 0.0)),
            artifact=data.get("artifact"),
        )


# A check receives a HarnessContext and returns a CheckResult; it may also raise,
# which the child turns into an ERROR result.
CheckFn = Callable[["object"], CheckResult]


@dataclass(frozen=True)
class CheckSpec:
    name: str
    category: str
    description: str
    requires: tuple[str, ...]
    fn: CheckFn
    timeout_s: float = 60.0


_REGISTRY: "dict[str, CheckSpec]" = {}


def register_check(
    name: str,
    *,
    category: str,
    description: str,
    requires: tuple[str, ...] = (),
    timeout_s: float = 60.0,
) -> Callable[[CheckFn], CheckFn]:
    """Decorator: register a check function under ``name``."""

    def decorate(fn: CheckFn) -> CheckFn:
        if name in _REGISTRY:
            raise ValueError(f"duplicate check name: {name}")
        _REGISTRY[name] = CheckSpec(
            name=name, category=category, description=description,
            requires=tuple(requires), fn=fn, timeout_s=timeout_s,
        )
        return fn

    return decorate


def all_checks() -> list[CheckSpec]:
    return list(_REGISTRY.values())


def get_check(name: str) -> CheckSpec:
    return _REGISTRY[name]


# ── configuration ────────────────────────────────────────────────────────────


@dataclass
class HarnessConfig:
    width: int = 1280
    height: int = 720
    fps: int = 30
    clip_path: str = ""          # a playable media file; generated if empty + ffmpeg
    images_dir: str = ""         # scene image assets; a temp one is used if empty
    out_dir: str = ""            # where assets/artifacts land; a temp dir if empty
    hold_seconds: float = 0.0    # extra dwell so an operator can watch visual checks
    interactive: bool = False    # prompt for manual confirmation on visual checks

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HarnessConfig":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def resolved_out_dir(self) -> Path:
        if self.out_dir:
            path = Path(self.out_dir)
        else:
            path = Path(os.environ.get("TMPDIR", "/tmp")) / "solin-gui-validation"
        path.mkdir(parents=True, exist_ok=True)
        return path


# ── capability detection (parent side, cheap) ────────────────────────────────


def detect_capabilities(config: HarnessConfig | None = None) -> set[str]:
    """The capability tags satisfied on this host, for skip gating."""
    caps: set[str] = set()
    if _has_display():
        caps.add(CAP_DISPLAY)
    if shutil.which("ffprobe"):
        caps.add(CAP_FFPROBE)
    if shutil.which("ffmpeg"):
        caps.add(CAP_FFMPEG)
    if loopback_devices():
        caps.add(CAP_V4L2LOOPBACK)
    return caps


def _has_display() -> bool:
    if sys.platform == "win32" or sys.platform == "darwin":
        return True  # a native window server is always present
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def loopback_devices() -> list[str]:
    """Linux v4l2loopback sink device paths (``/dev/videoN``), newest-first."""
    if sys.platform != "linux":
        return []
    found: list[str] = []
    base = Path("/sys/class/video4linux")
    if not base.exists():
        return []
    for node in sorted(base.glob("video*")):
        dev = Path("/dev") / node.name
        if not dev.exists() or not os.access(dev, os.W_OK):
            continue
        try:
            name = (node / "name").read_text(errors="replace").strip().lower()
        except OSError:
            name = ""
        # v4l2loopback devices are virtual (not on a physical bus) and can carry
        # any card_label (e.g. "Solin Virtual Camera"), so match the virtual sysfs
        # path — not just the word "loopback" in the name.
        is_virtual = "/devices/virtual/" in os.path.realpath(str(node))
        if is_virtual or "loopback" in name:
            found.append(str(dev))
    return found


# ── running (parent orchestration) ───────────────────────────────────────────

# A runner executes one spec against a config and returns its result. The default
# spawns a subprocess; tests inject a fake to exercise gating/aggregation.
CheckRunner = Callable[[CheckSpec, HarnessConfig], CheckResult]


def run_selected(
    config: HarnessConfig,
    selected: list[CheckSpec],
    *,
    capabilities: set[str] | None = None,
    runner: CheckRunner | None = None,
) -> list[CheckResult]:
    """Gate each selected check by capability, then run the rest via ``runner``."""
    caps = detect_capabilities(config) if capabilities is None else capabilities
    run = runner or subprocess_check_runner
    results: list[CheckResult] = []
    for spec in selected:
        missing = [cap for cap in spec.requires if cap not in caps]
        if missing:
            results.append(CheckResult(
                name=spec.name, category=spec.category, status=CheckStatus.SKIP,
                summary=f"missing: {', '.join(missing)}",
                details={"missing_capabilities": missing},
            ))
            continue
        results.append(run(spec, config))
    return results


def subprocess_check_runner(spec: CheckSpec, config: HarnessConfig) -> CheckResult:
    """Run one check in a fresh child process and read back its result file.

    The child boots its own libobs runtime and writes the result JSON to a file
    (never stdout — libobs writes to stdout, so stdout/stderr are diagnostics
    only). A crash, non-zero exit, or timeout becomes an ERROR result carrying the
    tail of the child's output.
    """
    out_dir = config.resolved_out_dir()
    result_path = out_dir / f"result-{spec.name}.json"
    config_path = out_dir / f"config-{spec.name}.json"
    if result_path.exists():
        result_path.unlink()
    config_path.write_text(json.dumps(config.to_dict()))

    cmd = [
        sys.executable, "-m", "solin.tools.gui_validation",
        "--run-check", spec.name,
        "--config-file", str(config_path),
        "--result-file", str(result_path),
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=spec.timeout_s + 15.0,
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            name=spec.name, category=spec.category, status=CheckStatus.ERROR,
            summary=f"timed out after {spec.timeout_s:.0f}s",
            details={"stderr_tail": _tail(exc.stderr)},
            duration_s=time.monotonic() - started,
        )
    duration = time.monotonic() - started
    if result_path.exists():
        try:
            result = CheckResult.from_dict(json.loads(result_path.read_text()))
            # Trust the child's own timing but backfill if it did not set one.
            if not result.duration_s:
                result = dataclasses.replace(result, duration_s=duration)
            return result
        except (OSError, ValueError, KeyError):
            pass  # fall through to the crash path
    return CheckResult(
        name=spec.name, category=spec.category, status=CheckStatus.ERROR,
        summary=f"no result (exit {completed.returncode})",
        details={"stdout_tail": _tail(completed.stdout), "stderr_tail": _tail(completed.stderr)},
        duration_s=duration,
    )


def execute_check(spec: CheckSpec, config: HarnessConfig, result_path: Path) -> CheckResult:
    """Child-side: run one check against a live context and persist the result."""
    from solin.tools.gui_validation.context import HarnessContext

    started = time.monotonic()
    context = HarnessContext(config)
    context.spec = spec  # so ctx.result() can label itself
    try:
        result = spec.fn(context)
    except Exception as exc:  # noqa: BLE001 - a check crash is a reportable ERROR
        import traceback
        result = CheckResult(
            name=spec.name, category=spec.category, status=CheckStatus.ERROR,
            summary=f"{type(exc).__name__}: {exc}",
            details={"traceback": _tail(traceback.format_exc(), 4000)},
        )
    finally:
        context.close()
    if not result.duration_s:
        result = dataclasses.replace(result, duration_s=time.monotonic() - started)
    result_path.write_text(json.dumps(result.to_dict()))
    return result


def _tail(text: str | None, limit: int = 2000) -> str:
    if not text:
        return ""
    return text[-limit:]
