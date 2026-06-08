// AnalogClockSector.qml — remaining/overrun wash for any analog clock face.
import QtQuick 2.15

Canvas {
    id: root

    property var sector: ({})

    readonly property bool sectorVisible: sector && sector.visible === true
    readonly property real endAngle: sectorVisible ? safeAngle(sector.end_angle) : 0
    readonly property real currentAngle: sectorVisible ? safeAngle(sector.current_angle) : 0
    readonly property bool showRemaining: sectorVisible && sector.show_remaining === true
    readonly property bool showOverrun: sectorVisible && sector.show_overrun === true
    readonly property string paintKey: [
        sectorVisible,
        showRemaining,
        showOverrun,
        Math.round(endAngle * 10),
        Math.round(currentAngle * 10)
    ].join(":")

    visible: sectorVisible
    antialiasing: true

    function safeAngle(value) {
        var angle = Number(value);
        return isNaN(angle) ? 0 : angle;
    }

    onPaintKeyChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: requestPaint()

    onPaint: {
        var ctx = getContext("2d");
        ctx.reset();
        ctx.clearRect(0, 0, width, height);

        var size = Math.min(width, height);
        if (!root.sectorVisible || size <= 0)
            return;

        var cx = width / 2;
        var cy = height / 2;
        var outerRadius = size * 0.372;
        var innerRadius = size * 0.100;

        function normalizedSweep(startDeg, endDeg) {
            var sweep = (endDeg - startDeg) % 360;
            if (sweep < 0)
                sweep += 360;
            return sweep;
        }

        function toRadians(angleDeg) {
            return (angleDeg - 90) * Math.PI / 180;
        }

        function point(angleRad, radius) {
            return {
                x: cx + Math.cos(angleRad) * radius,
                y: cy + Math.sin(angleRad) * radius
            };
        }

        function drawSector(startDeg, endDeg, fillStyle, strokeStyle) {
            var sweep = normalizedSweep(startDeg, endDeg);
            if (sweep <= 0.05)
                return;

            var startRad = toRadians(startDeg);
            var endRad = startRad + sweep * Math.PI / 180;
            var outerStart = point(startRad, outerRadius);
            var innerEnd = point(endRad, innerRadius);

            ctx.save();
            ctx.beginPath();
            ctx.moveTo(outerStart.x, outerStart.y);
            ctx.arc(cx, cy, outerRadius, startRad, endRad, false);
            ctx.lineTo(innerEnd.x, innerEnd.y);
            ctx.arc(cx, cy, innerRadius, endRad, startRad, true);
            ctx.closePath();
            ctx.fillStyle = fillStyle;
            ctx.fill();
            ctx.strokeStyle = strokeStyle;
            ctx.lineWidth = Math.max(1, size * 0.0018);
            ctx.stroke();
            ctx.restore();
        }

        if (root.showRemaining) {
            drawSector(
                root.currentAngle,
                root.endAngle,
                "rgba(45, 192, 105, 0.22)",
                "rgba(23, 142, 79, 0.30)"
            );
        }

        if (root.showOverrun) {
            drawSector(
                root.endAngle,
                root.currentAngle,
                "rgba(255, 72, 72, 0.34)",
                "rgba(217, 36, 53, 0.42)"
            );
        }
    }
}
