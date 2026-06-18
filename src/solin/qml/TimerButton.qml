// TimerButton.qml — a compact, animated button used across the Timer tab.
// Controlled & presentational: callers set `variant`/`accent` and react to
// `clicked()`. Variants: "primary" (filled), "soft" (tinted) and "ghost".
import QtQuick 2.15
import QtQuick.Controls 2.15

Rectangle {
    id: ctl

    property string text: ""
    property string variant: "ghost"          // primary | soft | ghost
    property color accent: "#388bfd"
    property string iconName: ""
    property string iconColorHex: ""
    property int iconSize: 14
    property string tip: ""
    signal clicked()

    readonly property bool _filled: variant === "primary"
    readonly property bool _soft: variant === "soft"

    implicitHeight: 34
    implicitWidth: row.implicitWidth + (row.implicitWidth > 0 ? 26 : 22)
    radius: 8
    opacity: enabled ? 1.0 : 0.4

    color: _filled
           ? (ma.containsMouse ? Qt.lighter(accent, 1.14) : accent)
           : _soft
             ? Qt.rgba(accent.r, accent.g, accent.b, ma.containsMouse ? 0.22 : 0.14)
             : (ma.containsMouse ? "#1a1f2a" : "transparent")
    border.width: 1
    border.color: _filled
                  ? "transparent"
                  : _soft
                    ? Qt.rgba(accent.r, accent.g, accent.b, ma.containsMouse ? 0.55 : 0.3)
                    : (ma.containsMouse ? "#2a3040" : "#1e2430")

    scale: ma.pressed && enabled ? 0.97 : 1.0

    Behavior on color { ColorAnimation { duration: 130 } }
    Behavior on border.color { ColorAnimation { duration: 130 } }
    Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 7

        Image {
            visible: ctl.iconName !== ""
            anchors.verticalCenter: parent.verticalCenter
            width: ctl.iconSize
            height: ctl.iconSize
            sourceSize.width: ctl.iconSize * 2
            sourceSize.height: ctl.iconSize * 2
            source: ctl.iconName === "" ? "" :
                    "image://timericons/" + ctl.iconName + "/" + (ctl.iconSize * 2) + "/" +
                    (ctl.iconColorHex !== "" ? ctl.iconColorHex
                     : ctl._filled ? "ffffff" : "c9d1d9")
        }

        Text {
            visible: ctl.text !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: ctl.text
            color: ctl._filled ? "#ffffff"
                   : ctl._soft ? ctl.accent
                   : (ma.containsMouse ? "#e6edf3" : "#c9d1d9")
            font.pixelSize: 13
            font.weight: Font.DemiBold
            Behavior on color { ColorAnimation { duration: 130 } }
        }
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        enabled: ctl.enabled
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: ctl.clicked()
    }

    ToolTip.visible: ctl.tip !== "" && ma.containsMouse
    ToolTip.text: ctl.tip
    ToolTip.delay: 450
}
