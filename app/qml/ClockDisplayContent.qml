// ClockDisplayContent.qml — renders one clock/timer face inside its bounds.
import QtQuick 2.15

Item {
    id: root

    property var faceModel: ({})
    property real displayPct: 1.0

    readonly property bool isAnalogOnly: faceModel.mode === "analog"
    readonly property bool isAnalogDigital: faceModel.mode === "analog_digital"
    readonly property bool hasAnalogFace: root.isAnalogOnly || root.isAnalogDigital
    readonly property string analogStyle: faceModel.analog_style ? faceModel.analog_style : "signature"
    readonly property bool overrun: faceModel.overrun === true
    readonly property var durationSector: faceModel.duration_sector ? faceModel.duration_sector : ({})
    readonly property real availableWidth: Math.max(1, width)
    readonly property real availableHeight: Math.max(1, height)
    readonly property real digitalProbeWidth: Math.max(1, digitalProbe.implicitWidth)
    readonly property real digitalProbeHeight: Math.max(1, digitalProbe.implicitHeight)
    readonly property real digitalMaxPrimaryPx: Math.min(
        availableWidth * 100 / digitalProbeWidth,
        availableHeight * 100 / digitalProbeHeight)
    readonly property int primaryPx: Math.max(12, Math.round(digitalMaxPrimaryPx * displayPct))
    readonly property real analogDiameter: Math.max(24, Math.min(availableWidth, availableHeight) * displayPct)
    readonly property real analogDigitalHeightRatio: 1.22
    readonly property real analogDigitalDiameter: Math.max(24,
        Math.min(availableWidth, availableHeight / analogDigitalHeightRatio) * displayPct)
    readonly property real analogDigitalHeight: analogDigitalDiameter * analogDigitalHeightRatio

    DigitalClockReadout {
        id: digitalProbe
        visible: false
        primaryText: root.faceModel.primary_text ? root.faceModel.primary_text : ""
        secondsText: root.faceModel.seconds_text ? root.faceModel.seconds_text : ""
        secondaryText: root.faceModel.secondary_text ? root.faceModel.secondary_text : ""
        primaryPx: 100
    }

    DigitalClockReadout {
        anchors.centerIn: parent
        visible: !root.hasAnalogFace
        primaryText: root.faceModel.primary_text ? root.faceModel.primary_text : ""
        secondsText: root.faceModel.seconds_text ? root.faceModel.seconds_text : ""
        secondaryText: root.faceModel.secondary_text ? root.faceModel.secondary_text : ""
        digitColor: root.overrun ? "#ff5555" : "#ffffff"
        primaryPx: root.primaryPx
    }

    AnalogClockFace {
        anchors.centerIn: parent
        visible: root.isAnalogOnly
        width: root.analogDiameter
        height: width
        style: root.analogStyle
        hourAngle: root.faceModel.hour_angle ? root.faceModel.hour_angle : 0
        minuteAngle: root.faceModel.minute_angle ? root.faceModel.minute_angle : 0
        secondAngle: root.faceModel.second_angle ? root.faceModel.second_angle : 0
        durationSector: root.durationSector
    }

    AnalogDigitalClockFace {
        anchors.centerIn: parent
        visible: root.isAnalogDigital
        width: root.analogDigitalDiameter
        height: root.analogDigitalHeight
        analogDiameter: root.analogDigitalDiameter
        style: root.analogStyle
        hourAngle: root.faceModel.hour_angle ? root.faceModel.hour_angle : 0
        minuteAngle: root.faceModel.minute_angle ? root.faceModel.minute_angle : 0
        secondAngle: root.faceModel.second_angle ? root.faceModel.second_angle : 0
        primaryText: root.faceModel.primary_text ? root.faceModel.primary_text : ""
        secondsText: root.faceModel.seconds_text ? root.faceModel.seconds_text : ""
        secondaryText: root.faceModel.secondary_text ? root.faceModel.secondary_text : ""
        durationSector: root.durationSector
    }
}
