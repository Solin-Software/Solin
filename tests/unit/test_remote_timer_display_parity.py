from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urljoin
from urllib.request import pathname2url

import pytest

from solin.core.remote_control.timer.contracts import (
    TimerClockWireConfig,
    TimerLiveWireSnapshot,
)
from solin.core.timer.models import (
    AnalogClockStyle,
    ClockConfig,
    ClockMode,
    Direction,
    PartState,
    PartTimerDisplay,
    Section,
    TimerSnapshot,
)
from solin.core.timer.render import build_render_model


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMER_DISPLAY_JS = (
    PROJECT_ROOT
    / "src"
    / "solin"
    / "resources"
    / "remote_control"
    / "timer"
    / "scripts"
    / "timer-display.js"
)


def _file_url(path: Path) -> str:
    return urljoin("file:", pathname2url(str(path.resolve())))


def _local_utc_offset_minutes(epoch_seconds: float) -> int:
    offset = datetime.fromtimestamp(epoch_seconds).astimezone().utcoffset()
    return int(round(offset.total_seconds() / 60.0)) if offset is not None else 0


def _assert_models_match(js_model: dict, py_model: dict, label: str) -> None:
    assert set(js_model) == set(py_model), label
    for key in (
        "active",
        "display_mode",
        "mode",
        "analog_style",
        "primary_text",
        "seconds_text",
        "secondary_text",
        "overrun",
        "show_seconds",
        "text_scale_pct",
    ):
        assert js_model[key] == py_model[key], f"{label}:{key}"

    for angle_key in ("hour_angle", "minute_angle", "second_angle"):
        assert js_model[angle_key] == pytest.approx(py_model[angle_key], abs=1e-6), (
            f"{label}:{angle_key}"
        )

    for face_key in ("clock", "timer"):
        js_face = js_model[face_key]
        py_face = py_model[face_key]
        for field in (
            "mode",
            "analog_style",
            "primary_text",
            "seconds_text",
            "secondary_text",
            "overrun",
        ):
            assert js_face[field] == py_face[field], f"{label}:{face_key}.{field}"
        for angle_key in ("hour_angle", "minute_angle", "second_angle"):
            assert js_face[angle_key] == pytest.approx(py_face[angle_key], abs=1e-6), (
                f"{label}:{face_key}.{angle_key}"
            )
        js_sector = js_face["duration_sector"]
        py_sector = py_face["duration_sector"]
        assert js_sector["visible"] == py_sector["visible"], f"{label}:{face_key}.sector.visible"
        if py_sector["visible"]:
            assert js_sector["show_remaining"] == py_sector["show_remaining"]
            assert js_sector["show_overrun"] == py_sector["show_overrun"]
            assert js_sector["end_angle"] == pytest.approx(py_sector["end_angle"], abs=1e-6)
            assert js_sector["current_angle"] == pytest.approx(
                py_sector["current_angle"], abs=1e-6
            )


def test_js_timer_display_render_model_matches_python_build_render_model() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to verify JS/Python timer display parity")

    scenarios: list[tuple[str, TimerSnapshot, ClockConfig]] = [
        (
            "idle-digital-24h",
            TimerSnapshot(
                active=False,
                wall_clock_epoch=1_721_157_945.0,
                direction=Direction.DOWN,
            ),
            ClockConfig(mode=ClockMode.DIGITAL, hour_format_24h=True, show_seconds=True),
        ),
        (
            "idle-digital-12h-ampm-no-seconds",
            TimerSnapshot(
                active=False,
                wall_clock_epoch=1_721_172_305.0,
                direction=Direction.DOWN,
            ),
            ClockConfig(
                mode=ClockMode.DIGITAL,
                hour_format_24h=False,
                show_ampm=True,
                show_seconds=False,
                text_scale_pct=85,
            ),
        ),
        (
            "idle-analog-signature",
            TimerSnapshot(
                active=False,
                wall_clock_epoch=1_721_130_127.7,
                direction=Direction.DOWN,
            ),
            ClockConfig(
                mode=ClockMode.ANALOG,
                analog_style=AnalogClockStyle.SIGNATURE,
            ),
        ),
        (
            "idle-analog-digital-classic",
            TimerSnapshot(
                active=False,
                wall_clock_epoch=1_721_130_127.7,
                direction=Direction.DOWN,
            ),
            ClockConfig(
                mode=ClockMode.ANALOG_DIGITAL,
                analog_style=AnalogClockStyle.CLASSIC,
                hour_format_24h=False,
                show_ampm=True,
                show_seconds=True,
            ),
        ),
        (
            "running-countdown-under-hour",
            TimerSnapshot(
                active=True,
                wall_clock_epoch=1_721_130_200.0,
                direction=Direction.DOWN,
                active_part_id="part-1",
                active_part_title="Treasures",
                active_section=Section.TREASURES,
                planned_seconds=600,
                elapsed_seconds=125.0,
                remaining_seconds=475.0,
                overrun=False,
                state=PartState.RUNNING,
            ),
            ClockConfig(
                mode=ClockMode.DIGITAL,
                direction=Direction.DOWN,
                part_timer_display=PartTimerDisplay.TIMER,
            ),
        ),
        (
            "running-countdown-over-hour",
            TimerSnapshot(
                active=True,
                wall_clock_epoch=1_721_130_200.0,
                direction=Direction.DOWN,
                active_part_id="part-long",
                active_part_title="Watchtower",
                active_section=Section.WATCHTOWER,
                planned_seconds=3900,
                elapsed_seconds=65.0,
                remaining_seconds=3835.0,
                overrun=False,
                state=PartState.RUNNING,
            ),
            ClockConfig(
                mode=ClockMode.DIGITAL,
                direction=Direction.DOWN,
                part_timer_display=PartTimerDisplay.TIMER,
            ),
        ),
        (
            "running-overrun-with-analog-clock-timer-sector",
            TimerSnapshot(
                active=True,
                wall_clock_epoch=1_721_130_900.0,
                direction=Direction.DOWN,
                active_part_id="part-2",
                active_part_title="Spiritual Gems",
                active_section=Section.TREASURES,
                planned_seconds=600,
                elapsed_seconds=642.0,
                remaining_seconds=-42.0,
                overrun=True,
                state=PartState.RUNNING,
            ),
            ClockConfig(
                mode=ClockMode.ANALOG,
                analog_style=AnalogClockStyle.SIGNATURE,
                direction=Direction.DOWN,
                part_timer_display=PartTimerDisplay.CLOCK_TIMER,
            ),
        ),
        (
            "running-count-up-clock-only-with-remaining-sector",
            TimerSnapshot(
                active=True,
                wall_clock_epoch=1_721_131_200.0,
                direction=Direction.UP,
                active_part_id="part-3",
                active_part_title="Bible Reading",
                active_section=Section.TREASURES,
                planned_seconds=240,
                elapsed_seconds=90.0,
                remaining_seconds=150.0,
                overrun=False,
                state=PartState.RUNNING,
            ),
            ClockConfig(
                mode=ClockMode.ANALOG_DIGITAL,
                analog_style=AnalogClockStyle.CLASSIC,
                direction=Direction.UP,
                part_timer_display=PartTimerDisplay.CLOCK,
            ),
        ),
        (
            "stopped-frozen-elapsed",
            TimerSnapshot(
                active=True,
                wall_clock_epoch=1_721_131_500.0,
                direction=Direction.DOWN,
                active_part_id="part-3",
                active_part_title="Bible Reading",
                active_section=Section.TREASURES,
                planned_seconds=240,
                elapsed_seconds=231.0,
                remaining_seconds=9.0,
                overrun=False,
                state=PartState.STOPPED,
            ),
            ClockConfig(
                mode=ClockMode.ANALOG,
                direction=Direction.DOWN,
                part_timer_display=PartTimerDisplay.CLOCK_TIMER,
            ),
        ),
    ]

    wire_inputs = []
    expected_models = []
    for label, snap, cfg in scenarios:
        py_model = build_render_model(snap, cfg)
        utc_offset = _local_utc_offset_minutes(snap.wall_clock_epoch)
        epoch_ms = int(round(snap.wall_clock_epoch * 1000.0))
        wire_cfg = TimerClockWireConfig.from_clock_config(cfg)
        if snap.active:
            wire_live = TimerLiveWireSnapshot(
                live_revision=1,
                server_epoch_ms=epoch_ms,
                utc_offset_minutes=utc_offset,
                active=True,
                state=snap.state,
                direction=snap.direction,
                active_part_id=snap.active_part_id,
                active_part_title=snap.active_part_title or "Part",
                active_section=snap.active_section,
                planned_seconds=snap.planned_seconds,
                elapsed_seconds=snap.elapsed_seconds,
                clock_config=wire_cfg,
            )
        else:
            wire_live = TimerLiveWireSnapshot.idle(
                server_epoch_ms=epoch_ms,
                utc_offset_minutes=utc_offset,
                clock_config=wire_cfg,
            )
        wire_inputs.append({"label": label, "live": wire_live.to_dict()})
        expected_models.append((label, py_model))

    module_url = _file_url(TIMER_DISPLAY_JS)
    script = f"""
    import {{ buildRenderModel, parseDurationInput }} from {json.dumps(module_url)};
    const inputs = {json.dumps(wire_inputs)};
    const models = inputs.map((entry) => ({{
      label: entry.label,
      model: buildRenderModel(entry.live, 1000, 1000),
    }}));
    const parsed = {{
      plainMinutes: parseDurationInput("5"),
      mmss: parseDurationInput("04:30"),
      hhmmss: parseDurationInput("1:05:15"),
      tooShort: parseDurationInput("00:45"),
      badSeconds: parseDurationInput("03:60"),
      empty: parseDurationInput("   "),
    }};
    process.stdout.write(JSON.stringify({{ models, parsed }}));
    """

    result = subprocess.run(  # noqa: S603 - fixed local Node.js parity harness
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)

    for (label, py_model), js_entry in zip(expected_models, output["models"], strict=True):
        assert js_entry["label"] == label
        _assert_models_match(js_entry["model"], py_model, label)

    assert output["parsed"] == {
        "plainMinutes": 300,
        "mmss": 270,
        "hhmmss": 3915,
        "tooShort": None,
        "badSeconds": None,
        "empty": None,
    }
