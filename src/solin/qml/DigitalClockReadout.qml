// DigitalClockReadout.qml — main digital time plus small subdued seconds.
import QtQuick 2.15

Item {
    id: root

    property string primaryText: ""
    property string secondsText: ""
    property string secondaryText: ""
    property color digitColor: "#ffffff"
    property int primaryPx: 100
    property real secondsRatio: 0.58
    property real secondaryRatio: 0.21
    property real sideSpacingRatio: 0.05
    property real secondsOpacity: 0.58

    readonly property int secondsPx: Math.max(8, Math.round(primaryPx * secondsRatio))
    readonly property int secondaryPx: Math.max(8, Math.round(primaryPx * secondaryRatio))
    readonly property bool hasSeconds: secondsText.length > 0
    readonly property bool hasSecondary: secondaryText.length > 0
    readonly property int sideWidth: Math.max(seconds.implicitWidth, suffix.implicitWidth)

    implicitWidth: readout.implicitWidth
    implicitHeight: primary.implicitHeight

    Row {
        id: readout
        spacing: (sideSlot.visible ? Math.round(root.primaryPx * root.sideSpacingRatio) : 0)

        TimerDigits {
            id: primary
            text: root.primaryText
            color: root.digitColor
            font.weight: Font.DemiBold
            font.pixelSize: root.primaryPx
        }

        Item {
            id: sideSlot
            visible: root.hasSeconds || root.hasSecondary
            width: root.sideWidth
            height: primary.implicitHeight

            TimerDigits {
                id: seconds
                visible: root.hasSeconds
                anchors.right: parent.right
                y: Math.round(primary.baselineOffset - baselineOffset)
                text: root.secondsText
                color: root.digitColor
                opacity: root.secondsOpacity
                font.weight: Font.DemiBold
                font.pixelSize: root.secondsPx
            }

            Text {
                id: suffix
                visible: root.hasSecondary
                anchors.right: parent.right
                anchors.bottom: root.hasSeconds ? seconds.top : parent.bottom
                anchors.bottomMargin: root.hasSeconds
                                      ? Math.round(root.primaryPx * 0.015)
                                      : Math.round(root.primaryPx * 0.16)
                text: root.secondaryText
                color: "#aeb8c2"
                font.family: "Segoe UI"
                font.weight: Font.DemiBold
                font.pixelSize: root.secondaryPx
            }
        }
    }
}
