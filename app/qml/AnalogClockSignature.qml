// AnalogClockSignature.qml — detailed white analog face used as the default.
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
            var size = Math.min(width, height);
            if (size <= 0)
                return;

            var cx = width / 2;
            var cy = height / 2;
            var r = size * 0.485;
            var tau = Math.PI * 2;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

            function circle(radius, fillStyle, strokeStyle, lineWidth) {
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, tau);
                if (fillStyle) {
                    ctx.fillStyle = fillStyle;
                    ctx.fill();
                }
                if (strokeStyle && lineWidth > 0) {
                    ctx.strokeStyle = strokeStyle;
                    ctx.lineWidth = lineWidth;
                    ctx.stroke();
                }
            }

            // Soft projection shadow. The face itself uses nearly the full
            // canvas so Display size matches the Classic face perceptually.
            ctx.save();
            ctx.shadowColor = "rgba(0, 0, 0, 0.36)";
            ctx.shadowBlur = Math.max(6, r * 0.030);
            ctx.shadowOffsetY = r * 0.010;
            circle(r * 0.965, "#f4f7fb", "", 0);
            ctx.restore();

            var rimGradient = ctx.createLinearGradient(cx - r, cy - r, cx + r, cy + r);
            rimGradient.addColorStop(0.00, "#eef3f7");
            rimGradient.addColorStop(0.24, "#ffffff");
            rimGradient.addColorStop(0.55, "#bdc8d2");
            rimGradient.addColorStop(0.78, "#f9fbfd");
            rimGradient.addColorStop(1.00, "#8d9aa6");
            circle(r, rimGradient, "#0f1720", Math.max(1, r * 0.008));

            circle(r * 0.955, "#d9e1e8", "#f9fbfd", Math.max(1, r * 0.01));
            circle(r * 0.922, "#f9fbfd", "#aeb8c2", Math.max(1, r * 0.006));

            var faceGradient = ctx.createRadialGradient(
                cx - r * 0.18, cy - r * 0.22, r * 0.08,
                cx, cy, r * 0.90
            );
            faceGradient.addColorStop(0.00, "#ffffff");
            faceGradient.addColorStop(0.62, "#fbfcfd");
            faceGradient.addColorStop(1.00, "#edf2f6");
            circle(r * 0.895, faceGradient, "#d7dee5", Math.max(1, r * 0.005));

            var innerGlow = ctx.createRadialGradient(cx, cy, r * 0.12, cx, cy, r * 0.82);
            innerGlow.addColorStop(0.00, "rgba(255, 255, 255, 0.0)");
            innerGlow.addColorStop(0.72, "rgba(255, 255, 255, 0.0)");
            innerGlow.addColorStop(1.00, "rgba(120, 138, 154, 0.12)");
            circle(r * 0.875, innerGlow, "", 0);
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
            var size = Math.min(width, height);
            if (size <= 0)
                return;

            var cx = width / 2;
            var cy = height / 2;
            var r = size * 0.485;
            var tau = Math.PI * 2;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

            function point(angleDeg, radius) {
                var a = angleDeg * Math.PI / 180;
                return {
                    x: cx + radius * Math.sin(a),
                    y: cy - radius * Math.cos(a)
                };
            }

            function circle(radius, fillStyle, strokeStyle, lineWidth) {
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, tau);
                if (fillStyle) {
                    ctx.fillStyle = fillStyle;
                    ctx.fill();
                }
                if (strokeStyle && lineWidth > 0) {
                    ctx.strokeStyle = strokeStyle;
                    ctx.lineWidth = lineWidth;
                    ctx.stroke();
                }
            }

            for (var i = 0; i < 60; ++i) {
                var isHour = i % 5 === 0;
                var isQuarter = i % 15 === 0;
                var outer = point(i * 6, r * 0.838);
                var inner = point(i * 6, r * (isQuarter ? 0.740 : (isHour ? 0.770 : 0.805)));

                ctx.beginPath();
                ctx.moveTo(inner.x, inner.y);
                ctx.lineTo(outer.x, outer.y);
                ctx.lineCap = "round";
                ctx.lineWidth = isQuarter
                        ? Math.max(3, r * 0.017)
                        : (isHour ? Math.max(2, r * 0.012) : Math.max(1, r * 0.0048));
                ctx.strokeStyle = isHour ? "#17202a" : "#8f9aa5";
                ctx.stroke();
            }

            ctx.save();
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillStyle = "#17202a";
            ctx.font = "bold " + Math.round(Math.max(16, r * 0.105)) + "px 'Segoe UI'";
            for (var n = 1; n <= 12; ++n) {
                var labelPoint = point(n * 30, r * 0.635);
                ctx.save();
                ctx.shadowColor = "rgba(255, 255, 255, 0.90)";
                ctx.shadowBlur = Math.max(1, r * 0.008);
                ctx.fillText(String(n), labelPoint.x, labelPoint.y);
                ctx.restore();
            }
            ctx.restore();

            circle(r * 0.505, "", "rgba(42, 55, 70, 0.08)", Math.max(1, r * 0.004));
            circle(r * 0.160, "", "rgba(42, 55, 70, 0.10)", Math.max(1, r * 0.004));
        }
    }

    Canvas {
        id: hands
        anchors.fill: parent

        property real hourAngle: root.hourAngle
        property real minuteAngle: root.minuteAngle
        property real secondAngle: root.secondAngle

        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onHourAngleChanged: requestPaint()
        onMinuteAngleChanged: requestPaint()
        onSecondAngleChanged: requestPaint()
        Component.onCompleted: requestPaint()

        onPaint: {
            var ctx = getContext("2d");
            var size = Math.min(width, height);
            if (size <= 0)
                return;

            var cx = width / 2;
            var cy = height / 2;
            var r = size * 0.485;
            var tau = Math.PI * 2;

            ctx.reset();
            ctx.clearRect(0, 0, width, height);

            function circle(radius, fillStyle, strokeStyle, lineWidth) {
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, tau);
                if (fillStyle) {
                    ctx.fillStyle = fillStyle;
                    ctx.fill();
                }
                if (strokeStyle && lineWidth > 0) {
                    ctx.strokeStyle = strokeStyle;
                    ctx.lineWidth = lineWidth;
                    ctx.stroke();
                }
            }

            function taperedHand(angleDeg, length, tail, halfWidth, fillStyle, strokeStyle) {
                var a = angleDeg * Math.PI / 180;
                var dx = Math.sin(a);
                var dy = -Math.cos(a);
                var px = Math.cos(a);
                var py = Math.sin(a);
                var shoulder = Math.max(halfWidth * 1.5, length * 0.12);
                var tipX = cx + dx * length;
                var tipY = cy + dy * length;
                var neckX = cx + dx * shoulder;
                var neckY = cy + dy * shoulder;
                var tailX = cx - dx * tail;
                var tailY = cy - dy * tail;

                ctx.save();
                ctx.shadowColor = "rgba(15, 23, 32, 0.30)";
                ctx.shadowBlur = Math.max(2, r * 0.015);
                ctx.shadowOffsetY = Math.max(1, r * 0.008);
                ctx.beginPath();
                ctx.moveTo(tipX, tipY);
                ctx.lineTo(neckX + px * halfWidth, neckY + py * halfWidth);
                ctx.lineTo(tailX + px * halfWidth * 0.58, tailY + py * halfWidth * 0.58);
                ctx.lineTo(tailX - px * halfWidth * 0.58, tailY - py * halfWidth * 0.58);
                ctx.lineTo(neckX - px * halfWidth, neckY - py * halfWidth);
                ctx.closePath();
                ctx.fillStyle = fillStyle;
                ctx.fill();
                ctx.strokeStyle = strokeStyle;
                ctx.lineWidth = Math.max(1, halfWidth * 0.22);
                ctx.stroke();
                ctx.restore();
            }

            taperedHand(
                hands.hourAngle,
                r * 0.445,
                r * 0.080,
                Math.max(5, r * 0.037),
                "#202a35",
                "#0e1720"
            );
            taperedHand(
                hands.minuteAngle,
                r * 0.650,
                r * 0.092,
                Math.max(4, r * 0.026),
                "#1a2430",
                "#0b121a"
            );

            function secondHand(angleDeg) {
                var a = angleDeg * Math.PI / 180;
                var dx = Math.sin(a);
                var dy = -Math.cos(a);
                ctx.save();
                ctx.shadowColor = "rgba(190, 23, 35, 0.28)";
                ctx.shadowBlur = Math.max(2, r * 0.012);
                ctx.strokeStyle = "#d42435";
                ctx.lineWidth = Math.max(2, r * 0.008);
                ctx.lineCap = "round";
                ctx.beginPath();
                ctx.moveTo(cx - dx * r * 0.180, cy - dy * r * 0.180);
                ctx.lineTo(cx + dx * r * 0.735, cy + dy * r * 0.735);
                ctx.stroke();
                ctx.fillStyle = "#d42435";
                ctx.beginPath();
                ctx.arc(cx - dx * r * 0.205, cy - dy * r * 0.205, Math.max(3, r * 0.017), 0, tau);
                ctx.fill();
                ctx.restore();
            }

            secondHand(hands.secondAngle);

            var capGradient = ctx.createRadialGradient(
                cx - r * 0.018, cy - r * 0.024, r * 0.008,
                cx, cy, r * 0.056
            );
            capGradient.addColorStop(0.00, "#ffffff");
            capGradient.addColorStop(0.34, "#dce4eb");
            capGradient.addColorStop(1.00, "#52606d");
            circle(r * 0.060, capGradient, "#17202a", Math.max(1, r * 0.006));
            circle(r * 0.025, "#d42435", "#ffffff", Math.max(1, r * 0.004));

            ctx.save();
            ctx.globalAlpha = 0.55;
            ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
            ctx.lineWidth = Math.max(1, r * 0.010);
            ctx.beginPath();
            ctx.arc(cx, cy, r * 0.842, Math.PI * 1.12, Math.PI * 1.46);
            ctx.stroke();
            ctx.restore();
        }
    }
}
