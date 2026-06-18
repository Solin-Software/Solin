// TimerStepper.qml — a "−  value  +" control. Presentational: callers decide
// what a step means (minutes, counts, seconds…) via `decremented`/`incremented`.
// When `editable`, double-clicking the value reveals an inline text field that
// emits `edited(text)` on accept.
import QtQuick 2.15
import QtQuick.Controls 2.15

Row {
    id: ctl
    property string text: "0"
    property color accent: "#388bfd"
    property real fieldWidth: 64
    property bool editable: false
    property bool buttonsVisible: true
    property string decTip: ""
    property string incTip: ""
    signal decremented()
    signal incremented()
    signal edited(string value)

    property bool editing: false
    spacing: 6

    component StepBtn: Rectangle {
        id: sb
        property string glyph: "+"
        property string tip: ""
        signal pressed_()
        width: 28; height: 28; radius: 7
        color: sbMa.containsMouse ? Qt.rgba(ctl.accent.r, ctl.accent.g, ctl.accent.b, 0.18) : "#0f131a"
        border.width: 1
        border.color: sbMa.containsMouse ? Qt.rgba(ctl.accent.r, ctl.accent.g, ctl.accent.b, 0.55) : "#2a3040"
        scale: sbMa.pressed ? 0.9 : 1.0
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
        Behavior on scale { NumberAnimation { duration: 80 } }
        Text {
            anchors.centerIn: parent
            text: sb.glyph
            color: sbMa.containsMouse ? ctl.accent : "#c9d1d9"
            font.pixelSize: 16
            font.weight: Font.Medium
            Behavior on color { ColorAnimation { duration: 120 } }
        }
        TimerPointerArea {
            id: sbMa
            anchors.fill: parent
            onClicked: sb.pressed_()
        }
        ToolTip.visible: sb.tip !== "" && sbMa.containsMouse
        ToolTip.text: sb.tip
        ToolTip.delay: 450
    }

    StepBtn {
        visible: ctl.buttonsVisible
        anchors.verticalCenter: parent.verticalCenter
        glyph: "−"
        tip: ctl.decTip
        onPressed_: ctl.decremented()
    }

    Item {
        width: ctl.fieldWidth
        height: 28
        anchors.verticalCenter: parent.verticalCenter

        Text {
            visible: !ctl.editing
            anchors.centerIn: parent
            text: ctl.text
            color: "#e6edf3"
            font.pixelSize: 16
            font.weight: Font.Bold

            TimerPointerArea {
                anchors.fill: parent
                enabled: ctl.editable
                onDoubleClicked: {
                    editField.text = ctl.text
                    ctl.editing = true
                    editField.forceActiveFocus()
                    editField.selectAll()
                }
                ToolTip.visible: ctl.editable && containsMouse
                ToolTip.text: qsTr("Double-click to edit")
                ToolTip.delay: 600
            }
        }

        // Plain TextInput in a Rectangle — independent of the active Controls
        // style, so the editor always matches the app's dark theme.
        Rectangle {
            visible: ctl.editing
            anchors.fill: parent
            radius: 7
            color: "#0d1117"
            border.color: ctl.accent
            border.width: 1
            TextInput {
                id: editField
                anchors.fill: parent
                anchors.leftMargin: 6
                anchors.rightMargin: 6
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                color: "#e6edf3"
                selectionColor: ctl.accent
                selectedTextColor: "#ffffff"
                font.pixelSize: 15
                font.weight: Font.Bold
                clip: true
                inputMethodHints: Qt.ImhPreferNumbers
                onAccepted: { ctl.edited(text); ctl.editing = false }
                onActiveFocusChanged: if (!activeFocus) ctl.editing = false
            }
        }
    }

    StepBtn {
        visible: ctl.buttonsVisible
        anchors.verticalCenter: parent.verticalCenter
        glyph: "+"
        tip: ctl.incTip
        onPressed_: ctl.incremented()
    }
}
