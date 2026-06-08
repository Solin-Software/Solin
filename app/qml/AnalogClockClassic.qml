// AnalogClockClassic.qml — the original sparse analog idle clock.
import QtQuick 2.15

Canvas {
    id: root

    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0

    onHourAngleChanged: requestPaint()
    onMinuteAngleChanged: requestPaint()
    onSecondAngleChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

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

        hand(root.hourAngle, r * 0.5, Math.max(4, r * 0.045), "#ffffff");
        hand(root.minuteAngle, r * 0.75, Math.max(3, r * 0.03), "#ffffff");
        hand(root.secondAngle, r * 0.85, Math.max(1, r * 0.012), "#ff5555");

        // Center cap
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(cx, cy, Math.max(3, r * 0.03), 0, 2 * Math.PI);
        ctx.fill();
    }
}
