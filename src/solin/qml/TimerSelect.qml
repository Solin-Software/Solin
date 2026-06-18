// TimerSelect.qml — compact dropdown for timer option sets that may grow.
import QtQuick 2.15
import QtQuick.Controls 2.15

Rectangle {
    id: select

    property var options: []
    property int current: 0
    property color accent: "#388bfd"
    property int controlHeight: 34
    property int labelPixelSize: 12
    signal picked(int index)

    readonly property int count: Math.max(0, options.length)
    readonly property int safeCurrent: count > 0 ? Math.min(count - 1, Math.max(0, current)) : 0
    readonly property string currentLabel: count > 0 ? options[safeCurrent] : ""

    implicitHeight: controlHeight
    implicitWidth: 206
    radius: 9
    color: menu.opened ? "#141a23" : (mouse.containsMouse ? "#1a1f2a" : "#0f131a")
    border.color: menu.opened ? select.accent : "#1e2430"
    border.width: 1
    opacity: enabled ? 1.0 : 0.72

    Behavior on color { ColorAnimation { duration: 140 } }
    Behavior on border.color { ColorAnimation { duration: 140 } }
    Behavior on opacity { NumberAnimation { duration: 140 } }

    Text {
        id: label
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 34
        text: select.currentLabel
        color: select.enabled ? "#e6edf3" : "#8b949e"
        font.pixelSize: select.labelPixelSize
        font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignLeft
        verticalAlignment: Text.AlignVCenter
        maximumLineCount: 1
        elide: Text.ElideRight
        clip: true
        Behavior on color { ColorAnimation { duration: 140 } }
    }

    Image {
        anchors.right: parent.right
        anchors.rightMargin: 11
        anchors.verticalCenter: parent.verticalCenter
        width: 14
        height: 14
        rotation: menu.opened ? -90 : 90
        opacity: select.enabled ? 0.95 : 0.55
        sourceSize.width: 28
        sourceSize.height: 28
        source: "image://timericons/chevron_right/28/8b949e"
        Behavior on rotation { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
        Behavior on opacity { NumberAnimation { duration: 140 } }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        enabled: select.enabled
        hoverEnabled: true
        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: menu.opened ? menu.close() : menu.open()
    }

    ToolTip.visible: label.truncated && mouse.containsMouse
    ToolTip.text: select.currentLabel
    ToolTip.delay: 450

    Popup {
        id: menu
        x: 0
        y: select.height + 6
        width: select.width
        implicitHeight: optionsCol.implicitHeight + 8
        padding: 4
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 120 }
            NumberAnimation { property: "scale"; from: 0.98; to: 1.0; duration: 140; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90 }
        }

        background: Rectangle {
            radius: 9
            color: "#13161c"
            border.color: "#2a3040"
            border.width: 1
        }

        contentItem: Column {
            id: optionsCol
            spacing: 2

            Repeater {
                model: select.options
                delegate: Rectangle {
                    required property int index
                    required property string modelData

                    width: menu.width - 8
                    height: 34
                    radius: 7
                    color: optionMouse.containsMouse
                           ? Qt.rgba(0.22, 0.55, 0.99, 0.14)
                           : (index === select.safeCurrent ? Qt.rgba(0.22, 0.55, 0.99, 0.10) : "transparent")

                    Behavior on color { ColorAnimation { duration: 120 } }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.leftMargin: 6
                        anchors.verticalCenter: parent.verticalCenter
                        width: 3
                        height: 18
                        radius: 2
                        color: select.accent
                        visible: index === select.safeCurrent
                    }

                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: 16
                        anchors.rightMargin: 12
                        text: modelData
                        color: index === select.safeCurrent ? "#e6edf3" : "#c9d1d9"
                        font.pixelSize: select.labelPixelSize
                        font.weight: index === select.safeCurrent ? Font.DemiBold : Font.Medium
                        horizontalAlignment: Text.AlignLeft
                        verticalAlignment: Text.AlignVCenter
                        maximumLineCount: 1
                        elide: Text.ElideRight
                    }

                    MouseArea {
                        id: optionMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: {
                            select.picked(index)
                            menu.close()
                        }
                    }
                }
            }
        }
    }
}
