from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, QPointF, Property, Signal
from PySide6.QtQuickWidgets import QQuickWidget

from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.timer_icons import TimerIconProvider
from tests._qt import dispose_widget, wait_until


class _ClockProbe(QObject):
    model_changed = Signal()
    alert_changed = Signal()
    motion_changed = Signal()

    def __init__(self):
        super().__init__()
        self.face = {"display_mode": "clock", "clock": {"primary_text": "09:26"}}
        self.alert: dict = {}
        self.reduce_motion = False

    model = Property("QVariantMap", lambda self: self.face, notify=model_changed)
    handAlert = Property("QVariantMap", lambda self: self.alert, notify=alert_changed)
    reduceMotion = Property(bool, lambda self: self.reduce_motion, notify=motion_changed)

    def show_alert(self, **payload):
        self.alert = payload
        self.alert_changed.emit()

    def show_face(self, **payload):
        self.face = payload
        self.model_changed.emit()

    def suppress_motion(self):
        self.reduce_motion = True
        self.motion_changed.emit()


@pytest.fixture
def projection():
    clock = _ClockProbe()
    widget = QQuickWidget()
    try:
        widget.resize(960, 540)
        configure_qml_host(
            widget,
            type_name="ClockFace",
            clear_color="#000000",
            context_properties={"clock": clock, "timerDigitFontFamily": "Arial"},
            image_providers={"timericons": TimerIconProvider()},
        )
        assert widget.errors() == []
        root = widget.rootObject()
        assert root is not None
        widget.show()
        yield clock, widget, root
    finally:
        dispose_widget(widget)


def _visual_items(item):
    if item.isVisible():
        yield item
        for child in item.childItems():
            yield from _visual_items(child)


def _texts(item):
    return [text for child in _visual_items(item) if (text := child.property("text"))]


def _named_item(item, name):
    return next((child for child in _visual_items(item) if child.objectName() == name), None)


def test_clock_preview_with_only_a_render_model():
    widget = QQuickWidget()
    try:
        widget.resize(480, 270)
        configure_qml_host(
            widget,
            type_name="ClockFace",
            clear_color="#000000",
            context_properties={
                "clock": {"model": {"display_mode": "clock", "clock": {"primary_text": "09:26"}}},
            },
        )
        assert widget.errors() == []
        root = widget.rootObject()
        assert root is not None
        widget.show()
        assert "09:26" in _texts(root)
        assert _named_item(root, "clockHandAlertCard") is None
        assert _named_item(root, "clockHandAlertManual") is None
    finally:
        dispose_widget(widget)


def test_named_alert_keeps_queue_order_and_clock_readout(projection):
    clock, widget, root = projection
    names = ["<b>Ana</b>", "Bruno", "Carla", "Daniel", "Elisa", "Felipe", "Gabriel", "Helena"]
    assert _named_item(root, "clockHandAlertCard") is None
    face_text = next(child for child in _visual_items(root) if child.property("text") == "09:26")
    face_size = face_text.size()

    clock.show_alert(kind="automatic", count=12, names=names)
    card = _named_item(root, "clockHandAlertCard")
    assert card is not None
    layer = card.parentItem().parentItem()
    assert layer.opacity() < 1
    wait_until(lambda: layer.opacity() == 1, description="named alert fade-in")
    assert face_text.isVisible()
    assert face_text.size() == face_size
    assert [name for name in _texts(card) if name in names] == names[:7]
    assert "12" in _texts(card)
    assert "+5" in _texts(card)
    assert "09:26" in _texts(card)

    clock.show_face(
        display_mode="clock_timer",
        clock={"mode": "analog", "hour_angle": 311, "minute_angle": 126},
        timer={"primary_text": "1:24", "seconds_text": "56"},
    )
    assert {"1:24", "56"} <= set(_texts(card))
    clock.show_face(
        display_mode="clock",
        clock={
            "mode": "analog",
            "hour_angle": 310.5,
            "minute_angle": 126,
            "compact_readout": {"primary_text": "22:21", "seconds_text": "07"},
        },
    )
    assert {"22:21", "07"} <= set(_texts(card))
    clock.show_face(
        display_mode="clock",
        clock={"primary_text": "10:21", "seconds_text": "08", "secondary_text": "PM"},
    )
    assert {"10:21", "08", "PM"} <= set(_texts(card))

    widget.resize(640, 180)
    wait_until(lambda: root.height() == 180, description="short projection viewport")
    assert 0 <= card.y() <= card.y() + card.height() <= root.height()
    shown_names = [name for name in _texts(card) if name in names]
    assert shown_names == names[: len(shown_names)]
    assert 0 < len(shown_names) < 5
    assert f"+{12 - len(shown_names)}" in _texts(card)

    clock.show_alert(kind="automatic", count=1, names=["João"])
    assert "João" in _texts(card)
    assert not any(text.startswith("+") for text in _texts(card))

    clock.show_alert()
    assert _named_item(root, "clockHandAlertCard") is card
    assert "João" in _texts(card)
    wait_until(
        lambda: _named_item(root, "clockHandAlertCard") is None, description="named alert fade-out"
    )


def test_manual_alert_only_pulses_the_hand_and_respects_reduced_motion(projection):
    clock, _, root = projection
    clock.show_alert(kind="manual")
    hand = _named_item(root, "clockHandAlertManual")
    assert hand is not None
    assert _named_item(root, "clockHandAlertCard") is None
    assert not _texts(hand)
    assert 0 < hand.opacity() < 1
    assert hand.width() > root.height() * 0.38
    center = hand.mapToItem(root, QPointF(hand.width() / 2, hand.height() / 2))
    assert center.x() == pytest.approx(root.width() / 2, abs=1)
    assert center.y() == pytest.approx(root.height() / 2, abs=1)
    wait_until(lambda: hand.scale() > 1.01, description="visible hand pulse")

    clock.suppress_motion()
    wait_until(lambda: hand.scale() == 1.0, description="stationary hand")

    clock.show_alert(kind="automatic", count=1, names=["Ana"])
    assert _named_item(root, "clockHandAlertManual") is None
    clock.show_alert(kind="manual")
    hand = _named_item(root, "clockHandAlertManual")
    assert hand is not None
    assert hand.scale() == 1.0
