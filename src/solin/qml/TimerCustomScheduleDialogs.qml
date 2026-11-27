pragma Translator: "AdvancedTimerPage"

// TimerCustomScheduleDialogs.qml — cohesive modals using Solin's AppTextField
// (native I-beam cursor + click-outside focus dismissal), centered TimerButton
// controls, icon-only Section Appearance with explicit Save/Cancel, Edit Part,
// Rename Section, and dedicated Schedule Timing & Pacing modal.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: dialogs

    property var scheduleData: ({})
    property int draftGeneration: 0

    onDraftGenerationChanged: {
        if (addPartDraftMode) addPartDialog.close()
        if (addSectionDraftMode) addSectionDialog.close()
        if (appearanceDraftMode) sectionAppearanceDialog.close()
        if (moveDraftMode) moveDialog.close()
    }
    property color pal_bg: appTheme.bg
    property color pal_surface: appTheme.surface
    property color pal_surfaceAlt: appTheme.surfaceAlt
    property color pal_hover: appTheme.hover
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_accent: appTheme.accent
    property color pal_accentText: appTheme.accentText
    property color pal_danger: appTheme.danger

    signal scheduleCreated(string scheduleId)
    signal draftAddPartSubmitted(string title, int plannedSeconds, string sectionId)
    signal draftAddSectionSubmitted(string title, string iconChoice, string colorHex)
    signal draftSectionAppearanceSubmitted(string sectionId, string iconChoice, string colorHex)
    signal draftMoveSubmitted(string itemType, string itemId, string targetSectionId, string beforeId)

    readonly property string scheduleId: scheduleData && scheduleData.id ? scheduleData.id : ""
    readonly property int scheduleRevision: scheduleData && scheduleData.revision !== undefined ? scheduleData.revision : 0
    readonly property var entries: scheduleData && scheduleData.entries ? scheduleData.entries : []

    component ScheduleBoundPopup: Popup {
        property string targetId: ""
        property int targetRevision: 0
        property string errorMessage: ""

        function captureSchedule() {
            targetId = dialogs.scheduleId
            targetRevision = dialogs.scheduleRevision
            errorMessage = ""
        }

        function showFeedback() {
            errorMessage = (timer.feedback && timer.feedback.message) || ""
        }
    }

    component SelectableTile: Rectangle {
        signal activated()
        activeFocusOnTab: enabled
        Accessible.role: Accessible.RadioButton
        Accessible.onPressAction: activated()
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                activated()
                event.accepted = true
            }
        }
        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            radius: parent.radius + 3
            color: "transparent"
            border.width: 2
            border.color: dialogs.pal_accentText
            visible: parent.activeFocus
        }
    }

    component ModalFeedback: Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        visible: text.length > 0
        color: dialogs.pal_danger
        font.pixelSize: 12
        wrapMode: Text.WordWrap
    }

    function parseDurationToSeconds(text) {
        var cleaned = String(text || "").trim()
        if (!cleaned) return -1
        var parts = cleaned.split(":")
        for (var i = 0; i < parts.length; i++) {
            if (!/^\d+$/.test(parts[i])) return -1
        }
        if (parts.length === 1) {
            var mins = parseInt(parts[0], 10)
            return isFinite(mins) && mins >= 0 ? mins * 60 : -1
        }
        if (parts.length === 2) {
            var m = parseInt(parts[0], 10)
            var s = parseInt(parts[1], 10)
            return isFinite(m) && isFinite(s) && m >= 0 && s >= 0 && s < 60 ? m * 60 + s : -1
        }
        if (parts.length === 3) {
            var h = parseInt(parts[0], 10)
            var mm = parseInt(parts[1], 10)
            var ss = parseInt(parts[2], 10)
            return isFinite(h) && isFinite(mm) && isFinite(ss) && h >= 0 && mm >= 0 && mm < 60 && ss >= 0 && ss < 60
                   ? h * 3600 + mm * 60 + ss : -1
        }
        return -1
    }

    // ── Public openers ────────────────────────────────────────────────────────
    function openNewSchedule() {
        newScheduleTitle.text = ""
        firstPartTitle.text = ""
        firstPartDuration.text = "05:00"
        newScheduleError = ""
        newScheduleDialog.open()
        newScheduleTitle.forceActiveFocus()
    }

    function openEditPart(partRow) {
        if (!partRow || !partRow.id) return
        editPartId = partRow.id
        editPartOriginalTitle = partRow.title || ""
        editPartOriginalSeconds = partRow.plannedSeconds || 0
        editPartMinSeconds = timer.minPlannedPartSeconds
        editPartDurationEditable = partRow.durationEditable === true
        editPartHasTitleOverride = partRow.hasTitleOverride === true
        editPartIsCompleted = partRow.state === "stopped"
        editPartTitleField.text = partRow.title || ""
        editPartDurationField.text = partRow.plannedLabel || "05:00"
        editPartDialog.errorMessage = ""
        timer.deferAssignmentLookupForEdit()
        editPartDialog.targetId = timer.scheduleSummary.scheduleId
        editPartDialog.targetRevision = timer.scheduleSummary.scheduleRevision
        editPartDialog.open()
        editPartTitleField.forceActiveFocus()
        editPartTitleField.selectAll()
    }

    function openRenameSchedule(currentTitle) {
        renameScheduleInput.text = currentTitle || ""
        renameScheduleDialog.captureSchedule()
        renameScheduleDialog.open()
        renameScheduleInput.forceActiveFocus()
        renameScheduleInput.selectAll()
    }

    function openRenameSection(sectionId, currentTitle) {
        renameSectionId = sectionId || ""
        renameSectionInput.text = currentTitle || ""
        renameSectionDialog.captureSchedule()
        renameSectionDialog.open()
        renameSectionInput.forceActiveFocus()
        renameSectionInput.selectAll()
    }

    function openAddPart(defaultSectionId, draftMode, customEntries) {
        addPartDraftMode = draftMode === true
        addPartEntriesOverride = JSON.parse(JSON.stringify(customEntries || entries))
        addPartTitleInput.text = ""
        addPartDurationInput.text = "05:00"
        addPartSectionId = defaultSectionId || ""
        addPartDialog.captureSchedule()
        addPartDialog.open()
        addPartTitleInput.forceActiveFocus()
    }

    function openAddSection(draftMode) {
        addSectionDraftMode = draftMode === true
        addSectionTitleInput.text = ""
        addSectionIcon = "default"
        addSectionColor = ""
        addSectionDialog.captureSchedule()
        addSectionDialog.open()
        addSectionTitleInput.forceActiveFocus()
    }

    function openSectionAppearance(sectionId, sectionTitle, iconChoice, customColor, draftMode) {
        appearanceDraftMode = draftMode === true
        appearanceSectionId = sectionId || ""
        appearanceSectionTitle = sectionTitle || ""
        appearanceIconChoice = iconChoice || "default"
        appearanceColor = customColor || ""
        appearanceHexInput.text = appearanceColor
        sectionAppearanceDialog.captureSchedule()
        sectionAppearanceDialog.open()
    }

    function openTimingModal() {
        timingDialog.openForSchedule()
    }

    function openDuplicate(currentTitle) {
        dupTitleInput.text = currentTitle ? qsTr("%1 (copy)").arg(currentTitle) : ""
        duplicateDialog.captureSchedule()
        duplicateDialog.open()
        dupTitleInput.forceActiveFocus()
        dupTitleInput.selectAll()
    }

    function openDeleteConfirm(scheduleId, title, revision) {
        deleteDialog.targetId = scheduleId || dialogs.scheduleId
        deleteTargetTitle = title || ""
        deleteDialog.targetRevision = revision !== undefined ? revision : dialogs.scheduleRevision
        deleteDialog.errorMessage = ""
        deleteDialog.open()
    }

    function openResetConfirm(scheduleId, revision) {
        resetDialog.targetId = scheduleId || ""
        resetDialog.targetRevision = revision !== undefined ? revision : 0
        resetDialog.errorMessage = ""
        resetDialog.open()
    }

    function openMoveDialog(itemType, itemId, itemTitle, currentSectionId, draftMode, customEntries) {
        moveDraftMode = draftMode === true
        moveEntriesOverride = JSON.parse(JSON.stringify(customEntries || entries))
        moveItemType = itemType
        moveItemId = itemId
        moveItemTitle = itemTitle
        moveTargetSectionId = currentSectionId || ""
        moveTargetBeforeId = ""
        moveDialog.captureSchedule()
        moveDialog.open()
    }

    // ── Internal state ────────────────────────────────────────────────────────
    property string newScheduleError: ""

    property string editPartId: ""
    property string editPartOriginalTitle: ""
    property int editPartOriginalSeconds: 0
    property int editPartMinSeconds: 0
    property bool editPartDurationEditable: true
    property bool editPartHasTitleOverride: false
    property bool editPartIsCompleted: false

    property string renameSectionId: ""

    property bool addPartDraftMode: false
    property var addPartEntriesOverride: null
    property string addPartSectionId: ""

    property bool addSectionDraftMode: false
    property string addSectionIcon: "default"
    property string addSectionColor: ""

    property bool appearanceDraftMode: false
    property string appearanceSectionId: ""
    property string appearanceSectionTitle: ""
    property string appearanceIconChoice: "default"
    property string appearanceColor: ""

    property string deleteTargetTitle: ""
    property bool moveDraftMode: false
    property var moveEntriesOverride: null
    property string moveItemType: "part"
    property string moveItemId: ""
    property string moveItemTitle: ""
    property string moveTargetSectionId: ""
    property string moveTargetBeforeId: ""

    function sectionOptionsFrom(sourceEntries) {
        var list = sourceEntries || entries
        var opts = [{ id: "", title: qsTr("No section (root)") }]
        for (var i = 0; i < list.length; i++) {
            if (list[i].type === "section")
                opts.push({ id: list[i].id, title: list[i].title })
        }
        var counts = Object.create(null)
        var used = Object.create(null)
        var nextNumber = Object.create(null)
        for (var j = 0; j < opts.length; j++) {
            var title = String(opts[j].title || "")
            counts[title] = (counts[title] || 0) + 1
            used[title] = true
        }
        for (var k = 1; k < opts.length; k++) {
            var name = String(opts[k].title || "")
            if (counts[name] > 1) {
                var number = nextNumber[name] || 1
                var label
                do {
                    label = qsTr("%1 (%2)").arg(name).arg(number++)
                } while (used[label])
                nextNumber[name] = number
                used[label] = true
                opts[k].title = label
            }
        }
        return opts
    }

    // Unified icon tiles: "No icon" first, then Default, then all 14 catalog icons
    readonly property var appearanceIconTiles: {
        var tiles = [
            {
                id: "none",
                iconKey: "ban",
                label: qsTr("No icon"),
                isNone: true
            },
            {
                id: "default",
                iconKey: timer.defaultSectionIconKey,
                label: qsTr("Default")
            }
        ]
        var cat = timer.sectionIconCatalog
        for (var i = 0; i < cat.length; i++) {
            tiles.push({
                id: cat[i].id,
                iconKey: cat[i].iconKey,
                label: cat[i].label
            })
        }
        return tiles
    }

    readonly property var appearancePreviewPalette: {
        // The theme dependency refreshes the palette when the application theme changes.
        appTheme.isDark
        return timer.customSectionPalette(appearanceColor)
    }

    function appearancePreviewIconKey() {
        if (appearanceIconChoice === "none") return ""
        if (appearanceIconChoice === "default") return timer.defaultSectionIconKey
        return appearanceIconChoice
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // EDIT PART DIALOG (unified for Meetings & Custom Schedules!)
    // ═══════════════════════════════════════════════════════════════════════════
    ScheduleBoundPopup {
        id: editPartDialog
        objectName: "timerEditPartDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(410, parent ? parent.width - 32 : 410))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: editPartDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Edit part")
                    color: dialogs.pal_textPrimary
                    font.pixelSize: 16; font.weight: Font.Bold
                }
                TimerButton {
                    iconName: "close"; iconSize: 14
                    tip: qsTranslate("SettingsWidget", "Close")
                    Layout.preferredWidth: 32; implicitHeight: 32
                    onClicked: editPartDialog.close()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 5
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Part name")
                        color: dialogs.pal_textSecondary; font.pixelSize: 12; font.weight: Font.Medium
                    }
                    TimerButton {
                        objectName: "timerRestorePartTitleButton"
                        visible: dialogs.editPartHasTitleOverride
                        text: qsTr("Restore default")
                        iconName: "reset"; iconSize: 12
                        variant: "ghost"
                        implicitHeight: 26
                        onClicked: {
                            if (timer.restoreEditedPartTitle(editPartDialog.targetId, editPartDialog.targetRevision, dialogs.editPartId))
                                editPartDialog.close()
                            else
                                editPartDialog.showFeedback()
                        }
                    }
                }

                TimerModalTextField {
                    id: editPartTitleField
                    objectName: "timerEditPartTitleInput"
                    Layout.fillWidth: true
                    onAccepted: editPartSaveBtn.clicked()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 5
                Text {
                    text: qsTr("Planned duration")
                    color: dialogs.pal_textSecondary; font.pixelSize: 12; font.weight: Font.Medium
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    TimerModalTextField {
                        id: editPartDurationField
                        objectName: "timerEditPartDurationInput"
                        Layout.preferredWidth: 110
                        horizontalAlignment: Text.AlignHCenter
                        font.weight: Font.DemiBold
                        enabled: dialogs.editPartDurationEditable
                        opacity: enabled ? 1.0 : 0.55
                        onAccepted: editPartSaveBtn.clicked()
                    }
                    Text {
                        visible: !dialogs.editPartDurationEditable
                        Layout.fillWidth: true
                        text: dialogs.editPartIsCompleted
                              ? qsTr("Reset this part to change its planned duration.")
                              : qsTr("This part's duration is fixed.")
                        color: dialogs.pal_textMuted; font.pixelSize: 11; wrapMode: Text.WordWrap
                    }
                    TimerButton {
                        visible: dialogs.editPartIsCompleted && !dialogs.editPartDurationEditable
                        text: qsTr("Reset")
                        iconName: "reset"; iconSize: 12
                        variant: "soft"; accent: dialogs.pal_accent
                        implicitHeight: 32
                        onClicked: {
                            if (!timer.resetEditedPart(editPartDialog.targetId, editPartDialog.targetRevision, dialogs.editPartId)) {
                                editPartDialog.showFeedback()
                                return
                            }
                            editPartDialog.targetRevision = timer.scheduleSummary.scheduleRevision
                            var row = timer.parts.find(function(part) { return part.id === dialogs.editPartId })
                            if (row) {
                                dialogs.editPartIsCompleted = row.state === "stopped"
                                dialogs.editPartDurationEditable = row.durationEditable === true
                            }
                            editPartDialog.errorMessage = ""
                        }
                    }
                }
            }

            ModalFeedback { text: editPartDialog.errorMessage }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: editPartDialog.close() }
                TimerButton {
                    id: editPartSaveBtn
                    objectName: "timerEditPartSaveButton"
                    text: qsTr("Save"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: editPartTitleField.text.trim().length > 0
                    onClicked: {
                        editPartDialog.errorMessage = ""
                        var newTitle = editPartTitleField.text.trim()
                        if (!newTitle) return
                        var newSecs = dialogs.editPartOriginalSeconds
                        if (dialogs.editPartDurationEditable) {
                            newSecs = dialogs.parseDurationToSeconds(editPartDurationField.text)
                            if (newSecs !== dialogs.editPartOriginalSeconds
                                    && (newSecs < dialogs.editPartMinSeconds || newSecs > timer.maxPlannedPartSeconds)) {
                                editPartDialog.errorMessage = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                                return
                            }
                        }
                        var titleChange = newTitle === dialogs.editPartOriginalTitle ? "" : newTitle
                        var secondsChange = dialogs.editPartDurationEditable && newSecs !== dialogs.editPartOriginalSeconds ? newSecs : -1
                        if (timer.savePartEdit(editPartDialog.targetId, editPartDialog.targetRevision, dialogs.editPartId, titleChange, secondsChange))
                            editPartDialog.close()
                        else
                            editPartDialog.showFeedback()
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // NEW SCHEDULE DIALOG (fast single-part creation; sections/times optional)
    // ═══════════════════════════════════════════════════════════════════════════
    Popup {
        id: newScheduleDialog
        objectName: "timerNewScheduleDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(420, parent ? parent.width - 32 : 420))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: newScheduleDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: qsTr("New schedule")
                    color: dialogs.pal_textPrimary
                    font.pixelSize: 16; font.weight: Font.Bold
                }
                TimerButton {
                    iconName: "close"; iconSize: 14
                    tip: qsTranslate("SettingsWidget", "Close")
                    Layout.preferredWidth: 32; implicitHeight: 32
                    onClicked: newScheduleDialog.close()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 5
                Text {
                    text: qsTr("Schedule name")
                    color: dialogs.pal_textSecondary; font.pixelSize: 12; font.weight: Font.Medium
                }
                TimerModalTextField {
                    id: newScheduleTitle
                    objectName: "timerNewScheduleTitleInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("e.g., Volunteer training")
                    onAccepted: createBtn.clicked()
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: dialogs.pal_border; opacity: 0.7 }

            Text {
                Layout.fillWidth: true
                text: qsTr("First part (optional — sections and times can be added later)")
                color: dialogs.pal_textMuted; font.pixelSize: 11; wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                TimerModalTextField {
                    id: firstPartTitle
                    objectName: "timerNewScheduleFirstPartInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("Part name (e.g., Welcome)")
                    onAccepted: createBtn.clicked()
                }
                TimerModalTextField {
                    id: firstPartDuration
                    objectName: "timerNewScheduleFirstDurationInput"
                    Layout.preferredWidth: 82
                    horizontalAlignment: Text.AlignHCenter
                    font.weight: Font.DemiBold
                    text: "05:00"
                    onAccepted: createBtn.clicked()
                }
            }

            Text {
                visible: dialogs.newScheduleError.length > 0
                Layout.fillWidth: true
                text: dialogs.newScheduleError
                color: dialogs.pal_danger; font.pixelSize: 12; wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: newScheduleDialog.close() }
                TimerButton {
                    id: createBtn
                    objectName: "timerCreateScheduleConfirmButton"
                    text: qsTr("Create schedule"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: newScheduleTitle.text.trim().length > 0
                    onClicked: {
                        if (!newScheduleTitle.text.trim()) return
                        dialogs.newScheduleError = ""
                        var secs = 0
                        if (firstPartTitle.text.trim().length > 0) {
                            secs = dialogs.parseDurationToSeconds(firstPartDuration.text)
                            if (secs <= 0 || secs > timer.maxPlannedPartSeconds) {
                                dialogs.newScheduleError = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                                return
                            }
                        }
                        var createdId = timer.createCustomSchedule(newScheduleTitle.text, firstPartTitle.text, secs)
                        if (createdId) {
                            newScheduleDialog.close()
                            dialogs.scheduleCreated(createdId)
                        } else if (timer.feedback && timer.feedback.message) {
                            dialogs.newScheduleError = timer.feedback.message
                        }
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // RENAME SCHEDULE & RENAME SECTION DIALOGS
    // ═══════════════════════════════════════════════════════════════════════════
    ScheduleBoundPopup {
        id: renameScheduleDialog
        objectName: "timerRenameScheduleDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(380, parent ? parent.width - 32 : 380))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: renameScheduleDialog.availableWidth
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: qsTr("Rename schedule"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: renameScheduleDialog.close() }
            }
            TimerModalTextField {
                id: renameScheduleInput
                objectName: "timerRenameScheduleInput"
                Layout.fillWidth: true
                onAccepted: renameConfirmBtn.clicked()
            }
            ModalFeedback { text: renameScheduleDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: renameScheduleDialog.close() }
                TimerButton {
                    id: renameConfirmBtn
                    text: qsTr("Save"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: renameScheduleInput.text.trim().length > 0
                    onClicked: {
                        if (!renameScheduleInput.text.trim()) return
                        if (timer.renameCustomSchedule(renameScheduleDialog.targetId, renameScheduleDialog.targetRevision, renameScheduleInput.text))
                            renameScheduleDialog.close()
                        else
                            renameScheduleDialog.showFeedback()
                    }
                }
            }
        }
    }

    ScheduleBoundPopup {
        id: renameSectionDialog
        objectName: "timerRenameSectionDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(380, parent ? parent.width - 32 : 380))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: renameSectionDialog.availableWidth
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: qsTr("Rename section"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: renameSectionDialog.close() }
            }
            TimerModalTextField {
                id: renameSectionInput
                objectName: "timerRenameSectionInput"
                Layout.fillWidth: true
                onAccepted: renameSectionConfirmBtn.clicked()
            }
            ModalFeedback { text: renameSectionDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: renameSectionDialog.close() }
                TimerButton {
                    id: renameSectionConfirmBtn
                    text: qsTr("Save"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: renameSectionInput.text.trim().length > 0
                    onClicked: {
                        if (!renameSectionInput.text.trim()) return
                        if (timer.renameCustomSection(renameSectionDialog.targetId, renameSectionDialog.targetRevision, dialogs.renameSectionId, renameSectionInput.text))
                            renameSectionDialog.close()
                        else
                            renameSectionDialog.showFeedback()
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // QUICK ADD PART & QUICK ADD SECTION DIALOGS
    // ═══════════════════════════════════════════════════════════════════════════
    ScheduleBoundPopup {
        id: addPartDialog
        objectName: "timerQuickAddPartDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(400, parent ? parent.width - 32 : 400))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: addPartDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: qsTr("Add part"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: addPartDialog.close() }
            }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                ColumnLayout {
                    Layout.fillWidth: true; spacing: 4
                    Text { text: qsTr("Part name"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                    TimerModalTextField {
                        id: addPartTitleInput
                        objectName: "timerAddPartTitleInput"
                        Layout.fillWidth: true
                        onAccepted: addPartConfirmBtn.clicked()
                    }
                }
                ColumnLayout {
                    Layout.preferredWidth: 90; spacing: 4
                    Text { text: qsTr("Duration"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                    TimerModalTextField {
                        id: addPartDurationInput
                        objectName: "timerAddPartDurationInput"
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        font.weight: Font.DemiBold
                        text: "05:00"
                        onAccepted: addPartConfirmBtn.clicked()
                    }
                }
            }

            ColumnLayout {
                readonly property var secOpts: dialogs.sectionOptionsFrom(dialogs.addPartEntriesOverride)
                visible: secOpts.length > 1
                Layout.fillWidth: true; spacing: 4
                Text { text: qsTr("Section (optional)"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                TimerSelect {
                    Layout.fillWidth: true
                    options: {
                        var labels = []
                        for (var i = 0; i < parent.secOpts.length; i++) labels.push(parent.secOpts[i].title)
                        return labels
                    }
                    optionKeys: parent.secOpts.map(function(section) { return section.id })
                    current: {
                        for (var i = 0; i < parent.secOpts.length; i++) {
                            if (parent.secOpts[i].id === dialogs.addPartSectionId) return i
                        }
                        return 0
                    }
                    onPicked: function(idx) {
                        if (idx >= 0 && idx < parent.secOpts.length)
                            dialogs.addPartSectionId = parent.secOpts[idx].id
                    }
                }
            }

            ModalFeedback { text: addPartDialog.errorMessage }

            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: addPartDialog.close() }
                TimerButton {
                    id: addPartConfirmBtn
                    objectName: "timerAddPartConfirmButton"
                    text: qsTr("Add part"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: addPartTitleInput.text.trim().length > 0
                    onClicked: {
                        var trimmedTitle = addPartTitleInput.text.trim()
                        if (!trimmedTitle) return
                        addPartDialog.errorMessage = ""
                        var secs = dialogs.parseDurationToSeconds(addPartDurationInput.text)
                        if (secs <= 0 || secs > timer.maxPlannedPartSeconds) {
                            addPartDialog.errorMessage = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                            return
                        }
                        if (dialogs.addPartDraftMode) {
                            addPartDialog.errorMessage = timer.timerNameError(addPartTitleInput.text)
                            if (addPartDialog.errorMessage) return
                            dialogs.draftAddPartSubmitted(trimmedTitle, secs, dialogs.addPartSectionId)
                            addPartDialog.close()
                        } else if (timer.addCustomPart(addPartDialog.targetId, addPartDialog.targetRevision, trimmedTitle, secs, dialogs.addPartSectionId, "")) {
                            addPartDialog.close()
                        } else if (timer.feedback && timer.feedback.message) {
                            addPartDialog.showFeedback()
                        }
                    }
                }
            }
        }
    }

    ScheduleBoundPopup {
        id: addSectionDialog
        objectName: "timerQuickAddSectionDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(400, parent ? parent.width - 32 : 400))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: addSectionDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: qsTr("Add section"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: addSectionDialog.close() }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 4
                Text { text: qsTr("Section name"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                TimerModalTextField {
                    id: addSectionTitleInput
                    objectName: "timerAddSectionTitleInput"
                    Layout.fillWidth: true
                    placeholderText: qsTr("e.g., Practice")
                    onAccepted: addSectionConfirmBtn.clicked()
                }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 6
                Text { text: qsTr("Color (optional)"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                Flow {
                    Layout.fillWidth: true; spacing: 6
                    SelectableTile {
                        Accessible.name: qsTr("Default color")
                        Accessible.checked: dialogs.addSectionColor === ""
                        onActivated: dialogs.addSectionColor = ""
                        width: 24; height: 24; radius: 12
                        color: dialogs.pal_surfaceAlt
                        border.color: dialogs.addSectionColor === "" ? dialogs.pal_accent : dialogs.pal_borderStrong
                        border.width: dialogs.addSectionColor === "" ? 2 : 1
                        TimerIcon {
                            anchors.centerIn: parent; width: 12; height: 12
                            rasterSize: 24
                            iconName: "ban"
                            tint: dialogs.pal_textMuted
                        }
                        TimerPointerArea { id: defAddColorArea; anchors.fill: parent; onClicked: parent.activated() }
                        AppToolTip { visible: defAddColorArea.containsMouse; text: qsTr("Default color") }
                    }
                    Repeater {
                        model: timer.sectionColorPresets
                        delegate: SelectableTile {
                            required property var modelData
                            Accessible.name: modelData.label
                            Accessible.checked: dialogs.addSectionColor === modelData.color
                            onActivated: dialogs.addSectionColor = modelData.color
                            width: 24; height: 24; radius: 12
                            color: modelData.color
                            border.color: dialogs.addSectionColor === modelData.color ? appTheme.white : "transparent"
                            border.width: dialogs.addSectionColor === modelData.color ? 2 : 0
                            TimerPointerArea { id: swArea; anchors.fill: parent; onClicked: parent.activated() }
                            AppToolTip { visible: swArea.containsMouse; text: modelData.label }
                        }
                    }
                }
            }

            ModalFeedback { text: addSectionDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: addSectionDialog.close() }
                TimerButton {
                    id: addSectionConfirmBtn
                    objectName: "timerAddSectionConfirmButton"
                    text: qsTr("Add section"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: addSectionTitleInput.text.trim().length > 0
                    onClicked: {
                        var trimmedSectionTitle = addSectionTitleInput.text.trim()
                        if (!trimmedSectionTitle) return
                        if (dialogs.addSectionDraftMode) {
                            addSectionDialog.errorMessage = timer.timerNameError(addSectionTitleInput.text)
                            if (addSectionDialog.errorMessage) return
                            dialogs.draftAddSectionSubmitted(trimmedSectionTitle, dialogs.addSectionIcon, dialogs.addSectionColor)
                            addSectionDialog.close()
                        } else if (timer.addCustomSectionStyled(addSectionDialog.targetId, addSectionDialog.targetRevision, trimmedSectionTitle, dialogs.addSectionIcon, dialogs.addSectionColor, "")) {
                            addSectionDialog.close()
                        } else {
                            addSectionDialog.showFeedback()
                        }
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // SECTION APPEARANCE DIALOG (Icon-only grid + Colors + Explicit Save/Cancel)
    // ═══════════════════════════════════════════════════════════════════════════
    ScheduleBoundPopup {
        id: sectionAppearanceDialog
        objectName: "timerSectionIconPickerDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(410, parent ? parent.width - 28 : 410))
        height: Math.max(0, Math.min(appearanceBody.implicitHeight + 138, parent ? parent.height - 28 : 620))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle {
            radius: 16; color: dialogs.pal_surface
            border.color: dialogs.pal_borderStrong; border.width: 1
        }

        contentItem: ColumnLayout {
            width: sectionAppearanceDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Section appearance")
                    color: dialogs.pal_textPrimary
                    font.pixelSize: 16; font.weight: Font.Bold
                }
                TimerButton {
                    iconName: "close"; iconSize: 14
                    tip: qsTranslate("SettingsWidget", "Close")
                    Layout.preferredWidth: 32; implicitHeight: 32
                    onClicked: sectionAppearanceDialog.close()
                }
            }

            Flickable {
                id: appearanceScroll
                objectName: "timerSectionAppearanceScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: width
                contentHeight: appearanceBody.implicitHeight + 4
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                ColumnLayout {
                    id: appearanceBody
                    width: appearanceScroll.width
                    spacing: 14

            // Live section header preview (preview only — saved on explicit Save!)
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 44
                radius: 10
                color: dialogs.pal_bg
                border.color: dialogs.pal_border
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12; anchors.rightMargin: 12
                    spacing: 9

                    Rectangle {
                        visible: dialogs.appearancePreviewIconKey().length > 0
                        Layout.preferredWidth: 26; Layout.preferredHeight: 26
                        radius: 7
                        color: dialogs.appearancePreviewPalette.badge
                        border.color: dialogs.appearancePreviewPalette.border
                        border.width: 1

                        TimerIcon {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            rasterSize: 30
                            iconName: dialogs.appearancePreviewIconKey()
                            tint: dialogs.appearancePreviewPalette.accent
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: dialogs.appearanceSectionTitle || qsTr("Section preview")
                        textFormat: Text.PlainText
                        color: dialogs.appearancePreviewPalette.text
                        font.pixelSize: 13; font.weight: Font.Bold
                        elide: Text.ElideRight
                    }
                }
            }

            // ── Color selection ───────────────────────────────────────────────
            ColumnLayout {
                Layout.fillWidth: true; spacing: 8

                Text {
                    text: qsTr("Section color")
                    color: dialogs.pal_textSecondary
                    font.pixelSize: 12; font.weight: Font.DemiBold
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8

                    // Default color swatch
                    SelectableTile {
                        Accessible.name: qsTr("Default color")
                        Accessible.checked: dialogs.appearanceColor === ""
                        onActivated: {
                            dialogs.appearanceColor = ""
                            appearanceHexInput.text = ""
                            sectionAppearanceDialog.errorMessage = ""
                        }
                        objectName: "timerSectionColorDefaultButton"
                        width: 28; height: 28; radius: 14
                        color: dialogs.pal_surfaceAlt
                        border.color: dialogs.appearanceColor === "" ? dialogs.pal_accent : dialogs.pal_borderStrong
                        border.width: dialogs.appearanceColor === "" ? 2.5 : 1
                        TimerIcon {
                            anchors.centerIn: parent; width: 13; height: 13
                            rasterSize: 26
                            iconName: "ban"
                            tint: dialogs.pal_textMuted
                        }
                        TimerPointerArea {
                            id: defColArea
                            anchors.fill: parent
                            onClicked: parent.activated()
                        }
                        AppToolTip { visible: defColArea.containsMouse; text: qsTr("Default color") }
                    }

                    Repeater {
                        model: timer.sectionColorPresets
                        delegate: SelectableTile {
                            required property var modelData
                            Accessible.name: modelData.label
                            Accessible.checked: dialogs.appearanceColor.toLowerCase() === modelData.color.toLowerCase()
                            onActivated: {
                                dialogs.appearanceColor = modelData.color
                                appearanceHexInput.text = modelData.color
                                sectionAppearanceDialog.errorMessage = ""
                            }
                            objectName: "timerColorOption_" + modelData.id
                            width: 28; height: 28; radius: 14
                            color: modelData.color
                            border.color: dialogs.appearanceColor.toLowerCase() === modelData.color.toLowerCase()
                                          ? appTheme.white : Qt.rgba(0, 0, 0, 0.25)
                            border.width: dialogs.appearanceColor.toLowerCase() === modelData.color.toLowerCase() ? 2.5 : 1

                            TimerPointerArea {
                                id: swatchArea
                                anchors.fill: parent
                                onClicked: parent.activated()
                            }
                            AppToolTip { visible: swatchArea.containsMouse; text: modelData.label }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Text { text: qsTr("Custom hex"); color: dialogs.pal_textMuted; font.pixelSize: 11 }
                    TimerModalTextField {
                        id: appearanceHexInput
                        objectName: "timerSectionColorHexInput"
                        Layout.preferredWidth: 108
                        implicitHeight: 30
                        font.pixelSize: 12
                        placeholderText: "#41b6c4"
                        onTextEdited: {
                            var raw = text.trim()
                            if (!raw) {
                                dialogs.appearanceColor = ""
                                sectionAppearanceDialog.errorMessage = ""
                                return
                            }
                            var hex = raw.charAt(0) === "#" ? raw : ("#" + raw)
                            if (/^#[0-9a-fA-F]{6}$/.test(hex)) {
                                dialogs.appearanceColor = hex.toLowerCase()
                                sectionAppearanceDialog.errorMessage = ""
                            }
                        }
                    }
                    Item { Layout.fillWidth: true }
                }

                ModalFeedback { text: sectionAppearanceDialog.errorMessage; font.pixelSize: 11 }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: dialogs.pal_border; opacity: 0.7 }

            // ── Icon-only grid (Default as normal icon + 14 symbols + ⊘ No icon) ──
            ColumnLayout {
                Layout.fillWidth: true; spacing: 8

                Text {
                    text: qsTr("Section icon")
                    color: dialogs.pal_textSecondary
                    font.pixelSize: 12; font.weight: Font.DemiBold
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8

                    Repeater {
                        model: dialogs.appearanceIconTiles
                        delegate: SelectableTile {
                            required property var modelData
                            onActivated: dialogs.appearanceIconChoice = modelData.id
                            Accessible.checked: selected
                            objectName: modelData.id === "default"
                                        ? "timerSectionIconDefaultButton"
                                        : (modelData.id === "none"
                                           ? "timerSectionIconNoneButton"
                                           : ("timerIconOption_" + modelData.id))
                            readonly property bool selected: dialogs.appearanceIconChoice === modelData.id

                            width: 40; height: 40; radius: 10
                            color: selected
                                   ? dialogs.pal_accent
                                   : (tileArea.containsMouse ? dialogs.pal_hover : dialogs.pal_bg)
                            border.color: selected
                                          ? dialogs.pal_accent
                                          : (tileArea.containsMouse ? dialogs.pal_borderStrong : dialogs.pal_border)
                            border.width: 1

                            Behavior on color { ColorAnimation { duration: 110 } }
                            Behavior on border.color { ColorAnimation { duration: 110 } }

                            Accessible.name: modelData.label

                            TimerIcon {
                                anchors.centerIn: parent
                                width: 17; height: 17
                                rasterSize: 34
                                iconName: modelData.iconKey
                                tint: selected ? appTheme.white : (modelData.isNone ? dialogs.pal_textMuted : dialogs.pal_textPrimary)
                            }

                            TimerPointerArea {
                                id: tileArea
                                anchors.fill: parent
                                onClicked: parent.activated()
                            }

                            AppToolTip {
                                visible: tileArea.containsMouse
                                text: modelData.label
                            }
                        }
                    }
                }
            }
                }
            }

            // Explicit Cancel / Save footer (nothing saved until Save!)
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton {
                    objectName: "timerSectionAppearanceCancelButton"
                    text: qsTr("Cancel")
                    variant: "ghost"
                    onClicked: sectionAppearanceDialog.close()
                }
                TimerButton {
                    objectName: "timerSectionAppearanceSaveButton"
                    text: qsTr("Save")
                    variant: "primary"
                    accent: dialogs.pal_accent
                    onClicked: {
                        var rawHex = appearanceHexInput.text.trim()
                        var finalColor = ""
                        if (rawHex.length > 0) {
                            var h = rawHex.charAt(0) === "#" ? rawHex : ("#" + rawHex)
                            if (!/^#[0-9a-fA-F]{6}$/.test(h)) {
                                sectionAppearanceDialog.errorMessage = qsTr("Use a 6-digit hex color like #41b6c4.")
                                return
                            }
                            finalColor = h.toLowerCase()
                        }
                        sectionAppearanceDialog.errorMessage = ""
                        if (dialogs.appearanceDraftMode) {
                            dialogs.draftSectionAppearanceSubmitted(dialogs.appearanceSectionId, dialogs.appearanceIconChoice, finalColor)
                            sectionAppearanceDialog.close()
                        } else if (timer.saveCustomSectionAppearance(sectionAppearanceDialog.targetId, sectionAppearanceDialog.targetRevision, dialogs.appearanceSectionId, dialogs.appearanceIconChoice, finalColor)) {
                            sectionAppearanceDialog.close()
                        } else {
                            sectionAppearanceDialog.showFeedback()
                        }
                    }
                }
            }
        }
    }

    TimerScheduleTimingDialog {
        id: timingDialog
        scheduleData: dialogs.scheduleData
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // DUPLICATE / DELETE / RESET / MOVE DIALOGS
    // ═══════════════════════════════════════════════════════════════════════════
    ScheduleBoundPopup {
        id: duplicateDialog
        objectName: "timerDuplicateScheduleDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(380, parent ? parent.width - 32 : 380))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle { radius: 16; color: dialogs.pal_surface; border.color: dialogs.pal_borderStrong; border.width: 1 }

        contentItem: ColumnLayout {
            width: duplicateDialog.availableWidth
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: qsTr("Duplicate schedule"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: duplicateDialog.close() }
            }
            Text { Layout.fillWidth: true; text: qsTr("Creates a copy of the structure with all results reset."); color: dialogs.pal_textMuted; font.pixelSize: 12; wrapMode: Text.WordWrap }
            TimerModalTextField {
                id: dupTitleInput
                objectName: "timerDuplicateTitleInput"
                Layout.fillWidth: true
                onAccepted: dupConfirmBtn.clicked()
            }
            ModalFeedback { text: duplicateDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: duplicateDialog.close() }
                TimerButton {
                    id: dupConfirmBtn
                    objectName: "timerDuplicateConfirmButton"
                    text: qsTr("Duplicate"); variant: "primary"; accent: dialogs.pal_accent
                    enabled: dupTitleInput.text.trim().length > 0
                    onClicked: {
                        if (!dupTitleInput.text.trim()) return
                        var copyId = timer.duplicateCustomSchedule(duplicateDialog.targetId, duplicateDialog.targetRevision, dupTitleInput.text)
                        if (copyId) duplicateDialog.close()
                        else duplicateDialog.showFeedback()
                    }
                }
            }
        }
    }

    ScheduleBoundPopup {
        id: deleteDialog
        objectName: "timerDeleteScheduleDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(380, parent ? parent.width - 32 : 380))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle { radius: 16; color: dialogs.pal_surface; border.color: dialogs.pal_borderStrong; border.width: 1 }

        contentItem: ColumnLayout {
            width: deleteDialog.availableWidth
            spacing: 12
            Text { text: qsTr("Delete schedule?"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
            Text { Layout.fillWidth: true; text: qsTr("“%1” and its recorded results will be permanently removed.").arg(dialogs.deleteTargetTitle); textFormat: Text.PlainText; color: dialogs.pal_textSecondary; font.pixelSize: 13; wrapMode: Text.WordWrap }
            ModalFeedback { text: deleteDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: deleteDialog.close() }
                TimerButton {
                    objectName: "timerDeleteConfirmButton"
                    text: qsTr("Delete"); variant: "primary"; accent: dialogs.pal_danger
                    onClicked: {
                        if (timer.deleteCustomSchedule(deleteDialog.targetId, deleteDialog.targetRevision)) deleteDialog.close()
                        else deleteDialog.showFeedback()
                    }
                }
            }
        }
    }

    ScheduleBoundPopup {
        id: resetDialog
        objectName: "timerResetResultsDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(360, parent ? parent.width - 32 : 360))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle { radius: 16; color: dialogs.pal_surface; border.color: dialogs.pal_borderStrong; border.width: 1 }

        contentItem: ColumnLayout {
            width: resetDialog.availableWidth
            spacing: 12
            Text { text: qsTr("Reset results?"); color: dialogs.pal_textPrimary; font.pixelSize: 16; font.weight: Font.Bold }
            Text { Layout.fillWidth: true; text: qsTr("All recorded times in this schedule will be cleared. The structure stays intact."); color: dialogs.pal_textSecondary; font.pixelSize: 13; wrapMode: Text.WordWrap }
            ModalFeedback { text: resetDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: resetDialog.close() }
                TimerButton {
                    objectName: "timerResetConfirmButton"
                    text: qsTr("Reset results"); variant: "primary"; accent: dialogs.pal_accent
                    onClicked: {
                        if (timer.resetAllResults(resetDialog.targetId, resetDialog.targetRevision)) resetDialog.close()
                        else resetDialog.showFeedback()
                    }
                }
            }
        }
    }

    ScheduleBoundPopup {
        id: moveDialog
        objectName: "timerMoveDialog"
        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(400, parent ? parent.width - 32 : 400))
        padding: 20
        modal: true; dim: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
        background: Rectangle { radius: 16; color: dialogs.pal_surface; border.color: dialogs.pal_borderStrong; border.width: 1 }

        readonly property var moveSourceEntries: dialogs.moveEntriesOverride || dialogs.entries

        readonly property var positionOptions: {
            var src = moveSourceEntries
            var list = []
            if (dialogs.moveItemType === "section") {
                for (var i = 0; i < src.length; i++) {
                    var e = src[i]
                    if (e.id !== dialogs.moveItemId)
                        list.push({ id: e.id, label: qsTr("Before “%1”").arg(e.title) })
                }
                list.push({ id: "", label: qsTr("At the end") })
                return list
            }
            if (dialogs.moveTargetSectionId === "") {
                for (var j = 0; j < src.length; j++) {
                    var rootEntry = src[j]
                    if (rootEntry.id !== dialogs.moveItemId)
                        list.push({ id: rootEntry.id, label: qsTr("Before “%1”").arg(rootEntry.title) })
                }
                list.push({ id: "", label: qsTr("At the end") })
                return list
            }
            for (var k = 0; k < src.length; k++) {
                var sec = src[k]
                if (sec.type === "section" && sec.id === dialogs.moveTargetSectionId && sec.parts) {
                    for (var m = 0; m < sec.parts.length; m++) {
                        var p = sec.parts[m]
                        if (p.id !== dialogs.moveItemId)
                            list.push({ id: p.id, label: qsTr("Before “%1”").arg(p.title) })
                    }
                    break
                }
            }
            list.push({ id: "", label: qsTr("At the end") })
            return list
        }

        contentItem: ColumnLayout {
            width: moveDialog.availableWidth
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Move “%1”").arg(dialogs.moveItemTitle)
                    textFormat: Text.PlainText
                    color: dialogs.pal_textPrimary; font.pixelSize: 15; font.weight: Font.Bold; elide: Text.ElideRight
                }
                TimerButton { iconName: "close"; iconSize: 14; Layout.preferredWidth: 32; implicitHeight: 32; onClicked: moveDialog.close() }
            }

            ColumnLayout {
                visible: dialogs.moveItemType === "part"
                Layout.fillWidth: true; spacing: 4
                readonly property var secOpts: dialogs.sectionOptionsFrom(moveDialog.moveSourceEntries)
                Text { text: qsTr("Destination"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                TimerSelect {
                    Layout.fillWidth: true
                    options: {
                        var labels = []
                        for (var i = 0; i < parent.secOpts.length; i++) labels.push(parent.secOpts[i].title)
                        return labels
                    }
                    optionKeys: parent.secOpts.map(function(section) { return section.id })
                    current: {
                        for (var i = 0; i < parent.secOpts.length; i++) {
                            if (parent.secOpts[i].id === dialogs.moveTargetSectionId) return i
                        }
                        return 0
                    }
                    onPicked: function(idx) {
                        if (idx >= 0 && idx < parent.secOpts.length) {
                            dialogs.moveTargetSectionId = parent.secOpts[idx].id
                            dialogs.moveTargetBeforeId = ""
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true; spacing: 4
                Text { text: qsTr("Position"); color: dialogs.pal_textSecondary; font.pixelSize: 12 }
                TimerSelect {
                    Layout.fillWidth: true
                    options: {
                        var out = []
                        for (var i = 0; i < moveDialog.positionOptions.length; i++)
                            out.push(moveDialog.positionOptions[i].label)
                        return out
                    }
                    optionKeys: moveDialog.positionOptions.map(function(position) { return position.id })
                    current: {
                        for (var i = 0; i < moveDialog.positionOptions.length; i++) {
                            if (moveDialog.positionOptions[i].id === dialogs.moveTargetBeforeId) return i
                        }
                        return Math.max(0, moveDialog.positionOptions.length - 1)
                    }
                    onPicked: function(idx) {
                        if (idx >= 0 && idx < moveDialog.positionOptions.length)
                            dialogs.moveTargetBeforeId = moveDialog.positionOptions[idx].id
                    }
                }
            }

            ModalFeedback { text: moveDialog.errorMessage }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                TimerButton { text: qsTr("Cancel"); variant: "ghost"; onClicked: moveDialog.close() }
                TimerButton {
                    objectName: "timerMoveConfirmButton"
                    text: qsTr("Move"); variant: "primary"; accent: dialogs.pal_accent
                    onClicked: {
                        if (dialogs.moveDraftMode) {
                            dialogs.draftMoveSubmitted(dialogs.moveItemType, dialogs.moveItemId, dialogs.moveTargetSectionId, dialogs.moveTargetBeforeId)
                            moveDialog.close()
                            return
                        }
                        var ok = false
                        if (dialogs.moveItemType === "section")
                            ok = timer.moveCustomSection(moveDialog.targetId, moveDialog.targetRevision, dialogs.moveItemId, dialogs.moveTargetBeforeId)
                        else
                            ok = timer.moveCustomPart(moveDialog.targetId, moveDialog.targetRevision, dialogs.moveItemId, dialogs.moveTargetSectionId, dialogs.moveTargetBeforeId)
                        if (ok) moveDialog.close()
                        else moveDialog.showFeedback()
                    }
                }
            }
        }
    }
}
