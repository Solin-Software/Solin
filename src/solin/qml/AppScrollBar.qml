import QtQuick
import QtQuick.Controls

ScrollBar {
    id: control
    policy: ScrollBar.AsNeeded
    width: 14
    contentItem: Rectangle {
        implicitWidth: 6
        radius: 3
        color: control.active || control.hovered
            ? appTheme.textMuted : appTheme.borderStrong
        opacity: control.active || control.hovered ? 1 : 0.35
        anchors.horizontalCenter: parent.horizontalCenter
        Behavior on color { ColorAnimation { duration: 150 } }
        Behavior on opacity { NumberAnimation { duration: 180 } }
    }
    background: Item {}
}
