// TimerStepper.qml — a "−  value  +" control. Presentational: callers decide
// what a step means (minutes, counts, seconds…) via `decremented`/`incremented`.
// When `editable`, double-clicking the value reveals an inline text field that
// emits `edited(text)` on accept.
import QtQuick 2.15
import QtQuick.Controls 2.15

Row {
    id: ctl
    property string text: "0"
    property color accent: appTheme.accent
    property real fieldWidth: 64
    property int valueFontSize: 16
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
        width: 34; height: 34; radius: 9
        color: sbMa.containsMouse ? Qt.rgba(ctl.accent.r, ctl.accent.g, ctl.accent.b, 0.18) : appTheme.surfaceAlt
        border.width: activeFocus ? 2 : 1
        border.color: activeFocus || sbMa.containsMouse
                      ? Qt.rgba(ctl.accent.r, ctl.accent.g, ctl.accent.b, 0.75)
                      : appTheme.borderStrong
        scale: sbMa.pressed ? 0.9 : 1.0
        activeFocusOnTab: true

        Accessible.role: Accessible.Button
        Accessible.name: tip

        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
        Behavior on scale { NumberAnimation { duration: 80 } }
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                sb.pressed_()
                event.accepted = true
            }
        }
        Text {
            anchors.centerIn: parent
            text: sb.glyph
            color: sbMa.containsMouse ? ctl.accent : appTheme.textSecondary
            font.pixelSize: 16
            font.weight: Font.Medium
            Behavior on color { ColorAnimation { duration: 120 } }
        }
        TimerPointerArea {
            id: sbMa
            anchors.fill: parent
            anchors.margins: -4
            onClicked: sb.pressed_()
        }
        ThemedToolTip {
            visible: sb.tip !== "" && sbMa.containsMouse
            text: sb.tip
        }
    }

    StepBtn {
        visible: ctl.buttonsVisible
        anchors.verticalCenter: parent.verticalCenter
        glyph: "−"
        tip: ctl.decTip
        onPressed_: ctl.decremented()
    }

    Item {
        id: valueField
        width: ctl.fieldWidth
        height: 34
        anchors.verticalCenter: parent.verticalCenter
        activeFocusOnTab: ctl.editable

        Accessible.role: Accessible.EditableText
        Accessible.name: ctl.text
        Accessible.description: ctl.editable ? qsTr("Double-click to edit") : ""

        Keys.onPressed: function(event) {
            if (ctl.editable && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_F2)) {
                editField.text = ctl.text
                ctl.editing = true
                editField.forceActiveFocus()
                editField.selectAll()
                event.accepted = true
            }
        }

        Rectangle {
            anchors.fill: parent
            visible: valueField.activeFocus && !ctl.editing
            radius: 8
            color: "transparent"
            border.width: 2
            border.color: ctl.accent
        }

        Text {
            visible: !ctl.editing
            anchors.centerIn: parent
            text: ctl.text
            color: appTheme.textPrimary
            font.pixelSize: ctl.valueFontSize
            font.weight: Font.Bold

            TimerPointerArea {
                id: valueMa
                anchors.fill: parent
                enabled: ctl.editable
                onDoubleClicked: {
                    editField.text = ctl.text
                    ctl.editing = true
                    editField.forceActiveFocus()
                    editField.selectAll()
                }
                ThemedToolTip {
                    visible: ctl.editable && valueMa.containsMouse
                    text: qsTr("Double-click to edit")
                    delay: 600
                }
            }
        }

        // Plain TextInput in a Rectangle — independent of the active Controls
        // style, so the editor always matches the app's dark theme.
        Rectangle {
            visible: ctl.editing
            anchors.fill: parent
            radius: 7
            color: appTheme.bg
            border.color: ctl.accent
            border.width: 1
            TextInput {
                id: editField
                anchors.fill: parent
                anchors.leftMargin: 6
                anchors.rightMargin: 6
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                color: appTheme.textPrimary
                selectionColor: ctl.accent
                selectedTextColor: appTheme.white
                font.pixelSize: Math.max(12, ctl.valueFontSize - 1)
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

    component ThemedToolTip: ToolTip {
        id: tip
        delay: 450
        padding: 6
        contentItem: Text {
            text: tip.text
            color: appTheme.textPrimary
            font.pixelSize: 12
        }
        background: Rectangle {
            color: appTheme.surface
            radius: 6
            border.width: 1
            border.color: appTheme.border_
        }
    }
}
