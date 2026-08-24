pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15

TextField {
    id: root
    property var theme: null

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    implicitHeight: 36
    leftPadding: 10
    rightPadding: 10
    color: textPrimary
    placeholderTextColor: textMuted
    selectionColor: accent
    selectedTextColor: "white"
    font.pixelSize: 11

    background: Rectangle {
        radius: 9
        color: root.surface
        border.width: 1
        border.color: root.activeFocus ? root.accent : root.borderColor
        Behavior on border.color { ColorAnimation { duration: 100 } }
    }
}
