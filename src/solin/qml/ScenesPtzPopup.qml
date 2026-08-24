pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Popup {
    id: root
    objectName: "scenesPtzPopup"
    property var bridge: null
    property var theme: null
    property string cameraId: ""
    property string movingCameraId: ""
    property real motionPan: 0
    property real motionTilt: 0
    property real motionZoom: 0
    property int speedPercent: 50
    property string feedback: ""

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"

    modal: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(360, parent ? parent.width - 24 : 360)
    padding: 16
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    focus: true

    function selectedCameras() {
        return bridge ? bridge.selectedLayerPtzCameras : []
    }

    function ensureCamera() {
        var cameras = selectedCameras()
        if (!cameras.length) {
            cameraId = ""
            return
        }
        var found = false
        for (var index = 0; index < cameras.length; ++index) {
            if (cameras[index].id === cameraId)
                found = true
        }
        if (!found)
            cameraId = cameras[0].id
        cameraCombo.currentIndex = cameras.findIndex(function(item) { return item.id === cameraId })
    }

    function startMotion(pan, tilt, zoom) {
        if (!bridge || !bridge.ptzControlsAvailable || !cameraId)
            return
        stopMotion()
        movingCameraId = cameraId
        motionPan = pan
        motionTilt = tilt
        motionZoom = zoom
        sendMotion()
        repeatTimer.start()
    }

    function sendMotion() {
        if (bridge && movingCameraId)
            bridge.movePtz(movingCameraId, motionPan, motionTilt, motionZoom,
                speedPercent / 100)
    }

    function stopMotion() {
        repeatTimer.stop()
        var id = movingCameraId
        movingCameraId = ""
        motionPan = 0
        motionTilt = 0
        motionZoom = 0
        if (bridge && id)
            bridge.stopPtz(id)
    }

    onAboutToShow: {
        ensureCamera()
        feedback = bridge && !bridge.ptzControlsAvailable
            ? qsTr("PTZ controls are unavailable for this Solin installation.") : ""
    }
    onOpened: ptzContent.forceActiveFocus()
    onClosed: stopMotion()

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onChanged() { root.ensureCamera() }
        function onPtzResult(cameraId, succeeded, errorCode) {
            if (!succeeded)
                root.feedback = qsTr("Camera control unavailable (%1).").arg(errorCode || "ptz_control_failed")
        }
    }

    Timer { id: repeatTimer; interval: 350; repeat: true; onTriggered: root.sendMotion() }

    background: Rectangle {
        color: root.surface
        radius: 15
        border.width: 1
        border.color: root.borderColor
    }

    contentItem: FocusScope {
        id: ptzContent
        focus: true

        onActiveFocusChanged: if (!activeFocus && root.visible) root.stopMotion()

        Keys.onPressed: function(event) {
            var motion = null
            if (event.key === Qt.Key_Left) motion = [-1, 0, 0]
            else if (event.key === Qt.Key_Right) motion = [1, 0, 0]
            else if (event.key === Qt.Key_Up) motion = [0, 1, 0]
            else if (event.key === Qt.Key_Down) motion = [0, -1, 0]
            else if (event.key === Qt.Key_Minus) motion = [0, 0, -1]
            else if (event.key === Qt.Key_Plus || event.key === Qt.Key_Equal) motion = [0, 0, 1]
            if (motion !== null) {
                if (!event.isAutoRepeat || !root.movingCameraId)
                    root.startMotion(motion[0], motion[1], motion[2])
                else
                    root.sendMotion()
                event.accepted = true
            }
        }
        Keys.onReleased: function(event) {
            if (!event.isAutoRepeat && [Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
                    Qt.Key_Minus, Qt.Key_Plus, Qt.Key_Equal].indexOf(event.key) >= 0) {
                root.stopMotion()
                event.accepted = true
            }
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 12

        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: qsTr("Camera control"); color: root.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold }
            Rectangle {
                Layout.preferredWidth: 30; Layout.preferredHeight: 30; radius: 8
                color: closeMouse.containsMouse ? root.hover : "transparent"
                Image { anchors.centerIn: parent; width: 13; height: 13; source: "image://sceneicons/close/16/" + String(root.textMuted).replace("#", "") }
                MouseArea { id: closeMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.close() }
            }
        }

        ScenesComboBox {
            id: cameraCombo
            theme: root.theme
            Layout.fillWidth: true
            visible: count > 1
            model: root.selectedCameras()
            textRole: "name"
            onActivated: {
                root.stopMotion()
                root.cameraId = model[currentIndex].id
            }
        }

        GridLayout {
            Layout.alignment: Qt.AlignHCenter
            columns: 3
            columnSpacing: 7
            rowSpacing: 7
            Item { Layout.preferredWidth: 54; Layout.preferredHeight: 42 }
            MotionButton { label: "↑"; description: qsTr("Move camera up"); pan: 0; tilt: 1; zoom: 0 }
            Item { Layout.preferredWidth: 54; Layout.preferredHeight: 42 }
            MotionButton { label: "←"; description: qsTr("Move camera left"); pan: -1; tilt: 0; zoom: 0 }
            MotionButton { label: "●"; description: qsTr("Stop camera"); stopButton: true }
            MotionButton { label: "→"; description: qsTr("Move camera right"); pan: 1; tilt: 0; zoom: 0 }
            Item { Layout.preferredWidth: 54; Layout.preferredHeight: 42 }
            MotionButton { label: "↓"; description: qsTr("Move camera down"); pan: 0; tilt: -1; zoom: 0 }
            Item { Layout.preferredWidth: 54; Layout.preferredHeight: 42 }
            MotionButton { label: "−"; description: qsTr("Zoom out"); pan: 0; tilt: 0; zoom: -1 }
            Text { Layout.alignment: Qt.AlignCenter; text: qsTr("Zoom"); color: root.textMuted; font.pixelSize: 10 }
            MotionButton { label: "+"; description: qsTr("Zoom in"); pan: 0; tilt: 0; zoom: 1 }
        }

        RowLayout {
            Layout.fillWidth: true
            Text { text: qsTr("Speed"); color: root.textMuted; font.pixelSize: 10 }
            Slider { Layout.fillWidth: true; from: 5; to: 100; value: root.speedPercent; stepSize: 5; onMoved: root.speedPercent = value }
            Text { text: Math.round(root.speedPercent) + "%"; color: root.textSecondary; font.pixelSize: 10 }
        }

        RowLayout {
            Layout.fillWidth: true
            visible: presetCombo.count > 0
            ScenesComboBox {
                id: presetCombo
                theme: root.theme
                Layout.fillWidth: true
                model: root.bridge && root.cameraId ? root.bridge.ptzPresets(root.cameraId) : []
                textRole: "name"
            }
            Rectangle {
                Layout.preferredWidth: 76; Layout.preferredHeight: 36; radius: 9
                color: recallMouse.containsMouse ? root.hover : root.surfaceSoft
                border.width: 1; border.color: root.borderColor
                Text { anchors.centerIn: parent; text: qsTr("Recall"); color: root.textSecondary; font.pixelSize: 10 }
                MouseArea {
                    id: recallMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        root.stopMotion()
                        var items = presetCombo.model
                        if (items.length) {
                            root.bridge.recallPtzPreset(items[presetCombo.currentIndex].id)
                            root.feedback = qsTr("Positioning camera…")
                        }
                    }
                }
            }
        }

            Text {
                Layout.fillWidth: true
                visible: root.feedback.length > 0
                text: root.feedback
                color: root.textMuted
                font.pixelSize: 9
                wrapMode: Text.Wrap
            }
        }
    }

    component MotionButton: Rectangle {
        id: motion
        property string label: ""
        property string description: ""
        property real pan: 0
        property real tilt: 0
        property real zoom: 0
        property bool stopButton: false
        Layout.preferredWidth: 54
        Layout.preferredHeight: 42
        radius: 10
        color: motionMouse.pressed ? root.accent
            : motionMouse.containsMouse ? root.hover : root.surfaceSoft
        border.width: 1
        border.color: motion.stopButton ? root.danger : root.borderColor
        opacity: root.bridge && root.bridge.ptzControlsAvailable ? 1 : 0.38
        Text { anchors.centerIn: parent; text: motion.label; color: motionMouse.pressed ? "white" : motion.stopButton ? root.danger : root.textPrimary; font.pixelSize: 16; font.weight: Font.DemiBold }
        MouseArea {
            id: motionMouse
            anchors.fill: parent
            enabled: root.bridge && root.bridge.ptzControlsAvailable
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onPressed: if (motion.stopButton) root.stopMotion(); else root.startMotion(motion.pan, motion.tilt, motion.zoom)
            onReleased: if (!motion.stopButton) root.stopMotion()
            onCanceled: root.stopMotion()
        }
        ToolTip.visible: motionMouse.containsMouse
        ToolTip.text: motion.description
        Accessible.name: motion.description
    }
}
