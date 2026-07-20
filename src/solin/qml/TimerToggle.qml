// TimerToggle.qml — a controlled on/off switch with a sliding knob.
// Emits `toggled(value)`; it does not mutate `checked` itself, so the backing
// model stays the single source of truth (the binding updates the visual).
import QtQuick 2.15

Item {
    id: ctl
    property bool checked: false
    property color accent: appTheme.accent
    property string accessibleName: ""
    signal toggled(bool value)

    implicitWidth: 48
    implicitHeight: 44
    opacity: enabled ? 1.0 : 0.72
    activeFocusOnTab: enabled

    Accessible.role: Accessible.CheckBox
    Accessible.name: accessibleName
    Accessible.checked: checked

    Behavior on opacity { NumberAnimation { duration: 140 } }

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
            ctl.toggled(!ctl.checked)
            event.accepted = true
        }
    }

    Rectangle {
        id: track
        anchors.centerIn: parent
        width: 42
        height: 24
        radius: height / 2
        color: ctl.checked ? ctl.accent : appTheme.borderStrong
        border.width: ctl.activeFocus ? 2 : 0
        border.color: ctl.activeFocus ? appTheme.white : "transparent"
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
