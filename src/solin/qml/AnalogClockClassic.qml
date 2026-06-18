// AnalogClockClassic.qml — the original sparse analog clock face.
import QtQuick 2.15

Item {
    id: root

    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0
    property var durationSector: ({})

    Canvas {
        id: dial
        anchors.fill: parent

        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Component.onCompleted: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            var w = width;
            var h = height;
            var cx = w / 2;
            var cy = h / 2;
            var r = Math.min(cx, cy) - 4;
            if (r <= 0)
                return;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

            ctx.lineWidth = Math.max(2, r * 0.02);
            ctx.strokeStyle = "#2a2f36";
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, 2 * Math.PI);
            ctx.stroke();
        }
    }

    AnalogClockSector {
        anchors.fill: parent
        sector: root.durationSector
    }

    Canvas {
        id: markings
        anchors.fill: parent

        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Component.onCompleted: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            var w = width;
            var h = height;
            var cx = w / 2;
            var cy = h / 2;
            var r = Math.min(cx, cy) - 4;
            if (r <= 0)
                return;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

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
        }
    }

    Canvas {
        id: hands
        anchors.fill: parent

        property real hourAngle: root.hourAngle
        property real minuteAngle: root.minuteAngle
        property real secondAngle: root.secondAngle

        onHourAngleChanged: requestPaint()
        onMinuteAngleChanged: requestPaint()
        onSecondAngleChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Component.onCompleted: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            var w = width;
            var h = height;
            var cx = w / 2;
            var cy = h / 2;
            var r = Math.min(cx, cy) - 4;
            if (r <= 0)
                return;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

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

            hand(hands.hourAngle, r * 0.5, Math.max(4, r * 0.045), "#ffffff");
            hand(hands.minuteAngle, r * 0.75, Math.max(3, r * 0.03), "#ffffff");
            hand(hands.secondAngle, r * 0.85, Math.max(1, r * 0.012), "#ff5555");

            ctx.fillStyle = "#ffffff";
            ctx.beginPath();
            ctx.arc(cx, cy, Math.max(3, r * 0.03), 0, 2 * Math.PI);
            ctx.fill();
        }
    }
}
