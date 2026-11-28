import QtQuick

Item {
    id: root
    required property bool expanded
    required property real contentHeight
    property bool animated: true
    readonly property real targetHeight: expanded ? contentHeight : 0
    property real animatedHeight: targetHeight
    implicitHeight: animatedHeight
    visible: animatedHeight > 0
    enabled: expanded
    opacity: expanded ? 1 : 0
    clip: true

    Behavior on animatedHeight {
        enabled: root.animated
        NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
    }
    Behavior on opacity {
        enabled: root.animated
        NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
    }
}
