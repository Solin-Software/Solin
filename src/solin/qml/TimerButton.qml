// TimerButton.qml — a compact, animated button used across the Timer tab.
// Controlled & presentational: callers set `variant`/`accent` and react to
// `clicked()`. Variants: "primary" (filled), "soft" (tinted) and "ghost".
import QtQuick 2.15

Rectangle {
    id: ctl

    property string text: ""
    property string variant: "ghost"          // primary | soft | ghost
    property color accent: appTheme.accent
    property string iconName: ""
    property string iconColorHex: ""
    property int iconSize: 14
    property string tip: ""
    signal clicked()

    readonly property bool _filled: variant === "primary"
    readonly property bool _soft: variant === "soft"
    readonly property real _contentImplicitWidth: label.implicitWidth
                                                  + (icon.visible ? icon.width + row.spacing : 0)

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    implicitHeight: 34
    implicitWidth: _contentImplicitWidth + (_contentImplicitWidth > 0 ? 26 : 22)
    radius: 8
    opacity: enabled ? 1.0 : 0.4
    activeFocusOnTab: enabled

    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.description: tip

    color: _filled
           ? (ma.containsMouse ? Qt.lighter(accent, 1.14) : accent)
           : _soft
             ? Qt.rgba(accent.r, accent.g, accent.b, ma.containsMouse ? 0.22 : 0.14)
             : (ma.containsMouse ? appTheme.hover : "transparent")
    border.width: activeFocus ? 2 : 1
    border.color: activeFocus
                  ? (_filled ? appTheme.white : accent)
                  : _filled
                  ? "transparent"
                  : _soft
                    ? Qt.rgba(accent.r, accent.g, accent.b, ma.containsMouse ? 0.55 : 0.3)
                    : (ma.containsMouse ? appTheme.borderStrong : appTheme.border_)

    scale: ma.pressed && enabled ? 0.97 : 1.0

    Behavior on color { ColorAnimation { duration: 130 } }
    Behavior on border.color { ColorAnimation { duration: 130 } }
    Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
            ctl.clicked()
            event.accepted = true
        }
    }

    Row {
        id: row
        anchors.centerIn: parent
        width: Math.min(ctl._contentImplicitWidth, Math.max(0, ctl.width - 20))
        height: Math.max(icon.visible ? icon.height : 0, label.visible ? label.implicitHeight : 0)
        spacing: 7

        Image {
            id: icon
            visible: ctl.iconName !== ""
            anchors.verticalCenter: parent.verticalCenter
            width: ctl.iconSize
            height: ctl.iconSize
            sourceSize.width: ctl.iconSize * 2
            sourceSize.height: ctl.iconSize * 2
            source: ctl.iconName === "" ? "" :
                    "image://timericons/" + ctl.iconName + "/" + (ctl.iconSize * 2) + "/" +
                    (ctl.iconColorHex !== "" ? ctl.iconColorHex
                     : ctl._filled ? ctl.iconHex(appTheme.white) : ctl.iconHex(appTheme.textSecondary))
        }

        Text {
            id: label
            visible: ctl.text !== ""
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, row.width - (icon.visible ? icon.width + row.spacing : 0))
            text: ctl.text
            color: ctl._filled ? appTheme.white
                   : ctl._soft ? ctl.accent
                   : (ma.containsMouse ? appTheme.textPrimary : appTheme.textSecondary)
            font.pixelSize: 13
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            maximumLineCount: 1
            elide: Text.ElideRight
            clip: true
            Behavior on color { ColorAnimation { duration: 130 } }
        }
    }

    ThemedToolTip {
        visible: ctl.tip !== "" && ma.containsMouse
        text: ctl.tip
    }

    ThemedToolTip {
        visible: ctl.tip === "" && label.truncated && ma.containsMouse
        text: ctl.text
    }

    TimerPointerArea {
        id: ma
        anchors.fill: parent
        enabled: ctl.enabled
        onClicked: ctl.clicked()
    }

    component ThemedToolTip: Rectangle {
        id: tip
        property alias text: label.text

        z: 10
        x: Math.round((parent.width - width) / 2)
        y: -height - 6
        implicitWidth: label.implicitWidth + 12
        implicitHeight: label.implicitHeight + 12
        color: appTheme.surface
        radius: 6
        border.width: 1
        border.color: appTheme.border_

        Text {
            id: label
            anchors.centerIn: parent
            text: tip.text
            color: appTheme.textPrimary
            font.pixelSize: 12
        }
    }
}
