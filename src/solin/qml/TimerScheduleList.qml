pragma Translator: "AdvancedTimerPage"

// TimerScheduleList.qml — single unified schedule list shared by Meetings and
// Custom Schedules. Three clean columns (Part | Planned | Result), zero
// accidental click-to-edit triggers during live operation, right-click context
// menu for editing/actions, visible empty custom sections, and bottom scroll
// clearance so the last row clears the floating QuickAccessToolbar.
import QtQuick
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: listRoot

    property var partsModel: []
    property var live: ({})
    property string scheduleKind: "meeting"
    property bool isRunning: false
    property string scheduleIdentity: ""
    property string selectedPartId: ""
    property color pal_bg: appTheme.bg
    property color pal_surface: appTheme.surface
    property color pal_surfaceAlt: appTheme.surfaceAlt
    property color pal_hover: appTheme.hover
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_textDim: appTheme.textDim
    property color pal_accent: appTheme.accent
    property color pal_accentText: appTheme.accentText
    property color pal_success: appTheme.success
    property color pal_danger: appTheme.danger

    signal partSelected(string partId)
    signal editPartRequested(var partRow)
    signal renameSectionRequested(string sectionId, string sectionTitle)
    signal sectionAppearanceRequested(string sectionId, string sectionTitle, string iconChoice, string customColor)
    signal addPartToSectionRequested(string sectionId)
    signal deletePartRequested(string partId)
    signal deleteSectionRequested(string sectionId)

    // Bottom clearance matching QuickAccessToolbar height (40) + margin (16) + breathing room
    readonly property int floatingToolbarClearance: 84
    readonly property bool narrowList: width < 400
    readonly property int plannedColW: narrowList ? 66 : 94
    readonly property int resultColW: narrowList ? 56 : 78

    // Context menu target state
    property string ctxKind: ""          // "part" | "section"
    property var ctxPartRow: null
    property string ctxSectionId: ""
    property string ctxSectionTitle: ""
    property string ctxSectionIconChoice: "default"
    property string ctxSectionColor: ""
    property int ctxSectionPartCount: 0

    function dismissContextMenu() {
        rowContextMenu.close()
        clearContextTarget()
    }

    function clearContextTarget() {
        ctxKind = ""
        ctxPartRow = null
        ctxSectionId = ""
        ctxSectionTitle = ""
        ctxSectionIconChoice = "default"
        ctxSectionColor = ""
        ctxSectionPartCount = 0
    }

    onScheduleIdentityChanged: dismissContextMenu()
    onScheduleKindChanged: dismissContextMenu()
    onIsRunningChanged: dismissContextMenu()
    onPartsModelChanged: dismissContextMenu()
    Connections {
        target: timer
        function onScheduleChanged() { listRoot.dismissContextMenu() }
    }

    function openPartContextMenu(row, sourceItem, localX, localY) {
        if (isRunning || !row || !row.id) return
        ctxKind = "part"
        ctxPartRow = row
        partSelected(row.id)
        positionAndOpenContextMenu(sourceItem, localX, localY)
    }

    function openSectionContextMenu(row, sourceItem, localX, localY) {
        if (scheduleKind !== "custom" || isRunning)
            return
        ctxKind = "section"
        ctxPartRow = null
        ctxSectionId = row.sectionKey || ""
        ctxSectionTitle = sectionDisplayTitle(row)
        ctxSectionIconChoice = row.sectionIconChoice || "default"
        ctxSectionColor = row.sectionCustomColor || ""
        ctxSectionPartCount = row.rowType === "empty_section" ? 0 : (row.sectionCount || 1)
        positionAndOpenContextMenu(sourceItem, localX, localY)
    }

    function positionAndOpenContextMenu(sourceItem, localX, localY) {
        var overlay = Overlay.overlay
        if (!overlay) {
            rowContextMenu.open()
            return
        }
        var pt = sourceItem.mapToItem(overlay, localX, localY)
        rowContextMenu.popupX = pt.x
        rowContextMenu.popupY = pt.y
        rowContextMenu.open()
    }

    function sectionDisplayTitle(row) {
        if (!row) return ""
        if (row.sectionTitle && row.sectionTitle.length > 0)
            return row.sectionTitle
        var key = row.sectionKey || ""
        if (key === "treasures")   return qsTranslate("_Section", "TREASURES FROM GOD'S WORD")
        if (key === "ministry")    return qsTranslate("_Section", "APPLY YOURSELF TO THE FIELD MINISTRY")
        if (key === "living")      return qsTranslate("_Section", "LIVING AS CHRISTIANS")
        if (key === "public_talk") return qsTranslate("_Section", "PUBLIC TALK")
        if (key === "watchtower")  return qsTranslate("_Section", "Watchtower Study")
        return key
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Column headers (matches mockup: Part | Planned | Result) ─────────
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 32

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 8

                Text {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: qsTr("Part")
                    color: listRoot.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                }
                Text {
                    Layout.preferredWidth: listRoot.plannedColW
                    text: qsTr("Planned")
                    color: listRoot.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.Medium
                    horizontalAlignment: Text.AlignRight
                }
                Text {
                    Layout.preferredWidth: listRoot.resultColW
                    text: qsTr("Result")
                    color: listRoot.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.Medium
                    horizontalAlignment: Text.AlignRight
                }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: listRoot.pal_border
                opacity: 0.75
            }
        }

        // ── Scrollable unified schedule rows ──────────────────────────────────
        Flickable {
            id: scroll
            objectName: "timerScheduleFlickable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: rowsColumn.implicitHeight + listRoot.floatingToolbarClearance
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick

            property bool wheelScrolling: false
            Timer {
                id: wheelStopTimer
                interval: 180
                onTriggered: scroll.wheelScrolling = false
            }
            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: function(ev) {
                    scroll.wheelScrolling = true
                    wheelStopTimer.restart()
                    var dy = ev.pixelDelta.y !== 0 ? ev.pixelDelta.y : ev.angleDelta.y / 2.2
                    var maxY = Math.max(0, scroll.contentHeight - scroll.height)
                    scroll.contentY = Math.max(0, Math.min(maxY, scroll.contentY - dy))
                    ev.accepted = true
                }
            }

            // Smooth week-change transition for meetings
            property real weekShiftOffset: 0
            property real weekShiftOpacity: 1
            ParallelAnimation {
                id: weekShiftAnim
                NumberAnimation {
                    target: scroll; property: "weekShiftOffset"
                    to: 0; duration: 220; easing.type: Easing.OutCubic
                }
                NumberAnimation {
                    target: scroll; property: "weekShiftOpacity"
                    to: 1.0; duration: 190; easing.type: Easing.OutQuad
                }
            }
            Connections {
                target: timer
                function onWeekShift(direction) {
                    if (!direction) return
                    weekShiftAnim.stop()
                    scroll.weekShiftOffset = direction * 18
                    scroll.weekShiftOpacity = 0.25
                    weekShiftAnim.start()
                }
            }

            ColumnLayout {
                id: rowsColumn
                x: scroll.weekShiftOffset
                opacity: scroll.weekShiftOpacity
                width: scroll.width - (verticalScroll.visible ? verticalScroll.width + 2 : 0)
                spacing: 0

                Repeater {
                    id: partsRepeater
                    model: listRoot.partsModel

                    delegate: ColumnLayout {
                        id: entryGroup
                        required property var modelData
                        required property int index

                        readonly property bool isEmptySection: modelData.rowType === "empty_section"
                        readonly property bool isCustom: listRoot.scheduleKind === "custom"
                        readonly property bool showHeader: modelData.firstOfSection === true && modelData.showSectionHeader === true
                        readonly property bool inSection: modelData.showSectionHeader === true
                        readonly property color sectionAccent: modelData.sectionColor || listRoot.pal_accent
                        readonly property color sectionText: modelData.sectionTextColor || listRoot.pal_textPrimary
                        readonly property color sectionBadgeBg: modelData.sectionBadgeBg || listRoot.pal_surfaceAlt
                        readonly property string sectionIconKey: modelData.sectionIconKey || ""

                        Layout.preferredWidth: rowsColumn.width
                        Layout.fillWidth: true
                        spacing: 0

                        // ── Unified section header (identical visual system for Meetings & Custom) ──
                        Rectangle {
                            id: sectionHeader
                            objectName: "timerSectionHeader"
                            visible: entryGroup.showHeader
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 42 : 0
                            Layout.topMargin: (visible && entryGroup.index > 0) ? 10 : 2
                            radius: 8
                            color: (entryGroup.isCustom && !listRoot.isRunning && secRowHover.hovered)
                                   ? Qt.rgba(listRoot.pal_hover.r, listRoot.pal_hover.g, listRoot.pal_hover.b, 0.55)
                                   : "transparent"

                            border.color: activeFocus ? listRoot.pal_accent : "transparent"
                            border.width: activeFocus ? 1.5 : 0
                            activeFocusOnTab: visible && entryGroup.isCustom && !listRoot.isRunning
                            onActiveFocusChanged: {
                                if (activeFocus) scroll.positionViewAtChild(sectionHeader, Flickable.Contain)
                            }
                            Accessible.role: Accessible.ListItem
                            Accessible.name: listRoot.sectionDisplayTitle(entryGroup.modelData)
                            Keys.onPressed: function(event) {
                                if (!listRoot.isRunning && (event.key === Qt.Key_Menu ||
                                        (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier)))) {
                                    listRoot.openSectionContextMenu(entryGroup.modelData, sectionHeader, 0, height)
                                    event.accepted = true
                                }
                            }
                            Behavior on color { ColorAnimation { duration: 110 } }

                            HoverHandler { id: secRowHover }

                            // Right-click on custom section header opens context menu
                            MouseArea {
                                anchors.fill: parent
                                enabled: entryGroup.isCustom && !listRoot.isRunning
                                acceptedButtons: Qt.RightButton
                                onClicked: function(mouse) {
                                    listRoot.openSectionContextMenu(entryGroup.modelData, sectionHeader, mouse.x, mouse.y)
                                }
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                anchors.topMargin: 4
                                anchors.bottomMargin: 4
                                spacing: 8

                                // Generic section icon badge (darker/tinted bg derived from section color)
                                Rectangle {
                                    id: iconBadge
                                    objectName: "timerSectionIconBadge"
                                    readonly property bool hasIcon: entryGroup.sectionIconKey.length > 0
                                    visible: hasIcon
                                    Layout.preferredWidth: visible ? 26 : 0
                                    Layout.preferredHeight: 26
                                    radius: 7
                                    color: entryGroup.sectionBadgeBg
                                    border.color: Qt.rgba(entryGroup.sectionAccent.r, entryGroup.sectionAccent.g, entryGroup.sectionAccent.b, 0.28)
                                    border.width: 1

                                    TimerIcon {
                                        anchors.centerIn: parent
                                        visible: iconBadge.hasIcon
                                        width: 15
                                        height: 15
                                        rasterSize: 30
                                        iconName: iconBadge.hasIcon ? entryGroup.sectionIconKey : ""
                                        tint: entryGroup.sectionAccent
                                    }
                                }

                                Text {
                                    id: sectionTitleText
                                    objectName: "timerSectionTitleText"
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    textFormat: Text.PlainText
                                    text: listRoot.sectionDisplayTitle(entryGroup.modelData)
                                    color: entryGroup.sectionText
                                    font.pixelSize: 13
                                    font.weight: Font.Bold
                                    elide: Text.ElideRight
                                }

                                // ONLY meeting-exclusive control in the list: part-count stepper
                                // for Ministry & Living as Christians
                                Loader {
                                    active: entryGroup.modelData.configurableCount === true
                                    visible: active
                                    Layout.alignment: Qt.AlignVCenter
                                    sourceComponent: TimerStepper {
                                        objectName: "timerSectionCountStepper"
                                        text: "" + (entryGroup.modelData.sectionCount || 1)
                                        accent: entryGroup.sectionAccent
                                        fieldWidth: 26
                                        valueFontSize: 12
                                        decTip: qsTr("Fewer parts")
                                        incTip: qsTr("More parts")
                                        onDecremented: timer.setSectionCount(entryGroup.modelData.section, (entryGroup.modelData.sectionCount || 1) - 1)
                                        onIncremented: timer.setSectionCount(entryGroup.modelData.section, (entryGroup.modelData.sectionCount || 1) + 1)
                                    }
                                }

                                Text {
                                    visible: !listRoot.narrowList && !!entryGroup.modelData.sectionTotalLabel
                                    text: entryGroup.modelData.sectionTotalLabel || ""
                                    color: listRoot.pal_textMuted
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                }
                            }
                        }

                        // ── Empty custom section row (so empty sections stay visible!) ──
                        Item {
                            objectName: "timerEmptySectionRow"
                            visible: entryGroup.isEmptySection
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 34 : 0

                            MouseArea {
                                anchors.fill: parent
                                enabled: entryGroup.isCustom && !listRoot.isRunning
                                acceptedButtons: Qt.RightButton
                                onClicked: function(mouse) {
                                    listRoot.openSectionContextMenu(entryGroup.modelData, parent, mouse.x, mouse.y)
                                }
                            }

                            Text {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.leftMargin: listRoot.narrowList ? 28 : 44
                                anchors.rightMargin: 12
                                anchors.verticalCenter: parent.verticalCenter
                                text: qsTr("Empty section")
                                color: listRoot.pal_textDim
                                font.pixelSize: 12
                                font.italic: true
                                elide: Text.ElideRight
                            }

                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12
                                height: 1
                                color: listRoot.pal_border
                                opacity: 0.35
                            }
                        }

                        // ── Unified part row (left-click selects; right-click opens context menu) ──
                        Rectangle {
                            id: partRow
                            objectName: "timerPartRow"
                            visible: !entryGroup.isEmptySection
                            readonly property string pid: entryGroup.modelData.id || ""
                            readonly property string partState: entryGroup.modelData.state || "idle"
                            readonly property bool isActive: !!listRoot.live && listRoot.live.active === true && listRoot.live.state === "running" && listRoot.live.active_part_id === pid
                            readonly property bool isSelected: !listRoot.isRunning && listRoot.selectedPartId === pid
                            readonly property bool isPartRunning: partState === "running"
                            readonly property bool isPartStopped: partState === "stopped"
                            readonly property bool isOverrun: isActive && !!listRoot.live && listRoot.live.overrun === true
                            readonly property bool hasAdjusted: entryGroup.modelData.hasAdjustedTarget === true

                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            implicitHeight: visible ? (hasAdjusted ? 52 : 44) : 0
                            radius: 8
                            color: isActive
                                   ? Qt.rgba(
                                         (isOverrun ? listRoot.pal_danger : entryGroup.sectionAccent).r,
                                         (isOverrun ? listRoot.pal_danger : entryGroup.sectionAccent).g,
                                         (isOverrun ? listRoot.pal_danger : entryGroup.sectionAccent).b,
                                         0.16
                                     )
                                   : (isSelected
                                      ? Qt.rgba(entryGroup.sectionAccent.r, entryGroup.sectionAccent.g, entryGroup.sectionAccent.b, 0.10)
                                      : (rowMouse.containsMouse ? listRoot.pal_hover : "transparent"))

                            Behavior on color { ColorAnimation { duration: 140 } }

                            border.color: partRow.activeFocus ? listRoot.pal_accent : "transparent"
                            border.width: partRow.activeFocus ? 1.5 : 0
                            activeFocusOnTab: visible && !listRoot.isRunning

                            onActiveFocusChanged: {
                                if (activeFocus) scroll.positionViewAtChild(partRow, Flickable.Contain)
                            }

                            Accessible.role: Accessible.ListItem
                            Accessible.name: (entryGroup.modelData.title || "") + ", " + (entryGroup.modelData.plannedLabel || "")
                            Accessible.onPressAction: {
                                if (!listRoot.isRunning)
                                    listRoot.partSelected(partRow.pid)
                            }

                            function focusAdjacentPart(step) {
                                var n = partsRepeater.count
                                for (var idx = entryGroup.index + step; idx >= 0 && idx < n; idx += step) {
                                    var grp = partsRepeater.itemAt(idx)
                                    if (!grp || grp.isEmptySection) continue
                                    for (var c = 0; c < grp.children.length; c++) {
                                        var child = grp.children[c]
                                        if (child && child.objectName === "timerPartRow" && child.visible) {
                                            child.forceActiveFocus()
                                            listRoot.partSelected(child.pid)
                                            return
                                        }
                                    }
                                }
                            }

                            Keys.onPressed: function(event) {
                                if (!listRoot.isRunning && (event.key === Qt.Key_Menu ||
                                        (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier)))) {
                                    listRoot.openPartContextMenu(entryGroup.modelData, partRow, 0, height)
                                    event.accepted = true
                                } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space)
                                    && !listRoot.isRunning) {
                                    listRoot.partSelected(partRow.pid)
                                    event.accepted = true
                                }
                            }
                            Keys.onUpPressed: function(event) {
                                if (!listRoot.isRunning) {
                                    partRow.focusAdjacentPart(-1)
                                    event.accepted = true
                                }
                            }
                            Keys.onDownPressed: function(event) {
                                if (!listRoot.isRunning) {
                                    partRow.focusAdjacentPart(1)
                                    event.accepted = true
                                }
                            }

                            MouseArea {
                                id: rowMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                cursorShape: !listRoot.isRunning ? Qt.PointingHandCursor : Qt.ArrowCursor
                                onClicked: function(mouse) {
                                    if (mouse.button === Qt.RightButton) {
                                        listRoot.openPartContextMenu(entryGroup.modelData, partRow, mouse.x, mouse.y)
                                    } else if (!listRoot.isRunning) {
                                        listRoot.partSelected(partRow.pid)
                                    }
                                }
                            }

                            // Left indicator pill tucked cleanly inside the rounded row
                            Rectangle {
                                visible: partRow.isActive || partRow.isSelected
                                anchors.left: parent.left
                                anchors.leftMargin: 4
                                anchors.top: parent.top
                                anchors.bottom: parent.bottom
                                anchors.topMargin: 7
                                anchors.bottomMargin: 7
                                width: 3
                                radius: 1.5
                                color: partRow.isOverrun ? listRoot.pal_danger : entryGroup.sectionAccent
                                opacity: partRow.isActive ? 1.0 : 0.78
                            }

                            // Subtle hairline divider between rows (matches mockup)
                            Rectangle {
                                visible: !partRow.isActive && !partRow.isSelected
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.leftMargin: 12
                                anchors.rightMargin: 12
                                height: 1
                                color: listRoot.pal_border
                                opacity: 0.42
                            }

                            RowLayout {
                                id: rowContent
                                anchors.fill: parent
                                anchors.leftMargin: 12 + (entryGroup.inSection ? (listRoot.narrowList ? 8 : 16) : 0)
                                anchors.rightMargin: 12
                                anchors.topMargin: 5
                                anchors.bottomMargin: 5
                                spacing: 8

                                // Status glyph (play triangle when running, check when completed)
                                Item {
                                    Layout.preferredWidth: 16
                                    Layout.preferredHeight: 16
                                    Layout.alignment: Qt.AlignVCenter

                                    TimerIcon {
                                        anchors.centerIn: parent
                                        visible: partRow.isPartRunning || partRow.isPartStopped
                                        width: partRow.isPartRunning ? 12 : 14
                                        height: partRow.isPartRunning ? 12 : 14
                                        rasterSize: 28
                                        iconName: partRow.isPartRunning ? "play" : (partRow.isPartStopped ? "check" : "")
                                        tint: partRow.isPartRunning ? (partRow.isOverrun ? listRoot.pal_danger : listRoot.pal_accentText) : (entryGroup.modelData.overTarget ? listRoot.pal_danger : listRoot.pal_textMuted)
                                    }
                                }

                                // Part title (safe against accidental clicks; right-click or central edit to rename)
                                Text {
                                    id: titleText
                                    objectName: "timerPartTitleText"
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.alignment: Qt.AlignVCenter
                                    textFormat: Text.PlainText
                                    text: entryGroup.modelData.title || ""
                                    color: partRow.isActive || partRow.isSelected
                                           ? listRoot.pal_textPrimary
                                           : (partRow.isPartStopped ? listRoot.pal_textSecondary : listRoot.pal_textPrimary)
                                    font.pixelSize: 13
                                    font.weight: (partRow.isActive || partRow.isSelected) ? Font.DemiBold : Font.Normal
                                    elide: Text.ElideRight
                                }

                                // Planned duration column (+ adjusted target when pacing differs)
                                ColumnLayout {
                                    Layout.preferredWidth: listRoot.plannedColW
                                    Layout.alignment: Qt.AlignVCenter
                                    spacing: 0

                                    Text {
                                        objectName: "timerPartPlannedText"
                                        Layout.fillWidth: true
                                        text: entryGroup.modelData.plannedLabel || ""
                                        color: partRow.isActive || partRow.isSelected
                                               ? listRoot.pal_textPrimary : listRoot.pal_textSecondary
                                        font.pixelSize: 13
                                        font.weight: (partRow.isActive || partRow.isSelected) ? Font.DemiBold : Font.Normal
                                        horizontalAlignment: Text.AlignRight
                                    }

                                    Text {
                                        objectName: "timerRowAdjustedTargetLabel"
                                        visible: partRow.hasAdjusted
                                        Layout.fillWidth: true
                                        text: qsTr("Target %1").arg(entryGroup.modelData.effectiveLabel || "")
                                        color: listRoot.pal_accentText
                                        font.pixelSize: 10
                                        font.weight: Font.DemiBold
                                        horizontalAlignment: Text.AlignRight
                                        elide: Text.ElideRight
                                    }
                                }

                                // Result column
                                Text {
                                    objectName: "timerPartResultText"
                                    Layout.preferredWidth: listRoot.resultColW
                                    Layout.alignment: Qt.AlignVCenter
                                    text: entryGroup.modelData.resultLabel && entryGroup.modelData.resultLabel.length > 0
                                          ? entryGroup.modelData.resultLabel : "—"
                                    color: entryGroup.modelData.overTarget
                                           ? listRoot.pal_danger
                                           : (partRow.isPartStopped ? listRoot.pal_textPrimary : listRoot.pal_textDim)
                                    font.pixelSize: 13
                                    font.weight: partRow.isPartStopped ? Font.DemiBold : Font.Normal
                                    horizontalAlignment: Text.AlignRight
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }
            }

            ScrollBar.vertical: ScrollBar {
                id: verticalScroll
                policy: ScrollBar.AsNeeded
                width: 10
                contentItem: Rectangle {
                    implicitWidth: 5
                    radius: 3
                    color: ((verticalScroll.active && !scroll.wheelScrolling) || verticalScroll.hovered)
                           ? listRoot.pal_textMuted : listRoot.pal_borderStrong
                    opacity: (verticalScroll.active || verticalScroll.hovered || scroll.moving) ? 1.0 : 0.35
                    anchors.horizontalCenter: parent.horizontalCenter
                    Behavior on color { ColorAnimation { duration: 150 } }
                    Behavior on opacity { NumberAnimation { duration: 180 } }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // RIGHT-CLICK CONTEXT MENU (Parts & Custom Sections)
    // ═══════════════════════════════════════════════════════════════════════════
    component ContextMenuRow: Rectangle {
        id: menuItem
        property string label: ""
        property string iconName: ""
        property bool destructive: false
        signal triggered()

        Layout.fillWidth: true
        implicitHeight: 34
        radius: 8
        opacity: enabled ? 1.0 : 0.42
        activeFocusOnTab: visible && enabled
        Accessible.role: Accessible.MenuItem
        Accessible.name: label
        Accessible.onPressAction: activate()
        function activate() {
            if (!visible || !enabled || !rowContextMenu.opened || !listRoot.ctxKind || listRoot.isRunning) return
            triggered()
            listRoot.dismissContextMenu()
        }
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                activate()
                event.accepted = true
            } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
                rowContextMenu.focusAction(menuItem, event.key === Qt.Key_Down ? 1 : -1)
                event.accepted = true
            }
        }
        color: (enabled && (itemMouse.containsMouse || activeFocus))
               ? (destructive
                  ? Qt.rgba(listRoot.pal_danger.r, listRoot.pal_danger.g, listRoot.pal_danger.b, 0.14)
                  : listRoot.pal_hover)
               : "transparent"

        Behavior on color { ColorAnimation { duration: 100 } }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 10
            anchors.rightMargin: 10
            spacing: 9

            TimerIcon {
                visible: menuItem.iconName !== ""
                Layout.preferredWidth: 14
                Layout.preferredHeight: 14
                rasterSize: 28
                iconName: menuItem.iconName
                tint: menuItem.destructive ? listRoot.pal_danger : listRoot.pal_textSecondary
            }

            Text {
                Layout.fillWidth: true
                text: menuItem.label
                color: menuItem.destructive ? listRoot.pal_danger : listRoot.pal_textPrimary
                font.pixelSize: 13
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
        }

        TimerPointerArea {
            id: itemMouse
            anchors.fill: parent
            enabled: menuItem.enabled
            onClicked: menuItem.activate()
        }
    }

    Popup {
        id: rowContextMenu
        objectName: "timerRowContextMenu"
        parent: Overlay.overlay
        property real popupX: 0
        property real popupY: 0
        x: parent ? Math.max(8, Math.min(parent.width - width - 8, popupX)) : 0
        y: parent ? Math.max(8, Math.min(parent.height - height - 8, popupY)) : 0
        width: 220
        padding: 6
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        function focusAction(current, step) {
            var items = menuContent.children
            var start = current ? items.indexOf(current) : (step > 0 ? -1 : items.length)
            for (var offset = 1; offset <= items.length; offset++) {
                var index = (start + step * offset + items.length * 2) % items.length
                var item = items[index]
                if (item && item.visible && item.enabled && item.activeFocusOnTab) {
                    item.forceActiveFocus()
                    return
                }
            }
        }
        onOpened: focusAction(null, 1)
        onAboutToHide: listRoot.clearContextTarget()

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 110; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.96; to: 1.0; duration: 110; easing.type: Easing.OutCubic }
        }

        background: Rectangle {
            radius: 12
            color: listRoot.pal_surface
            border.color: listRoot.pal_borderStrong
            border.width: 1
        }

        contentItem: ColumnLayout {
            id: menuContent
            spacing: 2

            // ── Part actions ──────────────────────────────────────────────────
            ContextMenuRow {
                objectName: "timerCtxStartPart"
                visible: listRoot.ctxKind === "part" && !listRoot.isRunning && !!listRoot.ctxPartRow
                iconName: "play"
                label: (listRoot.ctxPartRow && listRoot.ctxPartRow.state === "stopped")
                       ? qsTr("Restart")
                       : qsTr("Start")
                onTriggered: if (listRoot.ctxPartRow) timer.startPart(listRoot.ctxPartRow.id)
            }

            ContextMenuRow {
                objectName: "timerCtxResetPart"
                visible: listRoot.ctxKind === "part" && !listRoot.isRunning
                         && !!listRoot.ctxPartRow && listRoot.ctxPartRow.state === "stopped"
                iconName: "reset"
                label: qsTr("Reset this part")
                onTriggered: if (listRoot.ctxPartRow) timer.resetPart(listRoot.ctxPartRow.id)
            }

            ContextMenuRow {
                objectName: "timerCtxEditPart"
                visible: listRoot.ctxKind === "part" && !!listRoot.ctxPartRow
                enabled: !listRoot.isRunning
                iconName: "edit"
                label: qsTr("Edit part…")
                onTriggered: if (listRoot.ctxPartRow) listRoot.editPartRequested(listRoot.ctxPartRow)
            }

            Rectangle {
                visible: listRoot.ctxKind === "part" && listRoot.scheduleKind === "custom"
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: listRoot.pal_border
                opacity: 0.7
            }

            ContextMenuRow {
                objectName: "timerCtxDeletePart"
                visible: listRoot.ctxKind === "part" && listRoot.scheduleKind === "custom"
                enabled: !listRoot.isRunning
                destructive: true
                iconName: "trash"
                label: qsTr("Delete part")
                onTriggered: if (listRoot.ctxPartRow) listRoot.deletePartRequested(listRoot.ctxPartRow.id)
            }

            // ── Custom section actions ────────────────────────────────────────
            ContextMenuRow {
                objectName: "timerCtxRenameSection"
                visible: listRoot.ctxKind === "section"
                iconName: "edit"
                label: qsTr("Rename section…")
                onTriggered: listRoot.renameSectionRequested(listRoot.ctxSectionId, listRoot.ctxSectionTitle)
            }

            ContextMenuRow {
                objectName: "timerCtxSectionAppearance"
                visible: listRoot.ctxKind === "section"
                iconName: "sliders"
                label: qsTr("Section appearance…")
                onTriggered: listRoot.sectionAppearanceRequested(
                    listRoot.ctxSectionId,
                    listRoot.ctxSectionTitle,
                    listRoot.ctxSectionIconChoice,
                    listRoot.ctxSectionColor
                )
            }

            ContextMenuRow {
                objectName: "timerCtxAddPartToSection"
                visible: listRoot.ctxKind === "section"
                iconName: "plus"
                label: qsTr("Add part to section")
                onTriggered: listRoot.addPartToSectionRequested(listRoot.ctxSectionId)
            }

            Rectangle {
                visible: listRoot.ctxKind === "section"
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: listRoot.pal_border
                opacity: 0.7
            }

            ContextMenuRow {
                objectName: "timerCtxDeleteSection"
                visible: listRoot.ctxKind === "section"
                enabled: listRoot.ctxSectionPartCount === 0
                destructive: true
                iconName: "trash"
                label: qsTr("Delete section")
                onTriggered: listRoot.deleteSectionRequested(listRoot.ctxSectionId)
            }
        }
    }
}
