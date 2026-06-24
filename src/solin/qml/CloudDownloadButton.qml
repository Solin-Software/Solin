import QtQuick
import QtQuick.Controls.Basic

Rectangle {
    id: root

    property bool active: true
    property bool downloading: false
    property bool queued: false
    property real progress: -1.0
    property string iconSource: ""
    property string toolTipText: ""

    signal clicked()
    signal pointerEntered()
    signal pointerExited()

    implicitWidth: 26
    implicitHeight: 26
    radius: 7
    color: active && hitArea.containsMouse ? appTheme.accentTint : "transparent"

    readonly property real normalizedProgress: Math.max(0, Math.min(1, progress))
    readonly property color trackColor: appTheme.textMuted
    readonly property color accentColor: appTheme.accentHover
    readonly property color successColor: appTheme.success
    readonly property color warningColor: appTheme.warning

    function withAlpha(c, a) {
        return Qt.rgba(c.r, c.g, c.b, a)
    }

    Behavior on color { ColorAnimation { duration: 120 } }

    Image {
        anchors.centerIn: parent
        width: downloading ? 11 : 13
        height: downloading ? 11 : 13
        source: root.iconSource
        opacity: root.active ? (hitArea.containsMouse ? 1.0 : 0.76) : 0.0

        Behavior on width { NumberAnimation { duration: 120 } }
        Behavior on height { NumberAnimation { duration: 120 } }
        Behavior on opacity { NumberAnimation { duration: 120 } }
    }

    Item {
        id: ringLayer
        anchors.fill: parent
        visible: root.active && root.downloading
        opacity: visible ? 1.0 : 0.0

        RotationAnimation on rotation {
            running: ringLayer.visible && root.progress < 0
            from: 0
            to: 360
            duration: 1100
            loops: Animation.Infinite
        }

        Connections {
            target: root
            function onProgressChanged() {
                if (root.progress >= 0)
                    ringLayer.rotation = 0
            }
        }

        Canvas {
            id: ring
            anchors.centerIn: parent
            width: 22
            height: 22
            antialiasing: true

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)

                var cx = width / 2
                var cy = height / 2
                var radius = 8.2
                var start = -Math.PI / 2
                var amount = root.progress >= 0 ? root.normalizedProgress : 0.34

                ctx.lineWidth = 2
                ctx.lineCap = "round"

                ctx.beginPath()
                ctx.strokeStyle = root.withAlpha(root.trackColor, 0.24)
                ctx.arc(cx, cy, radius, 0, Math.PI * 2)
                ctx.stroke()

                ctx.beginPath()
                ctx.strokeStyle = root.progress >= 1 ? root.successColor : root.accentColor
                ctx.arc(cx, cy, radius, start, start + Math.PI * 2 * amount)
                ctx.stroke()
            }

            Connections {
                target: root
                function onProgressChanged() { ring.requestPaint() }
                function onDownloadingChanged() { ring.requestPaint() }
                function onActiveChanged() { ring.requestPaint() }
            }

            Component.onCompleted: requestPaint()
        }

        Behavior on opacity { NumberAnimation { duration: 140 } }
    }

    Canvas {
        id: queuedRing
        anchors.centerIn: parent
        width: 22
        height: 22
        antialiasing: true
        visible: root.active && root.queued && !root.downloading
        opacity: visible ? 1.0 : 0.0

        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            var cx = width / 2
            var cy = height / 2
            var radius = 8.2

            ctx.lineWidth = 2
            ctx.lineCap = "round"

            ctx.beginPath()
            ctx.strokeStyle = root.withAlpha(root.trackColor, 0.24)
            ctx.arc(cx, cy, radius, 0, Math.PI * 2)
            ctx.stroke()

            ctx.beginPath()
            ctx.strokeStyle = root.warningColor
            ctx.arc(cx, cy, radius, -Math.PI / 2, -Math.PI / 2 + Math.PI * 0.72)
            ctx.stroke()
        }

        Connections {
            target: root
            function onQueuedChanged() { queuedRing.requestPaint() }
            function onDownloadingChanged() { queuedRing.requestPaint() }
            function onActiveChanged() { queuedRing.requestPaint() }
        }

        Behavior on opacity { NumberAnimation { duration: 140 } }
        Component.onCompleted: requestPaint()
    }

    MouseArea {
        id: hitArea
        anchors.fill: parent
        enabled: root.active
        hoverEnabled: true
        preventStealing: true
        onEntered: root.pointerEntered()
        onExited: root.pointerExited()
        onClicked: root.clicked()
    }

    ToolTip {
        visible: root.active && hitArea.containsMouse && root.toolTipText !== ""
        text: root.toolTipText
        delay: 400
    }
}
