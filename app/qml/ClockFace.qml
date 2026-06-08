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
    readonly property bool isAnalog: (model.mode === "analog") && !active
    readonly property string analogStyle: model.analog_style ? model.analog_style : "signature"
    readonly property bool overrun: model.overrun === true
    readonly property real requestedDisplayPct: Number(model.text_scale_pct)
    readonly property real displayPct: Math.max(10, Math.min(100, isNaN(requestedDisplayPct) ? 100 : requestedDisplayPct)) / 100.0
    readonly property real safeInset: Math.round(Math.min(width, height) * 0.035)
    readonly property real availableWidth: Math.max(1, width - safeInset * 2)
    readonly property real availableHeight: Math.max(1, height - safeInset * 2)
    readonly property real secondaryRatio: 0.21
    readonly property real digitalSuffixSpacingRatio: 0.055
    readonly property real digitalSuffixBottomRatio: 0.16
    readonly property real digitalProbeWidth: Math.max(1,
        primaryProbe.implicitWidth
        + (secondaryProbe.visible ? 100 * digitalSuffixSpacingRatio + secondaryProbe.implicitWidth : 0))
    readonly property real digitalProbeHeight: Math.max(1,
        Math.max(primaryProbe.implicitHeight, secondaryProbe.visible ? secondaryProbe.implicitHeight : 0))
    readonly property real digitalMaxPrimaryPx: Math.min(
        availableWidth * 100 / digitalProbeWidth,
        availableHeight * 100 / digitalProbeHeight)
    readonly property int primaryPx: Math.max(12, Math.round(digitalMaxPrimaryPx * displayPct))
    readonly property int secondaryPx: Math.max(10, Math.round(primaryPx * secondaryRatio))
    readonly property real analogDiameter: Math.max(24, Math.min(availableWidth, availableHeight) * displayPct)

    TimerDigits {
        id: primaryProbe
        visible: false
        text: root.model.primary_text ? root.model.primary_text : ""
        font.weight: Font.DemiBold
        font.pixelSize: 100
    }

    Text {
        id: secondaryProbe
        visible: root.model.secondary_text && root.model.secondary_text.length > 0
        text: root.model.secondary_text ? root.model.secondary_text : ""
        font.family: "Segoe UI"
        font.weight: Font.Medium
        font.pixelSize: Math.round(100 * root.secondaryRatio)
    }

    // ── Digital readout (wall clock or countdown) ─────────────────────────────
    Row {
        anchors.centerIn: parent
        visible: !root.isAnalog
        spacing: suffixSlot.visible ? Math.round(primary.font.pixelSize * root.digitalSuffixSpacingRatio) : 0

        TimerDigits {
            id: primary
            text: root.model.primary_text ? root.model.primary_text : ""
            color: root.overrun ? "#ff5555" : "#ffffff"
            font.weight: Font.DemiBold
            font.pixelSize: root.primaryPx
        }

        Item {
            id: suffixSlot
            visible: root.model.secondary_text && root.model.secondary_text.length > 0
            width: suffix.implicitWidth
            height: primary.implicitHeight

            Text {
                id: suffix
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Math.round(primary.font.pixelSize * root.digitalSuffixBottomRatio)
                text: root.model.secondary_text ? root.model.secondary_text : ""
                color: "#aeb8c2"
                font.family: "Segoe UI"
                font.weight: Font.DemiBold
                font.pixelSize: root.secondaryPx
            }
        }
    }

    // ── Analog clock (idle only) ──────────────────────────────────────────────
    AnalogClockFace {
        id: analog
        anchors.centerIn: parent
        visible: root.isAnalog
        width: root.analogDiameter
        height: width
        style: root.analogStyle
        hourAngle: root.model.hour_angle ? root.model.hour_angle : 0
        minuteAngle: root.model.minute_angle ? root.model.minute_angle : 0
        secondAngle: root.model.second_angle ? root.model.second_angle : 0
    }
}
