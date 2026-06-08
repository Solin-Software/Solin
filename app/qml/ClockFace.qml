// ClockFace.qml — fullscreen clock/timer surface for the advanced timer window.
// Bound to the `clock` context property (ClockRenderBridge); `clock.model` is a
// flat render dict produced by app/core/timer/render.py. The QML stays purely
// declarative — all formatting/geometry comes pre-computed in the model.
import QtQuick 2.15

Rectangle {
    id: root
    color: "#000000"

    readonly property var model: clock.model
    readonly property bool active: model.active === true
    readonly property bool isAnalogOnly: (model.mode === "analog") && !active
    readonly property bool isAnalogDigital: (model.mode === "analog_digital") && !active
    readonly property bool hasAnalogFace: root.isAnalogOnly || root.isAnalogDigital
    readonly property string analogStyle: model.analog_style ? model.analog_style : "signature"
    readonly property bool overrun: model.overrun === true
    readonly property real requestedDisplayPct: Number(model.text_scale_pct)
    readonly property real displayPct: Math.max(10, Math.min(100, isNaN(requestedDisplayPct) ? 100 : requestedDisplayPct)) / 100.0
    readonly property real safeInset: Math.round(Math.min(width, height) * 0.035)
    readonly property real availableWidth: Math.max(1, width - safeInset * 2)
    readonly property real availableHeight: Math.max(1, height - safeInset * 2)
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
        primaryText: root.model.primary_text ? root.model.primary_text : ""
        secondsText: root.model.seconds_text ? root.model.seconds_text : ""
        secondaryText: root.model.secondary_text ? root.model.secondary_text : ""
        primaryPx: 100
    }

    // ── Digital readout (wall clock or countdown) ─────────────────────────────
    DigitalClockReadout {
        anchors.centerIn: parent
        visible: !root.hasAnalogFace
        primaryText: root.model.primary_text ? root.model.primary_text : ""
        secondsText: root.model.seconds_text ? root.model.seconds_text : ""
        secondaryText: root.model.secondary_text ? root.model.secondary_text : ""
        digitColor: root.overrun ? "#ff5555" : "#ffffff"
        primaryPx: root.primaryPx
    }

    // ── Analog clock (idle only) ──────────────────────────────────────────────
    AnalogClockFace {
        id: analog
        anchors.centerIn: parent
        visible: root.isAnalogOnly
        width: root.analogDiameter
        height: width
        style: root.analogStyle
        hourAngle: root.model.hour_angle ? root.model.hour_angle : 0
        minuteAngle: root.model.minute_angle ? root.model.minute_angle : 0
        secondAngle: root.model.second_angle ? root.model.second_angle : 0
    }

    AnalogDigitalClockFace {
        anchors.centerIn: parent
        visible: root.isAnalogDigital
        width: root.analogDigitalDiameter
        height: root.analogDigitalHeight
        analogDiameter: root.analogDigitalDiameter
        style: root.analogStyle
        hourAngle: root.model.hour_angle ? root.model.hour_angle : 0
        minuteAngle: root.model.minute_angle ? root.model.minute_angle : 0
        secondAngle: root.model.second_angle ? root.model.second_angle : 0
        primaryText: root.model.primary_text ? root.model.primary_text : ""
        secondsText: root.model.seconds_text ? root.model.seconds_text : ""
        secondaryText: root.model.secondary_text ? root.model.secondary_text : ""
    }
}
