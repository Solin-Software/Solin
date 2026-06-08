// ClockFace.qml — fullscreen clock/timer surface for the advanced timer window.
// Bound to the `clock` context property (ClockRenderBridge); `clock.model` is a
// flat render dict produced by app/core/timer/render.py. This root only chooses
// the active layout; ClockDisplayContent renders each face.
import QtQuick 2.15

Rectangle {
    id: root
    color: "#000000"

    readonly property var model: clock.model
    readonly property real requestedDisplayPct: Number(model.text_scale_pct)
    readonly property real displayPct: Math.max(10, Math.min(100, isNaN(requestedDisplayPct) ? 100 : requestedDisplayPct)) / 100.0
    readonly property real safeInset: Math.round(Math.min(width, height) * 0.035)
    readonly property real splitGap: Math.round(Math.min(width, height) * 0.055)
    readonly property bool splitMode: model.display_mode === "clock_timer"

    Item {
        id: safeArea
        anchors.fill: parent
        anchors.margins: root.safeInset

        ClockDisplayContent {
            anchors.fill: parent
            visible: !root.splitMode
            faceModel: root.model.display_mode === "clock" ? root.model.clock : root.model.timer
            displayPct: root.displayPct
        }

        Row {
            anchors.fill: parent
            visible: root.splitMode
            spacing: root.splitGap

            ClockDisplayContent {
                width: Math.max(1, (parent.width - parent.spacing) * 0.5)
                height: parent.height
                faceModel: root.model.clock
                displayPct: root.displayPct
            }

            ClockDisplayContent {
                width: Math.max(1, (parent.width - parent.spacing) * 0.5)
                height: parent.height
                faceModel: root.model.timer
                displayPct: root.displayPct
            }
        }
    }
}
