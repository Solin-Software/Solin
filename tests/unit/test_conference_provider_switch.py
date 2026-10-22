from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from solin.core.conference.provider import MeetingProvider, SequenceResult
from solin.core.conference.provider_switch import MeetingProviderSwitch


class _Provider(QObject):
    enabled_changed = Signal(bool)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    error = Signal(str)

    def __init__(self, enabled=False, connected=False, cancellable=False):
        super().__init__()
        self.is_available = True
        self.is_enabled = enabled
        self.is_connected = connected
        self.is_busy = False
        self.calls = []
        if cancellable:
            self.cancel = lambda: self.calls.append("cancel")

    def start(self):
        self.calls.append("start")

    def stop(self, *, wait=False, timeout=2.0):
        self.calls.append(("stop", wait))

    def request_pre_meeting(self):
        self.calls.append("pre")
        return True

    def request_post_meeting(self):
        self.calls.append("post")
        return False


def test_calls_and_signals_follow_the_active_provider():
    zoom, solin = _Provider(enabled=True, cancellable=True), _Provider(connected=True)
    switch = MeetingProviderSwitch({"zoom": zoom, "solin": solin}, "zoom")
    assert isinstance(switch, MeetingProvider)
    finished, connected, enabled = [], [], []
    switch.operation_finished.connect(finished.append)
    switch.connection_changed.connect(connected.append)
    switch.enabled_changed.connect(enabled.append)

    assert switch.request_pre_meeting() and zoom.calls == ["pre"]
    switch.cancel()
    zoom.operation_finished.emit(SequenceResult("pre_meeting"))
    assert finished == [SequenceResult("pre_meeting")]

    switch.set_active("solin")
    assert (enabled, connected) == ([False], [True])  # the new provider's state
    zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert len(finished) == 1  # the inactive provider is no longer forwarded
    assert not switch.request_post_meeting() and solin.calls == ["post"]
    switch.cancel()  # Solin has no cancel: a no-op
    switch.stop(wait=True)
    assert solin.calls[-1] == ("stop", True)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        MeetingProviderSwitch({"zoom": _Provider()}, "teams")
