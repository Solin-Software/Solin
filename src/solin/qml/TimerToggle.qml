// TimerToggle.qml — a controlled on/off switch with a sliding knob.
// Emits `toggled(value)`; it does not mutate `checked` itself, so the backing
// model stays the single source of truth (the binding updates the visual).
import QtQuick 2.15

Item {
    id: ctl
    property bool checked: false
    property color accent: appTheme.accent
    signal toggled(bool value)

    implicitWidth: 42
    implicitHeight: 24
    opacity: enabled ? 1.0 : 0.72
    Behavior on opacity { NumberAnimation { duration: 140 } }

    Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: ctl.checked ? ctl.accent : appTheme.borderStrong
        Behavior on color { ColorAnimation { duration: 160 } }

        Rectangle {
            id: knob
            width: 18
            height: 18
            radius: 9
            y: 3
            x: ctl.checked ? parent.width - width - 3 : 3
            color: appTheme.white
            Behavior on x { NumberAnimation { duration: 170; easing.type: Easing.OutCubic } }
        }
    }

    TimerPointerArea {
        anchors.fill: parent
        enabled: ctl.enabled
        onClicked: ctl.toggled(!ctl.checked)
    }
}
