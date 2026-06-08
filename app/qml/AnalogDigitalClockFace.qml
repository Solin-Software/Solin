// AnalogDigitalClockFace.qml — analog face with a compact digital readout below.
import QtQuick 2.15

Item {
    id: root

    property string style: "signature"
    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0
    property string primaryText: ""
    property string secondaryText: ""
    property real analogDiameter: Math.min(width, height)

    readonly property real gap: Math.round(analogDiameter * 0.035)
    readonly property int primaryPx: Math.max(10, Math.round(analogDiameter * 0.145))
    readonly property int secondaryPx: Math.max(8, Math.round(primaryPx * 0.46))

    AnalogClockFace {
        id: analog
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        width: root.analogDiameter
        height: width
        style: root.style
        hourAngle: root.hourAngle
        minuteAngle: root.minuteAngle
        secondAngle: root.secondAngle
    }

    Row {
        id: readout
        anchors.top: analog.bottom
        anchors.topMargin: root.gap
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: suffixSlot.visible ? Math.round(root.primaryPx * 0.055) : 0
        scale: Math.min(1.0, root.width / Math.max(1, implicitWidth))
        transformOrigin: Item.Top

        TimerDigits {
            id: primary
            text: root.primaryText
            color: "#ffffff"
            font.weight: Font.DemiBold
            font.pixelSize: root.primaryPx
        }

        Item {
            id: suffixSlot
            visible: root.secondaryText.length > 0
            width: suffix.implicitWidth
            height: primary.implicitHeight

            Text {
                id: suffix
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Math.round(root.primaryPx * 0.16)
                text: root.secondaryText
                color: "#aeb8c2"
                font.family: "Segoe UI"
                font.weight: Font.DemiBold
                font.pixelSize: root.secondaryPx
            }
        }
    }
}
