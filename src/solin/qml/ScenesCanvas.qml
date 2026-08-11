pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root
    objectName: "scenesCanvas"
    focus: true
    property var bridge: null
    property var theme: null
    signal requestSourceMenu(var anchor, real x, real y)
    signal requestLayerMenu(string layerId, var anchor, real x, real y)

    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"
    readonly property real outputAspect: bridge && bridge.outputHeight > 0
        ? bridge.outputWidth / bridge.outputHeight : 16 / 9
    property real guideX: -1
    property real guideY: -1

    function iconHex(colorValue) { return String(colorValue).replace("#", "") }

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onDocumentGenerationChanged() {
            root.guideX = -1
            root.guideY = -1
        }
    }

    Rectangle {
        anchors.fill: parent
        radius: 12
        color: root.surfaceSoft
        border.width: 1
        border.color: root.borderColor

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            onPressed: {
                root.forceActiveFocus()
                if (root.bridge)
                    root.bridge.selectLayer("")
            }
        }

        Item {
            id: canvasFrame
            objectName: "scenesCanvasFrame"
            anchors.centerIn: parent
            width: Math.max(1, Math.min(parent.width - 22, (parent.height - 22) * root.outputAspect))
            height: Math.max(1, width / root.outputAspect)
            clip: true

            Rectangle { anchors.fill: parent; color: "#03070d" }

            Image {
                id: preview
                objectName: "scenesPreviewImage"
                anchors.fill: parent
                visible: root.bridge && root.bridge.previewAvailable
                source: visible ? root.bridge.previewUrl : ""
                fillMode: Image.PreserveAspectFit
                asynchronous: false
                cache: false
                smooth: true
            }

            Column {
                visible: !root.bridge || !root.bridge.previewAvailable
                anchors.centerIn: parent
                z: 2
                spacing: 7
                Image {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 22; height: 22
                    opacity: 0.62
                    source: "image://sceneicons/video/24/" + root.iconHex(root.textMuted)
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: qsTr("Waiting for the first frame")
                    color: root.textMuted
                    font.pixelSize: 10
                }
            }

            MouseArea {
                id: canvasMenuArea
                anchors.fill: parent
                z: 0
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                preventStealing: true
                onPressed: function(mouse) {
                    root.forceActiveFocus()
                    if (mouse.button === Qt.RightButton)
                        root.requestSourceMenu(canvasMenuArea, mouse.x, mouse.y)
                    else if (root.bridge)
                        root.bridge.selectLayer("")
                }
            }

            Repeater {
                model: root.bridge ? root.bridge.layersModel : null
                delegate: Item {
                    id: layerBox
                    objectName: "scenesCanvasLayer-" + layerId
                    required property int index
                    required property string layerId
                    required property string layerName
                    required property bool layerVisible
                    required property bool layerLocked
                    required property real layerX
                    required property real layerY
                    required property real layerWidth
                    required property real layerHeight
                    required property real cropLeft
                    required property real cropTop
                    required property real cropRight
                    required property real cropBottom

                    readonly property bool selected: root.bridge && root.bridge.selectedLayerId === layerId
                    property real displayX: layerX
                    property real displayY: layerY
                    property real displayWidth: layerWidth
                    property real displayHeight: layerHeight
                    property real displayCropLeft: cropLeft
                    property real displayCropTop: cropTop
                    property real displayCropRight: cropRight
                    property real displayCropBottom: cropBottom
                    property point pressCanvasPoint: Qt.point(0, 0)
                    property real pressX: 0
                    property real pressY: 0
                    property real pressWidth: 1
                    property real pressHeight: 1
                    property real pressCropLeft: 0
                    property real pressCropTop: 0
                    property real pressCropRight: 0
                    property real pressCropBottom: 0
                    property string interactionMode: ""
                    property string activeHandle: ""
                    property bool interactionActive: false
                    property bool interactionDirty: false

                    onLayerXChanged: if (!interactionActive) displayX = layerX
                    onLayerYChanged: if (!interactionActive) displayY = layerY
                    onLayerWidthChanged: if (!interactionActive) displayWidth = layerWidth
                    onLayerHeightChanged: if (!interactionActive) displayHeight = layerHeight
                    onCropLeftChanged: if (!interactionActive) displayCropLeft = cropLeft
                    onCropTopChanged: if (!interactionActive) displayCropTop = cropTop
                    onCropRightChanged: if (!interactionActive) displayCropRight = cropRight
                    onCropBottomChanged: if (!interactionActive) displayCropBottom = cropBottom

                    x: displayX * canvasFrame.width
                    y: displayY * canvasFrame.height
                    width: displayWidth * canvasFrame.width
                    height: displayHeight * canvasFrame.height
                    visible: layerVisible
                    z: 1000 - index

                    function beginInteraction(handleName, mouseArea, mouse) {
                        if (layerLocked || !root.bridge)
                            return
                        root.bridge.selectLayer(layerId)
                        interactionMode = handleName.length ? "resize" : "move"
                        activeHandle = handleName
                        interactionActive = true
                        interactionDirty = false
                        pressCanvasPoint = mouseArea.mapToItem(canvasFrame, mouse.x, mouse.y)
                        pressX = displayX
                        pressY = displayY
                        pressWidth = displayWidth
                        pressHeight = displayHeight
                        pressCropLeft = displayCropLeft
                        pressCropTop = displayCropTop
                        pressCropRight = displayCropRight
                        pressCropBottom = displayCropBottom
                    }

                    function updateInteraction(mouseArea, mouse) {
                        if (!interactionActive || !root.bridge)
                            return
                        var point = mouseArea.mapToItem(canvasFrame, mouse.x, mouse.y)
                        var dx = (point.x - pressCanvasPoint.x) / canvasFrame.width
                        var dy = (point.y - pressCanvasPoint.y) / canvasFrame.height
                        var base = {
                            layerId: layerId,
                            x: pressX,
                            y: pressY,
                            width: pressWidth,
                            height: pressHeight,
                            cropLeft: pressCropLeft,
                            cropTop: pressCropTop,
                            cropRight: pressCropRight,
                            cropBottom: pressCropBottom
                        }
                        var values = root.bridge.calculateLayerGeometry(
                            layerId, interactionMode, activeHandle, dx, dy,
                            canvasFrame.width, canvasFrame.height,
                            (mouse.modifiers & Qt.ShiftModifier) !== 0,
                            (mouse.modifiers & Qt.AltModifier) !== 0,
                            (mouse.modifiers & Qt.ControlModifier) !== 0)
                        if (!values || !values.layerId)
                            return
                        displayX = values.x
                        displayY = values.y
                        displayWidth = values.width
                        displayHeight = values.height
                        displayCropLeft = values.cropLeft
                        displayCropTop = values.cropTop
                        displayCropRight = values.cropRight
                        displayCropBottom = values.cropBottom
                        interactionDirty = true
                        root.guideX = values.guideX
                        root.guideY = values.guideY
                        root.bridge.previewLayerGeometry(values)
                    }

                    function finishInteraction(commit) {
                        if (!interactionActive)
                            return
                        var values = {
                            layerId: layerId,
                            x: displayX,
                            y: displayY,
                            width: displayWidth,
                            height: displayHeight,
                            cropLeft: displayCropLeft,
                            cropTop: displayCropTop,
                            cropRight: displayCropRight,
                            cropBottom: displayCropBottom
                        }
                        var shouldCommit = commit && interactionDirty
                        interactionActive = false
                        interactionDirty = false
                        root.guideX = -1
                        root.guideY = -1
                        if (shouldCommit)
                            root.bridge.commitLayerGeometry(values)
                        else if (!commit) {
                            displayX = layerX
                            displayY = layerY
                            displayWidth = layerWidth
                            displayHeight = layerHeight
                            displayCropLeft = cropLeft
                            displayCropTop = cropTop
                            displayCropRight = cropRight
                            displayCropBottom = cropBottom
                        }
                    }

                    Rectangle {
                        anchors.fill: parent
                        color: "transparent"
                        border.width: layerBox.selected ? 2 : hoverArea.containsMouse ? 1 : 0
                        border.color: root.accent
                    }

                    Rectangle {
                        visible: layerBox.selected
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.margins: 5
                        width: Math.min(nameLabel.implicitWidth + 12, parent.width - 10)
                        height: 23
                        radius: 6
                        color: "#c00b1320"
                        Text {
                            id: nameLabel
                            anchors.centerIn: parent
                            width: Math.min(implicitWidth, parent.width - 10)
                            text: layerBox.layerName
                            color: "white"
                            font.pixelSize: 9
                            elide: Text.ElideRight
                        }
                    }

                    MouseArea {
                        id: hoverArea
                        anchors.fill: parent
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        hoverEnabled: true
                        cursorShape: layerBox.layerLocked ? Qt.ArrowCursor
                            : layerBox.interactionActive ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                        onPressed: function(mouse) {
                            root.bridge.selectLayer(layerBox.layerId)
                            if (mouse.button === Qt.RightButton) {
                                root.requestLayerMenu(
                                    layerBox.layerId, hoverArea, mouse.x, mouse.y)
                                return
                            }
                            layerBox.beginInteraction("", hoverArea, mouse)
                        }
                        onPositionChanged: function(mouse) { layerBox.updateInteraction(hoverArea, mouse) }
                        onReleased: function(mouse) { if (mouse.button === Qt.LeftButton) layerBox.finishInteraction(true) }
                        onCanceled: layerBox.finishInteraction(false)
                    }

                    Repeater {
                        model: [
                            { name: "top_left", x: 0, y: 0, cursor: Qt.SizeFDiagCursor },
                            { name: "top", x: 0.5, y: 0, cursor: Qt.SizeVerCursor },
                            { name: "top_right", x: 1, y: 0, cursor: Qt.SizeBDiagCursor },
                            { name: "right", x: 1, y: 0.5, cursor: Qt.SizeHorCursor },
                            { name: "bottom_right", x: 1, y: 1, cursor: Qt.SizeFDiagCursor },
                            { name: "bottom", x: 0.5, y: 1, cursor: Qt.SizeVerCursor },
                            { name: "bottom_left", x: 0, y: 1, cursor: Qt.SizeBDiagCursor },
                            { name: "left", x: 0, y: 0.5, cursor: Qt.SizeHorCursor }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            visible: layerBox.selected && !layerBox.layerLocked
                            width: 9; height: 9; radius: 2
                            x: modelData.x * layerBox.width - width / 2
                            y: modelData.y * layerBox.height - height / 2
                            color: "white"
                            border.width: 1
                            border.color: root.accent
                            z: 20
                            MouseArea {
                                id: handleMouse
                                anchors.fill: parent
                                anchors.margins: -5
                                hoverEnabled: true
                                cursorShape: modelData.cursor
                                preventStealing: true
                                onPressed: function(mouse) {
                                    layerBox.beginInteraction(modelData.name, handleMouse, mouse)
                                    mouse.accepted = true
                                }
                                onPositionChanged: function(mouse) { layerBox.updateInteraction(handleMouse, mouse) }
                                onReleased: layerBox.finishInteraction(true)
                                onCanceled: layerBox.finishInteraction(false)
                            }
                        }
                    }
                }
            }

            Rectangle {
                visible: root.guideX >= 0
                x: root.guideX * canvasFrame.width
                width: 1
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                color: root.accent
                z: 3000
            }
            Rectangle {
                visible: root.guideY >= 0
                y: root.guideY * canvasFrame.height
                height: 1
                anchors.left: parent.left
                anchors.right: parent.right
                color: root.accent
                z: 3000
            }
        }
    }

    Shortcut {
        sequence: "Ctrl+R"
        enabled: root.bridge && root.bridge.selectedLayerId.length > 0
        onActivated: root.bridge.resetLayerTransform(root.bridge.selectedLayerId)
    }
    Shortcut {
        sequence: "Esc"
        enabled: root.bridge && root.bridge.selectedLayerId.length > 0
        onActivated: root.bridge.selectLayer("")
    }
}
