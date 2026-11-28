pragma ComponentBehavior: Bound
import QtQuick

Rectangle {
    id: root

    property real shimmerPhase: 0
    property bool showThumbnail: false

    radius: 11
    color: appTheme.surface2
    clip: true

    Rectangle {
        id: thumbnailPlaceholder
        visible: root.showThumbnail
        anchors.left: parent.left
        anchors.leftMargin: 12
        anchors.verticalCenter: parent.verticalCenter
        width: 62
        height: 38
        radius: 9
        color: appTheme.borderStrong
        opacity: 0.38
    }

    Rectangle {
        anchors.left: root.showThumbnail ? thumbnailPlaceholder.right : parent.left
        anchors.leftMargin: root.showThumbnail ? 12 : 16
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(
            parent.width * (root.showThumbnail ? 0.42 : 0.55),
            root.showThumbnail ? 260 : 300
        )
        height: 8
        radius: 4
        color: appTheme.borderStrong
        opacity: 0.46
    }

    Rectangle {
        width: Math.max(90, parent.width * 0.28)
        height: parent.height * 1.8
        y: (parent.height - height) / 2
        x: (parent.width + width) * root.shimmerPhase - width
        rotation: -8
        opacity: 0.28
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: "transparent" }
            GradientStop { position: 0.5; color: appTheme.textMuted }
            GradientStop { position: 1; color: "transparent" }
        }
    }
}
