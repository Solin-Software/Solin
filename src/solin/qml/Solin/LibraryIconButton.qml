pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls.Basic

Rectangle {
    id: control

    property string iconName: ""
    property string toolTipText: ""
    property bool activeState: false
    property int iconSize: 15
    property color normalIconColor: appTheme.textMuted
    signal clicked()

    implicitWidth: 36
    implicitHeight: 36
    radius: 11
    color: enabled && pointer.containsMouse ? appTheme.hover : "transparent"
    border.width: 1
    border.color: activeState
        ? appTheme.amber
        : (enabled && pointer.containsMouse ? appTheme.hoverBorder : appTheme.border)
    opacity: enabled ? 1 : 0.38

    function colorHex(value) {
        return String(value).replace("#", "")
    }

    Image {
        anchors.centerIn: parent
        width: control.iconSize
        height: control.iconSize
        source: "image://libraryicons/" + control.iconName + "/"
            + control.iconSize + "/" + control.colorHex(
                control.activeState
                    ? appTheme.amber
                    : (pointer.containsMouse ? appTheme.textSecondary : control.normalIconColor)
            )
    }

    MouseArea {
        id: pointer
        anchors.fill: parent
        enabled: control.enabled
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: controller.pointerEnter()
        onExited: controller.pointerExit()
        onClicked: control.clicked()
    }

    ToolTip {
        id: tooltip
        visible: pointer.containsMouse && control.toolTipText !== ""
        text: control.toolTipText
        delay: 420
        padding: 7
        contentItem: Text {
            text: tooltip.text
            color: appTheme.textPrimary
            font.pixelSize: 12
        }
        background: Rectangle {
            color: appTheme.surfaceChrome
            radius: 7
            border.width: 1
            border.color: appTheme.border
        }
    }

    Behavior on color { ColorAnimation { duration: 120 } }
    Behavior on border.color { ColorAnimation { duration: 120 } }
}
