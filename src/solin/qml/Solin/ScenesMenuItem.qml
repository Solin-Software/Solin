pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

MenuItem {
    id: root
    property var theme: null
    property bool danger: false
    property bool section: false
    readonly property color surfaceHover: theme ? theme.hover : "#233043"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color dangerColor: theme ? theme.danger : "#ef6a6a"

    implicitHeight: section ? 27 : 36
    leftPadding: 11
    rightPadding: 11
    opacity: section || enabled ? 1 : 0.38

    contentItem: RowLayout {
        spacing: 8
        Text {
            Layout.fillWidth: true
            text: (root.checkable ? (root.checked ? "✓  " : "    ") : "") + root.text
            color: root.section ? root.textMuted
                : root.danger ? root.dangerColor
                : root.enabled ? root.textPrimary : root.textMuted
            font.pixelSize: root.section ? 8 : 10
            font.weight: root.section ? Font.DemiBold : Font.Medium
            font.capitalization: root.section ? Font.AllUppercase : Font.MixedCase
            font.letterSpacing: root.section ? 0.5 : 0
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        Text {
            visible: root.subMenu !== null
            text: "›"
            color: root.textMuted
            font.pixelSize: 16
            verticalAlignment: Text.AlignVCenter
        }
    }
    indicator: Item { visible: false }
    arrow: Item { visible: false }
    background: Rectangle {
        radius: 7
        color: root.highlighted && root.enabled && !root.section
            ? root.surfaceHover : "transparent"
    }
}
