// TimerSlider.qml — a lightweight, controlled horizontal slider.
// Emits `moved(value)` while dragging or clicking; `value` is driven by a
// binding so the model remains authoritative.
import QtQuick 2.15

Item {
    id: ctl
    property real from: 0
    property real to: 100
    property real value: 50
    property real step: 1
    property color accent: appTheme.accent
    signal moved(real value)

    implicitHeight: 22
    readonly property real _frac: Math.max(0, Math.min(1, (value - from) / Math.max(0.0001, to - from)))

    Rectangle {
        id: track
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width
        height: 5
        radius: 2.5
        color: appTheme.borderStrong

        Rectangle {
            width: handle.x + handle.width / 2
            height: parent.height
            radius: 2.5
            color: ctl.accent
        }
    }

    Rectangle {
        id: handle
        width: 15
        height: 15
        radius: 8
        anchors.verticalCenter: parent.verticalCenter
        x: ctl._frac * (ctl.width - width)
        color: appTheme.white
        border.color: ctl.accent
        border.width: 2
        scale: ma.pressed ? 1.18 : 1.0
        Behavior on scale { NumberAnimation { duration: 90 } }
    }

    TimerPointerArea {
        id: ma
        anchors.fill: parent
        function apply(mx) {
            var f = Math.max(0, Math.min(1, (mx - handle.width / 2) / Math.max(1, ctl.width - handle.width)))
            var v = ctl.from + f * (ctl.to - ctl.from)
            v = Math.round(v / ctl.step) * ctl.step
            ctl.moved(v)
        }
        onPressed: function(mouse) { apply(mouse.x) }
        onPositionChanged: function(mouse) { if (pressed) apply(mouse.x) }
    }
}
