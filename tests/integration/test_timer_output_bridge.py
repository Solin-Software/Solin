from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from solin.core.timer.models import ClockConfig, Direction, TimerSnapshot
from solin.ui.qml.timer_output import ClockRenderBridge


class _Engine(QObject):
    tick = Signal(object)

    def __init__(self, snapshot: TimerSnapshot) -> None:
        super().__init__()
        self.current_snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self) -> TimerSnapshot:
        self.snapshot_calls += 1
        return self.current_snapshot


def _snapshot(epoch: float = 1_700_000_000.0) -> TimerSnapshot:
    return TimerSnapshot(
        active=False,
        wall_clock_epoch=epoch,
        direction=Direction.DOWN,
    )


def test_clock_render_bridge_uses_engine_snapshot_and_deduplicates_updates():
    engine = _Engine(_snapshot())
    bridge = ClockRenderBridge(engine, ClockConfig)
    emissions: list[None] = []
    bridge.modelChanged.connect(lambda: emissions.append(None))

    initial_model = bridge.property("model")
    bridge.refresh_now()

    assert engine.snapshot_calls == 2
    assert bridge.property("model") == initial_model
    assert emissions == []


def test_clock_render_bridge_accepts_tick_payload_and_refreshes_config():
    engine = _Engine(_snapshot())
    config = ClockConfig(show_seconds=True)
    bridge = ClockRenderBridge(engine, lambda: config)
    emissions: list[None] = []
    bridge.modelChanged.connect(lambda: emissions.append(None))

    changed_snapshot = _snapshot(epoch=1_700_000_001.0)
    engine.tick.emit(changed_snapshot.to_dict())

    assert engine.snapshot_calls == 1
    assert len(emissions) == 1

    config.show_seconds = False
    bridge.refresh_now()

    assert engine.snapshot_calls == 2
    assert len(emissions) == 2
    assert bridge.property("model")["show_seconds"] is False
