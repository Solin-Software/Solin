// PlaylistTreeView.qml - reusable tree-shaped playlist surface
// Contains playlist items, sections/subsections, markers, drag/drop, and empty state.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: root

    property int contentLeftInset: 16
    property int contentRightInset: 12
    property int scrollbarGutter: 12
    property int scrollbarContentGap: 8
    property color bg: "#0d1117"
    property color surface: "#13161c"
    property color hover: "#1a1f2a"
    property color border_: "#1e2430"
    property color borderStrong: "#2a3040"
    property color textPrimary: "#e6edf3"
    property color textSecondary: "#c9d1d9"
    property color textMuted: "#8b949e"
    property color textDim: "#484f58"
    property color accent: "#388bfd"
    property color accentTint: "#132a46"
    property color previewBg: Qt.rgba(0.22, 0.55, 0.99, 0.12)
    property color previewBorder: "#388bfd"
    property color danger: "#f85149"
    property color dangerSubtle: "#3d1214"

    property var playlistController: null
    property bool hasItems: false
    property var playlistNodes: []
    readonly property bool hasController: playlistController !== null

    property string externalDropListId: "root"
    property int externalDropIndex: -1
    property int dropIndicatorIndex: -1
    property string pendingMarkerEditId: ""
    property int pendingMarkerEditAttempts: 0
    property var pendingMediaPatches: ({})

    function picon(name, size, colorHex) {
        return "image://playlisticons/" + name + "/" + size + "/" + colorHex
    }

    function commonTr(context, source) {
        return qsTranslate(context, source)
    }

    function pointerEntered() {
        if (root.hasController)
            root.playlistController.pointerEnter()
    }

    function pointerExited() {
        if (root.hasController)
            root.playlistController.pointerExit()
    }

    function getIndexAt(rootY) {
        var local = rootPlaylist.mapFromItem(root, 0, rootY)
        dragManager.updateExternalDrop(local.x, local.y)
        var target = rootPlaylist.dropTargetAt(local.x, local.y)
        externalDropListId = target.listId
        externalDropIndex = target.index
        return target.index
    }

    function clearExternalDropPreview() {
        dragManager.endExternalDrop()
        externalDropListId = "root"
        externalDropIndex = -1
        dropIndicatorIndex = -1
    }

    function requestMarkerEdit(markerId) {
        pendingMarkerEditId = markerId
        pendingMarkerEditAttempts = 0
        markerEditTimer.restart()
    }

    function rememberMediaPatch(itemId, title, duration, thumbSource) {
        pendingMediaPatches[itemId] = {
            "title": title,
            "duration": duration,
            "thumbSource": thumbSource
        }
    }

    function applyPendingMediaPatch(mediaItem) {
        var patch = pendingMediaPatches[mediaItem.nodeId]
        if (!patch)
            return
        mediaItem.applyMediaPatch(
            patch.title,
            patch.duration,
            patch.thumbSource
        )
        delete pendingMediaPatches[mediaItem.nodeId]
    }

    Timer {
        id: markerEditTimer
        interval: 0
        repeat: false
        onTriggered: {
            if (root.pendingMarkerEditId === "")
                return
            if (rootPlaylist.editMarkerNode(root.pendingMarkerEditId)) {
                root.pendingMarkerEditId = ""
                return
            }
            if (root.pendingMarkerEditAttempts < 4) {
                root.pendingMarkerEditAttempts += 1
                restart()
            } else {
                root.pendingMarkerEditId = ""
            }
        }
    }

    Item {
        id: dragOverlay
        anchors.fill: parent
        z: 9999
    }

    QtObject {
        id: dragManager
        property var draggedItem: null
        property string draggedId: ""
        property string draggedType: ""
        property bool externalActive: false
        property bool active: draggedItem !== null || externalActive

        property Rectangle placeholder: Rectangle {
            id: placeholderRect
            property var parentList: null
            property bool suspendBehavior: false
            parent: dragOverlay
            visible: false
            width: 100
            height: 48
            radius: 8
            color: root.previewBg
            border.width: 2
            border.color: root.previewBorder

            Behavior on y {
                enabled: !placeholderRect.suspendBehavior
                NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
            }
            Behavior on x {
                enabled: !placeholderRect.suspendBehavior
                NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
            }
        }

        function startDrag(item) {
            if (!item || draggedItem)
                return

            var currentList = item.parentList
            if (!currentList)
                return

            draggedItem = item
            draggedId = item.nodeId
            draggedType = item.nodeType
            item.opacity = 0.34

            var globalPos = item.parent.mapToItem(dragOverlay, item.x, item.y)
            var localPos = currentList.mapFromItem(dragOverlay, globalPos.x, globalPos.y)

            placeholder.suspendBehavior = true
            placeholder.width = item.width
            placeholder.height = item.height
            placeholder.x = localPos.x
            placeholder.y = localPos.y
            placeholder.parentList = currentList
            placeholder.parent = currentList
            placeholder.visible = true
            currentList.insertBeforeNode(placeholder, item)
            currentList.removeNode(item)
            Qt.callLater(function() { placeholder.suspendBehavior = false })
        }

        function updateExternalDrop(localX, localY) {
            if (draggedItem)
                return
            var wasInactive = !externalActive
            externalActive = true
            draggedId = ""
            draggedType = "media"
            placeholder.suspendBehavior = wasInactive
            placeholder.width = Math.max(240, rootPlaylist.width)
            placeholder.height = 76
            placeholder.visible = true
            rootPlaylist.routePlaceholderAt(localX, localY)
            if (wasInactive)
                Qt.callLater(function() { placeholder.suspendBehavior = false })
        }

        function endExternalDrop() {
            if (!externalActive)
                return
            if (placeholder.parentList)
                placeholder.parentList.removeNode(placeholder)
            placeholder.visible = false
            placeholder.parent = dragOverlay
            placeholder.parentList = null
            externalActive = false
            draggedId = ""
            draggedType = ""
        }

        function endDrag() {
            if (!draggedItem)
                return

            var item = draggedItem
            var targetList = placeholder.parentList
            var ok = false

            if (targetList) {
                var index = targetList.indexOfNode(placeholder)
                ok = root.hasController
                     && root.playlistController.moveNode(draggedId, targetList.listId, index)
                if (ok) {
                    targetList.insertBeforeNode(item, placeholder)
                    targetList.removeNode(placeholder)
                }
            }

            item.opacity = 1.0
            placeholder.visible = false
            placeholder.parent = dragOverlay
            placeholder.parentList = null

            draggedItem = null
            draggedId = ""
            draggedType = ""
            externalActive = false

            if (!ok)
                rootPlaylist.rebuildFromNodes(root.playlistNodes)
        }

        function cancelDrag() {
            if (!draggedItem)
                return
            draggedItem.opacity = 1.0
            if (placeholder.parentList) {
                placeholder.parentList.insertBeforeNode(draggedItem, placeholder)
                placeholder.parentList.removeNode(placeholder)
            }
            placeholder.visible = false
            placeholder.parent = dragOverlay
            placeholder.parentList = null
            draggedItem = null
            draggedId = ""
            draggedType = ""
            externalActive = false
        }

        function isDescendant(parentItem, childItem) {
            var current = childItem
            while (current) {
                if (current === parentItem)
                    return true
                current = current.parent
            }
            return false
        }
    }

    Connections {
        target: root.hasController ? root.playlistController : null
        function onStateChanged() {
            if (!dragManager.active)
                rootPlaylist.scheduleRebuild(root.playlistNodes)
        }

        function onMediaChanged(itemId, title, duration, thumbSource) {
            if (!rootPlaylist.updateMediaNode(itemId, title, duration, thumbSource))
                root.rememberMediaPatch(itemId, title, duration, thumbSource)
        }

        function onMediaInserted(listId, insertIndex, nodes) {
            if (!rootPlaylist.insertNodes(listId, insertIndex, nodes))
                rootPlaylist.scheduleRebuild(root.playlistNodes)
        }

        function onNodesInserted(listId, insertIndex, nodes) {
            if (!rootPlaylist.insertNodes(listId, insertIndex, nodes))
                rootPlaylist.scheduleRebuild(root.playlistNodes)
        }

        function onNodeReplaced(nodeId, nodes) {
            if (!rootPlaylist.replaceNode(nodeId, nodes))
                rootPlaylist.scheduleRebuild(root.playlistNodes)
        }

        function onSectionCountsChanged(counts) {
            rootPlaylist.updateSectionCounts(counts)
        }

        function onMarkerEditRequested(markerId) {
            root.requestMarkerEdit(markerId)
        }

        function onCloudChanged(itemId, visible, active, progress, tooltip) {
            rootPlaylist.updateCloudNode(itemId, visible, active, progress, tooltip)
        }
    }

        // ── Content area ──────────────────────────────────────────────────────
        Item {
            anchors.fill: parent

            // Empty state
            Rectangle {
                anchors.centerIn: parent
                width: Math.min(parent.width - 60, 360)
                height: emptyCol.implicitHeight + 48
                radius: 12
                color: "transparent"
                border.width: 1
                border.color: root.border_
                visible: !root.hasItems
                opacity: 0.75

                ColumnLayout {
                    id: emptyCol
                    anchors.centerIn: parent
                    spacing: 8

                    Image {
                        Layout.alignment: Qt.AlignHCenter
                        width: 28
                        height: 28
                        source: root.picon("plus", 28, "30363d")
                        opacity: 0.65
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTranslate("_PlaylistEditView", "Drag media files here or click  ＋  at the top\nClick an item to project it · Drag the grip ⠿ to reorder")
                        color: root.textMuted
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                    }
                }
            }

            // Scrollable content
            Flickable {
                id: playlistScroll
                property real wheelTargetY: contentY
                property bool wheelScrolling: false

                anchors.fill: parent
                anchors.topMargin: 6
                anchors.bottomMargin: 4
                visible: root.hasItems
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                maximumFlickVelocity: 5200
                flickDeceleration: 2800
                contentWidth: width
                contentHeight: rootPlaylist.height + 16

                function clampContentY(value) {
                    var maxY = Math.max(0, contentHeight - height)
                    return Math.max(0, Math.min(maxY, value))
                }

                function smoothWheelScroll(delta) {
                    if (!wheelAnim.running)
                        wheelTargetY = contentY
                    wheelTargetY = clampContentY(wheelTargetY + delta)
                    wheelAnim.stop()
                    wheelScrolling = true
                    wheelAnim.to = wheelTargetY
                    wheelAnim.start()
                }

                NumberAnimation {
                    id: wheelAnim
                    target: playlistScroll
                    property: "contentY"
                    duration: 200
                    easing.type: Easing.OutCubic
                    onStopped: playlistScroll.wheelScrolling = false
                }

                WheelHandler {
                    target: playlistScroll
                    acceptedDevices: PointerDevice.Mouse
                    onWheel: function(event) {
                        var hasPixelDelta = event.pixelDelta.y !== 0
                        var rawDelta = hasPixelDelta ? event.pixelDelta.y : event.angleDelta.y
                        var scrollDelta = hasPixelDelta ? -rawDelta : -rawDelta * 0.35
                        playlistScroll.smoothWheelScroll(scrollDelta)
                        event.accepted = true
                    }
                }

                DropList {
                    id: rootPlaylist
                    objectName: "rootPlaylist"
                    x: root.contentLeftInset
                    width: Math.max(0, playlistScroll.width
                                       - root.contentLeftInset
                                       - root.scrollbarGutter
                                       - root.scrollbarContentGap)
                    listId: "root"
                    listKind: "root"
                    depth: 0

                    Component.onCompleted: scheduleRebuild(root.playlistNodes)
                }

                ScrollBar.vertical: ScrollBar {
                    id: vScroll
                    policy: ScrollBar.AsNeeded
                    width: root.scrollbarGutter
                    contentItem: Rectangle {
                        implicitWidth: 6
                        radius: 3
                        color: ((vScroll.active && !playlistScroll.wheelScrolling) || vScroll.hovered)
                               ? root.textMuted
                               : root.borderStrong
                        opacity: (vScroll.active || vScroll.hovered || playlistScroll.moving) ? 1.0 : 0.35
                        anchors.horizontalCenter: parent.horizontalCenter

                        Behavior on color { ColorAnimation { duration: 150 } }
                        Behavior on opacity { NumberAnimation { duration: 180 } }
                    }
                }
            }
        }


    // ═══════════════════════════════════════════════════════════════════════════
    // COMPONENTS
    // ═══════════════════════════════════════════════════════════════════════════

    component DropList: Item {
        id: listRoot
        property string listId: "root"
        property string listKind: "root"
        property int depth: 0
        property var items: []
        property var pendingNodes: []
        property real listContentHeight: 0
        property bool layoutQueued: false
        property bool collapsed: false

        implicitHeight: Math.max(listKind === "root" ? 30 : 64, listContentHeight)
        height: collapsed ? 0 : implicitHeight
        opacity: collapsed ? 0 : 1
        clip: true

        onWidthChanged: requestLayout()

        Behavior on height {
            NumberAnimation { duration: 190; easing.type: Easing.OutCubic }
        }
        Behavior on opacity {
            NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
        }

        function canAcceptDrag() {
            return dragManager.active
                   && !listRoot.collapsed
                   && root.hasController
                   && root.playlistController.canDrop(dragManager.draggedId,
                                         dragManager.draggedType,
                                         listRoot.listId)
        }

        function rebuildFromNodes(nodes) {
            if (dragManager.active)
                return
            var oldItems = items.slice()
            for (var i = 0; i < oldItems.length; i++)
                oldItems[i].destroy()
            items = []

            var source = nodes || []
            for (var n = 0; n < source.length; n++) {
                var node = source[n]
                var comp = componentForNode(node)
                var obj = comp.createObject(listRoot, {
                    "node": node,
                    "nodeId": node.id,
                    "nodeType": node.type,
                    "parentList": listRoot,
                    "depth": listRoot.depth
                })
                if (obj)
                    items.push(obj)
            }
            doLayout()
        }

        function componentForNode(node) {
            if (node.type === "media")
                return mediaComponent
            if (node.type === "marker")
                return markerComponent
            return sectionComponent
        }

        function createNodeObject(node) {
            var comp = componentForNode(node)
            return comp.createObject(listRoot, {
                "node": node,
                "nodeId": node.id,
                "nodeType": node.type,
                "parentList": listRoot,
                "depth": listRoot.depth
            })
        }

        function scheduleRebuild(nodes) {
            pendingNodes = nodes || []
            rebuildTimer.restart()
        }

        function indexOfNode(node) {
            return items.indexOf(node)
        }

        function updateNodeList(node) {
            if (node === dragManager.placeholder) {
                if (node.parentList && node.parentList !== listRoot)
                    node.parentList.removeNode(node)
                node.parentList = listRoot
                return
            }
            if (node.parentList && node.parentList !== listRoot)
                node.parentList.removeNode(node)
            node.parentList = listRoot
        }

        function moveNodeToContainer(node) {
            if (node.parent === listRoot)
                return
            var globalPos = node.parent ? node.parent.mapToItem(dragOverlay, node.x, node.y)
                                        : Qt.point(node.x, node.y)
            var localPos = listRoot.mapFromItem(dragOverlay, globalPos.x, globalPos.y)
            node.suspendBehavior = true
            node.parent = listRoot
            node.x = localPos.x
            node.y = localPos.y
            Qt.callLater(function() {
                if (node)
                    node.suspendBehavior = false
            })
        }

        function appendNode(node) {
            var currentIdx = items.indexOf(node)
            if (currentIdx !== -1)
                items.splice(currentIdx, 1)
            updateNodeList(node)
            moveNodeToContainer(node)
            items.push(node)
            requestLayout()
        }

        function insertBeforeNode(node, refNode) {
            if (node === refNode)
                return
            var currentIdx = items.indexOf(node)
            if (currentIdx !== -1)
                items.splice(currentIdx, 1)
            updateNodeList(node)
            moveNodeToContainer(node)
            var refIdx = items.indexOf(refNode)
            if (refIdx < 0)
                items.push(node)
            else
                items.splice(refIdx, 0, node)
            requestLayout()
        }

        function removeNode(node) {
            var idx = items.indexOf(node)
            if (idx >= 0) {
                items.splice(idx, 1)
                requestLayout()
            }
        }

        function insertNodes(targetListId, insertIndex, nodes) {
            if (listId === targetListId) {
                var idx = Math.max(0, Math.min(insertIndex, items.length))
                for (var n = 0; n < nodes.length; n++) {
                    var obj = createNodeObject(nodes[n])
                    if (obj) {
                        items.splice(idx + n, 0, obj)
                        updateNodeList(obj)
                    }
                }
                requestLayout()
                return true
            }

            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (child && child.bodyList
                        && child.bodyList.insertNodes(targetListId, insertIndex, nodes)) {
                    return true
                }
            }
            return false
        }

        function replaceNode(nodeId, nodes) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (!child)
                    continue
                if (child.nodeId === nodeId) {
                    items.splice(i, 1)
                    child.destroy()
                    for (var n = 0; n < nodes.length; n++) {
                        var obj = createNodeObject(nodes[n])
                        if (obj) {
                            items.splice(i + n, 0, obj)
                            updateNodeList(obj)
                        }
                    }
                    requestLayout()
                    return true
                }
                if (child.bodyList && child.bodyList.replaceNode(nodeId, nodes))
                    return true
            }
            return false
        }

        function requestLayout() {
            if (layoutQueued)
                return
            layoutQueued = true
            layoutTimer.start()
        }

        function doLayout() {
            var cy = listKind === "root" ? 0 : 6
            var left = 0
            var childWidth = width - left
            if (childWidth <= 0)
                return

            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                child.width = childWidth
                child.x = left
                child.y = cy
                cy += child.height + (listKind === "root" ? 6 : 4)
            }
            listContentHeight = cy + (listKind === "root" ? 0 : 8)
        }

        function itemBelow(localY) {
            var nearest = null
            var maxOffset = -Infinity
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (child === dragManager.draggedItem || child === dragManager.placeholder)
                    continue
                var offset = localY - (child.y + child.height / 2)
                if (offset < 0 && offset > maxOffset) {
                    maxOffset = offset
                    nearest = child
                }
            }
            return nearest
        }

        function calculateInsertIndex(localY) {
            var insertIndex = 0
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (child === dragManager.draggedItem || child === dragManager.placeholder)
                    continue
                if (localY > child.y + child.height / 2)
                    insertIndex++
            }
            return insertIndex
        }

        function updatePlaceholder(localY) {
            if (!canAcceptDrag()) {
                if (dragManager.placeholder.parentList === listRoot)
                    removeNode(dragManager.placeholder)
                return
            }
            if (dragManager.isDescendant(dragManager.draggedItem, listRoot))
                return
            var ref = itemBelow(localY)
            if (ref)
                insertBeforeNode(dragManager.placeholder, ref)
            else
                appendNode(dragManager.placeholder)
        }

        function dropTargetAt(localX, localY) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (child && child.bodyList && localY >= child.y && localY <= child.y + child.height) {
                    var mapped = child.bodyList.mapFromItem(listRoot, localX, localY)
                    if (mapped.y >= 0 && mapped.y <= child.bodyList.height)
                        return child.bodyList.dropTargetAt(mapped.x, mapped.y)
                }
            }
            return { "listId": listId, "index": calculateInsertIndex(localY) }
        }

        function routePlaceholderAt(localX, localY) {
            if (collapsed) {
                if (dragManager.placeholder.parentList === listRoot)
                    removeNode(dragManager.placeholder)
                return
            }
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (child && child.bodyList && localY >= child.y && localY <= child.y + child.height) {
                    var mapped = child.bodyList.mapFromItem(listRoot, localX, localY)
                    if (mapped.y >= 0 && mapped.y <= child.bodyList.height) {
                        child.bodyList.routePlaceholderAt(mapped.x, mapped.y)
                        return
                    }
                }
            }
            updatePlaceholder(localY)
        }

        function updateMediaNode(itemId, title, duration, thumbSource) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (!child)
                    continue
                if (child.nodeType === "media" && child.nodeId === itemId) {
                    child.applyMediaPatch(title, duration, thumbSource)
                    return true
                }
                if (child.bodyList && child.bodyList.updateMediaNode(
                            itemId, title, duration, thumbSource)) {
                    return true
                }
            }
            return false
        }

        function updateCloudNode(itemId, visible, active, progress, tooltip) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (!child)
                    continue
                if (child.nodeType === "media" && child.nodeId === itemId) {
                    child.applyCloudPatch(visible, active, progress, tooltip)
                    return true
                }
                if (child.bodyList && child.bodyList.updateCloudNode(
                            itemId, visible, active, progress, tooltip)) {
                    return true
                }
            }
            return false
        }

        function editMarkerNode(markerId) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (!child)
                    continue
                if (child.nodeType === "marker" && child.nodeId === markerId) {
                    child.beginEditing()
                    return true
                }
                if (child.bodyList && child.bodyList.editMarkerNode(markerId))
                    return true
            }
            return false
        }

        function updateSectionCounts(counts) {
            for (var i = 0; i < items.length; i++) {
                var child = items[i]
                if (!child)
                    continue
                if ((child.nodeType === "section" || child.nodeType === "subsection")
                        && counts[child.nodeId] !== undefined) {
                    child.applyItemCountPatch(counts[child.nodeId])
                }
                if (child.bodyList)
                    child.bodyList.updateSectionCounts(counts)
            }
        }

        Timer {
            id: layoutTimer
            interval: 0
            repeat: false
            onTriggered: {
                listRoot.layoutQueued = false
                listRoot.doLayout()
            }
        }

        Timer {
            id: rebuildTimer
            interval: 0
            repeat: false
            onTriggered: listRoot.rebuildFromNodes(listRoot.pendingNodes)
        }
    }

    // ── DraggableItem ─────────────────────────────────────────────────────────
    component DraggableItem: Item {
        id: draggable
        property var node
        property string nodeId: ""
        property string nodeType: ""
        property var parentList: null
        property int depth: 0
        property alias dragArea: dragMouse
        property bool dragStarted: false
        property bool suspendBehavior: false
        property real pressX: 0
        property real pressY: 0
        signal clicked()

        onHeightChanged: {
            if (parentList && parentList.requestLayout)
                parentList.requestLayout()
        }

        Behavior on y {
            enabled: !dragStarted && !suspendBehavior
            NumberAnimation { duration: 210; easing.type: Easing.OutCubic }
        }
        Behavior on x {
            enabled: !dragStarted && !suspendBehavior
            NumberAnimation { duration: 210; easing.type: Easing.OutCubic }
        }

        function beginVisualDrag() {
            if (dragStarted)
                return
            dragStarted = true
            suspendBehavior = true
            var globalPos = parent.mapToItem(dragOverlay, x, y)
            parent = dragOverlay
            x = globalPos.x
            y = globalPos.y
            Qt.callLater(function() { draggable.suspendBehavior = false })
            dragManager.startDrag(draggable)
        }

        function moveWithPointer(mouse) {
            var overlayPoint = dragMouse.mapToItem(dragOverlay, mouse.x, mouse.y)
            x = overlayPoint.x - pressX
            y = overlayPoint.y - pressY
            var local = rootPlaylist.mapFromItem(dragOverlay, overlayPoint.x, overlayPoint.y)
            rootPlaylist.routePlaceholderAt(local.x, local.y)
        }

        MouseArea {
            id: dragMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: draggable.dragStarted
                         ? Qt.ClosedHandCursor
                         : Qt.OpenHandCursor

            onPressed: function(mouse) {
                var itemPos = draggable.mapFromItem(dragMouse, mouse.x, mouse.y)
                draggable.pressX = itemPos.x
                draggable.pressY = itemPos.y
            }
            onPositionChanged: function(mouse) {
                if (!pressed)
                    return
                if (!draggable.dragStarted) {
                    var itemPos = draggable.mapFromItem(dragMouse, mouse.x, mouse.y)
                    var dx = itemPos.x - draggable.pressX
                    var dy = itemPos.y - draggable.pressY
                    if ((dx * dx + dy * dy) < 36)
                        return
                    draggable.beginVisualDrag()
                }
                if (draggable.dragStarted) {
                    draggable.moveWithPointer(mouse)
                }
            }
            onReleased: {
                if (draggable.dragStarted) {
                    draggable.dragStarted = false
                    dragManager.endDrag()
                } else {
                    draggable.clicked()
                }
            }
            onCanceled: {
                draggable.dragStarted = false
                dragManager.cancelDrag()
            }
        }
    }

    // ── MediaCard ──────────────────────────────────────────────────────────────

    Component {
        id: mediaComponent
        MediaCard {}
    }

    component MediaCard: DraggableItem {
        id: mediaRoot
        nodeType: "media"
        height: 72
        property string displayTitle: node ? node.title : ""
        property string displayDuration: node ? node.duration : ""
        property string thumbSource: node ? node.thumbSource : ""
        property bool cloudVisible: node ? node.cloudVisible : false
        property bool cloudActive: node ? node.cloudActive : false
        property real cloudProgress: node ? node.cloudProgress : -1
        property string cloudTooltip: node ? node.cloudTooltip : ""
        property bool isMissing: node ? (node.isMissing === true) : false

        Component.onCompleted: {
            dragArea.parent = mediaDragZone
            dragArea.anchors.fill = mediaDragZone
            root.applyPendingMediaPatch(mediaRoot)
        }

        onClicked: {
            if (isMissing) return
            if (root.hasController) root.playlistController.projectItem(nodeId)
        }

        function applyMediaPatch(title, duration, newThumbSource) {
            displayTitle = title
            displayDuration = duration
            if (newThumbSource !== "" && newThumbSource !== thumbSource)
                thumbSource = newThumbSource
            if (node) {
                node.title = title
                node.duration = duration
                if (newThumbSource !== "")
                    node.thumbSource = newThumbSource
            }
        }

        function applyCloudPatch(visible, active, progress, tooltip) {
            cloudVisible = visible
            cloudActive = active
            cloudProgress = progress
            cloudTooltip = tooltip
            if (node) {
                node.cloudVisible = visible
                node.cloudActive = active
                node.cloudProgress = progress
                node.cloudTooltip = tooltip
            }
        }

        Rectangle {
            id: mediaCard
            anchors.fill: parent
            radius: 8
            color: mediaRoot.dragArea.containsMouse ? root.hover : root.surface
            border.width: 1
            border.color: mediaRoot.dragArea.containsMouse ? root.borderStrong : root.border_
            clip: true
            opacity: mediaRoot.isMissing ? 0.50 : 1.0
            Behavior on opacity { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }

            Behavior on color { ColorAnimation { duration: 120 } }
            Behavior on border.color { ColorAnimation { duration: 120 } }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: root.pointerEntered()
                onExited: root.pointerExited()
                onClicked: {
                    if (mediaRoot.isMissing) return
                    if (root.hasController) root.playlistController.projectItem(mediaRoot.nodeId)
                }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 4
                anchors.rightMargin: 8
                anchors.topMargin: 6
                anchors.bottomMargin: 6
                spacing: 0

                // Drag handle
                Item {
                    id: mediaDragZone
                    Layout.preferredWidth: 24
                    Layout.fillHeight: true

                    Image {
                        anchors.centerIn: parent
                        width: 12
                        height: 12
                        source: root.picon("grip", 12, "484f58")
                        opacity: 0.6
                    }
                }

                // Thumbnail — 16:9 ratio
                Rectangle {
                    Layout.preferredWidth: 100
                    Layout.preferredHeight: 58
                    Layout.alignment: Qt.AlignVCenter
                    radius: 6
                    color: "#0d1117"
                    clip: true

                    Image {
                        id: mediaThumbImage
                        anchors.fill: parent
                        source: mediaRoot.thumbSource
                        sourceSize.width: 200
                        sourceSize.height: 116
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        cache: false
                        visible: mediaRoot.thumbSource !== "" && status === Image.Ready
                        opacity: visible ? 1.0 : 0.0
                        Behavior on opacity {
                            NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
                        }
                    }

                    // Placeholder icon
                    Image {
                        anchors.centerIn: parent
                        visible: !mediaThumbImage.visible
                        width: 22
                        height: 22
                        opacity: 0.88
                        source: root.picon(
                            node && node.mediaType === "audio" ? "media_audio"
                            : node && node.mediaType === "image" ? "media_image"
                            : "media_video",
                            22,
                            node && node.mediaType === "audio" ? "d2a8ff"
                            : node && node.mediaType === "image" ? "7ee787"
                            : "58a6ff")
                    }

                    // Duration pill on thumbnail
                    Rectangle {
                        visible: mediaRoot.displayDuration !== ""
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.rightMargin: 4
                        anchors.bottomMargin: 4
                        width: durationLbl.implicitWidth + 8
                        height: 16
                        radius: 3
                        color: "#cc000000"

                        Text {
                            id: durationLbl
                            anchors.centerIn: parent
                            text: mediaRoot.displayDuration
                            color: "#e6edf3"
                            font.pixelSize: 9
                            font.weight: Font.DemiBold
                        }
                    }
                }

                // Text info — title first, badge below
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: 10
                    spacing: 3

                    Item { Layout.fillHeight: true }

                    // Title
                    Text {
                        Layout.fillWidth: true
                        text: mediaRoot.displayTitle
                        color: root.textSecondary
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        elide: Text.ElideRight
                        maximumLineCount: 2
                        wrapMode: Text.WordWrap
                        lineHeight: 1.2
                    }

                    // Badge pill — normal type or missing warning
                    Rectangle {
                        visible: mediaRoot.isMissing || (node && node.badge !== "")
                        width: badgeText.implicitWidth + 10
                        height: 16
                        radius: 4
                        color: mediaRoot.isMissing ? "#2d1b00"
                             : node && node.mediaType === "audio" ? "#1d1530"
                             : node && node.mediaType === "image" ? "#122218"
                             : "#101d2e"

                        Text {
                            id: badgeText
                            anchors.centerIn: parent
                            text: mediaRoot.isMissing
                                  ? "⚠ " + qsTranslate("_PlaylistEditView", "Offline / Syncing")
                                  : (node ? node.badge : "")
                            color: mediaRoot.isMissing ? "#e3a008"
                                 : node && node.mediaType === "audio" ? "#d2a8ff"
                                 : node && node.mediaType === "image" ? "#7ee787"
                                 : "#58a6ff"
                            font.pixelSize: 9
                            font.weight: Font.Bold
                            font.letterSpacing: 0.4
                        }
                    }

                    Item { Layout.fillHeight: true }
                }

                // Right action cluster — fixed width keeps controls aligned and separated.
                Item {
                    Layout.preferredWidth: 62
                    Layout.preferredHeight: 28
                    Layout.alignment: Qt.AlignVCenter

                    RowLayout {
                        anchors.fill: parent
                        spacing: 8

                        CloudDownloadButton {
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Layout.alignment: Qt.AlignVCenter
                            iconSource: root.picon("cloud", 13, "8b949e")
                            toolTipText: mediaRoot.cloudTooltip
                            downloading: mediaRoot.cloudActive
                            progress: mediaRoot.cloudProgress
                            active: mediaRoot.cloudVisible
                            enabled: mediaRoot.cloudVisible
                            opacity: mediaRoot.cloudVisible ? 1.0 : 0.0
                            onPointerEntered: root.pointerEntered()
                            onPointerExited: root.pointerExited()
                            onClicked: if (root.hasController) root.playlistController.downloadItem(mediaRoot.nodeId)
                            Behavior on opacity { NumberAnimation { duration: 160 } }
                        }

                        HeaderButton {
                            iconName: "more"
                            iconSize: 12
                            colorHex: "484f58"
                            implicitWidth: 26
                            implicitHeight: 26
                            onClicked: itemMenu.open()
                            Menu {
                                id: itemMenu
                                width: 150
                                background: MenuPanel {}
                                MenuItem {
                                    text: root.commonTr("MediaDownloadActionDialog", "Play")
                                    icon.source: root.picon("play_all", 13, "8b949e")
                                    enabled: !mediaRoot.isMissing
                                    onTriggered: if (root.hasController) root.playlistController.projectItem(mediaRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuItem {
                                    text: root.commonTr("_WatchedFolderCard", "Rename")
                                    icon.source: root.picon("edit", 13, "8b949e")
                                    onTriggered: if (root.hasController) root.playlistController.renameItem(mediaRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: root.borderStrong } }
                                MenuItem {
                                    text: root.commonTr("_WatchedFolderCard", "Delete")
                                    icon.source: root.picon("trash", 13, "f85149")
                                    onTriggered: if (root.hasController) root.playlistController.removeItem(mediaRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source; danger: true }
                                    background: MenuBg { hovered: parent.hovered; danger: true }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // ── MarkerCard ────────────────────────────────────────────────────────────

    Component {
        id: markerComponent
        MarkerCard {}
    }

    component MarkerCard: DraggableItem {
        id: markerRoot
        nodeType: "marker"
        height: 38
        property string displayText: node ? node.text : ""
        property bool editing: false
        property bool committing: false

        Component.onCompleted: {
            dragArea.parent = markerDragZone
            dragArea.anchors.fill = markerDragZone
        }

        function beginEditing() {
            editing = true
            markerEditor.text = displayText
            markerEditor.forceActiveFocus()
            markerEditor.selectAll()
        }

        function commitEditing() {
            if (!editing || committing)
                return
            committing = true
            displayText = markerEditor.text
            if (node)
                node.text = displayText
            editing = false
            if (root.hasController)
                root.playlistController.renameMarker(markerRoot.nodeId, displayText)
            committing = false
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 2
            anchors.rightMargin: 4
            spacing: 8

            Item {
                id: markerDragZone
                Layout.preferredWidth: 22
                Layout.fillHeight: true

                Image {
                    anchors.centerIn: parent
                    width: 12
                    height: 12
                    source: root.picon("grip", 12, "484f58")
                    opacity: 0.42
                }
            }

            Rectangle {
                Layout.preferredWidth: 28
                Layout.preferredHeight: 1
                Layout.alignment: Qt.AlignVCenter
                color: root.borderStrong
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                Layout.alignment: Qt.AlignVCenter

                MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.LeftButton
                    onEntered: root.pointerEntered()
                    onExited: root.pointerExited()
                    onDoubleClicked: markerRoot.beginEditing()
                }

                Text {
                    anchors.fill: parent
                    visible: !markerRoot.editing
                    text: markerRoot.displayText
                    color: markerRoot.displayText === "" ? root.textDim : root.textMuted
                    font.pixelSize: 11
                    font.weight: Font.Medium
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }

                TextField {
                    id: markerEditor
                    anchors.fill: parent
                    visible: markerRoot.editing
                    selectByMouse: true
                    text: markerRoot.displayText
                    placeholderText: qsTranslate("_PlaylistEditView", "Marker")
                    color: root.textSecondary
                    placeholderTextColor: root.textDim
                    font.pixelSize: 11
                    leftPadding: 6
                    rightPadding: 6
                    topPadding: 0
                    bottomPadding: 0
                    verticalAlignment: TextInput.AlignVCenter
                    background: Rectangle {
                        radius: 5
                        color: "#0f141c"
                        border.width: 1
                        border.color: markerEditor.activeFocus ? root.borderStrong : root.border_
                    }
                    onAccepted: markerRoot.commitEditing()
                    onActiveFocusChanged: if (!activeFocus) markerRoot.commitEditing()
                }
            }

            Rectangle {
                Layout.preferredWidth: 28
                Layout.preferredHeight: 1
                Layout.alignment: Qt.AlignVCenter
                color: root.borderStrong
            }

            HeaderButton {
                iconName: "trash"
                iconSize: 12
                colorHex: "8b949e"
                implicitWidth: 24
                implicitHeight: 24
                toolTipText: qsTranslate("_PlaylistEditView", "Delete marker")
                onClicked: if (root.hasController) root.playlistController.deleteMarker(markerRoot.nodeId)
            }
        }
    }

    // ── SectionCard ────────────────────────────────────────────────────────────

    Component {
        id: sectionComponent
        SectionCard {}
    }

    component SectionCard: DraggableItem {
        id: sectionRoot
        nodeType: node && node.type === "subsection" ? "subsection" : "section"
        height: card.height
        property alias bodyList: childList
        property bool collapsed: node ? node.collapsed : false
        property bool isSubsection: sectionRoot.nodeType === "subsection"
        property int displayItemCount: node ? node.itemCount : 0

        Component.onCompleted: {
            dragArea.parent = sectionDragZone
            dragArea.anchors.fill = sectionDragZone
        }

        function toggleCollapsed() {
            collapsed = !collapsed
            if (node)
                node.collapsed = collapsed
            if (root.hasController)
                root.playlistController.toggleCollapse(nodeId)
        }

        function applyItemCountPatch(count) {
            displayItemCount = count
            if (node)
                node.itemCount = count
        }

        onClicked: sectionRoot.toggleCollapsed()

        Rectangle {
            id: card
            width: sectionRoot.width
            height: contentColumn.implicitHeight
            radius: sectionRoot.isSubsection ? 6 : 10
            color: sectionRoot.isSubsection ? "#0f1318" : root.surface
            border.width: 1
            border.color: sectionRoot.isSubsection ? "#181e28" : root.border_
            clip: true

            // Section accent rail. It sits inside the card so the section color
            // reads as part of the surface instead of as an external border.
            Rectangle {
                width: 4
                anchors.left: parent.left
                anchors.leftMargin: 8
                anchors.top: parent.top
                anchors.topMargin: 8
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 8
                radius: 2
                color: node ? node.color : root.accent
                visible: !sectionRoot.isSubsection
                opacity: 0.95
            }

            Column {
                id: contentColumn
                width: parent.width
                spacing: 0

                // Section header
                Item {
                    id: header
                    width: contentColumn.width
                    height: sectionRoot.isSubsection ? 38 : 48

                    Rectangle {
                        anchors.fill: parent
                        color: headerMa.containsMouse ? Qt.rgba(1, 1, 1, 0.02) : "transparent"
                        Behavior on color { ColorAnimation { duration: 120 } }
                    }

                    MouseArea {
                        id: headerMa
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: root.pointerEntered()
                        onExited: root.pointerExited()
                        onClicked: sectionRoot.toggleCollapsed()
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: sectionRoot.isSubsection ? 4 : 18
                        anchors.rightMargin: 8
                        spacing: sectionRoot.isSubsection ? 2 : 6

                        // Drag handle
                        Item {
                            id: sectionDragZone
                            Layout.preferredWidth: sectionRoot.isSubsection ? 24 : 22
                            Layout.fillHeight: true

                            Image {
                                anchors.centerIn: parent
                                width: 12
                                height: 12
                                source: root.picon("grip", 12, "484f58")
                                opacity: 0.5
                            }
                        }

                        // Section title
                        Text {
                            Layout.fillWidth: true
                            text: node ? node.title : ""
                            color: sectionRoot.isSubsection
                                   ? root.textMuted
                                   : (node ? node.textColor : root.textPrimary)
                            font.pixelSize: sectionRoot.isSubsection ? 11 : 13
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                        }

                        // Item count badge
                        Rectangle {
                            visible: sectionRoot.displayItemCount > 0
                            Layout.preferredWidth: countLabel.implicitWidth + 12
                            Layout.preferredHeight: 18
                            radius: 9
                            color: sectionRoot.isSubsection
                                   ? "#151a22"
                                   : (node ? node.badgeBg : "#1a1f28")

                            Text {
                                id: countLabel
                                anchors.centerIn: parent
                                text: sectionRoot.displayItemCount
                                color: sectionRoot.isSubsection
                                       ? root.textDim
                                       : (node ? node.textColor : root.textMuted)
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }
                        }

                        // Collapse chevron
                        Image {
                            Layout.preferredWidth: 12
                            Layout.preferredHeight: 12
                            Layout.alignment: Qt.AlignVCenter
                            source: root.picon("chevron_down", 12, "484f58")
                            rotation: sectionRoot.collapsed ? -180 : 0
                            Behavior on rotation { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                            MouseArea {
                                anchors.fill: parent
                                anchors.margins: -6
                                hoverEnabled: true
                                onEntered: root.pointerEntered()
                                onExited: root.pointerExited()
                                onClicked: sectionRoot.toggleCollapsed()
                            }
                        }

                        // Context menu
                        HeaderButton {
                            iconName: "more"
                            iconSize: 12
                            colorHex: "484f58"
                            implicitWidth: 24
                            implicitHeight: 24
                            onClicked: sectionMenu.open()
                            Menu {
                                id: sectionMenu
                                width: sectionRoot.isSubsection ? 158 : 176
                                background: MenuPanel {}
                                MenuItem {
                                    text: sectionRoot.isSubsection
                                          ? qsTranslate("_PlaylistEditView", "Rename subsection")
                                          : qsTranslate("_PlaylistEditView", "Rename section")
                                    icon.source: root.picon("edit", 13, "8b949e")
                                    onTriggered: if (root.hasController) root.playlistController.renameSection(sectionRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuItem {
                                    visible: !sectionRoot.isSubsection
                                    height: visible ? implicitHeight : 0
                                    text: qsTranslate("_PlaylistEditView", "Change color")
                                    icon.source: root.picon("palette", 13, "8b949e")
                                    onTriggered: if (root.hasController) root.playlistController.recolorSection(sectionRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuItem {
                                    visible: !sectionRoot.isSubsection
                                    height: visible ? implicitHeight : 0
                                    text: qsTranslate("_PlaylistEditView", "Add subsection")
                                    icon.source: root.picon("section", 13, "8b949e")
                                    onTriggered: if (root.hasController) root.playlistController.newSubsectionClicked(sectionRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuItem {
                                    visible: sectionRoot.isSubsection
                                    height: visible ? implicitHeight : 0
                                    text: qsTranslate("_PlaylistEditView", "Add marker")
                                    icon.source: root.picon("marker", 13, "8b949e")
                                    onTriggered: if (root.hasController) root.playlistController.newMarkerClicked(sectionRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                                    background: MenuBg { hovered: parent.hovered }
                                }
                                MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: root.borderStrong } }
                                MenuItem {
                                    text: sectionRoot.isSubsection
                                          ? qsTranslate("_PlaylistEditView", "Delete subsection")
                                          : qsTranslate("_PlaylistEditView", "Delete section")
                                    icon.source: root.picon("trash", 13, "f85149")
                                    onTriggered: if (root.hasController) root.playlistController.deleteSection(sectionRoot.nodeId)
                                    contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source; danger: true }
                                    background: MenuBg { hovered: parent.hovered; danger: true }
                                }
                            }
                        }
                    }
                }

                // Children area
                DropList {
                    id: childList
                    property int contentInset: sectionRoot.isSubsection ? 20 : 16
                    width: contentColumn.width - contentInset * 2
                    x: contentInset
                    listId: (sectionRoot.isSubsection ? "subsection:" : "section:") + sectionRoot.nodeId
                    listKind: sectionRoot.nodeType
                    depth: sectionRoot.depth + 1
                    collapsed: sectionRoot.collapsed
                    Component.onCompleted: scheduleRebuild(node ? node.children : [])
                }

                // Bottom padding
                Item {
                    width: 1
                    height: sectionRoot.collapsed ? 0 : 6
                    Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                }
            }
        }
    }


    // ═══════════════════════════════════════════════════════════════════════════
    // TREE SUB-COMPONENTS
    // ═══════════════════════════════════════════════════════════════════════════

    component HeaderButton: Rectangle {
        id: hdrBtn
        property string iconName: ""
        property int iconSize: 14
        property string colorHex: "8b949e"
        property string toolTipText: ""
        property bool accentButton: false
        signal clicked()

        implicitWidth: 32
        implicitHeight: 32
        radius: 8
        color: enabled && hdrMa.containsMouse
               ? (accentButton ? "#1a3d6d" : root.hover)
               : (accentButton ? root.accentTint : "transparent")
        border.width: 1
        border.color: enabled && hdrMa.containsMouse
                      ? (accentButton ? "#2d5fa6" : root.borderStrong)
                      : (accentButton ? "#1f4470" : root.border_)
        opacity: enabled ? 1.0 : 0.38
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Image {
            anchors.centerIn: parent
            width: hdrBtn.iconSize
            height: hdrBtn.iconSize
            source: root.picon(hdrBtn.iconName, hdrBtn.iconSize,
                               hdrMa.containsMouse ? "c9d1d9" : hdrBtn.colorHex)
        }

        MouseArea {
            id: hdrMa
            anchors.fill: parent
            enabled: hdrBtn.enabled
            hoverEnabled: true
            onEntered: root.pointerEntered()
            onExited: root.pointerExited()
            onClicked: hdrBtn.clicked()
        }

        ToolTip.visible: hdrBtn.enabled && hdrMa.containsMouse && hdrBtn.toolTipText !== ""
        ToolTip.text: hdrBtn.toolTipText
        ToolTip.delay: 400
    }

    component MenuPanel: Rectangle {
        color: "#1c2128"
        radius: 8
        border.width: 1
        border.color: root.borderStrong
    }

    component MenuLabel: RowLayout {
        property string label: ""
        property string iconSrc: ""
        property bool danger: false
        spacing: 8
        anchors.leftMargin: 8
        Image {
            Layout.preferredWidth: 13
            Layout.preferredHeight: 13
            source: iconSrc
        }
        Text {
            Layout.fillWidth: true
            text: label
            color: danger ? root.danger : root.textSecondary
            font.pixelSize: 12
        }
    }

    component MenuBg: Rectangle {
        property bool hovered: false
        property bool danger: false
        color: hovered ? (danger ? root.dangerSubtle : "#262c36") : "transparent"
        radius: 6
    }
}
