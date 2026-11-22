import pytest
from PySide6.QtCore import QSettings

from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.core.foundation.settings_store import SettingsStore
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.integrations.automation.zoom.state import (
    AudioState,
    RaisedHand,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
)
from solin.core.timer.hand_alert import TimerHandAlert


@pytest.fixture
def zoom_settings(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs.ini")
    monkeypatch.setattr(SettingsStore, "_settings", lambda _: QSettings(path, QSettings.Format.IniFormat))
    return ZoomSettingsStore(SettingsStore.for_namespace("test", "prefs"))


def test_manual_requires_integration_and_automatic_names_take_priority(zoom_settings):
    controller = TimerHandAlertController(zoom_settings)
    published = []
    controller.alert_changed.connect(published.append)
    controller.set_outputs(available=True)
    controller.set_manual(True)
    assert not controller.manual
    assert not published
    controller.set_integrated(True)
    controller.set_manual(True)
    ready = RaisedHandsSnapshot(
        RaisedHandsStatus.READY,
        (RaisedHand("Bia", AudioState.MUTED, None), RaisedHand("Ana", AudioState.MUTED, None)),
        chronological=True,
    )
    assert published == [TimerHandAlert(manual=True)]
    controller.set_zoom_snapshot(ready)
    controller.set_manual(False)
    controller.set_manual(True)
    controller.set_zoom_snapshot(ready)
    assert published == [TimerHandAlert(manual=True), TimerHandAlert(names=("Bia", "Ana"))]

    controller.set_zoom_snapshot(RaisedHandsSnapshot(RaisedHandsStatus.PARTIAL, ready.hands))
    assert published[-1] == TimerHandAlert(manual=True)
    controller.set_zoom_snapshot(ready)
    controller.set_integrated(False)
    assert not controller.manual
    assert published[-1] == TimerHandAlert()
    controller.set_integrated(True)
    assert controller.alert.names == ("Bia", "Ana")
    controller.set_zoom_snapshot(RaisedHandsSnapshot(RaisedHandsStatus.INACTIVE))
    assert controller.alert == TimerHandAlert()


def test_losing_every_output_clears_manual_without_republishing_an_old_meeting(zoom_settings):
    controller = TimerHandAlertController(zoom_settings)
    controller.set_outputs(available=True)
    controller.set_integrated(True)
    controller.set_manual(True)
    controller.set_zoom_snapshot(
        RaisedHandsSnapshot(
            RaisedHandsStatus.READY,
            (RaisedHand("Ana", AudioState.MUTED, None),),
        )
    )
    assert controller.alert.names == ("Ana",)

    controller.set_outputs(available=False)
    controller.set_zoom_snapshot(RaisedHandsSnapshot(RaisedHandsStatus.NO_MEETING))
    controller.set_manual(True)
    assert not controller.manual
    assert controller.alert == TimerHandAlert()

    controller.set_outputs(available=True)
    assert controller.integrated
    assert controller.alert == TimerHandAlert()


def test_timer_integration_is_restored_without_the_manual_hand(zoom_settings):
    controller = TimerHandAlertController(zoom_settings)
    assert not controller.integrated
    controller.set_outputs(available=True)
    controller.set_integrated(True)
    controller.set_manual(True)

    restored = TimerHandAlertController(zoom_settings)
    assert restored.integrated
    assert not restored.manual
    restored.set_zoom_snapshot(
        RaisedHandsSnapshot(
            RaisedHandsStatus.READY, (RaisedHand("Ana", AudioState.MUTED, None),)
        )
    )
    assert restored.alert == TimerHandAlert()
    restored.set_outputs(available=True)
    assert restored.alert.names == ("Ana",)
    restored.set_integrated(False)
    assert restored.alert == TimerHandAlert()
    assert not TimerHandAlertController(zoom_settings).integrated


def test_demo_is_dev_only_and_restores_the_latest_real_snapshot(monkeypatch, zoom_settings):
    module = "solin.controllers.timer_hand_alert_controller"
    monkeypatch.setattr(f"{module}.IS_DEV", False)
    controller = TimerHandAlertController(zoom_settings)
    controller.set_outputs(available=True)
    controller.set_demo(True)
    assert controller.demo_snapshot is None
    assert controller.alert == TimerHandAlert()

    monkeypatch.setattr(f"{module}.IS_DEV", True)
    controller.set_demo(True)
    assert len(controller.alert.names) == 15

    controller.set_integrated(True)
    controller.set_zoom_snapshot(
        RaisedHandsSnapshot(
            RaisedHandsStatus.READY, (RaisedHand("Real participant", AudioState.MUTED, None),)
        )
    )
    assert len(controller.alert.names) == 15
    controller.set_demo(False)
    assert controller.alert.names == ("Real participant",)
