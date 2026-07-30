pragma Translator: "TalkThemeEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    objectName: "talkThemeLayerPanel"
    clip: true

    property var bridge: null
    property var theme: null
    property color surface: theme ? theme.surface : "#172131"
    property color hover: theme ? theme.hover : "#233043"
    property color hoverStrong: theme ? theme.hoverStrong : "#2a374a"
    property color borderColor: theme ? theme.border_ : "#2c394b"
    property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    property color textMuted: theme ? theme.textMuted : "#768397"
    property color accent: theme ? theme.accent : "#7367f0"
    readonly property int layerRowHeight: 48
    readonly property int layerRowSpacing: 6
    readonly property int layerRowSlot: layerRowHeight + layerRowSpacing
    property bool layerDragActive: false
    property string draggedLayerId: ""
    property string draggedLayerName: ""
    property bool draggedLayerVisible: true
    property bool draggedLayerSelected: false
    property int layerDragOriginIndex: -1
    property int layerDragTargetIndex: -1
    property real layerDragPointerY: 0
    property real layerDragGrabOffsetY: 0

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function beginLayerDrag(layerRow, pointerY, grabOffsetY) {
        if (!layerRow || layerDragActive || !bridge)
            return
        draggedLayerId = layerRow.layerId
        draggedLayerName = layerRow.layerName
        draggedLayerVisible = layerRow.layerVisible
        draggedLayerSelected = layerRow.selected
        layerDragOriginIndex = layerRow.index
        layerDragPointerY = pointerY
        layerDragGrabOffsetY = grabOffsetY
        layerDragTargetIndex = targetIndexForPointer(pointerY)
        layerDragActive = true
    }

    function targetIndexForPointer(pointerY) {
        if (layerList.count <= 0)
            return -1
        var viewportPoint = layerList.mapFromItem(root, 0, pointerY)
        var contentPoint = layerList.contentItem.mapFromItem(
            layerList, 0, viewportPoint.y)
        var ghostTop = contentPoint.y - layerDragGrabOffsetY
        return Math.max(
            0,
            Math.min(layerList.count - 1, Math.round(ghostTop / layerRowSlot)))
    }

    function updateLayerDrag(pointerY) {
        if (!layerDragActive || layerList.count <= 0)
            return
        layerDragPointerY = pointerY
        layerDragTargetIndex = targetIndexForPointer(pointerY)
    }

    function finishLayerDrag() {
        if (!layerDragActive)
            return
        var layerId = draggedLayerId
        var originIndex = layerDragOriginIndex
        var targetIndex = layerDragTargetIndex
        resetLayerDrag()
        if (bridge && layerId && targetIndex !== originIndex)
            bridge.reorderLayer(layerId, targetIndex)
    }

    function cancelLayerDrag() {
        if (layerDragActive)
            resetLayerDrag()
    }

    function resetLayerDrag() {
        layerDragActive = false
        draggedLayerId = ""
        draggedLayerName = ""
        draggedLayerVisible = true
        draggedLayerSelected = false
        layerDragOriginIndex = -1
        layerDragTargetIndex = -1
        layerDragPointerY = 0
        layerDragGrabOffsetY = 0
    }

    function previewOffsetFor(layerIndex, layerId) {
        if (!layerDragActive || layerId === draggedLayerId)
            return 0
        if (layerDragOriginIndex < layerDragTargetIndex
                && layerIndex > layerDragOriginIndex
                && layerIndex <= layerDragTargetIndex)
            return -layerRowSlot
        if (layerDragOriginIndex > layerDragTargetIndex
                && layerIndex >= layerDragTargetIndex
                && layerIndex < layerDragOriginIndex)
            return layerRowSlot
        return 0
    }

    onVisibleChanged: if (!visible) cancelLayerDrag()

    Shortcut {
        sequence: "Esc"
        enabled: root.layerDragActive
        onActivated: root.cancelLayerDrag()
    }

    Timer {
        interval: 16
        repeat: true
        running: root.layerDragActive
        onTriggered: {
            var point = layerList.mapFromItem(root, 0, root.layerDragPointerY)
            var edge = 28
            var delta = 0
            if (point.y < edge)
                delta = -Math.ceil((edge - point.y) / 5)
            else if (point.y > layerList.height - edge)
                delta = Math.ceil((point.y - layerList.height + edge) / 5)
            if (delta === 0)
                return
            var minimum = layerList.originY
            var maximum = Math.max(
                minimum,
                layerList.originY + layerList.contentHeight - layerList.height)
            var next = Math.max(minimum, Math.min(maximum, layerList.contentY + delta))
            if (next === layerList.contentY)
                return
            layerList.contentY = next
            root.updateLayerDrag(root.layerDragPointerY)
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 10

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            radius: 10
            color: addMouse.containsMouse ? Qt.lighter(root.accent, 1.08) : root.accent
            opacity: root.bridge && root.bridge.canAddLayer ? 1.0 : 0.42

            Row {
                anchors.centerIn: parent
                spacing: 7
                Image {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 14
                    height: 14
                    source: "image://talkthemeicons/plus/16/ffffff"
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Text")
                    color: "white"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                }
            }
            MouseArea {
                id: addMouse
                anchors.fill: parent
                enabled: root.bridge && root.bridge.canAddLayer
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.bridge.addTextLayer()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: qsTr("Front")
                color: root.textMuted
                font.pixelSize: 10
                font.capitalization: Font.AllUppercase
                font.letterSpacing: 0.6
            }
            Text {
                text: root.bridge ? qsTr("%1 layers").arg(root.bridge.layerCount) : ""
                color: root.textMuted
                font.pixelSize: 10
            }
        }

        ListView {
            id: layerList
            objectName: "talkThemeLayerList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: root.layerRowSpacing
            model: root.bridge ? root.bridge.layersModel : null
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            displaced: Transition {
                NumberAnimation { properties: "x,y"; duration: 150; easing.type: Easing.OutCubic }
            }

            Rectangle {
                id: layerDropPlaceholder
                objectName: "talkThemeLayerDropPlaceholder"
                parent: layerList.contentItem
                visible: root.layerDragActive
                x: 0
                y: Math.max(0, root.layerDragTargetIndex) * root.layerRowSlot
                width: layerList.width
                height: root.layerRowHeight
                radius: 10
                color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.14)
                border.width: 1
                border.color: root.accent
                z: 40

                Rectangle {
                    anchors.left: parent.left
                    anchors.leftMargin: 5
                    anchors.verticalCenter: parent.verticalCenter
                    width: 3
                    height: 24
                    radius: 2
                    color: root.accent
                }

                Behavior on y {
                    enabled: root.layerDragActive
                    NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
                }
            }

            delegate: Rectangle {
                id: layerRow
                objectName: "talkThemeLayerRow-" + layerId
                required property int index
                required property string layerId
                required property string layerName
                required property string layerText
                required property bool layerVisible

                property bool renaming: false
                property real previewOffsetY:
                    root.previewOffsetFor(index, layerId)
                readonly property bool selected:
                    root.bridge && root.bridge.selectedLayerId === layerId

                width: layerList.width
                height: root.layerRowHeight
                radius: 10
                color: selected ? (root.theme ? root.theme.accentTint : "#29244c")
                      : rowMouse.containsMouse ? root.hover : root.surface
                border.width: selected ? 1 : 0
                border.color: root.accent
                opacity: root.layerDragActive && root.draggedLayerId === layerId
                    ? 0 : layerVisible ? 1.0 : 0.58
                z: 1
                transform: Translate { y: layerRow.previewOffsetY }

                Behavior on color { ColorAnimation { duration: 100 } }
                Behavior on opacity { NumberAnimation { duration: 90 } }
                Behavior on previewOffsetY {
                    NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 5
                    anchors.rightMargin: 5
                    spacing: 3

                    Rectangle {
                        Layout.preferredWidth: 28
                        Layout.fillHeight: true
                        radius: 7
                        color: gripMouse.containsMouse || root.draggedLayerId === layerRow.layerId
                            ? (root.theme ? root.theme.accentTint : "#29244c")
                            : "transparent"
                        Image {
                            anchors.centerIn: parent
                            width: 14
                            height: 14
                            source: "image://talkthemeicons/grip/16/" + root.iconHex(
                                gripMouse.containsMouse || root.draggedLayerId === layerRow.layerId
                                    ? root.accent : root.textMuted)
                        }
                        MouseArea {
                            id: gripMouse
                            objectName: "talkThemeLayerGrip-" + layerRow.layerId
                            anchors.fill: parent
                            hoverEnabled: true
                            preventStealing: true
                            cursorShape: root.layerDragActive
                                && root.draggedLayerId === layerRow.layerId
                                ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                            property point pressPoint: Qt.point(0, 0)
                            property real grabOffsetY: 0
                            onPressed: function(mouse) {
                                pressPoint = mapToItem(root, mouse.x, mouse.y)
                                grabOffsetY = mapToItem(layerRow, mouse.x, mouse.y).y
                                mouse.accepted = true
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed)
                                    return
                                var point = mapToItem(root, mouse.x, mouse.y)
                                if (!root.layerDragActive) {
                                    var dx = point.x - pressPoint.x
                                    var dy = point.y - pressPoint.y
                                    if (dx * dx + dy * dy < 36)
                                        return
                                    root.beginLayerDrag(layerRow, point.y, grabOffsetY)
                                }
                                if (root.draggedLayerId === layerRow.layerId)
                                    root.updateLayerDrag(point.y)
                            }
                            onReleased: if (root.draggedLayerId === layerRow.layerId)
                                root.finishLayerDrag()
                            onCanceled: if (root.draggedLayerId === layerRow.layerId)
                                root.cancelLayerDrag()
                        }
                        ToolTip.visible: gripMouse.containsMouse && !root.layerDragActive
                        ToolTip.text: qsTr("Drag layers to change which text appears in front.")
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Text {
                            id: layerNameLabel
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            visible: !layerRow.renaming
                            text: layerRow.layerName
                            color: root.textPrimary
                            font.pixelSize: 11
                            font.weight: layerRow.selected ? Font.DemiBold : Font.Medium
                            elide: Text.ElideRight
                        }
                        TextField {
                            id: renameField
                            anchors.fill: parent
                            visible: layerRow.renaming
                            color: root.textPrimary
                            font.pixelSize: 11
                            leftPadding: 6
                            rightPadding: 6
                            background: Rectangle {
                                radius: 6
                                color: root.hoverStrong
                                border.width: 1
                                border.color: root.accent
                            }
                            function finishRename(commit) {
                                if (!layerRow.renaming)
                                    return
                                if (commit && text.trim() && root.bridge)
                                    root.bridge.renameLayer(layerRow.layerId, text)
                                layerRow.renaming = false
                            }
                            onActiveFocusChanged: if (!activeFocus) finishRename(true)
                            Keys.onReturnPressed: finishRename(true)
                            Keys.onEnterPressed: finishRename(true)
                            Keys.onEscapePressed: finishRename(false)
                        }
                        MouseArea {
                            id: rowMouse
                            anchors.fill: parent
                            enabled: !layerRow.renaming
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: if (root.bridge) root.bridge.selectLayer(layerRow.layerId)
                            onDoubleClicked: {
                                layerRow.renaming = true
                                renameField.text = layerRow.layerName
                                renameField.forceActiveFocus()
                                renameField.selectAll()
                            }
                        }
                    }

                    LayerAction {
                        iconName: layerRow.layerVisible ? "eye" : "eye-off"
                        toolTipText: layerRow.layerVisible ? qsTr("Hide layer") : qsTr("Show layer")
                        onClicked: if (root.bridge) root.bridge.toggleLayerVisibility(layerRow.layerId)
                    }
                    LayerAction {
                        iconName: "trash"
                        danger: true
                        toolTipText: qsTr("Delete layer")
                        onClicked: if (root.bridge) root.bridge.deleteLayer(layerRow.layerId)
                    }
                }
            }

            footer: Text {
                visible: layerList.count === 0
                width: layerList.width
                height: 130
                text: qsTr("No text layers yet\nAdd one when you are ready.")
                color: root.textMuted
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        Text {
            Layout.fillWidth: true
            text: qsTr("Drag layers to change which text appears in front.")
            color: root.textMuted
            font.pixelSize: 10
            wrapMode: Text.Wrap
        }
    }

    Item {
        id: layerDragGhost
        objectName: "talkThemeLayerDragGhost"
        visible: root.layerDragActive
        x: layerList.mapToItem(root, 0, 0).x
        y: {
            var listTop = layerList.mapToItem(root, 0, 0).y
            var requested = root.layerDragPointerY - root.layerDragGrabOffsetY
            return Math.max(
                listTop,
                Math.min(listTop + layerList.height - height, requested))
        }
        width: layerList.width
        height: root.layerRowHeight
        z: 1000
        scale: 1.012
        opacity: 0.97

        Rectangle {
            anchors.fill: parent
            anchors.topMargin: 4
            radius: 11
            color: "#44000000"
        }
        Rectangle {
            anchors.fill: parent
            radius: 10
            color: root.draggedLayerSelected
                ? (root.theme ? root.theme.accentTint : "#29244c")
                : root.hoverStrong
            border.width: 1
            border.color: root.accent

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 8
                anchors.rightMargin: 10
                spacing: 8
                Image {
                    Layout.preferredWidth: 14
                    Layout.preferredHeight: 14
                    source: "image://talkthemeicons/grip/16/" + root.iconHex(root.accent)
                }
                Text {
                    Layout.fillWidth: true
                    text: root.draggedLayerName
                    color: root.textPrimary
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Image {
                    Layout.preferredWidth: 13
                    Layout.preferredHeight: 13
                    source: "image://talkthemeicons/"
                        + (root.draggedLayerVisible ? "eye" : "eye-off")
                        + "/16/" + root.iconHex(root.textMuted)
                }
            }
        }
    }

    component LayerAction: Item {
        id: action
        property string iconName: ""
        property string toolTipText: ""
        property bool danger: false
        signal clicked()
        Layout.preferredWidth: 28
        Layout.fillHeight: true
        Rectangle {
            anchors.centerIn: parent
            width: 26
            height: 26
            radius: 7
            color: actionMouse.containsMouse
                ? (action.danger && root.theme ? root.theme.dangerSubtle : root.hoverStrong)
                : "transparent"
            Image {
                anchors.centerIn: parent
                width: 13
                height: 13
                source: "image://talkthemeicons/" + action.iconName + "/16/"
                    + root.iconHex(action.danger && actionMouse.containsMouse && root.theme
                                   ? root.theme.danger : root.textMuted)
            }
            MouseArea {
                id: actionMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: function(mouse) {
                    mouse.accepted = true
                    action.clicked()
                }
            }
            ToolTip.visible: actionMouse.containsMouse
            ToolTip.text: action.toolTipText
        }
    }
}
