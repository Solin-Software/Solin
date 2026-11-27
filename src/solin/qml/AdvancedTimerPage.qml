// AdvancedTimerPage.qml — unified Schedule workspace for Meetings and Custom
// Schedules. Shares one schedule list and one centralized clock/action panel
// across both tabs, keeps Export PDF and Settings in a fixed header position,
// and adapts smoothly from 340 px compact layouts to wide desktop split view.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: page
    objectName: "advancedTimerViewport"

    // Palette mirrors Solin's active theme tokens (writable so TimerView can pass overrides)
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
    property color pal_warning: appTheme.warning
    property color pal_danger: appTheme.danger

    // Responsive breakpoints based on effective QML content width
    readonly property bool ultraNarrow: width < 400
    readonly property bool compactBar: width < 560
    readonly property bool splitWorkspace: width >= 760
    readonly property int outerPad: ultraNarrow ? 8 : (compactBar ? 12 : 20)
    readonly property int rightColumnWidth: Math.min(340, Math.max(280, Math.round(Math.max(0, width - outerPad * 2) * 0.34)))

    // Tab and live state from TimerBridge
    readonly property string activeTab: timer.activeScheduleTab
    readonly property bool onMeetings: activeTab !== "custom"
    readonly property bool onCustom: activeTab === "custom"
    readonly property var live: timer.liveState
    readonly property bool isRunning: timer.isRunning
    readonly property var summary: timer.scheduleSummary
    readonly property string scheduleIdentity: String(summary.scheduleId || "") + ":" + String(summary.scheduleRevision || 0)
    readonly property var pacing: timer.pacingState
    readonly property var customList: timer.customSchedules
    readonly property var activeCustom: timer.activeCustomSchedule
    readonly property bool hasActiveCustom: onCustom && !!activeCustom && !!activeCustom.id
    readonly property bool canExportPdf: timer.canExportPdf
    readonly property var feedback: timer.feedback

    // Explicit structure-organization mode for custom schedules
    property bool editingCustomSchedule: false

    // Selected part in the unified schedule list (drives centralized clock panel when idle)
    property string selectedPartId: ""

    readonly property var resolvedSelectedPart: {
        var list = timer.parts
        if (list.length === 0) return null
        if (selectedPartId) {
            for (var i = 0; i < list.length; i++) {
                if (list[i].id === selectedPartId)
                    return list[i]
            }
        }
        for (var j = 0; j < list.length; j++) {
            if (list[j].state === "idle")
                return list[j]
        }
        return list[0]
    }

    onIsRunningChanged: {
        if (isRunning && editingCustomSchedule)
            editingCustomSchedule = false
        if (isRunning && live && live.active_part_id)
            selectedPartId = live.active_part_id
    }
    onActiveTabChanged: {
        editingCustomSchedule = false
        selectedPartId = ""
    }
    onHasActiveCustomChanged: {
        if (!hasActiveCustom)
            editingCustomSchedule = false
    }

    Connections {
        target: timer
        function onScheduleChanged() {
            // Keep selection valid if still present, otherwise let resolvedSelectedPart auto-pick
            var list = timer.parts
            if (page.selectedPartId) {
                var found = false
                for (var i = 0; i < list.length; i++) {
                    if (list[i].id === page.selectedPartId) {
                        found = true
                        break
                    }
                }
                if (!found) page.selectedPartId = ""
            }
        }
        function onLiveStateChanged() {
            var lv = timer.liveState
            if (lv && lv.active === true && lv.state === "running" && lv.active_part_id) {
                if (page.selectedPartId !== lv.active_part_id)
                    page.selectedPartId = lv.active_part_id
            }
        }
    }

    readonly property int selectedCustomIndex: {
        if (!hasActiveCustom) return -1
        for (var i = 0; i < customList.length; i++) {
            if (customList[i].id === activeCustom.id) return i
        }
        return -1
    }

    readonly property var customScheduleLabels: {
        var out = []
        for (var i = 0; i < customList.length; i++)
            out.push(customList[i].title || "")
        return out
    }

    // Sub-tab button (Meetings | Schedules) with active underline
    component SubTabButton: Item {
        id: subTab
        property string label: ""
        property bool active: false
        property bool blocked: false
        signal clicked()

        readonly property int idealWidth: Math.max(52, tabTxt.implicitWidth + 16)
        Layout.minimumWidth: 0
        Layout.preferredWidth: idealWidth
        Layout.maximumWidth: idealWidth
        Layout.preferredHeight: 36

        activeFocusOnTab: true
        Accessible.role: Accessible.PageTab
        Accessible.name: subTab.label
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                subTab.clicked()
                event.accepted = true
            }
        }

        Rectangle {
            anchors.fill: parent
            radius: 7
            color: (!subTab.active && tabArea.containsMouse && !subTab.blocked)
                   ? page.pal_hover : "transparent"
            border.color: subTab.activeFocus ? page.pal_accent : "transparent"
            border.width: subTab.activeFocus ? 1.5 : 0
            Behavior on color { ColorAnimation { duration: 120 } }
        }

        Text {
            id: tabTxt
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 6
            anchors.rightMargin: 6
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignHCenter
            text: subTab.label
            color: subTab.active
                   ? page.pal_textPrimary
                   : (tabArea.containsMouse && !subTab.blocked ? page.pal_textPrimary : page.pal_textMuted)
            font.pixelSize: 13
            font.weight: subTab.active ? Font.DemiBold : Font.Medium
            elide: Text.ElideRight
            opacity: subTab.blocked && !subTab.active ? 0.55 : 1.0
            Behavior on color { ColorAnimation { duration: 130 } }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 2
            radius: 1
            color: page.pal_accent
            opacity: subTab.active ? 1.0 : 0.0
            Behavior on opacity { NumberAnimation { duration: 160 } }
        }

        TimerPointerArea {
            id: tabArea
            anchors.fill: parent
            enabled: true
            onClicked: subTab.clicked()
        }

        AppToolTip {
            visible: tabArea.containsMouse && (tabTxt.truncated || (subTab.blocked && !subTab.active))
            text: (subTab.blocked && !subTab.active)
                  ? qsTr("Stop the running timer before switching schedules.")
                  : subTab.label
        }
    }

    Rectangle {
        anchors.fill: parent
        color: page.pal_bg
    }

    ColumnLayout {
        id: rootColumn
        objectName: "advancedTimerContent"
        anchors.fill: parent
        anchors.leftMargin: page.outerPad
        anchors.rightMargin: page.outerPad
        anchors.topMargin: 4
        anchors.bottomMargin: page.outerPad
        spacing: page.ultraNarrow ? 8 : 10

        // ═══════════════════════════════════════════════════════════════════════
        // 1. SUB-TAB BAR + FIXED RIGHT-HAND ACTIONS (identical across both tabs)
        // ═══════════════════════════════════════════════════════════════════════
        Item {
            id: subTabBar
            objectName: "timerSubTabBar"
            visible: !page.editingCustomSchedule
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 38 : 0

            readonly property int rightActionsReserve: (handBtn.visible ? handBtn.width + 6 : 0) + exportBtn.width + settingsBtn.width + 20
            readonly property int maxTabWidth: Math.max(48, Math.floor((width - rightActionsReserve) / 2))

            RowLayout {
                anchors.fill: parent
                spacing: page.ultraNarrow ? 4 : 6

                SubTabButton {
                    objectName: "timerMeetingsTabButton"
                    label: qsTr("Meetings")
                    active: page.onMeetings
                    blocked: page.isRunning
                    Layout.maximumWidth: Math.min(idealWidth, subTabBar.maxTabWidth)
                    onClicked: {
                        if (!page.onMeetings && timer.selectScheduleTab)
                            timer.selectScheduleTab("meetings")
                    }
                }

                SubTabButton {
                    objectName: "timerCustomSchedulesTabButton"
                    label: qsTr("Schedules")
                    active: page.onCustom
                    blocked: page.isRunning
                    Layout.maximumWidth: Math.min(idealWidth, subTabBar.maxTabWidth)
                    onClicked: {
                        if (!page.onCustom && timer.selectScheduleTab)
                            timer.selectScheduleTab("custom")
                    }
                }

                Item { Layout.fillWidth: true }

                TimerButton {
                    id: handBtn
                    objectName: "timerManualHandButton"
                    enabled: timer.manualHandAvailable === true
                    iconName: timer.manualHandRaised ? "raised_hand_filled" : "raised_hand"
                    iconSize: 15
                    variant: timer.manualHandRaised ? "soft" : "ghost"
                    accent: page.pal_warning
                    tip: !timer.manualHandAvailable
                         ? qsTr("Show the timer on a monitor to raise hand")
                         : (timer.manualHandRaised ? qsTr("Clear hand alert") : qsTr("Raise hand"))
                    Layout.preferredWidth: 34
                    implicitHeight: 32
                    onClicked: timer.setManualHand(!timer.manualHandRaised)
                }

                TimerButton {
                    id: exportBtn
                    objectName: "timerExportPdfButton"
                    text: page.compactBar ? "" : qsTr("Export PDF")
                    iconName: "export"
                    iconSize: 14
                    tip: page.canExportPdf
                         ? qsTr("Export PDF")
                         : qsTr("Add at least one part to export PDF")
                    variant: "ghost"
                    enabled: page.canExportPdf
                    Layout.preferredWidth: page.compactBar ? 34 : -1
                    implicitHeight: 32
                    onClicked: timer.exportSchedulePdf()
                }

                TimerButton {
                    id: settingsBtn
                    objectName: "timerSettingsButton"
                    iconName: "settings"
                    iconSize: 15
                    tip: qsTr("Timer settings")
                    variant: "ghost"
                    Layout.preferredWidth: 34
                    implicitHeight: 32
                    onClicked: settingsDialog.present(settingsBtn)
                }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: page.pal_border
                opacity: 0.75
            }
        }

        // ═══════════════════════════════════════════════════════════════════════
        // 2. TAB-SPECIFIC CONTEXT BAR (Meetings week nav OR Custom selector/actions)
        // ═══════════════════════════════════════════════════════════════════════
        Item {
            id: contextBar
            Layout.fillWidth: true
            visible: page.onMeetings || (page.onCustom && !page.editingCustomSchedule && page.customList.length > 0)
            Layout.preferredHeight: visible
                                    ? (page.onMeetings ? meetingsContextFlow.implicitHeight : customContextFlow.implicitHeight)
                                    : 0

            // ── Meetings context controls ─────────────────────────────────────
            Flow {
                id: meetingsContextFlow
                visible: page.onMeetings
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: page.splitWorkspace ? 18 : 8

                RowLayout {
                    id: weekNavRow
                    objectName: "timerWeekNavigationRow"
                    width: page.compactBar
                           ? meetingsContextFlow.width
                           : (page.splitWorkspace
                              ? Math.max(240, meetingsContextFlow.width - page.rightColumnWidth - 18)
                              : Math.max(240, meetingsContextFlow.width - meetingTypeSeg.implicitWidth - 8))
                    spacing: 6

                    TimerButton {
                        objectName: "timerPreviousWeekButton"
                        iconName: "chevron_left"
                        iconSize: 14
                        tip: qsTr("Previous week")
                        variant: "ghost"
                        enabled: !page.isRunning
                        Layout.preferredWidth: 40
                        implicitHeight: 40
                        onClicked: timer.previousWeek()
                    }

                    Rectangle {
                        objectName: "timerWeekSummaryPill"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 140
                        implicitHeight: 40
                        radius: 8
                        color: !timer.isCurrentWeek
                               ? Qt.rgba(page.pal_warning.r, page.pal_warning.g, page.pal_warning.b, 0.08)
                               : page.pal_surface
                        border.color: !timer.isCurrentWeek
                                      ? Qt.rgba(page.pal_warning.r, page.pal_warning.g, page.pal_warning.b, 0.45)
                                      : page.pal_border
                        border.width: 1

                        Behavior on color { ColorAnimation { duration: 150 } }
                        Behavior on border.color { ColorAnimation { duration: 150 } }

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 0

                            Item { Layout.fillHeight: true }

                            Text {
                                id: weekLbl
                                objectName: "timerWeekLabel"
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignHCenter
                                text: timer.weekLabel || ""
                                color: page.pal_textPrimary
                                font.pixelSize: timer.isCurrentWeek ? 13 : 12
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }

                            Text {
                                objectName: "timerNotCurrentWeekNotice"
                                Layout.fillWidth: true
                                visible: !timer.isCurrentWeek
                                horizontalAlignment: Text.AlignHCenter
                                text: qsTr("Not the current week")
                                color: page.pal_warning
                                font.pixelSize: 10
                                font.weight: Font.Medium
                                elide: Text.ElideRight
                            }

                            Item { Layout.fillHeight: true }
                        }
                    }

                    TimerButton {
                        objectName: "timerNextWeekButton"
                        iconName: "chevron_right"
                        iconSize: 14
                        tip: qsTr("Next week")
                        variant: "ghost"
                        enabled: !page.isRunning
                        Layout.preferredWidth: 40
                        implicitHeight: 40
                        onClicked: timer.nextWeek()
                    }

                    TimerButton {
                        objectName: "timerCurrentWeekButton"
                        iconName: "home"
                        iconSize: 14
                        tip: timer.isCurrentWeek ? qsTr("Current week") : qsTr("Back to current week")
                        variant: timer.isCurrentWeek ? "ghost" : "soft"
                        accent: page.pal_accent
                        enabled: !timer.isCurrentWeek && !page.isRunning
                        Layout.preferredWidth: 40
                        implicitHeight: 40
                        onClicked: timer.goToCurrentWeek()
                    }
                }

                TimerSegment {
                    id: meetingTypeSeg
                    objectName: "timerMeetingTypeSegment"
                    width: page.compactBar
                           ? meetingsContextFlow.width
                           : (page.splitWorkspace ? page.rightColumnWidth : implicitWidth)
                    segHeight: 40
                    options: [qsTr("Midweek"), qsTr("Weekend")]
                    current: timer.meetingType === "weekend" ? 1 : 0
                    accent: page.pal_accent
                    stretch: page.compactBar || page.splitWorkspace
                    enabled: !page.isRunning
                    onPicked: function(idx) {
                        timer.setMeetingType(idx === 1 ? "weekend" : "midweek")
                    }
                }
            }

            // ── Custom Schedules context controls (matches mockup!) ───────────
            Flow {
                id: customContextFlow
                visible: page.onCustom && !page.editingCustomSchedule && page.customList.length > 0
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: 8

                RowLayout {
                    id: customLeftRow
                    width: page.compactBar ? customContextFlow.width : implicitWidth
                    spacing: 8

                    TimerSelect {
                        id: customSelector
                        objectName: "timerCustomScheduleSelect"
                        Layout.fillWidth: page.compactBar
                        Layout.preferredWidth: page.compactBar ? -1 : Math.min(260, Math.max(190, page.width * 0.25))
                        Layout.minimumWidth: 110
                        options: page.customScheduleLabels
                        optionKeys: page.customList.map(function(schedule) { return schedule.id })
                        current: page.selectedCustomIndex
                        accent: page.pal_accent
                        enabled: !page.isRunning && page.customList.length > 0
                        onPicked: function(idx) {
                            if (idx >= 0 && idx < page.customList.length)
                                timer.selectCustomSchedule(page.customList[idx].id)
                        }
                    }

                    TimerButton {
                        objectName: "timerNewScheduleButton"
                        text: page.ultraNarrow ? "" : qsTr("New schedule")
                        iconName: "plus"
                        iconSize: 13
                        tip: page.isRunning
                             ? qsTr("Stop the running timer before creating a schedule.")
                             : qsTr("New schedule")
                        variant: "ghost"
                        enabled: !page.isRunning
                        Layout.preferredWidth: page.ultraNarrow ? 34 : -1
                        implicitHeight: 34
                        onClicked: customDialogs.openNewSchedule()
                    }
                }

                Item {
                    visible: !page.compactBar
                    width: Math.max(0, customContextFlow.width - customLeftRow.width - customRightRow.width - 16)
                    height: 1
                }

                RowLayout {
                    id: customRightRow
                    width: page.compactBar ? customContextFlow.width : implicitWidth
                    spacing: 6

                    TimerButton {
                        objectName: "timerEditScheduleButton"
                        Layout.fillWidth: page.compactBar
                        text: qsTr("Edit schedule")
                        iconName: "sliders"
                        iconSize: 13
                        tip: page.isRunning
                             ? qsTr("Stop the running timer before organizing this schedule.")
                             : qsTr("Organize parts and sections")
                        variant: "ghost"
                        enabled: page.hasActiveCustom && !page.isRunning
                        implicitHeight: 34
                        onClicked: page.editingCustomSchedule = true
                    }

                    TimerButton {
                        id: moreMenuBtn
                        objectName: "timerScheduleMoreButton"
                        iconName: "more_horiz"
                        iconSize: 15
                        tip: qsTr("More schedule actions")
                        variant: "ghost"
                        enabled: page.hasActiveCustom
                        Layout.preferredWidth: 34
                        implicitHeight: 34
                        onClicked: scheduleMoreMenu.open()
                    }
                }
            }
        }

        // ═══════════════════════════════════════════════════════════════════════
        // 3. DRAFT AND CATALOG RECOVERY
        // ═══════════════════════════════════════════════════════════════════════
        Rectangle {
            objectName: "timerDraftFeedback"
            visible: page.editingCustomSchedule && !!page.feedback && page.feedback.scope === "editor" && !!page.feedback.message
            Layout.fillWidth: true
            implicitHeight: feedbackRow.implicitHeight + 14
            radius: 10
            color: (page.feedback.code === "blocked" || page.feedback.code === "schedule_stale")
                   ? Qt.rgba(page.pal_warning.r, page.pal_warning.g, page.pal_warning.b, 0.12)
                   : Qt.rgba(page.pal_danger.r, page.pal_danger.g, page.pal_danger.b, 0.12)
            border.color: (page.feedback.code === "blocked" || page.feedback.code === "schedule_stale")
                          ? page.pal_warning : page.pal_danger
            border.width: 1

            RowLayout {
                id: feedbackRow
                anchors.fill: parent
                anchors.margins: 7
                spacing: 8

                Text {
                    objectName: "timerFeedbackText"
                    Layout.fillWidth: true
                    text: page.feedback && page.feedback.message ? page.feedback.message : ""
                    textFormat: Text.PlainText
                    color: (page.feedback.code === "blocked" || page.feedback.code === "schedule_stale")
                           ? page.pal_warning : page.pal_danger
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    wrapMode: Text.WordWrap
                }

                TimerButton {
                    objectName: "timerFeedbackReloadButton"
                    visible: page.feedback && page.feedback.retryable === true
                    text: qsTr("Reload")
                    iconName: "refresh"
                    iconSize: 12
                    variant: "soft"
                    accent: page.pal_warning
                    implicitHeight: 26
                    onClicked: {
                        page.editingCustomSchedule = false
                        timer.reloadActiveCustomSchedule()
                    }
                }

                TimerButton {
                    iconName: "close"
                    iconSize: 12
                    tip: qsTranslate("SettingsWidget", "Close")
                    variant: "ghost"
                    Layout.preferredWidth: 26
                    implicitHeight: 26
                    onClicked: timer.clearFeedback()
                }
            }
        }

        Rectangle {
            objectName: "timerCatalogFeedbackBanner"
            visible: timer.customCatalogAvailable === false
            Layout.fillWidth: true
            implicitHeight: catalogFeedbackRow.implicitHeight + 14
            radius: 10
            color: Qt.rgba(page.pal_danger.r, page.pal_danger.g, page.pal_danger.b, 0.12)
            border.color: page.pal_danger

            RowLayout {
                id: catalogFeedbackRow
                anchors.fill: parent
                anchors.margins: 7
                spacing: 8
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Could not load custom schedules. Try again.")
                    textFormat: Text.PlainText
                    color: page.pal_danger
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                TimerButton {
                    objectName: "timerCatalogRetryButton"
                    text: qsTr("Reload")
                    iconName: "refresh"
                    iconSize: 12
                    variant: "soft"
                    accent: page.pal_danger
                    implicitHeight: 26
                    onClicked: timer.refreshCustomCatalog()
                }
            }
        }

        // ═══════════════════════════════════════════════════════════════════════
        // 4. MAIN WORKSPACE AREA
        // ═══════════════════════════════════════════════════════════════════════
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            // ── A. Empty state (Custom Schedules tab with no saved schedules) ──
            Rectangle {
                id: emptyStateCard
                objectName: "timerEmptyStateCard"
                visible: page.onCustom && page.customList.length === 0
                anchors.centerIn: parent
                width: Math.min(400, parent.width)
                implicitHeight: emptyCol.implicitHeight + 40
                radius: 16
                color: page.pal_surface
                border.color: page.pal_border
                border.width: 1

                ColumnLayout {
                    id: emptyCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: 20
                    spacing: 12

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.preferredWidth: 48
                        Layout.preferredHeight: 48
                        radius: 14
                        color: Qt.rgba(page.pal_accent.r, page.pal_accent.g, page.pal_accent.b, 0.14)
                        TimerIcon {
                            anchors.centerIn: parent
                            width: 24; height: 24
                            rasterSize: 48
                            iconName: timer.defaultSectionIconKey
                            tint: page.pal_accentText
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: qsTr("No custom schedules yet")
                        color: page.pal_textPrimary
                        font.pixelSize: 16
                        font.weight: Font.Bold
                        wrapMode: Text.WordWrap
                    }

                    Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        text: qsTr("Create a schedule for trainings, assemblies, or any timed event. Sections and target times are optional.")
                        color: page.pal_textMuted
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                    }

                    TimerButton {
                        objectName: "timerEmptyCreateButton"
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTr("New schedule")
                        iconName: "plus"
                        iconSize: 13
                        variant: "primary"
                        accent: page.pal_accent
                        enabled: !page.isRunning
                        implicitHeight: 36
                        onClicked: customDialogs.openNewSchedule()
                    }
                }
            }

            // ── B. Immersive structure organizer mode (Custom Schedules) ──────
            TimerScheduleEditor {
                id: scheduleEditor
                visible: page.onCustom && page.hasActiveCustom && page.editingCustomSchedule
                anchors.fill: parent
                scheduleData: page.activeCustom
                onCancelRequested: page.editingCustomSchedule = false
                onSaved: page.editingCustomSchedule = false
                onMoveRequested: function(itemType, itemId, itemTitle, currentSectionId, draftEntries) {
                    customDialogs.openMoveDialog(itemType, itemId, itemTitle, currentSectionId, true, draftEntries)
                }
                onAddPartRequested: function(defaultSectionId, draftEntries) {
                    customDialogs.openAddPart(defaultSectionId, true, draftEntries)
                }
                onAddSectionRequested: customDialogs.openAddSection(true)
                onSectionAppearanceRequested: function(sectionId, sectionTitle, iconChoice, customColor) {
                    customDialogs.openSectionAppearance(sectionId, sectionTitle, iconChoice, customColor, true)
                }
            }

            // ── C. Unified live operation workspace (Meetings & Custom Schedules) ──
            Item {
                objectName: "timerOperationWorkspace"
                visible: !emptyStateCard.visible && !scheduleEditor.visible
                anchors.fill: parent

                TimerScheduleList {
                    id: scheduleList
                    objectName: page.splitWorkspace ? "timerWideScheduleList" : "timerCompactScheduleList"
                    x: 0
                    y: page.splitWorkspace ? 0 : clockPanel.height + 8
                    width: page.splitWorkspace ? Math.max(0, parent.width - page.rightColumnWidth - 18) : parent.width
                    height: Math.max(0, parent.height - y)
                    partsModel: timer.displayEntries
                    live: page.live
                    scheduleKind: page.onCustom ? "custom" : "meeting"
                    scheduleIdentity: page.scheduleIdentity
                    isRunning: page.isRunning
                    selectedPartId: page.resolvedSelectedPart ? (page.resolvedSelectedPart.id || "") : ""
                    onPartSelected: function(partId) { page.selectedPartId = partId }
                    onEditPartRequested: function(partRow) { customDialogs.openEditPart(partRow) }
                    onRenameSectionRequested: function(sectionId, sectionTitle) {
                        customDialogs.openRenameSection(sectionId, sectionTitle)
                    }
                    onSectionAppearanceRequested: function(sectionId, sectionTitle, iconChoice, customColor) {
                        customDialogs.openSectionAppearance(sectionId, sectionTitle, iconChoice, customColor, false)
                    }
                    onAddPartToSectionRequested: function(sectionId) {
                        customDialogs.openAddPart(sectionId, false, null)
                    }
                    onDeletePartRequested: function(partId) {
                        timer.deleteCustomPart(page.activeCustom.id || "", page.activeCustom.revision || 0, partId)
                    }
                    onDeleteSectionRequested: function(sectionId) {
                        timer.deleteCustomSection(page.activeCustom.id || "", page.activeCustom.revision || 0, sectionId)
                    }
                }

                TimerClockPanel {
                    id: clockPanel
                    objectName: page.splitWorkspace ? "timerWideClockPanel" : "timerCompactClockStrip"
                    x: page.splitWorkspace ? Math.max(0, parent.width - page.rightColumnWidth) : 0
                    y: 0
                    width: page.splitWorkspace ? Math.min(page.rightColumnWidth, parent.width) : parent.width
                    height: page.splitWorkspace ? Math.max(0, Math.min(implicitHeight, parent.height - 6)) : implicitHeight
                    compactStrip: !page.splitWorkspace
                    live: page.live
                    summary: page.summary
                    pacing: page.pacing
                    selectedPart: page.resolvedSelectedPart
                    scheduleKind: page.onCustom ? "custom" : "meeting"
                    onCustomTimingRequested: customDialogs.openTimingModal()
                    onResetAllRequested: customDialogs.openResetConfirm(page.summary.scheduleId || "", page.summary.scheduleRevision || 0)
                    onEditSelectedPartRequested: function(partRow) { customDialogs.openEditPart(partRow) }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // 5. THREE-DOT OVERFLOW MENU FOR CUSTOM SCHEDULES
    // ═══════════════════════════════════════════════════════════════════════════
    Popup {
        id: scheduleMoreMenu
        objectName: "timerScheduleMoreMenu"
        parent: Overlay.overlay
        x: {
            if (!parent) return 0
            var pos = moreMenuBtn.mapToItem(parent, moreMenuBtn.width - width, moreMenuBtn.height + 6)
            return Math.max(8, Math.min(parent.width - width - 8, pos.x))
        }
        y: {
            if (!parent) return 0
            var pos = moreMenuBtn.mapToItem(parent, 0, moreMenuBtn.height + 6)
            return Math.max(8, Math.min(parent.height - height - 8, pos.y))
        }
        width: 224
        padding: 6
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            radius: 12
            color: page.pal_surface
            border.color: page.pal_borderStrong
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: 2

            // Dedicated Schedule Timing & Pacing modal (separated from structure editing!)
            TimerButton {
                objectName: "timerTimingMenuItem"
                Layout.fillWidth: true
                text: qsTr("Timing and pacing…")
                iconName: "clock"
                iconSize: 13
                variant: "ghost"
                enabled: !page.isRunning
                implicitHeight: 34
                onClicked: {
                    scheduleMoreMenu.close()
                    customDialogs.openTimingModal()
                }
            }

            TimerButton {
                objectName: "timerRenameScheduleMenuItem"
                Layout.fillWidth: true
                text: qsTr("Rename schedule…")
                iconName: "edit"
                iconSize: 13
                variant: "ghost"
                enabled: !page.isRunning
                implicitHeight: 34
                onClicked: {
                    scheduleMoreMenu.close()
                    customDialogs.openRenameSchedule(page.activeCustom.title || "")
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: page.pal_border
                opacity: 0.7
            }

            TimerButton {
                objectName: "timerDuplicateMenuItem"
                Layout.fillWidth: true
                text: qsTr("Duplicate")
                iconName: "copy"
                iconSize: 13
                variant: "ghost"
                enabled: !page.isRunning
                implicitHeight: 34
                onClicked: {
                    scheduleMoreMenu.close()
                    customDialogs.openDuplicate(page.activeCustom.title || "")
                }
            }

            TimerButton {
                objectName: "timerResetAllMenuItem"
                Layout.fillWidth: true
                text: qsTr("Reset results")
                iconName: "reset"
                iconSize: 13
                variant: "ghost"
                enabled: !page.isRunning && page.summary && page.summary.hasAnyTimingData === true
                implicitHeight: 34
                onClicked: {
                    scheduleMoreMenu.close()
                    customDialogs.openResetConfirm(page.summary.scheduleId || "", page.summary.scheduleRevision || 0)
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: page.pal_border
                opacity: 0.7
            }

            TimerButton {
                objectName: "timerDeleteMenuItem"
                Layout.fillWidth: true
                text: qsTr("Delete")
                iconName: "trash"
                iconColor: page.pal_danger
                iconSize: 13
                variant: "ghost"
                accent: page.pal_danger
                enabled: !page.isRunning
                implicitHeight: 34
                onClicked: {
                    scheduleMoreMenu.close()
                    customDialogs.openDeleteConfirm(
                        page.activeCustom.id || "",
                        page.activeCustom.title || "",
                        page.activeCustom.revision || 0
                    )
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // 6. SHARED DIALOGS (Settings Gear + Custom Schedule Modals)
    // ═══════════════════════════════════════════════════════════════════════════
    TimerSettingsDialog {
        id: settingsDialog
    }

    TimerCustomScheduleDialogs {
        id: customDialogs
        scheduleData: page.activeCustom
        draftGeneration: scheduleEditor.draftGeneration
        onScheduleCreated: function() {
            page.editingCustomSchedule = false
        }
        onDraftAddPartSubmitted: function(title, plannedSeconds, sectionId) {
            scheduleEditor.applyDraftAddPart(title, plannedSeconds, sectionId)
        }
        onDraftAddSectionSubmitted: function(title, iconChoice, colorHex) {
            scheduleEditor.applyDraftAddSection(title, iconChoice, colorHex)
        }
        onDraftSectionAppearanceSubmitted: function(sectionId, iconChoice, colorHex) {
            scheduleEditor.applyDraftSectionAppearance(sectionId, iconChoice, colorHex)
        }
        onDraftMoveSubmitted: function(itemType, itemId, targetSectionId, beforeId) {
            scheduleEditor.applyDraftMove(itemType, itemId, targetSectionId, beforeId)
        }
    }
}
