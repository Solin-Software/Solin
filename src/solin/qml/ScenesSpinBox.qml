pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15

SpinBox {
    id: root
    property var theme: null
    property string unitText: ""

    function pendingValue() {
        var parsed = Number(root.valueFromText(valueEditor.text, root.locale))
        if (!isFinite(parsed))
            return root.value
        return Math.max(root.from, Math.min(root.to, Math.round(parsed)))
    }

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    implicitHeight: 36
    editable: true
    opacity: enabled ? 1 : 0.4
    font.pixelSize: 11

    contentItem: TextInput {
        id: valueEditor
        objectName: root.objectName.length > 0
            ? root.objectName + "-input" : "scenesSpinBoxInput"
        z: 2
        leftPadding: 10
        rightPadding: 30 + (unitLabel.visible ? unitLabel.implicitWidth + 8 : 0)
        text: root.displayText
        color: root.textPrimary
        selectionColor: root.accent
        selectedTextColor: "white"
        font: root.font
        horizontalAlignment: TextInput.AlignLeft
        verticalAlignment: TextInput.AlignVCenter
        readOnly: !root.editable
        selectByMouse: true
        validator: root.validator
        inputMethodHints: Qt.ImhFormattedNumbersOnly
    }

    Text {
        id: unitLabel
        objectName: root.objectName.length > 0
            ? root.objectName + "-unit" : "scenesSpinBoxUnit"
        anchors.right: parent.right
        anchors.rightMargin: 28
        anchors.verticalCenter: parent.verticalCenter
        visible: text.length > 0
        text: root.unitText
        color: root.textMuted
        font: root.font
    }

    up.indicator: Rectangle {
        x: root.width - width - 2
        y: 2
        width: 22
        height: (root.height - 4) / 2
        radius: 6
        color: root.up.pressed ? root.hover
            : root.up.hovered ? Qt.lighter(root.surface, 1.12) : "transparent"
        Text {
            anchors.centerIn: parent
            text: "⌃"
            color: root.up.enabled ? root.textMuted : root.borderColor
            font.pixelSize: 10
        }
    }
    down.indicator: Rectangle {
        x: root.width - width - 2
        y: root.height / 2
        width: 22
        height: (root.height - 4) / 2
        radius: 6
        color: root.down.pressed ? root.hover
            : root.down.hovered ? Qt.lighter(root.surface, 1.12) : "transparent"
        Text {
            anchors.centerIn: parent
            text: "⌄"
            color: root.down.enabled ? root.textMuted : root.borderColor
            font.pixelSize: 10
        }
    }

    background: Rectangle {
        radius: 9
        color: root.surface
        border.width: 1
        border.color: root.activeFocus ? root.accent : root.borderColor
        Behavior on border.color { ColorAnimation { duration: 100 } }
    }
}
