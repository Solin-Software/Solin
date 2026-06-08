// JWMediaCatalogModal.qml — Full-screen overlay for browsing JW.org videos.
// Self-contained modal with grid layout, skeleton loading, search, and placement popup.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: modalRoot
    anchors.fill: parent
    visible: false
    z: 1000
    color: "transparent"

    // ── Theme ─────────────────────────────────────────────────────────────────
    readonly property color bg:              "#0d1117"
    readonly property color surface:         "#13161c"
    readonly property color surfaceElevated: "#1c2128"
    readonly property color hover:           "#1a1f2a"
    readonly property color border_:         "#1e2430"
    readonly property color borderStrong:    "#2a3040"
    readonly property color textPrimary:     "#e6edf3"
    readonly property color textSecondary:   "#c9d1d9"
    readonly property color textMuted:       "#8b949e"
    readonly property color textDim:         "#484f58"
    readonly property color accent:          "#388bfd"
    readonly property color accentTint:      "#132a46"

    // ── State ─────────────────────────────────────────────────────────────────
    property bool isOpen: false
    property real preservedGridY: 0

    readonly property bool hasBridge: typeof catalogBridge !== "undefined" && catalogBridge !== null

    // ── Category label resolver ───────────────────────────────────────────────
    function categoryLabel(key) {
        var labels = {
            "all":      qsTranslate("JWMediaCatalogBridge", "All"),
            "latest":   qsTranslate("JWMediaCatalogBridge", "Latest"),
            "featured": qsTranslate("JWMediaCatalogBridge", "Featured"),
            "studio":   qsTranslate("JWMediaCatalogBridge", "Studio")
        }
        return labels[key] || key
    }

    function paginationItems(current, total, maxPages) {
        var items = []
        if (total <= 1)
            return items
        maxPages = Math.max(5, maxPages || 9)

        function addPage(page) {
            items.push({ "type": "page", "page": page, "label": String(page) })
        }

        function addGap() {
            items.push({ "type": "gap", "page": 0, "label": "..." })
        }

        if (total <= maxPages) {
            for (var page = 1; page <= total; page++)
                addPage(page)
            return items
        }

        addPage(1)

        var middleSlots = Math.max(1, maxPages - 4)
        var start = Math.max(2, current - Math.floor(middleSlots / 2))
        var end = Math.min(total - 1, start + middleSlots - 1)
        start = Math.max(2, end - middleSlots + 1)

        if (current <= Math.ceil(middleSlots / 2) + 2) {
            start = 2
            end = Math.min(total - 1, middleSlots + 1)
        }
        if (current >= total - Math.ceil(middleSlots / 2) - 1) {
            end = total - 1
            start = Math.max(2, total - middleSlots)
        }

        if (start > 2)
            addGap()
        for (var i = start; i <= end; i++)
            addPage(i)
        if (end < total - 1)
            addGap()

        addPage(total)
        return items
    }

    function paginationCapacity(width) {
        var available = Math.max(260, width * 0.65)
        var slots = Math.floor((available - 96) / 36)
        slots = Math.max(5, slots)
        return slots % 2 === 0 ? slots - 1 : slots
    }

    function gridCellWidth(width) {
        var columns = Math.max(1, Math.min(6, Math.floor(width / 180)))
        return width / columns
    }

    function pointInsideSearchBox(x, y) {
        var p = searchBox.mapToItem(modalPanel, 0, 0)
        return x >= p.x && x <= p.x + searchBox.width
            && y >= p.y && y <= p.y + searchBox.height
    }

    function clearSearchFocusIfOutside(x, y) {
        if (searchField.activeFocus && !pointInsideSearchBox(x, y))
            modalPanel.forceActiveFocus()
    }

    // ── Open / Close ──────────────────────────────────────────────────────────
    function open() {
        if (isOpen) return
        visible = true
        isOpen = true
        openAnim.start()
        if (hasBridge) catalogBridge.openModal()
        modalPanel.forceActiveFocus()
    }

    function close() {
        if (!isOpen) return
        isOpen = false
        closeAnim.start()
    }

    // ── Connections ───────────────────────────────────────────────────────────
    Connections {
        target: modalRoot.hasBridge ? catalogBridge : null

        function onModalShouldClose() {
            modalRoot.close()
        }

        function onItemAddedSuccessfully(message) {
            // Could show a toast; for now the modal just acknowledges
        }

        function onCatalogPageRefreshAboutToStart() {
            modalRoot.preservedGridY = videoGrid.contentY
        }

        function onCatalogPageRefreshFinished() {
            Qt.callLater(function() {
                videoGrid.contentY = videoGrid.clampContentY(modalRoot.preservedGridY)
            })
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    //  OVERLAY BACKGROUND
    // ═══════════════════════════════════════════════════════════════════════════
    Rectangle {
        id: overlayBg
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.65)
        opacity: 0

        MouseArea {
            anchors.fill: parent
            onClicked: modalRoot.close()
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    //  MODAL PANEL
    // ═══════════════════════════════════════════════════════════════════════════
    Rectangle {
        id: modalPanel
        anchors.centerIn: parent
        width:  Math.min(parent.width - 32, parent.width * 0.85)
        height: Math.min(parent.height - 32, parent.height * 0.75)
        radius: 14
        color:  modalRoot.surface
        border.width: 1
        border.color: modalRoot.border_
        clip: true
        opacity: 0
        scale: 0.95
        focus: true

        // Keyboard handling
        Keys.onEscapePressed: modalRoot.close()

        TapHandler {
            acceptedButtons: Qt.LeftButton
            gesturePolicy: TapHandler.WithinBounds
            onTapped: function(point, button) {
                modalRoot.clearSearchFocusIfOutside(point.position.x, point.position.y)
            }
        }

        // The visual panel itself does not consume mouse events.  Without this
        // catcher, clicks on empty panel space fall through to overlayBg and
        // are treated as outside clicks.
        MouseArea {
            anchors.fill: parent
            z: 0
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onClicked: function(mouse) { mouse.accepted = true }
            onPressed: function(mouse) {
                modalRoot.clearSearchFocusIfOutside(mouse.x, mouse.y)
                mouse.accepted = true
            }
            onReleased: function(mouse) { mouse.accepted = true }
        }

        // ── Entrance animation ────────────────────────────────────────────────
        ParallelAnimation {
            id: openAnim
            NumberAnimation { target: overlayBg;  property: "opacity"; from: 0; to: 1;    duration: 200; easing.type: Easing.OutCubic }
            NumberAnimation { target: modalPanel; property: "opacity"; from: 0; to: 1;    duration: 200; easing.type: Easing.OutCubic }
            NumberAnimation { target: modalPanel; property: "scale";   from: 0.95; to: 1; duration: 200; easing.type: Easing.OutCubic }
        }

        // ── Exit animation ────────────────────────────────────────────────────
        ParallelAnimation {
            id: closeAnim
            NumberAnimation { target: overlayBg;  property: "opacity"; from: 1; to: 0;    duration: 150; easing.type: Easing.InCubic }
            NumberAnimation { target: modalPanel; property: "opacity"; from: 1; to: 0;    duration: 150; easing.type: Easing.InCubic }
            NumberAnimation { target: modalPanel; property: "scale";   from: 1; to: 0.95; duration: 150; easing.type: Easing.InCubic }

            onFinished: {
                modalRoot.visible = false
                if (modalRoot.hasBridge) catalogBridge.reset()
            }
        }

        // Trigger entrance animation when isOpen changes to true
        onVisibleChanged: {
            if (modalRoot.isOpen && visible)
                openAnim.start()
        }

        // ── Main column layout ────────────────────────────────────────────────
        ColumnLayout {
            z: 1
            anchors.fill: parent
            anchors.margins: 0
            spacing: 0

            // ── Header ────────────────────────────────────────────────────────
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 52

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTranslate("JWMediaCatalogBridge", "Browse JW.org")
                    color: modalRoot.textPrimary
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }

                Row {
                    anchors.right: closeBtn.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 8

                    BusyRing {
                        anchors.verticalCenter: parent.verticalCenter
                        visible: modalRoot.hasBridge
                                 && catalogBridge.isLoading
                                 && !catalogBridge.catalogComplete
                    }

                    CheckMark {
                        anchors.verticalCenter: parent.verticalCenter
                        visible: modalRoot.hasBridge
                                 && catalogBridge.catalogComplete
                                 && !catalogBridge.isLoading
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: modalRoot.hasBridge ? catalogBridge.loadStatusText : ""
                        color: modalRoot.textDim
                        font.pixelSize: 11
                        visible: modalRoot.hasBridge
                                 && catalogBridge.isLoading
                                 && !catalogBridge.catalogComplete
                        width: Math.min(130, implicitWidth)
                        elide: Text.ElideRight
                    }
                }

                // Close button
                Rectangle {
                    id: closeBtn
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    width: 32
                    height: 32
                    radius: 8
                    color: closeMa.containsMouse ? modalRoot.hover : "transparent"
                    border.width: closeMa.containsMouse ? 1 : 0
                    border.color: modalRoot.border_

                    Behavior on color { ColorAnimation { duration: 120 } }

                    Text {
                        anchors.centerIn: parent
                        text: "✕"
                        color: closeMa.containsMouse ? modalRoot.textPrimary : modalRoot.textMuted
                        font.pixelSize: 14
                        font.weight: Font.Medium

                        Behavior on color { ColorAnimation { duration: 120 } }
                    }

                    MouseArea {
                        id: closeMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: modalRoot.close()
                    }
                }
            }

            // ── Header divider ────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: modalRoot.border_
                opacity: 0.6
            }

            // ── Search bar ────────────────────────────────────────────────────
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 52
                Layout.leftMargin: 16
                Layout.rightMargin: 16

                Rectangle {
                    id: searchBox
                    anchors.fill: parent
                    anchors.topMargin: 10
                    anchors.bottomMargin: 4
                    radius: 8
                    color: searchField.activeFocus ? "#0e1720" : modalRoot.bg
                    border.width: 1
                    border.color: searchField.activeFocus ? modalRoot.accent : modalRoot.border_

                    Behavior on color        { ColorAnimation { duration: 140 } }
                    Behavior on border.color  { ColorAnimation { duration: 140 } }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 8
                        spacing: 8

                        SearchIcon {
                            Layout.preferredWidth: 14
                            Layout.preferredHeight: 14
                            active: searchField.activeFocus
                        }

                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true

                            Text {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                text: qsTranslate("JWMediaCatalogBridge", "Search by title...")
                                color: modalRoot.textDim
                                font.pixelSize: 13
                                visible: searchField.text.length === 0 && !searchField.activeFocus
                                elide: Text.ElideRight
                            }

                            TextInput {
                                id: searchField
                                anchors.fill: parent
                                color: modalRoot.textPrimary
                                selectionColor: "#1f6feb"
                                selectedTextColor: "#ffffff"
                                selectByMouse: true
                                font.pixelSize: 13
                                verticalAlignment: TextInput.AlignVCenter
                                clip: true

                                onTextChanged: {
                                    searchDebounce.restart()
                                }
                            }
                        }

                        // Clear button
                        Rectangle {
                            Layout.preferredWidth: 22
                            Layout.preferredHeight: 22
                            radius: 6
                            visible: searchField.text.length > 0
                            color: clearSearchMa.containsMouse ? modalRoot.hover : "transparent"

                            Text {
                                anchors.centerIn: parent
                                text: "✕"
                                color: clearSearchMa.containsMouse ? modalRoot.textSecondary : modalRoot.textMuted
                                font.pixelSize: 12
                                Behavior on color { ColorAnimation { duration: 100 } }
                            }

                            MouseArea {
                                id: clearSearchMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    searchField.text = ""
                                    searchField.forceActiveFocus()
                                    if (modalRoot.hasBridge) catalogBridge.setSearchQuery("")
                                }
                            }
                        }
                    }
                }

                Timer {
                    id: searchDebounce
                    interval: 300
                    repeat: false
                    onTriggered: {
                        if (modalRoot.hasBridge)
                            catalogBridge.setSearchQuery(searchField.text)
                    }
                }
            }

            // ── Results count ─────────────────────────────────────────────────
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                Layout.leftMargin: 18
                Layout.rightMargin: 18

                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.right: audioDescChip.left
                    anchors.rightMargin: 12
                    elide: Text.ElideRight
                    text: {
                        if (!modalRoot.hasBridge) return ""
                        var count = catalogBridge.resultCount
                        if (count === 0)
                            return qsTranslate("JWMediaCatalogBridge", "No results")
                        return count + " " + qsTranslate("JWMediaCatalogBridge", "videos")
                    }
                    color: modalRoot.textDim
                    font.pixelSize: 11
                }

                ToggleChip {
                    id: audioDescChip
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    label: qsTranslate("JWMediaCatalogBridge", "Audio description")
                    checked: modalRoot.hasBridge && catalogBridge.includeAudioDescription
                    visible: modalRoot.hasBridge
                    onToggled: if (modalRoot.hasBridge)
                        catalogBridge.setIncludeAudioDescription(checked)
                }
            }

            // ── Divider ───────────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.rightMargin: 16
                Layout.preferredHeight: 1
                color: modalRoot.border_
                opacity: 0.4
            }

            // ── Content area (Grid / Skeleton / Error / Empty) ────────────────
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.topMargin: 4

                // ── Error state ───────────────────────────────────────────────
                Rectangle {
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 48, 360)
                    height: errorCol.implicitHeight + 32
                    radius: 10
                    color: "transparent"
                    border.width: 1
                    border.color: "#3d1214"
                    visible: modalRoot.hasBridge && catalogBridge.errorMessage !== ""
                    z: 2

                    ColumnLayout {
                        id: errorCol
                        anchors.centerIn: parent
                        spacing: 8

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: "⚠"
                            font.pixelSize: 24
                            color: "#f85149"
                        }
                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            Layout.maximumWidth: 300
                            text: modalRoot.hasBridge ? catalogBridge.errorMessage : ""
                            color: modalRoot.textSecondary
                            font.pixelSize: 12
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ── Empty state ───────────────────────────────────────────────
                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: 8
                    visible: modalRoot.hasBridge
                             && !catalogBridge.isLoading
                             && catalogBridge.resultCount === 0
                             && catalogBridge.errorMessage === ""

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "📭"
                        font.pixelSize: 28
                        opacity: 0.6
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTranslate("JWMediaCatalogBridge", "No videos found")
                        color: modalRoot.textMuted
                        font.pixelSize: 13
                    }
                }

                // ── Skeleton grid ─────────────────────────────────────────────
                GridView {
                    id: skeletonGrid
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.topMargin: 8
                    visible: modalRoot.hasBridge && catalogBridge.isLoading
                             && catalogBridge.resultCount === 0
                    clip: true
                    interactive: false
                    cellWidth:  modalRoot.gridCellWidth(skeletonGrid.width)
                    cellHeight: 170
                    model: 6

                    delegate: Item {
                        width:  skeletonGrid.cellWidth
                        height: skeletonGrid.cellHeight

                        SkeletonCard {
                            anchors.fill: parent
                            anchors.margins: 6
                        }
                    }
                }

                // ── Video grid ────────────────────────────────────────────────
                GridView {
                    id: videoGrid
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.topMargin: 8
                    visible: !skeletonGrid.visible
                             && (modalRoot.hasBridge && catalogBridge.errorMessage === "")
                    clip: true
                    cellWidth:  modalRoot.gridCellWidth(videoGrid.width)
                    cellHeight: 170
                    cacheBuffer: 600
                    boundsBehavior: Flickable.StopAtBounds
                    maximumFlickVelocity: 4800
                    flickDeceleration: 2400
                    model: modalRoot.hasBridge ? catalogBridge.model : null

                    // ── Smooth wheel scrolling ────────────────────────────────
                    property real wheelTargetY: contentY

                    function clampContentY(value) {
                        var maxY = Math.max(0, contentHeight - height)
                        return Math.max(0, Math.min(maxY, value))
                    }

                    function smoothWheelScroll(delta) {
                        if (!gridWheelAnim.running) wheelTargetY = contentY
                        wheelTargetY = clampContentY(wheelTargetY + delta)
                        gridWheelAnim.stop()
                        gridWheelAnim.to = wheelTargetY
                        gridWheelAnim.start()
                    }

                    NumberAnimation {
                        id: gridWheelAnim
                        target: videoGrid
                        property: "contentY"
                        duration: 200
                        easing.type: Easing.OutCubic
                    }

                    WheelHandler {
                        target: videoGrid
                        acceptedDevices: PointerDevice.Mouse
                        onWheel: function(event) {
                            var hasPixelDelta = event.pixelDelta.y !== 0
                            var rawDelta = hasPixelDelta ? event.pixelDelta.y : event.angleDelta.y
                            var scrollDelta = hasPixelDelta ? -rawDelta : -rawDelta * 0.45
                            videoGrid.smoothWheelScroll(scrollDelta)
                            event.accepted = true
                        }
                    }

                    // ── Lazy loading trigger ──────────────────────────────────
                    onContentYChanged: {
                        // Page navigation happens in the footer.  Keep this
                        // handler reserved for future scroll-state polish.
                    }

                    // ── Delegate ──────────────────────────────────────────────
                    delegate: Item {
                        id: cardShell
                        width:  videoGrid.cellWidth
                        height: videoGrid.cellHeight

                        required property int    index
                        required property string title
                        required property string thumbnailSource
                        required property string durationText

                        VideoCard {
                            anchors.fill: parent
                            anchors.margins: 6
                            cardTitle:    cardShell.title
                            cardThumb:    cardShell.thumbnailSource
                            cardDuration: cardShell.durationText
                            onClicked: {
                                if (modalRoot.hasBridge)
                                    catalogBridge.selectItem(cardShell.index)
                            }
                        }
                    }

                    ScrollBar.vertical: ScrollBar {
                        id: gridScrollBar
                        policy: ScrollBar.AsNeeded
                        width: 8
                        contentItem: Rectangle {
                            implicitWidth: 4
                            radius: 2
                            color: (gridScrollBar.active || gridScrollBar.hovered)
                                   ? modalRoot.textMuted : modalRoot.borderStrong
                            opacity: (gridScrollBar.active || gridScrollBar.hovered
                                      || videoGrid.moving) ? 1.0 : 0.3
                            anchors.horizontalCenter: parent.horizontalCenter
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                            Behavior on color   { ColorAnimation   { duration: 150 } }
                        }
                    }
                }
            }

            // ── Pagination ───────────────────────────────────────────────────
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: modalRoot.hasBridge && catalogBridge.pageCount > 1 ? 48 : 0
                visible: modalRoot.hasBridge && catalogBridge.pageCount > 1

                Row {
                    anchors.centerIn: parent
                    spacing: 4

                    ArrowButton {
                        direction: -1
                        enabled: modalRoot.hasBridge && catalogBridge.currentPage > 1
                        onClicked: if (modalRoot.hasBridge) catalogBridge.previousPage()
                    }

                    Repeater {
                        model: modalRoot.hasBridge
                               ? modalRoot.paginationItems(
                                     catalogBridge.currentPage,
                                     catalogBridge.pageCount,
                                     modalRoot.paginationCapacity(modalPanel.width)
                                 )
                               : []

                        Item {
                            required property var modelData
                            width: modelData.type === "gap" ? 18 : pageItem.width
                            height: 32

                            Text {
                                anchors.centerIn: parent
                                visible: modelData.type === "gap"
                                text: modelData.label
                                color: modalRoot.textDim
                                font.pixelSize: 13
                            }

                            PageButton {
                                id: pageItem
                                visible: modelData.type === "page"
                                label: modelData.label
                                active: modalRoot.hasBridge
                                        && catalogBridge.currentPage === modelData.page
                                onClicked: if (modalRoot.hasBridge)
                                    catalogBridge.setPage(modelData.page)
                            }
                        }
                    }

                    ArrowButton {
                        direction: 1
                        enabled: modalRoot.hasBridge && catalogBridge.currentPage < catalogBridge.pageCount
                        onClicked: if (modalRoot.hasBridge) catalogBridge.nextPage()
                    }
                }
            }
        }

        // ═══════════════════════════════════════════════════════════════════════
        //  PLACEMENT POPUP OVERLAY
        // ═══════════════════════════════════════════════════════════════════════
        Rectangle {
            id: placementOverlay
            anchors.fill: parent
            color: Qt.rgba(0, 0, 0, 0.50)
            visible: modalRoot.hasBridge && catalogBridge.showPlacement
            opacity: visible ? 1 : 0
            z: 100

            Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    // Clicking backdrop cancels placement
                    if (modalRoot.hasBridge) catalogBridge.cancelSelection()
                }
            }

            PlacementPopup {
                id: placementCard
                anchors.centerIn: parent
                width: Math.min(parent.width - 40, 340)
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    //  INLINE COMPONENTS
    // ═══════════════════════════════════════════════════════════════════════════

    // ── SearchIcon ────────────────────────────────────────────────────────────
    component SearchIcon: Canvas {
        id: searchIcon
        property bool active: false

        implicitWidth: 14
        implicitHeight: 14
        antialiasing: true

        onActiveChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()

        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            ctx.strokeStyle = active ? "#6e7a8a" : "#4d5866"
            ctx.lineWidth = 1.7
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.beginPath()
            ctx.arc(width * 0.46, height * 0.46, width * 0.27, 0, Math.PI * 2)
            ctx.stroke()
            ctx.beginPath()
            ctx.moveTo(width * 0.66, height * 0.66)
            ctx.lineTo(width * 0.86, height * 0.86)
            ctx.stroke()
        }
    }

    // ── BusyRing ──────────────────────────────────────────────────────────────
    component BusyRing: Item {
        id: ring
        width: 16
        height: 16

        Canvas {
            id: ringCanvas
            anchors.fill: parent
            antialiasing: true

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.lineWidth = 2
                ctx.lineCap = "round"
                ctx.strokeStyle = modalRoot.accent
                ctx.beginPath()
                ctx.arc(width / 2, height / 2, 5.5, -Math.PI * 0.15, Math.PI * 1.35)
                ctx.stroke()
            }

            RotationAnimation on rotation {
                running: ring.visible
                from: 0
                to: 360
                duration: 900
                loops: Animation.Infinite
            }
        }
    }

    // ── CheckMark ─────────────────────────────────────────────────────────────
    component CheckMark: Item {
        width: 16
        height: 16

        Canvas {
            anchors.fill: parent
            antialiasing: true

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.lineWidth = 2
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                ctx.strokeStyle = "#3fb950"
                ctx.beginPath()
                ctx.moveTo(4, 8.4)
                ctx.lineTo(7, 11.2)
                ctx.lineTo(12.3, 5.2)
                ctx.stroke()
            }
        }
    }

    // ── ToggleChip ────────────────────────────────────────────────────────────
    component ToggleChip: Rectangle {
        id: chip
        property string label: ""
        property bool checked: false
        signal toggled(bool checked)

        width: chipContent.implicitWidth + 18
        height: 28
        radius: 7
        color: chipMa.containsMouse ? modalRoot.hover : "transparent"
        border.width: 1
        border.color: checked ? "#1f6feb" : modalRoot.border_

        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Row {
            id: chipContent
            anchors.centerIn: parent
            spacing: 8

            Rectangle {
                width: 24
                height: 14
                radius: 7
                anchors.verticalCenter: parent.verticalCenter
                color: chip.checked ? modalRoot.accentTint : modalRoot.bg
                border.width: 1
                border.color: chip.checked ? modalRoot.accent : modalRoot.borderStrong

                Rectangle {
                    width: 8
                    height: 8
                    radius: 4
                    y: 3
                    x: chip.checked ? 13 : 3
                    color: chip.checked ? "#79c0ff" : modalRoot.textDim

                    Behavior on x { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
                    Behavior on color { ColorAnimation { duration: 120 } }
                }
            }

            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: chip.label
                color: chipMa.containsMouse || chip.checked ? modalRoot.textSecondary : modalRoot.textDim
                font.pixelSize: 11
                font.weight: chip.checked ? Font.Medium : Font.Normal
            }
        }

        MouseArea {
            id: chipMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: chip.toggled(!chip.checked)
        }
    }

    // ── ArrowButton ───────────────────────────────────────────────────────────
    component ArrowButton: Rectangle {
        id: arrowBtn
        property int direction: 1
        signal clicked()

        width: 32
        height: 32
        radius: 8
        color: {
            if (!enabled) return "transparent"
            if (arrowMa.containsMouse) return modalRoot.hover
            return "transparent"
        }
        opacity: enabled ? 1.0 : 0.35

        Behavior on color { ColorAnimation { duration: 120 } }

        Canvas {
            id: arrowCanvas
            anchors.centerIn: parent
            width: 12
            height: 12
            antialiasing: true

            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.lineWidth = 1.8
                ctx.lineCap = "round"
                ctx.lineJoin = "round"
                ctx.strokeStyle = arrowMa.containsMouse ? modalRoot.textSecondary : modalRoot.textMuted
                ctx.beginPath()
                if (arrowBtn.direction < 0) {
                    ctx.moveTo(7.5, 2.5)
                    ctx.lineTo(4.5, 6)
                    ctx.lineTo(7.5, 9.5)
                } else {
                    ctx.moveTo(4.5, 2.5)
                    ctx.lineTo(7.5, 6)
                    ctx.lineTo(4.5, 9.5)
                }
                ctx.stroke()
            }

            Connections {
                target: arrowMa
                function onContainsMouseChanged() { arrowCanvas.requestPaint() }
            }
        }

        MouseArea {
            id: arrowMa
            anchors.fill: parent
            enabled: arrowBtn.enabled
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: arrowBtn.clicked()
        }
    }

    // ── PageButton ────────────────────────────────────────────────────────────
    component PageButton: Rectangle {
        id: pageBtn
        property string label: ""
        property bool active: false
        signal clicked()

        width: Math.max(32, pageLabel.implicitWidth + 16)
        height: 32
        radius: 8
        color: {
            if (!enabled) return "transparent"
            if (active) return modalRoot.accentTint
            if (pageMa.containsMouse) return modalRoot.hover
            return "transparent"
        }
        border.width: active ? 1 : 0
        border.color: "#1f6feb"
        opacity: enabled ? 1.0 : 0.35

        Behavior on color { ColorAnimation { duration: 120 } }

        Text {
            id: pageLabel
            anchors.centerIn: parent
            text: pageBtn.label
            color: {
                if (pageBtn.active) return "#79c0ff"
                if (pageMa.containsMouse) return modalRoot.textSecondary
                return modalRoot.textMuted
            }
            font.pixelSize: 13
            font.weight: pageBtn.active ? Font.DemiBold : Font.Medium

            Behavior on color { ColorAnimation { duration: 120 } }
        }

        MouseArea {
            id: pageMa
            anchors.fill: parent
            enabled: pageBtn.enabled
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: pageBtn.clicked()
        }
    }

    // ── VideoCard ─────────────────────────────────────────────────────────────
    component VideoCard: Rectangle {
        id: card
        property string cardTitle:    ""
        property string cardThumb:    ""
        property string cardDuration: ""
        signal clicked()

        radius: 8
        color: cardMa.containsMouse ? modalRoot.hover : "transparent"
        border.width: cardMa.containsMouse ? 1 : 0
        border.color: cardMa.containsMouse ? modalRoot.borderStrong : "transparent"
        scale: cardMa.containsMouse ? 1.03 : 1.0

        Behavior on color        { ColorAnimation   { duration: 120 } }
        Behavior on border.color { ColorAnimation   { duration: 120 } }
        Behavior on scale        { NumberAnimation  { duration: 120; easing.type: Easing.OutCubic } }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 5
            spacing: 6

            // Thumbnail area
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: (parent.width - 10) * 9 / 16

                // Fallback colored rect
                Rectangle {
                    anchors.fill: parent
                    radius: 6
                    color: modalRoot.surfaceElevated
                    visible: !thumbImage.visible
                }

                // Thumbnail image
                Image {
                    id: thumbImage
                    anchors.fill: parent
                    source: card.cardThumb
                    fillMode: Image.PreserveAspectCrop
                    visible: status === Image.Ready
                    smooth: true
                    asynchronous: true
                }

                // Rounded corners overlay (border trick for smooth rounded look)
                Rectangle {
                    anchors.fill: parent
                    radius: 6
                    color: "transparent"
                    border.width: 1
                    border.color: Qt.rgba(1, 1, 1, 0.06)
                }

                // Duration badge
                Rectangle {
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.rightMargin: 5
                    anchors.bottomMargin: 5
                    width: durationLabel.implicitWidth + 10
                    height: 18
                    radius: 4
                    color: Qt.rgba(0, 0, 0, 0.72)
                    visible: card.cardDuration !== ""

                    Text {
                        id: durationLabel
                        anchors.centerIn: parent
                        text: card.cardDuration
                        color: "#ffffff"
                        font.pixelSize: 10
                        font.weight: Font.Medium
                        font.family: "Consolas"
                    }
                }

                // Loading shimmer when thumb is loading
                Rectangle {
                    anchors.fill: parent
                    radius: 6
                    visible: card.cardThumb !== "" && thumbImage.status === Image.Loading
                    clip: true
                    color: modalRoot.surfaceElevated

                    Rectangle {
                        id: thumbLoadShimmer
                        width: parent.width * 1.5
                        height: parent.height
                        y: 0

                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: modalRoot.surfaceElevated }
                            GradientStop { position: 0.4; color: modalRoot.borderStrong }
                            GradientStop { position: 0.5; color: modalRoot.borderStrong }
                            GradientStop { position: 1.0; color: modalRoot.surfaceElevated }
                        }

                        NumberAnimation on x {
                            from: -thumbLoadShimmer.width
                            to:    thumbLoadShimmer.parent ? thumbLoadShimmer.parent.width : 200
                            duration: 1500
                            loops: Animation.Infinite
                            easing.type: Easing.InOutQuad
                        }
                    }
                }
            }

            // Title text
            Text {
                Layout.fillWidth: true
                Layout.fillHeight: true
                text: card.cardTitle
                color: cardMa.containsMouse ? modalRoot.textPrimary : modalRoot.textSecondary
                font.pixelSize: 12
                font.weight: Font.Medium
                elide: Text.ElideRight
                maximumLineCount: 2
                wrapMode: Text.Wrap
                verticalAlignment: Text.AlignTop
                lineHeight: 1.2

                Behavior on color { ColorAnimation { duration: 120 } }
            }
        }

        MouseArea {
            id: cardMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: card.clicked()
        }
    }

    // ── SkeletonCard ──────────────────────────────────────────────────────────
    component SkeletonCard: Rectangle {
        id: skeleton
        radius: 8
        color: "transparent"

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 5
            spacing: 6

            // Thumbnail skeleton
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: (parent.width - 10) * 9 / 16
                radius: 6
                color: modalRoot.surfaceElevated
                clip: true

                Rectangle {
                    id: shimmer1
                    width: parent.width * 1.5
                    height: parent.height

                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: modalRoot.surfaceElevated }
                        GradientStop { position: 0.35; color: modalRoot.borderStrong }
                        GradientStop { position: 0.50; color: modalRoot.borderStrong }
                        GradientStop { position: 1.0; color: modalRoot.surfaceElevated }
                    }

                    NumberAnimation on x {
                        from: -shimmer1.width
                        to:   shimmer1.parent ? shimmer1.parent.width : 200
                        duration: 1500
                        loops: Animation.Infinite
                        easing.type: Easing.InOutQuad
                    }
                }
            }

            // Title skeleton — line 1
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 10
                Layout.rightMargin: 16
                radius: 3
                color: modalRoot.surfaceElevated
                clip: true

                Rectangle {
                    id: shimmer2
                    width: parent.width * 1.5
                    height: parent.height

                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: modalRoot.surfaceElevated }
                        GradientStop { position: 0.35; color: modalRoot.borderStrong }
                        GradientStop { position: 0.50; color: modalRoot.borderStrong }
                        GradientStop { position: 1.0; color: modalRoot.surfaceElevated }
                    }

                    NumberAnimation on x {
                        from: -shimmer2.width
                        to:   shimmer2.parent ? shimmer2.parent.width : 200
                        duration: 1500
                        loops: Animation.Infinite
                        easing.type: Easing.InOutQuad
                    }
                }
            }

            // Title skeleton — line 2
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 10
                Layout.rightMargin: 40
                radius: 3
                color: modalRoot.surfaceElevated
                clip: true

                Rectangle {
                    id: shimmer3
                    width: parent.width * 1.5
                    height: parent.height

                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: modalRoot.surfaceElevated }
                        GradientStop { position: 0.35; color: modalRoot.borderStrong }
                        GradientStop { position: 0.50; color: modalRoot.borderStrong }
                        GradientStop { position: 1.0; color: modalRoot.surfaceElevated }
                    }

                    NumberAnimation on x {
                        from: -shimmer3.width
                        to:   shimmer3.parent ? shimmer3.parent.width : 200
                        duration: 1500
                        loops: Animation.Infinite
                        easing.type: Easing.InOutQuad
                    }
                }
            }

            Item { Layout.fillHeight: true }
        }
    }

    // ── PlacementPopup ────────────────────────────────────────────────────────
    component PlacementPopup: Rectangle {
        id: popup
        height: popupContent.implicitHeight + 32
        radius: 12
        color: modalRoot.surface
        border.width: 1
        border.color: modalRoot.borderStrong

        MouseArea {
            anchors.fill: parent
            z: 0
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onClicked: function(mouse) { mouse.accepted = true }
            onPressed: function(mouse) { mouse.accepted = true }
            onReleased: function(mouse) { mouse.accepted = true }
        }

        // Slide-up entrance
        transform: Translate {
            id: popupTranslate
            y: placementOverlay.visible ? 0 : 24
            Behavior on y { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
        }

        // Subtle shadow
        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            z: -1
            radius: parent.radius + 1
            color: Qt.rgba(0, 0, 0, 0.3)
        }

        ColumnLayout {
            id: popupContent
            z: 1
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 16
            spacing: 12

            // Title
            Text {
                text: qsTranslate("JWMediaCatalogBridge", "Where to add?")
                color: modalRoot.textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }

            // Selected video preview
            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                // Thumbnail
                Rectangle {
                    Layout.preferredWidth: 72
                    Layout.preferredHeight: 40
                    radius: 6
                    color: modalRoot.surfaceElevated
                    clip: true

                    Image {
                        anchors.fill: parent
                        source: modalRoot.hasBridge ? catalogBridge.pendingItemThumb : ""
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        visible: status === Image.Ready
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: modalRoot.hasBridge ? catalogBridge.pendingItemTitle : ""
                    color: modalRoot.textSecondary
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.Wrap
                    lineHeight: 1.2
                }
            }

            // Divider
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: modalRoot.border_
                opacity: 0.5
            }

            // Placement options
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4

                Repeater {
                    model: modalRoot.hasBridge ? catalogBridge.placementOptions : []

                    Rectangle {
                        id: optionBtn
                        required property var modelData

                        Layout.fillWidth: true
                        height: 38
                        radius: 8
                        color: optionMa.containsMouse ? modalRoot.hover : "transparent"
                        border.width: optionMa.containsMouse ? 1 : 0
                        border.color: optionMa.containsMouse ? modalRoot.borderStrong : "transparent"

                        Behavior on color { ColorAnimation { duration: 100 } }

                        // Accent bar for section-type options
                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: 4
                            anchors.verticalCenter: parent.verticalCenter
                            width: 3
                            height: 18
                            radius: 1.5
                            color: optionBtn.modelData.color || modalRoot.accent
                            visible: optionBtn.modelData.type === "section"
                            opacity: 0.7
                        }

                        Text {
                            anchors.left: parent.left
                            anchors.leftMargin: optionBtn.modelData.type === "section" ? 16 : 12
                            anchors.right: parent.right
                            anchors.rightMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            text: optionBtn.modelData.label
                            color: optionMa.containsMouse ? modalRoot.textPrimary
                                                          : modalRoot.textSecondary
                            font.pixelSize: 13
                            font.weight: Font.Medium
                            elide: Text.ElideRight

                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        MouseArea {
                            id: optionMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (modalRoot.hasBridge)
                                    catalogBridge.confirmPlacement(optionBtn.modelData.id)
                            }
                        }
                    }
                }
            }

            // Cancel button
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 4
                height: 34
                radius: 8
                color: cancelBtnMa.containsMouse ? modalRoot.hover : modalRoot.surfaceElevated
                border.width: 1
                border.color: modalRoot.border_

                Behavior on color { ColorAnimation { duration: 100 } }

                Text {
                    anchors.centerIn: parent
                    text: qsTranslate("JWMediaCatalogBridge", "Cancel")
                    color: cancelBtnMa.containsMouse ? modalRoot.textPrimary : modalRoot.textMuted
                    font.pixelSize: 13
                    font.weight: Font.Medium

                    Behavior on color { ColorAnimation { duration: 100 } }
                }

                MouseArea {
                    id: cancelBtnMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (modalRoot.hasBridge) catalogBridge.cancelSelection()
                    }
                }
            }
        }
    }
}
