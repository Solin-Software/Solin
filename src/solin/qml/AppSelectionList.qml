import QtQuick
import QtQuick.Controls

ListView {
    id: control
    readonly property real scrollbarGutter: 14
    readonly property real delegateWidth: Math.max(0, width - scrollbarGutter)
    clip: true
    boundsBehavior: Flickable.DragAndOvershootBounds
    flickableDirection: Flickable.VerticalFlick
    rightMargin: scrollbarGutter
    ScrollBar.vertical: AppScrollBar {}
}
