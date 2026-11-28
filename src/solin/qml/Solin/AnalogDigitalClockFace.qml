// AnalogDigitalClockFace.qml — analog face with a compact digital readout below.
import QtQuick 2.15

Item {
    id: root

    property string style: "signature"
    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0
    property string primaryText: ""
    property string secondsText: ""
    property string secondaryText: ""
    property var durationSector: ({})
    property real analogDiameter: Math.min(width, height)

    readonly property real gap: Math.round(analogDiameter * 0.035)
    readonly property int primaryPx: Math.max(10, Math.round(analogDiameter * 0.145))

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
        durationSector: root.durationSector
    }

    DigitalClockReadout {
        id: readout
        anchors.top: analog.bottom
        anchors.topMargin: root.gap
        anchors.horizontalCenter: parent.horizontalCenter
        scale: Math.min(1.0, root.width / Math.max(1, implicitWidth))
        transformOrigin: Item.Top
        primaryText: root.primaryText
        secondsText: root.secondsText
        secondaryText: root.secondaryText
        digitColor: "#ffffff"
        primaryPx: root.primaryPx
    }
}
