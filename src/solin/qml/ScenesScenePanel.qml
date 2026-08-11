pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    objectName: "scenesScenePanel"
    property var bridge: null
    property var theme: null
    property bool embedded: false
    property bool showHeader: true
    signal requestProfileMenu(var anchor)
    signal requestCreateScene()
    signal requestSceneMenu(
        string sceneId,
        string sceneName,
        bool isDefaultScene,
        bool isMediaScene,
        bool isLiveScene,
        bool hasTransitionOverride,
        string transitionKind,
        int transitionDurationMs,
        int sceneCount,
        var anchor,
        real x,
        real y)

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color accentTint: theme ? theme.accentTint : "#17335b"
    readonly property color success: theme ? theme.success : "#36c98f"
    readonly property int rowHeight: 64
    readonly property int rowSpacing: 7
    readonly property int rowSlot: rowHeight + rowSpacing
    readonly property string dragCursorSource: "scene-reorder"
    property bool dragActive: false
    property string draggedId: ""
    property string draggedName: ""
    property string draggedMetadata: ""
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
        bridge.selectScene(row.sceneId)
        draggedId = row.sceneId
        draggedName = row.sceneName
        draggedMetadata = row.sceneMetadata
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
        var top = sceneList.mapToItem(root, 0, 0).y
        return Math.max(top, Math.min(top + sceneList.height - rowHeight,
            pointerY - grabOffsetY))
    }

    function targetIndex(pointerY) {
        if (sceneList.count <= 0)
            return -1
        var viewport = sceneList.mapFromItem(root, 0, pointerY)
        var content = sceneList.contentItem.mapFromItem(sceneList, 0, viewport.y)
        return Math.max(0, Math.min(sceneList.count - 1,
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
            bridge.reorderScene(id, target)
    }

    function cancelDrag() {
        var wasActive = dragActive
        dragActive = false
        draggedId = ""
        draggedName = ""
        draggedMetadata = ""
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
            var point = sceneList.mapFromItem(root, 0, root.dragPointerY)
            var edge = 28
            var delta = point.y < edge ? -Math.ceil((edge - point.y) / 5)
                : point.y > sceneList.height - edge
                    ? Math.ceil((point.y - sceneList.height + edge) / 5) : 0
            if (delta === 0)
                return
            var maximum = Math.max(sceneList.originY,
                sceneList.originY + sceneList.contentHeight - sceneList.height)
            sceneList.contentY = Math.max(sceneList.originY,
                Math.min(maximum, sceneList.contentY + delta))
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
            Text {
                Layout.fillWidth: true
                text: qsTr("Scenes")
                color: root.textPrimary
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            Rectangle {
                id: profileMenuButton
                Layout.preferredWidth: 30
                Layout.preferredHeight: 30
                radius: 8
                color: profileMouse.containsMouse ? root.hover : "transparent"
                Image {
                    anchors.centerIn: parent
                    width: 14; height: 14
                    source: "image://sceneicons/more/16/" + root.iconHex(root.textMuted)
                }
                MouseArea {
                    id: profileMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.requestProfileMenu(profileMenuButton)
                }
            }
        }

        ListView {
            id: sceneList
            objectName: "scenesSceneList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: root.rowSpacing
            model: root.bridge ? root.bridge.scenesModel : null
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Rectangle {
                parent: sceneList.contentItem
                visible: root.dragActive
                x: 0
                y: Math.max(0, root.dragTargetIndex) * root.rowSlot
                width: sceneList.width
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
                id: sceneRow
                required property int index
                required property string sceneId
                required property string sceneName
                required property string sceneMetadata
                required property bool isDefaultScene
                required property bool isMediaScene
                required property bool isLiveScene
                required property bool hasTransitionOverride
                required property string transitionOverrideKind
                required property int transitionOverrideDurationMs
                readonly property bool selected: root.bridge && root.bridge.selectedSceneId === sceneId
                property real previewOffsetY: root.previewOffset(index, sceneId)

                width: sceneList.width
                height: root.rowHeight
                radius: 11
                color: selected ? root.accentTint : rowMouse.containsMouse ? root.hover : root.surfaceSoft
                border.width: selected || isLiveScene ? 1 : 0
                border.color: isLiveScene ? root.success : root.accent
                opacity: root.dragActive && root.draggedId === sceneId ? 0 : 1
                transform: Translate { y: sceneRow.previewOffsetY }
                Behavior on color { ColorAnimation { duration: 110 } }
                Behavior on previewOffsetY { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 8
                    spacing: 7

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        spacing: 2
                        Text {
                            Layout.fillWidth: true
                            text: sceneRow.sceneName
                            color: root.textPrimary
                            font.pixelSize: 11
                            font.weight: sceneRow.selected ? Font.DemiBold : Font.Medium
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: sceneRow.sceneMetadata.length ? sceneRow.sceneMetadata : "\u00a0"
                            color: sceneRow.isLiveScene ? root.success : root.textMuted
                            font.pixelSize: 9
                            elide: Text.ElideRight
                        }
                    }

                    Rectangle {
                        Layout.preferredWidth: 26
                        Layout.preferredHeight: 30
                        radius: 8
                        color: menuMouse.containsMouse ? root.hover : "transparent"
                        Image { anchors.centerIn: parent; width: 13; height: 13; source: "image://sceneicons/more/16/" + root.iconHex(root.textMuted) }
                        MouseArea {
                            id: menuMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.bridge.selectScene(sceneRow.sceneId)
                                root.requestSceneMenu(
                                    sceneRow.sceneId,
                                    sceneRow.sceneName,
                                    sceneRow.isDefaultScene,
                                    sceneRow.isMediaScene,
                                    sceneRow.isLiveScene,
                                    sceneRow.hasTransitionOverride,
                                    sceneRow.transitionOverrideKind,
                                    sceneRow.transitionOverrideDurationMs,
                                    sceneList.count,
                                    menuMouse,
                                    0,
                                    menuMouse.height + 4)
                            }
                        }
                    }
                }

                MouseArea {
                    id: rowMouse
                    objectName: "scenesSceneDrag-" + sceneRow.sceneId
                    anchors.fill: parent
                    anchors.rightMargin: 34
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    activeFocusOnTab: true
                    hoverEnabled: true
                    preventStealing: true
                    cursorShape: root.dragActive && root.draggedId === sceneRow.sceneId
                        ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                    readonly property string cursorSource: "scene-drag:" + sceneRow.sceneId
                    property point pressPoint: Qt.point(0, 0)
                    property real grabOffsetY: 0
                    onEntered: if (root.bridge) {
                        root.bridge.beginPointer(cursorSource, Number(cursorShape))
                    }
                    onExited: if (root.bridge) root.bridge.endPointer(cursorSource)
                    onCursorShapeChanged: if (containsMouse && root.bridge) {
                        root.bridge.updatePointer(cursorSource, Number(cursorShape))
                    }
                    Component.onDestruction: if (root.bridge) {
                        root.bridge.endPointer(cursorSource)
                    }
                    onPressed: function(mouse) {
                        forceActiveFocus()
                        if (mouse.button !== Qt.LeftButton)
                            return
                        pressPoint = mapToItem(root, mouse.x, mouse.y)
                        grabOffsetY = mapToItem(sceneRow, mouse.x, mouse.y).y
                    }
                    onPositionChanged: function(mouse) {
                        if (!pressed || !(mouse.buttons & Qt.LeftButton))
                            return
                        var point = mapToItem(root, mouse.x, mouse.y)
                        if (!root.dragActive) {
                            var dx = point.x - pressPoint.x
                            var dy = point.y - pressPoint.y
                            if (dx * dx + dy * dy < 36)
                                return
                            root.beginDrag(sceneRow, point.y, grabOffsetY)
                        }
                        if (root.draggedId === sceneRow.sceneId)
                            root.updateDrag(point.y)
                    }
                    onReleased: if (root.draggedId === sceneRow.sceneId) root.finishDrag()
                    onCanceled: if (root.draggedId === sceneRow.sceneId) root.cancelDrag()
                    Keys.onPressed: function(event) {
                        if (!(event.modifiers & Qt.AltModifier) || !root.bridge)
                            return
                        if (event.key === Qt.Key_Up && sceneRow.index > 0) {
                            root.bridge.reorderScene(sceneRow.sceneId, sceneRow.index - 1)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Down
                                && sceneRow.index < sceneList.count - 1) {
                            root.bridge.reorderScene(sceneRow.sceneId, sceneRow.index + 1)
                            event.accepted = true
                        }
                    }
                    onClicked: function(mouse) {
                        root.bridge.selectScene(sceneRow.sceneId)
                        if (mouse.button === Qt.RightButton)
                            root.requestSceneMenu(
                                sceneRow.sceneId,
                                sceneRow.sceneName,
                                sceneRow.isDefaultScene,
                                sceneRow.isMediaScene,
                                sceneRow.isLiveScene,
                                sceneRow.hasTransitionOverride,
                                sceneRow.transitionOverrideKind,
                                sceneRow.transitionOverrideDurationMs,
                                sceneList.count,
                                rowMouse,
                                mouse.x,
                                mouse.y)
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            radius: 10
            color: addMouse.containsMouse ? root.hover : "transparent"
            border.width: 1
            border.color: root.borderColor
            Row { anchors.centerIn: parent; spacing: 6
                Image { anchors.verticalCenter: parent.verticalCenter; width: 13; height: 13; source: "image://sceneicons/plus/16/" + root.iconHex(root.textSecondary) }
                Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Scene"); color: root.textSecondary; font.pixelSize: 11 }
            }
            MouseArea { id: addMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.requestCreateScene() }
        }
    }

    Item {
        visible: root.dragActive
        x: sceneList.mapToItem(root, 0, 0).x
        y: root.dragGhostY
        width: sceneList.width
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
            RowLayout { anchors.fill: parent; anchors.margins: 12; spacing: 8
                ColumnLayout { Layout.fillWidth: true; spacing: 1
                    Text { Layout.fillWidth: true; text: root.draggedName; color: root.textPrimary; font.pixelSize: 11; font.weight: Font.DemiBold; elide: Text.ElideRight }
                    Text { Layout.fillWidth: true; text: root.draggedMetadata.length ? root.draggedMetadata : "\u00a0"; color: root.textMuted; font.pixelSize: 9; elide: Text.ElideRight }
                }
            }
        }
    }
}
