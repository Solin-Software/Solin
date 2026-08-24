pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    property var theme: null
    property string text: ""
    property bool primary: false
    property bool danger: false
    signal clicked()

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color dangerColor: theme ? theme.danger : "#ef6a6a"

    implicitWidth: Math.max(86, label.implicitWidth + 28)
    implicitHeight: 38
    radius: 10
    opacity: enabled ? 1 : 0.4
    color: primary ? accent
        : buttonMouse.containsMouse && enabled ? hover : surfaceSoft
    border.width: primary ? 0 : 1
    border.color: danger ? dangerColor : borderColor

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: root.primary ? "white"
            : root.danger ? root.dangerColor : root.textSecondary
        font.pixelSize: 11
        font.weight: Font.DemiBold
    }

    MouseArea {
        id: buttonMouse
        anchors.fill: parent
        enabled: root.enabled
        hoverEnabled: true
        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: root.clicked()
    }
}
