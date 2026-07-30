import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Effects 6.5

Item {
    id: root
    objectName: "talkThemeCanvas"
    clip: true
    focus: interactive

    property var bridge: null
    property var layerModel: bridge ? bridge.layersModel : null
    property var background: bridge ? bridge.background : ({})
    property bool interactive: true
    property bool adjustBackground: false
    readonly property bool adjustingImage:
        adjustBackground && Boolean(background.url)
    property bool finalOutput: false
    property int captureWidth: 0
    property int captureHeight: 0
    property var geometryOverrides: ({})
    property var textOverrides: ({})
    property string editingLayerId: ""
    readonly property real backgroundBlur: Math.max(
        0,
        Math.min(
            bridge ? bridge.backgroundBlurMaximum : 1,
            Number(background.blur || 0)))
    readonly property int layerCount: bridge ? bridge.layerCount : 0
    readonly property bool readyForCapture:
        !background.url || background.url === "" || backgroundImage.status === Image.Ready

    signal canvasPressed()

    function fontWeight(value) {
        if (value === "bold") return Font.Bold
        if (value === "semibold") return Font.DemiBold
        if (value === "medium") return Font.Medium
        return Font.Normal
    }

    function geometryFor(layerId, fallbackX, fallbackY, fallbackWidth, fallbackFontSize) {
        var value = geometryOverrides[layerId]
        if (value === undefined) {
            return {
                x: fallbackX,
                y: fallbackY,
                width: fallbackWidth,
                fontSize: fallbackFontSize
            }
        }
        return {
            x: value.x === undefined ? fallbackX : value.x,
            y: value.y === undefined ? fallbackY : value.y,
            width: value.width === undefined ? fallbackWidth : value.width,
            fontSize: value.fontSize === undefined ? fallbackFontSize : value.fontSize
        }
    }

    function displayedText(layerId, fallbackText) {
        var value = textOverrides[layerId]
        return value === undefined ? fallbackText : value
    }

    function setGeometryOverride(layerId, x, y, width, fontSize) {
        var next = Object.assign({}, geometryOverrides)
        next[layerId] = {
            x: Number(x),
            y: Number(y),
            width: Number(width),
            fontSize: Number(fontSize)
        }
        geometryOverrides = next
    }

    function clearGeometryOverride(layerId) {
        var next = Object.assign({}, geometryOverrides)
        delete next[layerId]
        geometryOverrides = next
    }

    function setTextOverride(layerId, value) {
        var next = Object.assign({}, textOverrides)
        next[layerId] = value
        textOverrides = next
    }

    function clearTextOverride(layerId) {
        var next = Object.assign({}, textOverrides)
        delete next[layerId]
        textOverrides = next
    }

    function beginInlineEdit(layerId) {
        if (editingLayerId !== "" && editingLayerId !== layerId)
            commitInlineEdit()
        for (var row = 0; row < layerRepeater.count; ++row) {
            var item = layerRepeater.itemAt(row)
            if (item && item.layerId === layerId) {
                item.startInlineEditing()
                return
            }
        }
        Qt.callLater(function() {
            for (var row = 0; row < layerRepeater.count; ++row) {
                var item = layerRepeater.itemAt(row)
                if (item && item.layerId === layerId) {
                    item.startInlineEditing()
                    return
                }
            }
        })
    }

    function cancelInlineEdit() {
        if (editingLayerId === "")
            return false
        for (var row = 0; row < layerRepeater.count; ++row) {
            var item = layerRepeater.itemAt(row)
            if (item && item.layerId === editingLayerId) {
                item.finishInlineEditing(false)
                return true
            }
        }
        editingLayerId = ""
        return true
    }

    function commitInlineEdit() {
        if (editingLayerId === "")
            return false
        for (var row = 0; row < layerRepeater.count; ++row) {
            var item = layerRepeater.itemAt(row)
            if (item && item.layerId === editingLayerId) {
                item.finishInlineEditing(true)
                return true
            }
        }
        editingLayerId = ""
        return false
    }

    Keys.onPressed: function(event) {
        if (!root.interactive || !root.bridge)
            return
        if (root.adjustingImage) {
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Escape) {
            if (!root.cancelInlineEdit()) {
                root.bridge.selectLayer("")
                root.canvasPressed()
            }
            event.accepted = true
            return
        }
        if (root.editingLayerId !== "")
            return
        var selectedId = root.bridge.selectedLayerId
        if (!selectedId)
            return
        if (event.key === Qt.Key_Delete || event.key === Qt.Key_Backspace) {
            root.bridge.deleteLayer(selectedId)
            event.accepted = true
            return
        }
        var step = event.modifiers & Qt.ShiftModifier ? 10 / 1920 : 1 / 1920
        if (event.key === Qt.Key_Left)
            root.bridge.nudgeLayer(selectedId, -step, 0)
        else if (event.key === Qt.Key_Right)
            root.bridge.nudgeLayer(selectedId, step, 0)
        else if (event.key === Qt.Key_Up)
            root.bridge.nudgeLayer(selectedId, 0, -step)
        else if (event.key === Qt.Key_Down)
            root.bridge.nudgeLayer(selectedId, 0, step)
        else
            return
        event.accepted = true
    }

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onEditLayerRequested(layerId) {
            root.beginInlineEdit(layerId)
        }
    }

    Rectangle {
        anchors.fill: parent
        color: root.background.base_color || "#11182a"
    }

    Item {
        id: imageFrame
        anchors.fill: parent
        clip: true

        readonly property real sourceWidth: Math.max(1, backgroundImage.implicitWidth)
        readonly property real sourceHeight: Math.max(1, backgroundImage.implicitHeight)
        readonly property real containScale: Math.min(width / sourceWidth, height / sourceHeight)
        readonly property real coverScale: Math.max(width / sourceWidth, height / sourceHeight)
        readonly property real baseScale:
            root.background.fill_mode === "contain" ? containScale : coverScale
        readonly property real zoom: Math.max(0.1, Number(root.background.zoom || 1))
        readonly property real drawnWidth: sourceWidth * baseScale * zoom
        readonly property real drawnHeight: sourceHeight * baseScale * zoom

        Image {
            id: backgroundImage
            source: root.background.url || ""
            asynchronous: false
            cache: true
            smooth: true
            mipmap: true
            fillMode: Image.Stretch
            width: imageFrame.drawnWidth
            height: imageFrame.drawnHeight
            x: (imageFrame.width - width) / 2
               + Number(root.background.norm_x || 0) * imageFrame.width
            y: (imageFrame.height - height) / 2
               + Number(root.background.norm_y || 0) * imageFrame.height
            sourceSize.width: Math.max(
                1,
                root.finalOutput && root.captureWidth > 0 ? root.captureWidth : root.width)
            sourceSize.height: Math.max(
                1,
                root.finalOutput && root.captureHeight > 0 ? root.captureHeight : root.height)
            layer.enabled: root.backgroundBlur > 0
            layer.effect: MultiEffect {
                autoPaddingEnabled: false
                blurEnabled: true
                blurMax: 32
                blur: root.backgroundBlur
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: root.background.overlay_color || "#000000"
        opacity: root.background.url ? Number(root.background.overlay_opacity || 0) : 0
    }

    MouseArea {
        id: emptyCanvasArea
        objectName: "talkThemeBackgroundPointer"
        anchors.fill: parent
        z: root.adjustingImage ? 2000 : 0
        enabled: root.interactive && !root.finalOutput
        preventStealing: true
        cursorShape: root.adjustingImage
            ? (pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor)
            : Qt.ArrowCursor
        property point pressPoint: Qt.point(0, 0)
        property real startX: 0
        property real startY: 0

        onPressed: function(mouse) {
            root.forceActiveFocus()
            if (!root.bridge)
                return
            if (root.adjustingImage) {
                pressPoint = Qt.point(mouse.x, mouse.y)
                startX = Number(root.background.norm_x || 0)
                startY = Number(root.background.norm_y || 0)
                root.bridge.beginBackgroundMove()
            } else {
                root.cancelInlineEdit()
                root.bridge.selectLayer("")
                root.canvasPressed()
            }
        }
        onPositionChanged: function(mouse) {
            if (!pressed || !root.adjustingImage || !root.bridge)
                return
            root.bridge.moveBackground(
                startX + (mouse.x - pressPoint.x) / Math.max(1, width),
                startY + (mouse.y - pressPoint.y) / Math.max(1, height))
        }
        onReleased: if (root.adjustingImage && root.bridge) root.bridge.endBackgroundMove()
        onCanceled: if (root.adjustingImage && root.bridge) root.bridge.cancelBackgroundMove()
    }

    Repeater {
        id: layerRepeater
        model: root.layerModel

        delegate: Item {
            id: layerItem
            required property int index
            required property string layerId
            required property string layerName
            required property string layerText
            required property string templateKey
            required property real layerX
            required property real layerY
            required property real layerWidth
            required property real fontSize
            required property string fontFamily
            required property string fontWeight
            required property string textColor
            required property string textAlignment
            required property bool layerVisible
            required property real letterSpacing
            required property real lineHeight
            required property string snapX
            required property string snapY

            readonly property var geometry: root.geometryFor(
                layerId, layerX, layerY, layerWidth, fontSize)
            readonly property real effectiveFontSize: Number(geometry.fontSize)
            readonly property real automaticHeight: Math.max(
                layerTextItem.implicitHeight / Math.max(1, root.height),
                effectiveFontSize * Number(lineHeight || 1.05),
                0.02)
            readonly property string value: root.displayedText(layerId, layerText)
            readonly property bool selected:
                root.interactive && !root.adjustingImage && root.bridge
                && root.bridge.selectedLayerId === layerId
            readonly property bool editing: root.editingLayerId === layerId
            readonly property real centeredEditY: Math.max(
                0,
                Math.min(
                    Math.max(0, 1 - automaticHeight),
                    inlineEditCenterY - automaticHeight / 2))
            property bool moving: false
            property bool resizing: false
            property point pointerOrigin: Qt.point(0, 0)
            property real originX: 0
            property real originY: 0
            property real inlineEditCenterY: 0
            property string activeSnapX: ""
            property string activeSnapY: ""
            property string lockedAxis: ""
            property bool finishingEdit: false

            x: Number(geometry.x) * root.width
            y: Number(editing ? centeredEditY : geometry.y) * root.height
            width: Math.max(1, Number(geometry.width) * root.width)
            height: Math.max(1, automaticHeight * root.height)
            z: root.layerCount - index
            visible: Boolean(layerVisible) && (value.length > 0 || selected || editing)

            function reportRenderedHeight() {
                if (root.bridge && root.interactive && !root.finalOutput)
                    root.bridge.setLayerRenderedHeight(layerId, height / Math.max(1, root.height))
            }

            onHeightChanged: reportRenderedHeight()
            onValueChanged: Qt.callLater(reportRenderedHeight)
            Component.onCompleted: reportRenderedHeight()

            function startInlineEditing() {
                if (!root.interactive || root.adjustingImage || !root.bridge || !layerVisible)
                    return
                root.bridge.selectLayer(layerId)
                finishingEdit = false
                inlineEditor.text = layerText
                inlineEditCenterY = Number(geometry.y) + automaticHeight / 2
                root.editingLayerId = layerId
                root.setTextOverride(layerId, inlineEditor.text)
                inlineEditor.forceActiveFocus()
                inlineEditor.selectAll()
            }

            function finishInlineEditing(commit) {
                if (!editing || finishingEdit)
                    return
                finishingEdit = true
                var resultText = inlineEditor.text
                var resultY = centeredEditY
                root.clearTextOverride(layerId)
                root.editingLayerId = ""
                if (commit && root.bridge)
                    root.bridge.commitInlineText(layerId, resultText, resultY)
                finishingEdit = false
                root.forceActiveFocus()
            }

            Text {
                id: layerTextItem
                width: parent.width
                text: layerItem.value
                color: layerItem.textColor || "#FFFFFFFF"
                font.family: layerItem.fontFamily || "Arial"
                font.pixelSize: Math.max(8, layerItem.effectiveFontSize * root.height)
                font.weight: root.fontWeight(layerItem.fontWeight)
                font.letterSpacing: Number(layerItem.letterSpacing || 0)
                horizontalAlignment: layerItem.textAlignment === "center"
                    ? Text.AlignHCenter
                    : layerItem.textAlignment === "right" ? Text.AlignRight : Text.AlignLeft
                wrapMode: Text.Wrap
                lineHeightMode: Text.ProportionalHeight
                lineHeight: Number(layerItem.lineHeight || 1.05)
                renderType: root.finalOutput ? Text.QtRendering : Text.NativeRendering
                visible: !layerItem.editing || root.finalOutput
            }

            TextEdit {
                id: inlineEditor
                anchors.fill: parent
                clip: true
                visible: layerItem.editing && root.interactive && !root.finalOutput
                color: layerItem.textColor || "#FFFFFFFF"
                selectionColor: root.bridge ? "#7367f0" : "#7367f0"
                selectedTextColor: "#ffffff"
                font.family: layerItem.fontFamily || "Arial"
                font.pixelSize: Math.max(8, layerItem.effectiveFontSize * root.height)
                font.weight: root.fontWeight(layerItem.fontWeight)
                font.letterSpacing: Number(layerItem.letterSpacing || 0)
                horizontalAlignment: layerItem.textAlignment === "center"
                    ? Text.AlignHCenter
                    : layerItem.textAlignment === "right" ? Text.AlignRight : Text.AlignLeft
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                onTextChanged: {
                    if (layerItem.editing && !layerItem.finishingEdit)
                        root.setTextOverride(layerItem.layerId, text)
                }
                onActiveFocusChanged: {
                    if (!activeFocus && layerItem.editing && !layerItem.finishingEdit)
                        layerItem.finishInlineEditing(true)
                }
                Keys.onPressed: function(event) {
                    if (event.key === Qt.Key_Escape) {
                        layerItem.finishInlineEditing(false)
                        event.accepted = true
                    } else if ((event.modifiers & Qt.ControlModifier)
                               && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                        layerItem.finishInlineEditing(true)
                        event.accepted = true
                    }
                }

            }

            Rectangle {
                anchors.fill: parent
                anchors.margins: -6
                visible: layerItem.selected && !root.finalOutput
                color: "transparent"
                border.width: 1
                border.color: "#9b92ff"
                radius: 5
            }

            Repeater {
                model: [
                    { horizontal: -1, vertical: -1 },
                    { horizontal: 0, vertical: -1 },
                    { horizontal: 1, vertical: -1 },
                    { horizontal: -1, vertical: 0 },
                    { horizontal: 1, vertical: 0 },
                    { horizontal: -1, vertical: 1 },
                    { horizontal: 0, vertical: 1 },
                    { horizontal: 1, vertical: 1 }
                ]
                delegate: Item {
                    id: resizeHandle
                    objectName: "talkThemeResizeHandle-" + layerItem.layerId
                                + "-" + horizontal + "-" + vertical
                    required property var modelData
                    readonly property int horizontal: Number(modelData.horizontal)
                    readonly property int vertical: Number(modelData.vertical)
                    property point pressPoint: Qt.point(0, 0)
                    property real originX: 0
                    property real originY: 0
                    property real originWidth: 0
                    property real originHeight: 0
                    property real originFontSize: 0

                    visible: layerItem.selected && !layerItem.editing && !root.finalOutput
                    z: 100
                    width: 18
                    height: 18
                    x: horizontal < 0 ? -9
                       : horizontal > 0 ? layerItem.width - 9 : layerItem.width / 2 - 9
                    y: vertical < 0 ? -9
                       : vertical > 0 ? layerItem.height - 9 : layerItem.height / 2 - 9

                    Rectangle {
                        anchors.centerIn: parent
                        width: 8
                        height: 8
                        radius: 2
                        color: "#ffffff"
                        border.width: 2
                        border.color: "#7367f0"
                    }

                    MouseArea {
                        id: resizePointer
                        anchors.fill: parent
                        enabled: resizeHandle.visible && root.interactive
                        acceptedButtons: Qt.LeftButton
                        hoverEnabled: true
                        preventStealing: true
                        cursorShape: resizeHandle.horizontal === 0 ? Qt.SizeVerCursor
                            : resizeHandle.vertical === 0 ? Qt.SizeHorCursor
                            : resizeHandle.horizontal === resizeHandle.vertical
                                ? Qt.SizeFDiagCursor : Qt.SizeBDiagCursor

                        onPressed: function(mouse) {
                            if (!root.bridge)
                                return
                            mouse.accepted = true
                            root.forceActiveFocus()
                            var point = mapToItem(root, mouse.x, mouse.y)
                            resizeHandle.pressPoint = Qt.point(point.x, point.y)
                            resizeHandle.originX = Number(layerItem.geometry.x)
                            resizeHandle.originY = Number(layerItem.geometry.y)
                            resizeHandle.originWidth = Number(layerItem.geometry.width)
                            resizeHandle.originHeight = layerItem.height / Math.max(1, root.height)
                            resizeHandle.originFontSize = layerItem.effectiveFontSize
                            layerItem.resizing = true
                            root.bridge.beginLayerResize(layerItem.layerId)
                        }

                        onPositionChanged: function(mouse) {
                            if (!pressed || !layerItem.resizing)
                                return
                            var point = mapToItem(root, mouse.x, mouse.y)
                            var dxPixels = point.x - resizeHandle.pressPoint.x
                            var dyPixels = point.y - resizeHandle.pressPoint.y

                            if (resizeHandle.vertical === 0) {
                                var dx = dxPixels / Math.max(1, root.width)
                                var newX = resizeHandle.originX
                                var newWidth = resizeHandle.originWidth
                                if (resizeHandle.horizontal < 0) {
                                    var right = resizeHandle.originX + resizeHandle.originWidth
                                    newX = Math.max(0, Math.min(
                                        right - 0.08, resizeHandle.originX + dx))
                                    newWidth = right - newX
                                } else {
                                    newWidth = Math.max(0.08, Math.min(
                                        1 - resizeHandle.originX,
                                        resizeHandle.originWidth + dx))
                                }
                                root.setGeometryOverride(
                                    layerItem.layerId,
                                    newX,
                                    resizeHandle.originY,
                                    newWidth,
                                    resizeHandle.originFontSize)
                                return
                            }

                            var originWidthPixels = resizeHandle.originWidth * root.width
                            var originHeightPixels = resizeHandle.originHeight * root.height
                            var scale
                            if (resizeHandle.horizontal === 0) {
                                scale = 1 + resizeHandle.vertical * dyPixels
                                    / Math.max(1, originHeightPixels)
                            } else {
                                var projectedDelta = resizeHandle.horizontal * dxPixels
                                    * originWidthPixels
                                    + resizeHandle.vertical * dyPixels * originHeightPixels
                                var diagonalLengthSquared = originWidthPixels * originWidthPixels
                                    + originHeightPixels * originHeightPixels
                                scale = 1 + projectedDelta
                                    / Math.max(1, diagonalLengthSquared)
                            }

                            var minimumScale = Math.max(
                                0.08 / resizeHandle.originWidth,
                                0.02 / resizeHandle.originHeight,
                                0.012 / resizeHandle.originFontSize)
                            var maximumWidth
                            if (resizeHandle.horizontal < 0) {
                                maximumWidth = resizeHandle.originX + resizeHandle.originWidth
                            } else if (resizeHandle.horizontal > 0) {
                                maximumWidth = 1 - resizeHandle.originX
                            } else {
                                var centerX = resizeHandle.originX
                                    + resizeHandle.originWidth / 2
                                maximumWidth = 2 * Math.min(centerX, 1 - centerX)
                            }
                            var maximumHeight = resizeHandle.vertical < 0
                                ? resizeHandle.originY + resizeHandle.originHeight
                                : 1 - resizeHandle.originY
                            var maximumScale = Math.min(
                                maximumWidth / resizeHandle.originWidth,
                                maximumHeight / resizeHandle.originHeight,
                                0.30 / resizeHandle.originFontSize)
                            scale = Math.max(minimumScale, Math.min(maximumScale, scale))

                            var scaledWidth = resizeHandle.originWidth * scale
                            var scaledHeight = resizeHandle.originHeight * scale
                            var scaledFontSize = resizeHandle.originFontSize * scale
                            var scaledX
                            if (resizeHandle.horizontal < 0) {
                                scaledX = resizeHandle.originX
                                    + resizeHandle.originWidth - scaledWidth
                            } else if (resizeHandle.horizontal > 0) {
                                scaledX = resizeHandle.originX
                            } else {
                                scaledX = resizeHandle.originX
                                    + (resizeHandle.originWidth - scaledWidth) / 2
                            }
                            var scaledY = resizeHandle.vertical < 0
                                ? resizeHandle.originY
                                    + resizeHandle.originHeight - scaledHeight
                                : resizeHandle.originY
                            root.setGeometryOverride(
                                layerItem.layerId,
                                scaledX,
                                scaledY,
                                scaledWidth,
                                scaledFontSize)
                        }

                        onReleased: finishResize()
                        onCanceled: cancelResize()

                        function finishResize() {
                            if (!layerItem.resizing || !root.bridge)
                                return
                            var current = layerItem.geometry
                            layerItem.resizing = false
                            root.bridge.endLayerResize(
                                layerItem.layerId,
                                Number(current.x),
                                Number(current.y),
                                Number(current.width),
                                Number(current.fontSize),
                                layerItem.height / Math.max(1, root.height))
                            root.clearGeometryOverride(layerItem.layerId)
                        }

                        function cancelResize() {
                            if (!layerItem.resizing)
                                return
                            layerItem.resizing = false
                            root.clearGeometryOverride(layerItem.layerId)
                            if (root.bridge)
                                root.bridge.cancelLayerResize()
                        }
                    }
                }
            }

            MouseArea {
                id: layerPointer
                objectName: "talkThemeLayerPointer-" + layerItem.layerId
                property bool nativeCursorRegistered: false
                property int registeredCursorShape: -1
                z: 10
                anchors.fill: parent
                anchors.margins: -8
                enabled: root.interactive && !root.finalOutput
                         && !root.adjustingImage
                         && !layerItem.resizing
                acceptedButtons: layerItem.editing ? Qt.NoButton : Qt.LeftButton
                hoverEnabled: true
                cursorShape: layerItem.editing
                    ? Qt.IBeamCursor
                    : (pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor)

                function syncNativeCursor() {
                    var shouldRegister = enabled && containsMouse && root.bridge !== null
                    var shape = Number(cursorShape)
                    if (shouldRegister
                            && (!nativeCursorRegistered || registeredCursorShape !== shape)) {
                        root.bridge.beginPointer(shape)
                        nativeCursorRegistered = true
                        registeredCursorShape = shape
                    } else if (!shouldRegister && nativeCursorRegistered) {
                        root.bridge.endPointer()
                        nativeCursorRegistered = false
                        registeredCursorShape = -1
                    }
                }

                onEntered: syncNativeCursor()
                onExited: syncNativeCursor()
                onContainsMouseChanged: syncNativeCursor()
                onEnabledChanged: syncNativeCursor()
                onCursorShapeChanged: syncNativeCursor()
                Component.onDestruction: {
                    if (nativeCursorRegistered && root.bridge !== null)
                        root.bridge.endPointer()
                }

                onPressed: function(mouse) {
                    if (!root.bridge)
                        return
                    root.forceActiveFocus()
                    root.bridge.selectLayer(layerItem.layerId)
                    var point = mapToItem(root, mouse.x, mouse.y)
                    layerItem.pointerOrigin = Qt.point(point.x, point.y)
                    layerItem.originX = Number(layerItem.geometry.x)
                    layerItem.originY = Number(layerItem.geometry.y)
                    layerItem.activeSnapX = layerItem.snapX
                    layerItem.activeSnapY = layerItem.snapY
                    layerItem.lockedAxis = ""
                    layerItem.moving = true
                    root.bridge.beginLayerMove(layerItem.layerId)
                }
                onPositionChanged: function(mouse) {
                    if (!pressed || !layerItem.moving || !root.bridge)
                        return
                    var point = mapToItem(root, mouse.x, mouse.y)
                    var dx = point.x - layerItem.pointerOrigin.x
                    var dy = point.y - layerItem.pointerOrigin.y
                    if ((mouse.modifiers & Qt.ShiftModifier) && layerItem.lockedAxis === "")
                        layerItem.lockedAxis = Math.abs(dx) >= Math.abs(dy) ? "y" : "x"
                    if (!(mouse.modifiers & Qt.ShiftModifier))
                        layerItem.lockedAxis = ""
                    var candidateX = layerItem.originX + dx / Math.max(1, root.width)
                    var candidateY = layerItem.originY + dy / Math.max(1, root.height)
                    var result = root.bridge.moveLayer(
                        layerItem.layerId,
                        candidateX,
                        candidateY,
                        Number(layerItem.geometry.width),
                        layerItem.automaticHeight,
                        root.width,
                        root.height,
                        layerItem.activeSnapX,
                        layerItem.activeSnapY,
                        Boolean(mouse.modifiers & Qt.AltModifier),
                        layerItem.lockedAxis,
                        layerItem.originX,
                        layerItem.originY)
                    if (!result || result.x === undefined)
                        return
                    layerItem.activeSnapX = result.snap_x || ""
                    layerItem.activeSnapY = result.snap_y || ""
                    root.setGeometryOverride(
                        layerItem.layerId,
                        result.x,
                        result.y,
                        layerItem.geometry.width,
                        layerItem.geometry.fontSize)
                }
                onReleased: finishMove()
                onCanceled: cancelMove()
                onDoubleClicked: function(mouse) {
                    mouse.accepted = true
                    layerItem.startInlineEditing()
                }

                function finishMove() {
                    if (!layerItem.moving || !root.bridge)
                        return
                    var current = root.geometryFor(
                        layerItem.layerId,
                        layerItem.layerX,
                        layerItem.layerY,
                        layerItem.layerWidth,
                        layerItem.fontSize)
                    layerItem.moving = false
                    root.bridge.endLayerMove(
                        layerItem.layerId,
                        Number(current.x),
                        Number(current.y),
                        layerItem.activeSnapX,
                        layerItem.activeSnapY)
                    root.clearGeometryOverride(layerItem.layerId)
                }

                function cancelMove() {
                    if (!layerItem.moving)
                        return
                    layerItem.moving = false
                    root.clearGeometryOverride(layerItem.layerId)
                    if (root.bridge)
                        root.bridge.cancelLayerMove()
                }
            }
        }
    }

    Repeater {
        model: root.finalOutput || !root.bridge ? [] : root.bridge.snapGuides
        delegate: Item {
            id: guideItem
            required property var modelData
            z: 1000
            readonly property bool vertical: modelData.axis === "x"
            x: vertical ? Number(modelData.position) * root.width : 0
            y: vertical ? 0 : Number(modelData.position) * root.height
            width: vertical ? 1 : root.width
            height: vertical ? root.height : 1

            Rectangle {
                anchors.fill: parent
                color: "#b8b2ff"
                opacity: 0.9
            }

            Rectangle {
                visible: Boolean(modelData.label)
                x: guideItem.vertical
                   ? Math.max(5, Math.min(root.width - width - 5, -width / 2)) : 7
                y: guideItem.vertical ? 7
                   : Math.max(5, Math.min(root.height - height - 5, -height / 2))
                width: guideLabel.implicitWidth + 14
                height: 24
                radius: 7
                color: "#7367f0"
                Text {
                    id: guideLabel
                    anchors.centerIn: parent
                    text: modelData.label || ""
                    color: "white"
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                }
            }
        }
    }
}
