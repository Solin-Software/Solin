from __future__ import annotations

import io
import json

from solin.bootstrap.startup_timeline import StartupTimeline


def test_timeline_records_unique_ordered_marks_and_emits_json_once() -> None:
    timeline = StartupTimeline(started_ns=1_000_000, enabled=True)
    output = io.StringIO()

    timeline.mark("entrypoint", observed_ns=2_000_000)
    timeline.mark("entrypoint", observed_ns=3_000_000)
    timeline.mark("first_paint", observed_ns=4_500_000)

    assert timeline.emit_json(output) is True
    assert timeline.emit_json(output) is False
    assert json.loads(output.getvalue()) == {
        "schema": "solin.startup.v1",
        "phases": [
            {"name": "entrypoint", "elapsed_ms": 1.0},
            {"name": "first_paint", "elapsed_ms": 3.5},
        ],
    }


def test_disabled_timeline_has_negligible_empty_output() -> None:
    timeline = StartupTimeline(started_ns=0, enabled=False)

    assert timeline.mark("ignored", observed_ns=1_000_000) is None
    assert timeline.snapshot() == {"schema": "solin.startup.v1", "phases": []}
    assert timeline.emit_json(io.StringIO()) is False
