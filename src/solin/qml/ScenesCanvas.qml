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
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"
    readonly property color warning: theme ? theme.warning : "#d29922"
    readonly property real outputAspect: bridge && bridge.outputHeight > 0
        ? bridge.outputWidth / bridge.outputHeight : 16 / 9
    property real guideX: -1
    property real guideY: -1
    property var framingDraft: ({})
    readonly property bool hasSelectedLayer: bridge && bridge.selectedLayerId.length > 0
    readonly property bool framingAvailable: hasSelectedLayer
        && bridge.selectedLayer.framing_available === true

    function iconHex(colorValue) { return String(colorValue).replace("#", "") }

    function acceptFramingDraft(values) {
        if (!values || !values.layerId)
            return false
        framingDraft = values
        return true
    }

    function beginFraming() {
        if (!framingAvailable)
            return
        if (acceptFramingDraft(bridge.beginLayerFraming(bridge.selectedLayerId)))
            forceActiveFocus()
    }

    function updateFraming(values) {
        if (bridge && bridge.framingActive)
            acceptFramingDraft(bridge.updateLayerFraming(values))
    }

    function commitFraming() {
        if (!bridge || !bridge.framingActive)
            return
        bridge.commitLayerFraming()
        if (!bridge.framingActive)
            framingDraft = ({})
        forceActiveFocus()
    }

    function cancelFraming() {
        if (bridge && bridge.framingActive)
            bridge.cancelLayerFraming()
        framingDraft = ({})
        forceActiveFocus()
    }

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onDocumentGenerationChanged() {
            root.guideX = -1
            root.guideY = -1
        }
        function onFramingChanged() {
            if (!root.bridge.framingActive)
                root.framingDraft = ({})
        }
    }

    Keys.onPressed: function(event) {
        if (root.bridge && root.bridge.framingActive) {
            var step = (event.modifiers & Qt.ShiftModifier) !== 0 ? 10 : 1
            if (event.key === Qt.Key_Left) {
                root.updateFraming({ operation: "move", dx: -step / canvasFrame.width, dy: 0 })
            } else if (event.key === Qt.Key_Right) {
                root.updateFraming({ operation: "move", dx: step / canvasFrame.width, dy: 0 })
            } else if (event.key === Qt.Key_Up) {
                root.updateFraming({ operation: "move", dx: 0, dy: -step / canvasFrame.height })
            } else if (event.key === Qt.Key_Down) {
                root.updateFraming({ operation: "move", dx: 0, dy: step / canvasFrame.height })
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.commitFraming()
            } else if (event.key === Qt.Key_Escape) {
                root.cancelFraming()
            } else {
                return
            }
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_F && event.modifiers === Qt.NoModifier
                && root.framingAvailable) {
            root.beginFraming()
            event.accepted = true
        } else if (event.key === Qt.Key_Escape && root.hasSelectedLayer) {
            root.bridge.selectLayer("")
            event.accepted = true
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

            // Report this frame's geometry (in the QQuickWidget's root coords) so a
            // native GPU preview surface can be pinned exactly over it.
            function _reportRect() {
                if (root.bridge) {
                    var p = mapToItem(null, 0, 0)
                    root.bridge.reportCanvasRect(p.x, p.y, width, height)
                }
            }
            onXChanged: _reportRect()
            onYChanged: _reportRect()
            onWidthChanged: _reportRect()
            onHeightChanged: _reportRect()
            Component.onCompleted: _reportRect()

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
                    required property string sourceKind
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
                    required property bool sourceWarning
                    required property string sourceWarningText

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

                    // Idle publishes a transparent content frame, so this layer
                    // draws nothing — and an empty rectangle reads the same as a
                    // broken source. A faded glyph says "waiting for media".
                    Image {
                        objectName: "scenesCanvasContentIdle-" + layerBox.layerId
                        visible: layerBox.sourceKind === "solin_content"
                            && root.bridge && root.bridge.contentIdle
                        anchors.centerIn: parent
                        width: Math.max(
                            14,
                            Math.min(46, parent.width / 6, parent.height / 4))
                        height: width
                        opacity: 0.35
                        smooth: true
                        fillMode: Image.PreserveAspectFit
                        source: "image://sceneicons/video/96/"
                            + root.iconHex(root.textMuted)
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

                    Rectangle {
                        id: sourceWarningBadge
                        objectName: "scenesCanvasSourceWarning-" + layerBox.layerId
                        visible: layerBox.sourceWarning
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 6
                        width: 24
                        height: 24
                        radius: 12
                        color: Qt.rgba(
                            root.warning.r, root.warning.g, root.warning.b, 0.16)
                        border.width: 1
                        border.color: Qt.rgba(
                            root.warning.r, root.warning.g, root.warning.b, 0.38)
                        z: 10

                        Image {
                            anchors.centerIn: parent
                            width: 14
                            height: 14
                            source: "image://sceneicons/warning/16/"
                                + root.iconHex(root.warning)
                        }

                        MouseArea {
                            id: sourceWarningMouse
                            anchors.fill: parent
                            hoverEnabled: true
                        }
                        ToolTip.visible: sourceWarningMouse.containsMouse
                        ToolTip.text: layerBox.sourceWarningText
                        ToolTip.delay: 350
                    }

                    MouseArea {
                        id: hoverArea
                        anchors.fill: parent
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        hoverEnabled: true
                        cursorShape: layerBox.layerLocked ? Qt.ArrowCursor
                            : layerBox.interactionActive ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                        enabled: !root.bridge || !root.bridge.framingActive
                        onPressed: function(mouse) {
                            root.forceActiveFocus()
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
                                && (!root.bridge || !root.bridge.framingActive)
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

            Item {
                id: framingOverlay
                objectName: "scenesFramingOverlay"
                anchors.fill: parent
                visible: root.bridge && root.bridge.framingActive
                    && String(root.framingDraft.layerId || "").length > 0
                enabled: visible
                z: 4000

                readonly property real frameX: Number(root.framingDraft.x || 0)
                    * canvasFrame.width
                readonly property real frameY: Number(root.framingDraft.y || 0)
                    * canvasFrame.height
                readonly property real frameWidth: Number(root.framingDraft.width || 0)
                    * canvasFrame.width
                readonly property real frameHeight: Number(root.framingDraft.height || 0)
                    * canvasFrame.height

                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.AllButtons
                    onPressed: function(mouse) {
                        root.forceActiveFocus()
                        mouse.accepted = true
                    }
                }

                Rectangle {
                    x: 0; y: 0; width: parent.width; height: framingOverlay.frameY
                    color: "#9902070d"
                }
                Rectangle {
                    x: 0; y: framingOverlay.frameY
                    width: framingOverlay.frameX; height: framingOverlay.frameHeight
                    color: "#9902070d"
                }
                Rectangle {
                    x: framingOverlay.frameX + framingOverlay.frameWidth
                    y: framingOverlay.frameY
                    width: Math.max(0, parent.width - x)
                    height: framingOverlay.frameHeight
                    color: "#9902070d"
                }
                Rectangle {
                    x: 0; y: framingOverlay.frameY + framingOverlay.frameHeight
                    width: parent.width; height: Math.max(0, parent.height - y)
                    color: "#9902070d"
                }

                Item {
                    id: framingBox
                    objectName: "scenesFramingBox"
                    x: framingOverlay.frameX
                    y: framingOverlay.frameY
                    width: framingOverlay.frameWidth
                    height: framingOverlay.frameHeight
                    z: 2

                    Rectangle {
                        anchors.fill: parent
                        color: "transparent"
                        border.width: 2
                        border.color: root.accent
                    }

                    Repeater {
                        model: [1 / 3, 2 / 3]
                        Rectangle {
                            required property real modelData
                            x: modelData * framingBox.width
                            width: 1
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            color: "#99ffffff"
                        }
                    }
                    Repeater {
                        model: [1 / 3, 2 / 3]
                        Rectangle {
                            required property real modelData
                            y: modelData * framingBox.height
                            height: 1
                            anchors.left: parent.left
                            anchors.right: parent.right
                            color: "#99ffffff"
                        }
                    }

                    MouseArea {
                        id: framingMoveArea
                        anchors.fill: parent
                        hoverEnabled: true
                        acceptedButtons: Qt.LeftButton
                        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                        property point lastCanvasPoint: Qt.point(0, 0)
                        onPressed: function(mouse) {
                            root.forceActiveFocus()
                            lastCanvasPoint = mapToItem(canvasFrame, mouse.x, mouse.y)
                            mouse.accepted = true
                        }
                        onPositionChanged: function(mouse) {
                            if ((mouse.buttons & Qt.LeftButton) === 0)
                                return
                            var point = mapToItem(canvasFrame, mouse.x, mouse.y)
                            root.updateFraming({
                                operation: "move",
                                dx: (point.x - lastCanvasPoint.x) / canvasFrame.width,
                                dy: (point.y - lastCanvasPoint.y) / canvasFrame.height
                            })
                            lastCanvasPoint = point
                        }
                        onWheel: function(wheel) {
                            var anchor = mapToItem(canvasFrame, wheel.x, wheel.y)
                            root.updateFraming({
                                operation: "scale",
                                scale: Math.exp(-wheel.angleDelta.y / 1200),
                                anchorX: anchor.x / canvasFrame.width,
                                anchorY: anchor.y / canvasFrame.height
                            })
                            wheel.accepted = true
                        }
                    }

                    Repeater {
                        model: [
                            { name: "top_left", x: 0, y: 0, cursor: Qt.SizeFDiagCursor },
                            { name: "top_right", x: 1, y: 0, cursor: Qt.SizeBDiagCursor },
                            { name: "bottom_right", x: 1, y: 1, cursor: Qt.SizeFDiagCursor },
                            { name: "bottom_left", x: 0, y: 1, cursor: Qt.SizeBDiagCursor }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            width: 12; height: 12; radius: 3
                            x: modelData.x * framingBox.width - width / 2
                            y: modelData.y * framingBox.height - height / 2
                            color: "white"
                            border.width: 2
                            border.color: root.accent
                            z: 4
                            MouseArea {
                                id: framingHandleArea
                                anchors.fill: parent
                                anchors.margins: -6
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton
                                cursorShape: modelData.cursor
                                preventStealing: true
                                property point lastCanvasPoint: Qt.point(0, 0)
                                onPressed: function(mouse) {
                                    root.forceActiveFocus()
                                    lastCanvasPoint = mapToItem(canvasFrame, mouse.x, mouse.y)
                                    mouse.accepted = true
                                }
                                onPositionChanged: function(mouse) {
                                    if ((mouse.buttons & Qt.LeftButton) === 0)
                                        return
                                    var point = mapToItem(canvasFrame, mouse.x, mouse.y)
                                    root.updateFraming({
                                        operation: "resize",
                                        handle: modelData.name,
                                        dx: (point.x - lastCanvasPoint.x) / canvasFrame.width,
                                        dy: (point.y - lastCanvasPoint.y) / canvasFrame.height
                                    })
                                    lastCanvasPoint = point
                                }
                            }
                        }
                    }
                }
            }

            Rectangle {
                visible: root.guideX >= 0 && (!root.bridge || !root.bridge.framingActive)
                x: root.guideX * canvasFrame.width
                width: 1
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                color: root.accent
                z: 3000
            }
            Rectangle {
                visible: root.guideY >= 0 && (!root.bridge || !root.bridge.framingActive)
                y: root.guideY * canvasFrame.height
                height: 1
                anchors.left: parent.left
                anchors.right: parent.right
                color: root.accent
                z: 3000
            }
        }

        Rectangle {
            id: framingToolbar
            objectName: "scenesFramingToolbar"
            visible: root.hasSelectedLayer
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 12
            width: Math.min(parent.width - 24, framingActions.implicitWidth + 8)
            height: 38
            radius: 11
            color: "#ed111927"
            border.width: 1
            border.color: root.bridge && root.bridge.framingActive
                ? root.accent : root.borderColor
            z: 6000

            Row {
                id: framingActions
                anchors.centerIn: parent
                spacing: 5

                CanvasActionButton {
                    objectName: "scenesBeginFramingButton"
                    visible: !root.bridge || !root.bridge.framingActive
                    iconName: "crop"
                    label: qsTr("Frame…")
                    showLabel: root.width >= 520
                    actionEnabled: root.framingAvailable
                    toolTipText: actionEnabled
                        ? qsTr("Frame and fill the canvas (F)")
                        : root.bridge
                            ? root.bridge.selectedLayer.framing_unavailable_reason : ""
                    onClicked: root.beginFraming()
                }

                CanvasActionButton {
                    objectName: "scenesCancelFramingButton"
                    visible: root.bridge && root.bridge.framingActive
                    iconName: "close"
                    label: qsTr("Cancel")
                    showLabel: root.width >= 620
                    toolTipText: qsTr("Cancel framing (Esc)")
                    onClicked: root.cancelFraming()
                }

                Text {
                    visible: root.bridge && root.bridge.framingActive && root.width >= 720
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Drag to compose · Scroll to zoom")
                    color: root.textMuted
                    font.pixelSize: 9
                    leftPadding: 5
                    rightPadding: 5
                }

                CanvasActionButton {
                    objectName: "scenesCommitFramingButton"
                    visible: root.bridge && root.bridge.framingActive
                    iconName: "check"
                    label: qsTr("Apply")
                    showLabel: root.width >= 480
                    accentButton: true
                    toolTipText: qsTr("Apply framing (Enter)")
                    onClicked: root.commitFraming()
                }
            }
        }
    }

    component CanvasActionButton: Rectangle {
        id: actionButton
        property string iconName: ""
        property string label: ""
        property string toolTipText: ""
        property bool showLabel: true
        property bool accentButton: false
        property bool actionEnabled: true
        signal clicked()

        implicitWidth: showLabel ? actionLabel.implicitWidth + 42 : 30
        width: implicitWidth
        height: 30
        radius: 8
        opacity: actionEnabled ? 1.0 : 0.45
        color: accentButton
            ? root.accent
            : actionMouse.pressed
                ? root.borderColor
                : actionMouse.containsMouse ? root.hover : "transparent"

        Image {
            id: actionIcon
            width: 15; height: 15
            anchors.left: parent.left
            anchors.leftMargin: actionButton.showLabel ? 10 : 7.5
            anchors.verticalCenter: parent.verticalCenter
            source: "image://sceneicons/" + actionButton.iconName + "/16/"
                + root.iconHex(actionButton.accentButton ? "white" : root.textSecondary)
        }
        Text {
            id: actionLabel
            visible: actionButton.showLabel
            anchors.left: actionIcon.right
            anchors.leftMargin: 7
            anchors.verticalCenter: parent.verticalCenter
            text: actionButton.label
            color: actionButton.accentButton ? "white" : root.textPrimary
            font.pixelSize: 10
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: actionMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: actionButton.actionEnabled
                ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: {
                if (actionButton.actionEnabled)
                    actionButton.clicked()
            }
        }
        ToolTip.visible: actionMouse.containsMouse && actionButton.toolTipText.length > 0
        ToolTip.text: actionButton.toolTipText
        ToolTip.delay: 350
    }

    Shortcut {
        sequence: "Ctrl+R"
        enabled: root.bridge && root.bridge.selectedLayerId.length > 0
            && !root.bridge.framingActive
        onActivated: root.bridge.resetLayerTransform(root.bridge.selectedLayerId)
    }
}
