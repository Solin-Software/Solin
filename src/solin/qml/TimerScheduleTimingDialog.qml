pragma Translator: "AdvancedTimerPage"

import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Popup {
    required property var scheduleData
    property color pal_bg: appTheme.bg
    property color pal_surface: appTheme.surface
    property color pal_hover: appTheme.hover
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_accent: appTheme.accent
    property color pal_danger: appTheme.danger
    readonly property string scheduleId: scheduleData && scheduleData.id ? scheduleData.id : ""
    readonly property int scheduleRevision: scheduleData && scheduleData.revision !== undefined ? scheduleData.revision : 0
    readonly property var entries: scheduleData && scheduleData.entries ? scheduleData.entries : []

    readonly property var allParts: {
        var out = []
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i]
            if (e.type === "part") {
                out.push(e)
            } else if (e.type === "section" && e.parts) {
                for (var j = 0; j < e.parts.length; j++)
                    out.push(e.parts[j])
            }
        }
        return out
    }

    property bool timingAdvancedExpanded: false
    property string timingDraftScheduleId: ""
    property int timingDraftRevision: 0
    property var timingDraftParts: []
    property bool timingHadWindow: false
    property string timingLocalError: ""
    property var draftTimingWeights: ({})
    property var draftTimingBefore: ({})

    property string timingDraftTotalLabel: ""

    function parseSecondsInput(text) {
        var cleaned = String(text || "").trim()
        if (!cleaned) return -1
        if (/^\d+$/.test(cleaned)) {
            var secs = parseInt(cleaned, 10)
            return isFinite(secs) && secs >= 0 && secs <= timer.maxPlannedPartSeconds ? secs : -2
        }
        var parts = cleaned.split(":")
        if (parts.length === 2 && /^\d+$/.test(parts[0]) && /^\d{1,2}$/.test(parts[1])) {
            var m = parseInt(parts[0], 10)
            var s = parseInt(parts[1], 10)
            return isFinite(m) && isFinite(s) && m >= 0 && s >= 0 && s < 60
                   && m * 60 + s <= timer.maxPlannedPartSeconds ? m * 60 + s : -2
        }
        return -2
    }

    function openForSchedule() {
        if (!scheduleId) return
        timer.clearFeedback()
        syncTimingFields()
        timingAdvancedExpanded = false
        timingDialog.open()
    }

    function syncTimingFields() {
        timingDraftScheduleId = timingDialog.scheduleId
        timingDraftRevision = timingDialog.scheduleRevision
        timingDraftTotalLabel = scheduleData && scheduleData.plannedTotalLabel || ""
        timingLocalError = ""
        var w = scheduleData && scheduleData.pacingWindow ? scheduleData.pacingWindow : null
        timingHadWindow = !!(w && w.configured)
        timingStartField.text = w && w.startTime ? w.startTime : ""
        timingEndField.text = w && w.endTime ? w.endTime : ""
        timingUntrackedField.text = w && w.untrackedSeconds >= 0 ? String(w.untrackedSeconds) : ""
        timingClosingField.text = w && w.closingSeconds >= 0 ? String(w.closingSeconds) : ""

        var snapshotParts = []
        var weights = {}
        var before = {}
        var existingBefore = w && w.beforeParts ? w.beforeParts : ({})
        for (var i = 0; i < allParts.length; i++) {
            var p = allParts[i]
            var weight = p.adjustmentWeight || 0
            snapshotParts.push({
                id: p.id,
                title: p.title || "",
                plannedLabel: p.plannedLabel || "",
                eligibleForPacing: p.eligibleForPacing === true,
                adjustmentWeight: weight
            })
            weights[p.id] = weight
            before[p.id] = existingBefore[p.id] !== undefined ? String(existingBefore[p.id]) : ""
        }
        timingDraftParts = snapshotParts
        draftTimingWeights = weights
        draftTimingBefore = before
    }

    function hasAnyDraftWeights() {
        var keys = Object.keys(draftTimingWeights || {})
        for (var i = 0; i < keys.length; i++) {
            if ((draftTimingWeights[keys[i]] || 0) > 0) return true
        }
        return false
    }

    function setDraftWeight(partId, weightValue) {
        var next = Object.assign({}, draftTimingWeights || {})
        next[partId] = Math.max(0, Math.min(100, parseInt(weightValue, 10) || 0))
        draftTimingWeights = next
    }

    function setDraftBefore(partId, textValue) {
        var next = Object.assign({}, draftTimingBefore || {})
        next[partId] = String(textValue || "")
        draftTimingBefore = next
    }

    function clearDraftTimingWindow() {
        timingLocalError = ""
        timingStartField.text = ""
        timingEndField.text = ""
        timingUntrackedField.text = ""
        timingClosingField.text = ""
        var clearedBefore = {}
        for (var i = 0; i < timingDraftParts.length; i++)
            clearedBefore[timingDraftParts[i].id] = ""
        draftTimingBefore = clearedBefore
        timer.clearFeedback()
    }

    function submitTimingSettings() {
        timingLocalError = ""
        timer.clearFeedback()

        var startVal = timingStartField.text.trim()
        var endVal = timingEndField.text.trim()
        var hasWindowInput = startVal.length > 0 || endVal.length > 0

        var untrackedVal = -1
        var closingVal = -1
        var beforePayload = {}

        var beforeKeys = Object.keys(draftTimingBefore || {})
        if (!hasWindowInput && hasDraftEstimates) {
            timingLocalError = qsTr("Enter start and end times or clear the estimates before saving.")
            return
        }

        if (hasWindowInput) {
            untrackedVal = parseSecondsInput(timingUntrackedField.text)
            if (untrackedVal === -2) {
                timingLocalError = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                return
            }

            closingVal = parseSecondsInput(timingClosingField.text)
            if (closingVal === -2) {
                timingLocalError = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                return
            }

            for (var i = 0; i < beforeKeys.length; i++) {
                var k = beforeKeys[i]
                var parsedBefore = parseSecondsInput(draftTimingBefore[k])
                if (parsedBefore === -2) {
                    timingLocalError = qsTr("Enter a valid duration (e.g., 5 or 05:00).")
                    return
                }
                beforePayload[k] = parsedBefore
            }
        }

        var payload = {
            startTime: startVal,
            endTime: endVal,
            untrackedSeconds: untrackedVal,
            closingSeconds: closingVal,
            weights: draftTimingWeights || ({}),
            beforeParts: beforePayload
        }
        if (timer.saveCustomTimingSettings(timingDialog.timingDraftScheduleId, timingDialog.timingDraftRevision, payload)) {
            timingDialog.close()
        }
    }

    id: timingDialog
    objectName: "timerScheduleTimingDialog"
    parent: Overlay.overlay
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    width: Math.max(0, Math.min(520, parent ? parent.width - 28 : 520))
    height: Math.max(0, Math.min(timingBody.implicitHeight + 174 + (windowFeedback.visible ? windowFeedback.implicitHeight + 12 : 0), parent ? parent.height - 32 : 660))
    padding: 20
    modal: true; dim: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    readonly property bool hasDraftWindow: timingStartField.text.trim().length > 0 || timingEndField.text.trim().length > 0
    readonly property bool hasDraftEstimates: timingUntrackedField.text.trim().length > 0
        || timingClosingField.text.trim().length > 0
        || Object.keys(draftTimingBefore || {}).some(function(key) { return String(draftTimingBefore[key]).trim().length > 0 })
    readonly property bool canClearWindow: hasDraftWindow || timingHadWindow || hasDraftEstimates

    Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.50 }
    background: Rectangle {
        radius: 16; color: timingDialog.pal_surface
        border.color: timingDialog.pal_borderStrong; border.width: 1
    }

    contentItem: ColumnLayout {
        width: timingDialog.availableWidth
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                Layout.fillWidth: true; spacing: 2
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Schedule timing and pacing")
                    color: timingDialog.pal_textPrimary
                    font.pixelSize: 16; font.weight: Font.Bold; elide: Text.ElideRight
                }
                Text {
                    visible: timingDialog.timingDraftTotalLabel.length > 0
                    Layout.fillWidth: true
                    text: qsTr("Total planned parts: %1").arg(timingDialog.timingDraftTotalLabel)
                    color: timingDialog.pal_textMuted; font.pixelSize: 11; elide: Text.ElideRight
                }
            }
            TimerButton {
                iconName: "close"; iconSize: 14
                tip: qsTranslate("SettingsWidget", "Close")
                Layout.preferredWidth: 32; implicitHeight: 32
                onClicked: timingDialog.close()
            }
        }

        Rectangle {
            id: windowFeedback
            objectName: "timerWindowFeedbackBanner"
            visible: timingDialog.timingLocalError.length > 0 || (
                !!timer.feedback && !!timer.feedback.message && (
                    timer.feedback.scope === "window"
                    || String(timer.feedback.scope || "").indexOf("part:") === 0
                )
            )
            Layout.fillWidth: true
            implicitHeight: winFeedbackText.implicitHeight + 14
            radius: 8
            color: Qt.rgba(timingDialog.pal_danger.r, timingDialog.pal_danger.g, timingDialog.pal_danger.b, 0.12)
            border.color: timingDialog.pal_danger; border.width: 1
            Text {
                id: winFeedbackText
                anchors.fill: parent; anchors.margins: 7
                textFormat: Text.PlainText
                text: timingDialog.timingLocalError.length > 0
                      ? timingDialog.timingLocalError
                      : (timer.feedback && timer.feedback.message ? timer.feedback.message : "")
                color: timingDialog.pal_danger; font.pixelSize: 12; wrapMode: Text.WordWrap
            }
        }

        Flickable {
            id: timingScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: timingBody.implicitHeight + 6
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            ColumnLayout {
                id: timingBody
                width: timingScroll.width
                spacing: 14

                Text {
                    Layout.fillWidth: true
                    text: qsTr("Set start and end times so the timer can track whether you are ahead or behind, and adjust remaining parts when Automatic mode is active.")
                    color: timingDialog.pal_textMuted
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.preferredWidth: 100; Layout.minimumWidth: 0; spacing: 4
                        Text { Layout.fillWidth: true; elide: Text.ElideRight; text: qsTr("Start (HH:MM)"); color: timingDialog.pal_textSecondary; font.pixelSize: 11; font.weight: Font.Medium }
                        TimerModalTextField {
                            id: timingStartField
                            objectName: "timerWindowStartInput"
                            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredWidth: 0
                            placeholderText: "19:30"
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.preferredWidth: 100; Layout.minimumWidth: 0; spacing: 4
                        Text { Layout.fillWidth: true; elide: Text.ElideRight; text: qsTr("End (HH:MM)"); color: timingDialog.pal_textSecondary; font.pixelSize: 11; font.weight: Font.Medium }
                        TimerModalTextField {
                            id: timingEndField
                            objectName: "timerWindowEndInput"
                            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredWidth: 0
                            placeholderText: "20:30"
                        }
                    }
                }

                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: timingDialog.pal_border; opacity: 0.7 }

                ColumnLayout {
                    visible: timingDialog.timingDraftParts.length > 0
                    Layout.fillWidth: true; spacing: 6

                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Parts allowed to adjust automatically")
                        color: timingDialog.pal_textPrimary; font.pixelSize: 13; font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: timingDialog.hasAnyDraftWeights()
                              ? qsTr("In Automatic mode, only checked parts are shortened or extended to finish on time. Unchecked parts keep their planned duration.")
                              : qsTr("In Automatic mode, checked parts can be shortened or extended to finish on time. With none checked, all parts of 1 min or more adjust equally.")
                        color: timingDialog.pal_textMuted; font.pixelSize: 11; wrapMode: Text.WordWrap
                    }

                    Repeater {
                        model: timingDialog.timingDraftParts
                        delegate: Rectangle {
                            required property var modelData
                            readonly property bool eligible: modelData.eligibleForPacing === true
                            readonly property int currentWeight: (timingDialog.draftTimingWeights && timingDialog.draftTimingWeights[modelData.id] !== undefined)
                                ? timingDialog.draftTimingWeights[modelData.id]
                                : (modelData.adjustmentWeight || 0)
                            readonly property bool selected: currentWeight > 0
                            readonly property bool canToggle: eligible || selected

                            Layout.fillWidth: true
                            implicitHeight: 34
                            enabled: canToggle
                            activeFocusOnTab: enabled
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: modelData.title || ""
                            Accessible.checked: selected
                            Accessible.onPressAction: if (canToggle) timingDialog.setDraftWeight(modelData.id, selected ? 0 : 1)
                            Keys.onPressed: function(event) {
                                if (canToggle && (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                                    timingDialog.setDraftWeight(modelData.id, selected ? 0 : 1)
                                    event.accepted = true
                                }
                            }
                            border.width: activeFocus ? 2 : 0
                            border.color: timingDialog.pal_accent
                            radius: 8
                            color: partCheckHover.hovered && canToggle ? timingDialog.pal_hover : "transparent"
                            HoverHandler { id: partCheckHover }

                            RowLayout {
                                anchors.fill: parent; anchors.leftMargin: 6; anchors.rightMargin: 8
                                spacing: 10

                                Rectangle {
                                    objectName: "timerAdjustablePartCheck"
                                    Layout.preferredWidth: 18; Layout.preferredHeight: 18
                                    radius: 5
                                    color: selected ? timingDialog.pal_accent : timingDialog.pal_bg
                                    border.color: selected ? timingDialog.pal_accent : (eligible ? timingDialog.pal_borderStrong : timingDialog.pal_border)
                                    border.width: 1.5
                                    opacity: eligible ? 1.0 : 0.40

                                    TimerIcon {
                                        anchors.centerIn: parent
                                        visible: selected
                                        width: 11; height: 11
                                        rasterSize: 22
                                        iconName: "check"
                                        tint: appTheme.white
                                    }
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.title || ""
                                    textFormat: Text.PlainText
                                    color: eligible ? timingDialog.pal_textPrimary : timingDialog.pal_textMuted
                                    font.pixelSize: 12; elide: Text.ElideRight
                                }

                                Text {
                                    text: eligible ? (modelData.plannedLabel || "") : qsTr("Under 1 min")
                                    color: timingDialog.pal_textMuted; font.pixelSize: 11
                                }
                            }

                            TimerPointerArea {
                                anchors.fill: parent
                                enabled: canToggle
                                onClicked: timingDialog.setDraftWeight(modelData.id, selected ? 0 : 1)
                            }
                        }
                    }
                }

                TimerButton {
                    objectName: "timerAdvancedPacingToggle"
                    text: qsTr("Advanced options")
                    iconName: timingDialog.timingAdvancedExpanded ? "chevron_up" : "chevron_down"
                    iconSize: 12
                    variant: "ghost"
                    implicitHeight: 30
                    onClicked: timingDialog.timingAdvancedExpanded = !timingDialog.timingAdvancedExpanded
                }

                ColumnLayout {
                    visible: timingDialog.timingAdvancedExpanded
                    Layout.fillWidth: true; spacing: 10

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: advHelpCol.implicitHeight + 16
                        radius: 8
                        color: timingDialog.pal_bg
                        border.color: timingDialog.pal_border
                        border.width: 1

                        ColumnLayout {
                            id: advHelpCol
                            anchors.left: parent.left; anchors.right: parent.right
                            anchors.top: parent.top; anchors.margins: 8
                            spacing: 4

                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Weight (0–100): higher weights receive more adjustment. A weight of 0 keeps a part fixed when another part has a positive weight. If all weights are 0, eligible parts adjust equally.")
                                color: timingDialog.pal_textSecondary
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Leave the total estimate blank to use the time left after the planned parts. Breaks with an estimate are reserved first. The rest is split equally among blank breaks. Enter 0 to reserve no time.")
                                color: timingDialog.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true; spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Estimated time outside parts (sec)")
                            color: timingDialog.pal_textSecondary
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                        TimerModalTextField {
                            id: timingUntrackedField
                            objectName: "timerWindowUntrackedInput"
                            Layout.preferredWidth: 78; implicitHeight: 30
                            font.pixelSize: 12; horizontalAlignment: Text.AlignRight
                            enabled: timingDialog.hasDraftWindow; opacity: timingDialog.hasDraftWindow ? 1.0 : 0.45
                            placeholderText: qsTr("Auto")
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                    }

                    RowLayout {
                        visible: timingDialog.timingDraftParts.length > 0
                        Layout.fillWidth: true; spacing: 10
                        Layout.topMargin: 2
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Part")
                            color: timingDialog.pal_textMuted
                            font.pixelSize: 11; font.weight: Font.Medium
                        }
                        Text {
                            Layout.preferredWidth: 64
                            text: qsTr("Weight")
                            color: timingDialog.pal_textMuted
                            font.pixelSize: 11; font.weight: Font.Medium
                            horizontalAlignment: Text.AlignRight
                        }
                        Text {
                            Layout.preferredWidth: 126
                            text: qsTr("Break before (sec)")
                            color: timingDialog.pal_textMuted
                            font.pixelSize: 11; font.weight: Font.Medium
                            horizontalAlignment: Text.AlignRight
                        }
                    }

                    Repeater {
                        model: timingDialog.timingDraftParts
                        delegate: RowLayout {
                            required property var modelData
                            readonly property bool eligible: modelData.eligibleForPacing === true
                            readonly property int draftWeight: (timingDialog.draftTimingWeights && timingDialog.draftTimingWeights[modelData.id] !== undefined)
                                ? timingDialog.draftTimingWeights[modelData.id]
                                : (modelData.adjustmentWeight || 0)
                            readonly property string draftBefore: (timingDialog.draftTimingBefore && timingDialog.draftTimingBefore[modelData.id] !== undefined)
                                ? timingDialog.draftTimingBefore[modelData.id]
                                : ""

                            Layout.fillWidth: true; spacing: 10
                            Text {
                                Layout.fillWidth: true
                                text: modelData.title || ""
                                textFormat: Text.PlainText
                                color: timingDialog.pal_textSecondary
                                font.pixelSize: 12
                                elide: Text.ElideRight
                            }
                            TimerModalTextField {
                                Layout.preferredWidth: 64; implicitHeight: 28
                                font.pixelSize: 12; horizontalAlignment: Text.AlignRight
                                enabled: eligible; opacity: eligible ? 1.0 : 0.45
                                text: String(draftWeight)
                                inputMethodHints: Qt.ImhDigitsOnly
                                onTextEdited: {
                                    var w = parseInt(text.trim(), 10)
                                    timingDialog.setDraftWeight(modelData.id, isFinite(w) && w >= 0 ? w : 0)
                                }
                            }
                            TimerModalTextField {
                                Layout.preferredWidth: 126; implicitHeight: 28
                                font.pixelSize: 12; horizontalAlignment: Text.AlignRight
                                enabled: timingDialog.hasDraftWindow; opacity: timingDialog.hasDraftWindow ? 1.0 : 0.45
                                text: draftBefore
                                placeholderText: qsTr("Auto")
                                inputMethodHints: Qt.ImhDigitsOnly
                                onTextEdited: timingDialog.setDraftBefore(modelData.id, text)
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true; spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Time after last part (sec)")
                            color: timingDialog.pal_textSecondary
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                        TimerModalTextField {
                            id: timingClosingField
                            objectName: "timerWindowClosingInput"
                            Layout.preferredWidth: 78; implicitHeight: 30
                            font.pixelSize: 12; horizontalAlignment: Text.AlignRight
                            enabled: timingDialog.hasDraftWindow; opacity: timingDialog.hasDraftWindow ? 1.0 : 0.45
                            placeholderText: qsTr("Auto")
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                    }
                }
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: timingDialog.pal_border; opacity: 0.7 }

        RowLayout {
            Layout.fillWidth: true; spacing: 8
            TimerButton {
                objectName: "timerClearWindowButton"
                visible: timingDialog.canClearWindow
                text: qsTr("Clear time")
                variant: "ghost"
                implicitHeight: 34
                onClicked: timingDialog.clearDraftTimingWindow()
            }
            Item { Layout.fillWidth: true }
            TimerButton {
                objectName: "timerCancelTimingButton"
                text: qsTr("Cancel")
                variant: "ghost"
                implicitHeight: 34
                onClicked: timingDialog.close()
            }
            TimerButton {
                objectName: "timerSaveTimingButton"
                text: qsTr("Save")
                variant: "primary"
                accent: timingDialog.pal_accent
                implicitHeight: 34
                onClicked: timingDialog.submitTimingSettings()
            }
        }
    }
}
