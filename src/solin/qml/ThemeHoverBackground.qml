// ThemeHoverBackground.qml - themed hover fill that animates opacity only.
import QtQuick 2.15

Rectangle {
    id: root

    property bool hovered: false
    property color fillColor: appTheme.hover
    property real fillOpacity: appTheme.hoverOpacity
    property bool showBorder: false
    property color borderColor: appTheme.borderStrong
    property real borderOpacity: 1.0
    property int animationDuration: 120

    color: withAlpha(fillColor, hovered ? fillOpacity : 0.0)
    border.width: showBorder ? 1 : 0
    border.color: withAlpha(borderColor, hovered && showBorder ? borderOpacity : 0.0)

    function withAlpha(colorValue, alphaValue) {
        return Qt.rgba(colorValue.r, colorValue.g, colorValue.b, alphaValue)
    }

    Behavior on color {
        ColorAnimation {
            duration: root.animationDuration
            easing.type: Easing.OutCubic
        }
    }

    Behavior on border.color {
        ColorAnimation {
            duration: root.animationDuration
            easing.type: Easing.OutCubic
        }
    }
}
