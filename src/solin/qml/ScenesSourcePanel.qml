pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    objectName: "scenesSourcePanel"
    property var bridge: null
    property var theme: null
    property bool embedded: false
    property bool showHeader: true
    signal requestAddSource(var anchor)
    signal requestLayerMenu(string layerId, var anchor, real x, real y)
    signal requestPtz()

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color accentTint: theme ? theme.accentTint : "#17335b"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"
    readonly property int rowHeight: 58
    readonly property int rowSpacing: 7
    readonly property int rowSlot: rowHeight + rowSpacing
    readonly property string dragCursorSource: "source-reorder"
    property bool dragActive: false
    property string draggedId: ""
    property string draggedName: ""
    property bool draggedVisible: true
    property bool draggedLocked: false
    property int dragOriginIndex: -1
    property int dragTargetIndex: -1
    property real dragPointerY: 0
    property real dragGrabOffsetY: 0
    property real dragGhostY: 0

    color: embedded ? "transparent" : surface
    radius: embedded ? 0 : 14
    border.width: embedded ? 0 : 1
    border.color: borderColor
    clip: true

    function iconHex(colorValue) { return String(colorValue).replace("#", "") }

    function beginDrag(row, pointerY, grabOffsetY) {
        if (!row || dragActive || !bridge)
            return
        bridge.selectLayer(row.layerId)
        draggedId = row.layerId
        draggedName = row.layerName
        draggedVisible = row.layerVisible
        draggedLocked = row.layerLocked
        dragOriginIndex = row.index
        dragPointerY = pointerY
        dragGrabOffsetY = grabOffsetY
        dragGhostY = boundedGhostY(pointerY, grabOffsetY)
        dragTargetIndex = targetIndex(pointerY)
        dragActive = true
        bridge.beginPointerOverride(
            dragCursorSource, Number(Qt.ClosedHandCursor))
    }

    function boundedGhostY(pointerY, grabOffsetY) {
        var top = sourceList.mapToItem(root, 0, 0).y
        return Math.max(top, Math.min(top + sourceList.height - rowHeight,
            pointerY - grabOffsetY))
    }

    function targetIndex(pointerY) {
        if (sourceList.count <= 0)
            return -1
        var viewport = sourceList.mapFromItem(root, 0, pointerY)
        var content = sourceList.contentItem.mapFromItem(sourceList, 0, viewport.y)
        return Math.max(0, Math.min(sourceList.count - 1,
            Math.round((content.y - dragGrabOffsetY) / rowSlot)))
    }

    function updateDrag(pointerY) {
        if (!dragActive)
            return
        dragPointerY = pointerY
        dragGhostY = boundedGhostY(pointerY, dragGrabOffsetY)
        dragTargetIndex = targetIndex(pointerY)
    }

    function finishDrag() {
        if (!dragActive)
            return
        var id = draggedId
        var origin = dragOriginIndex
        var target = dragTargetIndex
        cancelDrag()
        if (bridge && id && origin !== target)
            bridge.reorderLayer(id, target)
    }

    function cancelDrag() {
        var wasActive = dragActive
        dragActive = false
        draggedId = ""
        draggedName = ""
        dragOriginIndex = -1
        dragTargetIndex = -1
        dragPointerY = 0
        dragGrabOffsetY = 0
        dragGhostY = 0
        if (wasActive && bridge)
            bridge.endPointerOverride(dragCursorSource)
    }

    function previewOffset(index, id) {
        if (!dragActive || id === draggedId)
            return 0
        if (dragOriginIndex < dragTargetIndex && index > dragOriginIndex && index <= dragTargetIndex)
            return -rowSlot
        if (dragOriginIndex > dragTargetIndex && index >= dragTargetIndex && index < dragOriginIndex)
            return rowSlot
        return 0
    }

    onVisibleChanged: if (!visible) cancelDrag()
    Shortcut { sequence: "Esc"; enabled: root.dragActive; onActivated: root.cancelDrag() }

    Timer {
        interval: 16
        repeat: true
        running: root.dragActive
        onTriggered: {
            var point = sourceList.mapFromItem(root, 0, root.dragPointerY)
            var edge = 28
            var delta = point.y < edge ? -Math.ceil((edge - point.y) / 5)
                : point.y > sourceList.height - edge
                    ? Math.ceil((point.y - sourceList.height + edge) / 5) : 0
            if (delta === 0)
                return
            var maximum = Math.max(sourceList.originY,
                sourceList.originY + sourceList.contentHeight - sourceList.height)
            sourceList.contentY = Math.max(sourceList.originY,
                Math.min(maximum, sourceList.contentY + delta))
            root.updateDrag(root.dragPointerY)
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.embedded ? 4 : 12
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: root.showHeader ? 30 : 0
            visible: root.showHeader
            Text { Layout.fillWidth: true; text: qsTr("Sources"); color: root.textPrimary; font.pixelSize: 13; font.weight: Font.DemiBold }
            Text { text: sourceList.count; color: root.textMuted; font.pixelSize: 10 }
        }

        ListView {
            id: sourceList
            objectName: "scenesSourceList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: root.rowSpacing
            model: root.bridge ? root.bridge.layersModel : null
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Rectangle {
                parent: sourceList.contentItem
                visible: root.dragActive
                x: 0
                y: Math.max(0, root.dragTargetIndex) * root.rowSlot
                width: sourceList.width
                height: root.rowHeight
                radius: 11
                color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
                border.width: 1
                border.color: root.accent
                z: 20
                Behavior on y {
                    enabled: root.dragActive
                    NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
                }
            }

            delegate: Rectangle {
                id: sourceRow
                required property int index
                required property string layerId
                required property string layerName
                required property bool layerVisible
                required property bool layerLocked
                required property bool ptzAvailable
                readonly property bool selected: root.bridge && root.bridge.selectedLayerId === layerId
                property real previewOffsetY: root.previewOffset(index, layerId)

                width: sourceList.width
                height: root.rowHeight
                radius: 11
                color: selected ? root.accentTint : rowMouse.containsMouse ? root.hover : root.surfaceSoft
                border.width: selected ? 1 : 0
                border.color: root.accent
                opacity: root.dragActive && root.draggedId === layerId ? 0 : layerVisible ? 1 : 0.56
                transform: Translate { y: sourceRow.previewOffsetY }
                Behavior on color { ColorAnimation { duration: 110 } }
                Behavior on opacity { NumberAnimation { duration: 100 } }
                Behavior on previewOffsetY { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 5
                    anchors.rightMargin: 5
                    spacing: 3

                    Item {
                        Layout.preferredWidth: 26
                        Layout.fillHeight: true
                        Image { anchors.centerIn: parent; width: 14; height: 14; source: "image://sceneicons/grip/16/" + root.iconHex(gripMouse.containsMouse ? root.accent : root.textMuted) }
                        MouseArea {
                            id: gripMouse
                            objectName: "scenesSourceDrag-" + sourceRow.layerId
                            anchors.fill: parent
                            hoverEnabled: true
                            preventStealing: true
                            cursorShape: root.dragActive && root.draggedId === sourceRow.layerId
                                ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                            readonly property string cursorSource: "source-drag:" + sourceRow.layerId
                            property point pressPoint: Qt.point(0, 0)
                            property real grabOffsetY: 0
                            onEntered: if (root.bridge) {
                                root.bridge.beginPointer(cursorSource, Number(cursorShape))
                            }
                            onExited: if (root.bridge) {
                                root.bridge.endPointer(cursorSource)
                            }
                            onCursorShapeChanged: if (containsMouse && root.bridge) {
                                root.bridge.updatePointer(cursorSource, Number(cursorShape))
                            }
                            Component.onDestruction: if (root.bridge) {
                                root.bridge.endPointer(cursorSource)
                            }
                            onPressed: function(mouse) {
                                pressPoint = mapToItem(root, mouse.x, mouse.y)
                                grabOffsetY = mapToItem(sourceRow, mouse.x, mouse.y).y
                                mouse.accepted = true
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed)
                                    return
                                var point = mapToItem(root, mouse.x, mouse.y)
                                if (!root.dragActive) {
                                    var dx = point.x - pressPoint.x
                                    var dy = point.y - pressPoint.y
                                    if (dx * dx + dy * dy < 36)
                                        return
                                    root.beginDrag(sourceRow, point.y, grabOffsetY)
                                }
                                root.updateDrag(point.y)
                            }
                            onReleased: if (root.draggedId === sourceRow.layerId) root.finishDrag()
                            onCanceled: if (root.draggedId === sourceRow.layerId) root.cancelDrag()
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Text {
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: sourceRow.layerName
                            color: root.textPrimary
                            font.pixelSize: 11
                            font.weight: sourceRow.selected ? Font.DemiBold : Font.Medium
                            elide: Text.ElideRight
                        }
                    }

                    SourceAction {
                        iconName: sourceRow.layerVisible ? "eye" : "eye-off"
                        toolTipText: sourceRow.layerVisible ? qsTr("Hide source") : qsTr("Show source")
                        onClicked: root.bridge.setLayerVisible(sourceRow.layerId, !sourceRow.layerVisible)
                    }
                    SourceAction {
                        iconName: sourceRow.layerLocked ? "lock" : "unlock"
                        toolTipText: sourceRow.layerLocked ? qsTr("Unlock source") : qsTr("Lock source")
                        onClicked: root.bridge.setLayerLocked(sourceRow.layerId, !sourceRow.layerLocked)
                    }
                    SourceAction {
                        id: optionsAction
                        iconName: "more"
                        toolTipText: qsTr("Source options")
                        onClicked: root.requestLayerMenu(
                            sourceRow.layerId, optionsAction, 0,
                            optionsAction.height + 4)
                    }
                }

                MouseArea {
                    id: rowMouse
                    anchors.fill: parent
                    anchors.leftMargin: 36
                    anchors.rightMargin: 92
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    activeFocusOnTab: true
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onPressed: forceActiveFocus()
                    Keys.onPressed: function(event) {
                        if (!(event.modifiers & Qt.AltModifier) || !root.bridge)
                            return
                        if (event.key === Qt.Key_Up && sourceRow.index > 0) {
                            root.bridge.reorderLayer(sourceRow.layerId, sourceRow.index - 1)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Down
                                && sourceRow.index < sourceList.count - 1) {
                            root.bridge.reorderLayer(sourceRow.layerId, sourceRow.index + 1)
                            event.accepted = true
                        }
                    }
                    onClicked: function(mouse) {
                        root.bridge.selectLayer(sourceRow.layerId)
                        if (mouse.button === Qt.RightButton)
                            root.requestLayerMenu(
                                sourceRow.layerId, rowMouse, mouse.x, mouse.y)
                    }
                }
            }

            footer: Text {
                visible: sourceList.count === 0
                width: sourceList.width
                height: 130
                text: qsTr("No sources yet\nAdd content, a camera, or another scene.")
                color: root.textMuted
                font.pixelSize: 10
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        Text {
            Layout.fillWidth: true
            visible: root.bridge && root.bridge.selectedLayerPtzCameras.length > 0
            text: qsTr("PTZ is available for the selected source")
            color: root.textMuted
            font.pixelSize: 9
            elide: Text.ElideRight
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 7
            Rectangle {
                id: addSourceButton
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                radius: 10
                color: addMouse.containsMouse ? root.hover : "transparent"
                border.width: 1
                border.color: root.borderColor
                Row { anchors.centerIn: parent; spacing: 6
                    Image { anchors.verticalCenter: parent.verticalCenter; width: 13; height: 13; source: "image://sceneicons/plus/16/" + root.iconHex(root.textSecondary) }
                    Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Source"); color: root.textSecondary; font.pixelSize: 11 }
                }
                MouseArea {
                    id: addMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.requestAddSource(addSourceButton)
                }
            }
            Rectangle {
                visible: root.bridge && root.bridge.selectedLayerPtzCameras.length > 0
                Layout.preferredWidth: 42
                Layout.preferredHeight: 38
                radius: 10
                color: ptzMouse.containsMouse ? root.hover : "transparent"
                border.width: 1
                border.color: root.borderColor
                Image { anchors.centerIn: parent; width: 15; height: 15; source: "image://sceneicons/crosshair/16/" + root.iconHex(root.textSecondary) }
                MouseArea { id: ptzMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.requestPtz() }
                ToolTip.visible: ptzMouse.containsMouse
                ToolTip.text: qsTr("PTZ controls")
            }
        }
    }

    Item {
        visible: root.dragActive
        x: sourceList.mapToItem(root, 0, 0).x
        y: root.dragGhostY
        width: sourceList.width
        height: root.rowHeight
        z: 1000
        scale: 1.012
        Rectangle { anchors.fill: parent; anchors.topMargin: 4; radius: 12; color: "#44000000" }
        Rectangle {
            anchors.fill: parent
            radius: 11
            color: root.hover
            border.width: 1
            border.color: root.accent
            RowLayout { anchors.fill: parent; anchors.margins: 10; spacing: 8
                Image { width: 14; height: 14; source: "image://sceneicons/grip/16/" + root.iconHex(root.accent) }
                Text { Layout.fillWidth: true; text: root.draggedName; color: root.textPrimary; font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideRight }
                Image { width: 13; height: 13; source: "image://sceneicons/" + (root.draggedVisible ? "eye" : "eye-off") + "/16/" + root.iconHex(root.textMuted) }
                Image { width: 13; height: 13; source: "image://sceneicons/" + (root.draggedLocked ? "lock" : "unlock") + "/16/" + root.iconHex(root.textMuted) }
            }
        }
    }

    component SourceAction: Item {
        id: action
        property string iconName: ""
        property string toolTipText: ""
        signal clicked()
        Layout.preferredWidth: 27
        Layout.fillHeight: true
        Rectangle {
            anchors.centerIn: parent
            width: 25; height: 28; radius: 7
            color: actionMouse.containsMouse ? root.hover : "transparent"
            Image { anchors.centerIn: parent; width: 13; height: 13; source: "image://sceneicons/" + action.iconName + "/16/" + root.iconHex(root.textMuted) }
            MouseArea { id: actionMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: action.clicked() }
            ToolTip.visible: actionMouse.containsMouse
            ToolTip.text: action.toolTipText
        }
    }
}
