pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15

ComboBox {
    id: root
    property var theme: null

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceRaised: theme ? theme.surface2 : "#1d2939"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color borderStrong: theme ? theme.borderStrong : "#46566c"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    implicitHeight: 36
    leftPadding: 10
    rightPadding: 30
    opacity: enabled ? 1 : 0.4
    font.pixelSize: 11

    contentItem: Text {
        leftPadding: root.leftPadding
        rightPadding: root.rightPadding
        text: root.displayText
        color: root.textPrimary
        font: root.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: Text {
        x: root.width - width - 11
        anchors.verticalCenter: parent.verticalCenter
        text: "⌄"
        color: root.textMuted
        font.pixelSize: 13
    }

    background: Rectangle {
        radius: 9
        color: root.surface
        border.width: 1
        border.color: root.activeFocus || root.popup.visible
            ? root.accent : root.borderColor
        Behavior on border.color { ColorAnimation { duration: 100 } }
    }

    delegate: ItemDelegate {
        id: option
        required property int index
        required property var modelData
        width: root.width - 8
        height: 34
        leftPadding: 9
        rightPadding: 9
        highlighted: root.highlightedIndex === index
        contentItem: Text {
            text: root.textRole.length && option.modelData
                ? String(option.modelData[root.textRole]) : String(option.modelData)
            color: root.textPrimary
            font.pixelSize: 11
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 7
            color: option.highlighted ? root.hover : "transparent"
        }
    }

    popup: Popup {
        y: root.height + 4
        width: root.width
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 250)
        padding: 4
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: root.popup.visible ? root.delegateModel : null
            currentIndex: root.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
            ScrollIndicator.vertical: ScrollIndicator { }
        }
        background: Rectangle {
            radius: 10
            color: root.surfaceRaised
            border.width: 1
            border.color: root.borderStrong
        }
    }
}
