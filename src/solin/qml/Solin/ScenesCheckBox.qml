pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15

CheckBox {
    id: root
    property var theme: null

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    spacing: 8
    opacity: enabled ? 1 : 0.4
    font.pixelSize: 10

    indicator: Rectangle {
        implicitWidth: 18
        implicitHeight: 18
        x: root.leftPadding
        y: parent.height / 2 - height / 2
        radius: 5
        color: root.checked ? root.accent : root.surface
        border.width: 1
        border.color: root.checked ? root.accent : root.borderColor
        Text {
            anchors.centerIn: parent
            visible: root.checked
            text: "✓"
            color: "white"
            font.pixelSize: 11
            font.weight: Font.Bold
        }
    }

    contentItem: Text {
        leftPadding: root.indicator.width + root.spacing
        text: root.text
        color: root.textSecondary
        font: root.font
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.Wrap
    }
}
