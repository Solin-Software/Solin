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
    Canvas {
        id: analog
        anchors.centerIn: parent
        visible: root.isAnalog
        width: root.analogDiameter
        height: width

        property real hourAngle: root.model.hour_angle ? root.model.hour_angle : 0
        property real minuteAngle: root.model.minute_angle ? root.model.minute_angle : 0
        property real secondAngle: root.model.second_angle ? root.model.second_angle : 0

        onHourAngleChanged: requestPaint()
        onMinuteAngleChanged: requestPaint()
        onSecondAngleChanged: requestPaint()
        onWidthChanged: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            var w = width, h = height;
            var cx = w / 2, cy = h / 2;
            var r = Math.min(cx, cy) - 4;
            ctx.reset();

            // Dial
            ctx.lineWidth = Math.max(2, r * 0.02);
            ctx.strokeStyle = "#2a2f36";
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, 2 * Math.PI);
            ctx.stroke();

            // Hour ticks
            ctx.strokeStyle = "#5a636d";
            for (var i = 0; i < 12; i++) {
                var a = (i / 12) * 2 * Math.PI;
                var inner = r * (i % 3 === 0 ? 0.82 : 0.88);
                ctx.lineWidth = (i % 3 === 0) ? Math.max(3, r * 0.03) : Math.max(1, r * 0.015);
                ctx.beginPath();
                ctx.moveTo(cx + inner * Math.sin(a), cy - inner * Math.cos(a));
                ctx.lineTo(cx + r * 0.96 * Math.sin(a), cy - r * 0.96 * Math.cos(a));
                ctx.stroke();
            }

            function hand(angleDeg, length, widthPx, color) {
                var a = angleDeg * Math.PI / 180;
                ctx.strokeStyle = color;
                ctx.lineWidth = widthPx;
                ctx.lineCap = "round";
                ctx.beginPath();
                ctx.moveTo(cx, cy);
                ctx.lineTo(cx + length * Math.sin(a), cy - length * Math.cos(a));
                ctx.stroke();
            }

            hand(hourAngle, r * 0.5, Math.max(4, r * 0.045), "#ffffff");
            hand(minuteAngle, r * 0.75, Math.max(3, r * 0.03), "#ffffff");
            hand(secondAngle, r * 0.85, Math.max(1, r * 0.012), "#ff5555");

            // Center cap
            ctx.fillStyle = "#ffffff";
            ctx.beginPath();
            ctx.arc(cx, cy, Math.max(3, r * 0.03), 0, 2 * Math.PI);
            ctx.fill();
        }
    }
}
