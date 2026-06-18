// ClockFace.qml — fullscreen clock/timer surface for the advanced timer window.
// Bound to the `clock` context property (ClockRenderBridge); `clock.model` is a
// flat render dict produced by src/solin/core/timer/render.py. This root chooses the
// active layout and keeps a short visual snapshot for clock/timer transitions;
// ClockDisplayContent renders each face.
import QtQuick 2.15

Rectangle {
    id: root
    color: "#000000"

    readonly property var model: clock.model
    property var lastStableModel: ({})
    property var transitionModel: ({})
    property string lastSurfaceKey: ""
    property bool transitionActive: false

    function cloneModel(source) {
        if (!source)
            return {}
        return JSON.parse(JSON.stringify(source))
    }

    function surfaceKeyFor(source) {
        if (!source)
            return ""
        var state = source.active === true ? "active" : "idle"
        var mode = source.display_mode ? source.display_mode : "clock"
        return state + ":" + mode
    }

    function syncTransitionModel() {
        var nextKey = surfaceKeyFor(root.model)
        if (root.lastSurfaceKey !== "" && nextKey !== root.lastSurfaceKey) {
            transitionAnim.stop()
            root.transitionModel = cloneModel(root.lastStableModel)
            root.transitionActive = true
            transitionSurface.opacity = 1.0
            currentSurface.opacity = 0.0
            transitionAnim.start()
        }
        root.lastStableModel = cloneModel(root.model)
        root.lastSurfaceKey = nextKey
    }

    Component.onCompleted: syncTransitionModel()
    onModelChanged: syncTransitionModel()

    component ClockSurface: Item {
        id: surface

        property var surfaceModel: ({})

        readonly property real requestedDisplayPct: Number(surfaceModel.text_scale_pct)
        readonly property real displayPct: Math.max(10, Math.min(100, isNaN(requestedDisplayPct) ? 100 : requestedDisplayPct)) / 100.0
        readonly property real safeInset: Math.round(Math.min(width, height) * 0.035)
        readonly property real splitGap: Math.round(Math.min(width, height) * 0.055)
        readonly property bool splitMode: surfaceModel.display_mode === "clock_timer"
        readonly property var clockFaceModel: surfaceModel.clock ? surfaceModel.clock : ({})
        readonly property var timerFaceModel: surfaceModel.timer ? surfaceModel.timer : ({})

        Item {
            id: safeArea
            anchors.fill: parent
            anchors.margins: surface.safeInset

            ClockDisplayContent {
                anchors.fill: parent
                visible: !surface.splitMode
                faceModel: surface.surfaceModel.display_mode === "clock"
                           ? surface.clockFaceModel : surface.timerFaceModel
                displayPct: surface.displayPct
            }

            Row {
                anchors.fill: parent
                visible: surface.splitMode
                spacing: surface.splitGap

                ClockDisplayContent {
                    width: Math.max(1, (parent.width - parent.spacing) * 0.5)
                    height: parent.height
                    faceModel: surface.clockFaceModel
                    displayPct: surface.displayPct
                }

                ClockDisplayContent {
                    width: Math.max(1, (parent.width - parent.spacing) * 0.5)
                    height: parent.height
                    faceModel: surface.timerFaceModel
                    displayPct: surface.displayPct
                }
            }
        }
    }

    ClockSurface {
        id: currentSurface
        anchors.fill: parent
        surfaceModel: root.model
    }

    ClockSurface {
        id: transitionSurface
        anchors.fill: parent
        visible: root.transitionActive
        opacity: 0.0
        surfaceModel: root.transitionModel
    }

    SequentialAnimation {
        id: transitionAnim

        NumberAnimation {
            target: transitionSurface
            property: "opacity"
            from: 1.0
            to: 0.0
            duration: 180
            easing.type: Easing.InOutQuad
        }

        NumberAnimation {
            target: currentSurface
            property: "opacity"
            from: 0.0
            to: 1.0
            duration: 210
            easing.type: Easing.InOutQuad
        }

        onStopped: {
            currentSurface.opacity = 1.0
            root.transitionActive = false
            root.transitionModel = ({})
        }
    }
}
