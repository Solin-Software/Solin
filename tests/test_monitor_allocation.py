"""Tests for the shared monitor allocation arbiter."""

from __future__ import annotations

from app.core.ui.monitor_allocation import (
    MonitorAllocationStore,
    OWNER_MEDIA,
    OWNER_OFF,
    OWNER_TIMER,
    ScreenIdentity,
)
from app.core.profiles import settings as _ps


class _Geo:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def x(self): return self._x
    def y(self): return self._y
    def width(self): return self._w
    def height(self): return self._h


class _ScreenStub:
    """Mimics the bits of QScreen that ScreenIdentity reads."""

    def __init__(self, name, manufacturer="", model="", serial="", geo=(0, 0, 1920, 1080)):
        self._name = name
        self._manufacturer = manufacturer
        self._model = model
        self._serial = serial
        self._geo = _Geo(*geo)

    def name(self): return self._name
    def manufacturer(self): return self._manufacturer
    def model(self): return self._model
    def serialNumber(self): return self._serial
    def geometry(self): return self._geo


def test_identity_prefers_stable_fields_over_name():
    a = _ScreenStub("\\\\.\\DISPLAY1", "Dell", "U2720Q", "ABC123")
    b = _ScreenStub("\\\\.\\DISPLAY9", "Dell", "U2720Q", "ABC123")
    # Same physical monitor reconnected under a different OS name → same key.
    assert ScreenIdentity.key(a) == ScreenIdentity.key(b)


def test_identity_falls_back_to_name_and_geometry():
    s = _ScreenStub("HDMI-1")
    key = ScreenIdentity.key(s)
    assert "HDMI-1" in key


def _with_temp_org(fn):
    original = _ps.current_org()
    _ps.set_org("SolinTest_monitor_alloc")
    try:
        fn()
    finally:
        _ps.prefs("Monitors").clear()
        _ps.set_org(original)


def test_default_owner_is_media():
    def body():
        store = MonitorAllocationStore()
        assert store.owner_of(_ScreenStub("DISPLAY1", serial="S1")) == OWNER_MEDIA
    _with_temp_org(body)


def test_set_and_persist_owner():
    def body():
        s = _ScreenStub("DISPLAY1", serial="S1")
        store = MonitorAllocationStore()
        store.set_owner(s, OWNER_TIMER)
        # A fresh store instance (no in-memory cache) reads the persisted value.
        assert MonitorAllocationStore().owner_of(s) == OWNER_TIMER
        store.set_owner(s, OWNER_OFF)
        assert MonitorAllocationStore().owner_of(s) == OWNER_OFF
    _with_temp_org(body)


def test_request_assignment_reports_media_timer_conflict():
    def body():
        s = _ScreenStub("DISPLAY1", serial="S1")
        store = MonitorAllocationStore()
        store.set_owner(s, OWNER_MEDIA)
        conflict = store.request_assignment(s, OWNER_TIMER)
        assert conflict is not None
        assert conflict.current_owner == OWNER_MEDIA
        assert conflict.requested_owner == OWNER_TIMER
        # Not applied until confirmed.
        assert store.owner_of(s) == OWNER_MEDIA
        store.confirm_assignment(s, OWNER_TIMER)
        assert store.owner_of(s) == OWNER_TIMER
    _with_temp_org(body)


def test_request_assignment_no_conflict_for_off():
    def body():
        s = _ScreenStub("DISPLAY1", serial="S1")
        store = MonitorAllocationStore()
        store.set_owner(s, OWNER_OFF)
        # off → timer is not a contested handoff; applies immediately.
        assert store.request_assignment(s, OWNER_TIMER) is None
        assert store.owner_of(s) == OWNER_TIMER
    _with_temp_org(body)


def test_timer_and_off_queries():
    def body():
        a = _ScreenStub("A", serial="SA")
        b = _ScreenStub("B", serial="SB")
        c = _ScreenStub("C", serial="SC")
        store = MonitorAllocationStore()
        store.set_owner(a, OWNER_TIMER)
        store.set_owner(b, OWNER_OFF)
        live = [a, b, c]
        assert store.timer_screens(live) == [a]
        assert store.media_off_names(live) == {"B"}
    _with_temp_org(body)
