pragma Translator: "AdvancedTimerPage"

// TimerClockPanel.qml — centralized live clock, pacing summary, and primary
// part controls shared by Meetings and Custom Schedules.
// Adapts seamlessly between the right-side card (wide) and sticky top strip (compact).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: panel

    property var live: ({})
    property var summary: ({})
    property var pacing: ({})
    property var selectedPart: null
    property string scheduleKind: "meeting"
    property bool compactStrip: false
    property bool narrowWidth: width < 380

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
    property color pal_success: appTheme.success
    property color pal_warning: appTheme.warning
    property color pal_danger: appTheme.danger

    signal customTimingRequested()
    signal resetAllRequested()
    signal editSelectedPartRequested(var partRow)

    property bool adjustOpen: false
    property bool showingCompactControls: false
    property string focusedControl: ""

    function rememberFocus(control, focused) {
        if (focused) {
            focusedControl = control
        } else if (focusedControl === control) {
            focusedControl = ""
        }
    }

    onCompactStripChanged: {
        var control = focusedControl
        showingCompactControls = compactStrip
        if (!control) return
        Qt.callLater(function() {
            var target = control === "primary" ? (compactStrip ? compactPrimary : widePrimary)
                       : control === "adjust" ? (compactStrip ? compactAdjust : wideAdjust)
                       : control === "reset" ? (compactStrip ? compactReset : wideReset)
                       : control === "resetAll" ? (compactStrip ? compactResetAll : wideResetAll)
                       : control === "edit" ? (compactStrip ? compactEdit : wideEdit)
                       : control === "next" ? (compactStrip ? compactNext : wideNext)
                       : control === "timing" ? (compactStrip ? compactTiming : wideTiming)
                       : null
            if (control === "resetAll" && compactStrip && primaryResets)
                target = compactPrimary
            if (control.indexOf("delta:") === 0) {
                var repeater = compactStrip ? compactAdjustments : wideAdjustments
                for (var i = 0; i < repeater.count; i++) {
                    var candidate = repeater.itemAt(i)
                    if (candidate && String(candidate.modelData.delta) === control.substring(6)) {
                        target = candidate
                        break
                    }
                }
            }
            if (target && target.visible && target.enabled)
                target.forceActiveFocus(Qt.TabFocusReason)
        })
    }

    readonly property bool isRunning: !!live && live.active === true && live.state === "running"
    readonly property bool isFrozenSelected: !isRunning
                                             && !!live && live.active === true && live.state === "stopped"
                                             && targetPartId.length > 0
                                             && live.active_part_id === targetPartId
    readonly property bool isOverrun: (isRunning || isFrozenSelected) && live.overrun === true
    readonly property string activePartId: isRunning ? (live.active_part_id || "") : ""
    readonly property bool allCompleted: !isRunning && !!summary && summary.allCompleted === true
    readonly property bool primaryResets: !isRunning && allCompleted && !targetPartId
    readonly property bool primaryEnabled: isRunning || targetPartId.length > 0 || allCompleted
    readonly property string primaryText: isRunning ? qsTr("Stop")
                                          : targetIsCompleted ? qsTr("Restart")
                                          : primaryResets ? qsTr("Reset results") : qsTr("Start")
    readonly property string primaryIcon: isRunning ? "stop" : primaryResets ? "reset" : "play"

    function activatePrimary() {
        if (isRunning)
            timer.stopPart(activePartId)
        else if (targetPartId.length > 0)
            timer.startPart(targetPartId)
        else if (allCompleted)
            resetAllRequested()
    }
    readonly property int partCount: summary && summary.totalCount ? summary.totalCount : 0
    readonly property int completedCount: summary && summary.completedCount ? summary.completedCount : 0
    readonly property string nextPartId: summary && summary.nextPartId ? summary.nextPartId : ""
    readonly property string nextPartTitle: summary && summary.nextPartTitle ? summary.nextPartTitle : ""
    readonly property string nextPartPlannedLabel: summary && summary.nextPartPlannedLabel ? summary.nextPartPlannedLabel : ""

    // Effective target part when stopped: user-selected row or auto-selected next idle part
    readonly property string targetPartId: !isRunning && selectedPart && selectedPart.id
                                           ? selectedPart.id
                                           : nextPartId
    readonly property string targetPartTitle: !isRunning && selectedPart && selectedPart.title
                                              ? selectedPart.title
                                              : nextPartTitle
    readonly property string targetPartState: !isRunning && selectedPart && selectedPart.state
                                              ? selectedPart.state
                                              : "idle"
    readonly property bool targetIsCompleted: !isRunning && targetPartState === "stopped"
    readonly property bool targetOverTarget: !isRunning && selectedPart && selectedPart.overTarget === true
    readonly property bool targetHasAdjusted: isRunning
                                              ? (!!summary && summary.activeHasAdjustedTarget === true)
                                              : (!!selectedPart && selectedPart.hasAdjustedTarget === true)
    readonly property string targetPlannedLabel: isRunning
                                                 ? (summary && summary.activePlannedLabel ? summary.activePlannedLabel : "")
                                                 : (selectedPart && selectedPart.plannedLabel ? selectedPart.plannedLabel : nextPartPlannedLabel)
    readonly property string targetEffectiveLabel: isRunning
                                                   ? (summary && summary.activeEffectiveLabel ? summary.activeEffectiveLabel : "")
                                                   : (selectedPart && selectedPart.effectiveLabel ? selectedPart.effectiveLabel : targetPlannedLabel)
    readonly property string targetResultLabel: !isRunning && selectedPart && selectedPart.resultLabel
                                                ? selectedPart.resultLabel : ""

    // Remaining-time progress ratio (matches mockup: 07:42 of 10:00 fills ~77%)
    readonly property real progressRatio: {
        if (!isRunning) {
            if (targetIsCompleted) return 1.0
            return targetPartId.length > 0 ? 1.0 : 0.0
        }
        if (isOverrun) return 1.0
        var total = Number(live.target_seconds || live.planned_seconds || 0)
        if (total <= 0) return 0.0
        var remaining = Number(live.remaining_seconds || 0)
        return Math.max(0.0, Math.min(1.0, remaining / total))
    }

    // Pacing state helpers
    readonly property bool pacingEnabled: !!pacing && pacing.mode && pacing.mode !== "off"
    readonly property string pacingAvailability: pacingEnabled ? (pacing.availability || "") : ""
    readonly property bool pacingReady: pacingAvailability === "ready"
    readonly property string pacingStatus: pacing && pacing.status ? pacing.status : ""
    readonly property int pacingDeltaMin: pacing && pacing.deviationMinutes ? pacing.deviationMinutes : 0
    readonly property color pacingColor: pacingStatus === "behind"
                                         ? pal_warning
                                         : (pacingStatus === "ahead" || pacingStatus === "on_time")
                                           ? pal_success
                                           : pal_textSecondary

    readonly property string pacingHeadline: {
        if (!pacingReady) return ""
        if (pacingStatus === "behind")
            return qsTr("Projected %1 min behind").arg(pacingDeltaMin)
        if (pacingStatus === "ahead")
            return qsTr("Projected %1 min ahead").arg(pacingDeltaMin)
        return qsTr("On schedule")
    }

    readonly property string pacingSubline: {
        if (!pacingReady) return ""
        return qsTr("Projected end %1 · Target %2")
            .arg(pacing.projectedEndLabel || "")
            .arg(pacing.targetEndLabel || "")
    }

    function formatClockSeconds(seconds) {
        var num = Number(seconds)
        if (!isFinite(num)) return "00:00"
        var sign = num < 0 ? "-" : ""
        var total = Math.round(Math.abs(num))
        var h = Math.floor(total / 3600)
        var rem = total % 3600
        var m = Math.floor(rem / 60)
        var s = rem % 60
        var mm = (m < 10 ? "0" : "") + m
        var ss = (s < 10 ? "0" : "") + s
        if (h > 0) return sign + h + ":" + mm + ":" + ss
        return sign + mm + ":" + ss
    }

    readonly property string displayDigits: {
        if (isRunning || isFrozenSelected) {
            if (live.display_seconds !== undefined)
                return formatClockSeconds(live.display_seconds)
            return live.display_text || "00:00"
        }
        if (targetIsCompleted && targetResultLabel)
            return targetResultLabel
        if (targetEffectiveLabel)
            return targetEffectiveLabel
        if (partCount > 0 && summary && summary.plannedTotalLabel)
            return summary.plannedTotalLabel
        return "00:00"
    }

    readonly property string statusBadgeText: {
        if (isOverrun) return qsTr("Part overtime")
        if (isRunning) return qsTr("In progress")
        if (targetIsCompleted)
            return targetOverTarget ? qsTr("Completed (over target)") : qsTr("Completed")
        if (allCompleted) return qsTr("Schedule completed")
        if (targetPartId.length > 0) return qsTr("Selected part")
        return qsTr("Ready")
    }

    readonly property color statusBadgeColor: {
        if (isOverrun || targetOverTarget) return pal_danger
        if (isRunning) return pal_accentText
        if (targetIsCompleted || allCompleted) return pal_success
        return pal_textMuted
    }

    onIsRunningChanged: {
        if (!isRunning) adjustOpen = false
    }

    radius: compactStrip ? 12 : 16
    color: pal_surface
    border.color: isOverrun ? pal_danger : (isRunning ? Qt.rgba(pal_accent.r, pal_accent.g, pal_accent.b, 0.42) : pal_border)
    border.width: 1
    implicitHeight: compactStrip ? compactCol.implicitHeight + 20 : wideCol.implicitHeight + 36

    Behavior on border.color { ColorAnimation { duration: 180 } }

    // ═══════════════════════════════════════════════════════════════════════════
    // WIDE MODE — Vertically scrollable right-side card (hugs content when room allows)
    // ═══════════════════════════════════════════════════════════════════════════
    Flickable {
        id: wideFlick
        objectName: "timerClockFlickable"
        visible: !panel.showingCompactControls
        anchors.fill: parent
        anchors.margins: 2
        contentWidth: width
        contentHeight: wideCol.implicitHeight + 32
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: function(ev) {
                var maxY = Math.max(0, wideFlick.contentHeight - wideFlick.height)
                if (maxY <= 0) {
                    ev.accepted = false
                    return
                }
                var dy = ev.pixelDelta.y !== 0 ? ev.pixelDelta.y : ev.angleDelta.y / 2.2
                wideFlick.contentY = Math.max(0, Math.min(maxY, wideFlick.contentY - dy))
                ev.accepted = true
            }
        }

        ColumnLayout {
            id: wideCol
            x: 16
            y: 16
            width: Math.max(0, wideFlick.width - 32 - (wideScrollBar.visible ? 8 : 0))
            spacing: 14

        // Status + active/selected part title (+ discreet Edit button when stopped)
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4

            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                Text {
                    objectName: "timerClockStatusLabel"
                    Layout.fillWidth: true
                    text: panel.statusBadgeText
                    color: panel.statusBadgeColor
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                TimerButton {
                    id: wideEdit
                    objectName: "timerEditSelectedPartButton"
                    onActiveFocusChanged: panel.rememberFocus("edit", activeFocus)
                    visible: !panel.isRunning && !!panel.selectedPart && !!panel.selectedPart.id
                    iconName: "edit"
                    iconSize: 13
                    tip: qsTr("Edit part…")
                    variant: "ghost"
                    Layout.preferredWidth: 28
                    implicitHeight: 28
                    onClicked: panel.editSelectedPartRequested(panel.selectedPart)
                }
            }

            Text {
                objectName: "timerClockActiveTitle"
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: panel.isRunning
                      ? (panel.live.active_part_title || "")
                      : (panel.targetPartTitle || qsTr("No parts in this schedule"))
                color: panel.pal_textPrimary
                font.pixelSize: 18
                font.weight: Font.Bold
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
        }

        // Giant tabular clock digits
        TimerDigits {
            objectName: "timerClockDigits"
            Layout.fillWidth: true
            text: panel.displayDigits
            color: panel.isOverrun
                   ? panel.pal_danger
                   : (panel.targetOverTarget ? panel.pal_danger : panel.pal_textPrimary)
            font.pixelSize: 48
            font.weight: Font.Bold
            fontSizeMode: Text.HorizontalFit
            minimumPixelSize: 26
            Behavior on color { ColorAnimation { duration: 160 } }
        }

        // Progress bar + target/planned metadata
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 7
                radius: 4
                color: panel.pal_surfaceAlt
                clip: true

                Rectangle {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: Math.max(panel.progressRatio > 0 ? 6 : 0, parent.width * panel.progressRatio)
                    radius: 4
                    color: (panel.isOverrun || panel.targetOverTarget)
                           ? panel.pal_danger
                           : (panel.targetIsCompleted ? panel.pal_success
                              : (panel.isRunning ? panel.pal_accent : Qt.rgba(panel.pal_accent.r, panel.pal_accent.g, panel.pal_accent.b, 0.38)))
                    Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                    Behavior on color { ColorAnimation { duration: 160 } }
                }
            }

            Text {
                objectName: "timerClockTargetLabel"
                visible: text.length > 0
                Layout.fillWidth: true
                text: {
                    if (panel.isRunning) {
                        if (panel.targetHasAdjusted)
                            return qsTr("Target %1 · Planned %2").arg(panel.targetEffectiveLabel).arg(panel.targetPlannedLabel)
                        return qsTr("Target %1").arg(panel.targetEffectiveLabel || panel.targetPlannedLabel)
                    }
                    if (panel.targetIsCompleted) {
                        if (panel.targetHasAdjusted)
                            return qsTr("Target %1 · Planned %2").arg(panel.targetEffectiveLabel).arg(panel.targetPlannedLabel)
                        return qsTr("Planned %1").arg(panel.targetPlannedLabel)
                    }
                    if (panel.targetPartId.length > 0) {
                        if (panel.targetHasAdjusted)
                            return qsTr("Target %1 · Planned %2").arg(panel.targetEffectiveLabel).arg(panel.targetPlannedLabel)
                        return qsTr("Planned %1").arg(panel.targetPlannedLabel)
                    }
                    return ""
                }
                color: panel.targetHasAdjusted ? panel.pal_accentText : panel.pal_textMuted
                font.pixelSize: 12
                font.weight: panel.targetHasAdjusted ? Font.DemiBold : Font.Normal
                elide: Text.ElideRight
            }
        }

        // Centralized primary actions
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            TimerButton {
                id: widePrimary
                objectName: "timerPrimaryActionButton"
                onActiveFocusChanged: panel.rememberFocus("primary", activeFocus)
                Layout.fillWidth: true
                text: panel.primaryText
                iconName: panel.primaryIcon
                iconSize: 12
                variant: "primary"
                accent: panel.isOverrun ? panel.pal_danger : panel.pal_accent
                enabled: panel.primaryEnabled
                implicitHeight: 38
                onClicked: panel.activatePrimary()
            }

            // While running: Adjust time toggle (matches mockup)
            TimerButton {
                id: wideAdjust
                objectName: "timerAdjustToggleButton"
                onActiveFocusChanged: panel.rememberFocus("adjust", activeFocus)
                visible: panel.isRunning
                Layout.fillWidth: true
                text: qsTr("Adjust time")
                variant: panel.adjustOpen ? "soft" : "ghost"
                accent: panel.pal_accent
                implicitHeight: 38
                onClicked: panel.adjustOpen = !panel.adjustOpen
            }

            // While stopped & inspecting a completed part: Reset this part
            TimerButton {
                id: wideReset
                objectName: "timerResetSelectedPartButton"
                onActiveFocusChanged: panel.rememberFocus("reset", activeFocus)
                visible: !panel.isRunning && panel.targetIsCompleted
                text: qsTr("Reset")
                iconName: "reset"
                iconSize: 12
                tip: qsTr("Reset this part")
                variant: "ghost"
                implicitHeight: 38
                onClicked: timer.resetPart(panel.targetPartId)
            }
        }

        // Manual time adjustment drawer (revealed smoothly while running)
        Rectangle {
            objectName: "timerAdjustDrawer"
            visible: panel.isRunning && panel.adjustOpen
            Layout.fillWidth: true
            implicitHeight: adjustRow.implicitHeight + 12
            radius: 10
            color: panel.pal_bg
            border.color: panel.pal_border
            border.width: 1

            RowLayout {
                id: adjustRow
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4

                Repeater {
                    id: wideAdjustments
                    model: [
                        { label: "−1 min", delta: -60 },
                        { label: "−1 s", delta: -1 },
                        { label: "+1 s", delta: 1 },
                        { label: "+1 min", delta: 60 }
                    ]
                    delegate: TimerButton {
                        required property var modelData
                        onActiveFocusChanged: panel.rememberFocus("delta:" + modelData.delta, activeFocus)
                        Layout.fillWidth: true
                        text: modelData.label
                        variant: "ghost"
                        implicitHeight: 30
                        onClicked: timer.adjustPart(panel.activePartId, modelData.delta)
                    }
                }
            }
        }

        // Secondary "Reset all results" strip when stopped and all parts completed (custom schedules)
        TimerButton {
            id: wideResetAll
            objectName: "timerResetAllCompletedButton"
            onActiveFocusChanged: panel.rememberFocus("resetAll", activeFocus)
            visible: !panel.isRunning && panel.allCompleted && panel.scheduleKind === "custom"
            Layout.fillWidth: true
            text: qsTr("Reset all results")
            iconName: "reset"
            iconSize: 12
            variant: "ghost"
            implicitHeight: 34
            onClicked: panel.resetAllRequested()
        }

        // ── Pacing projection / guidance section ──────────────────────────────
        Rectangle {
            visible: panel.pacingEnabled && (panel.pacingReady || panel.pacingAvailability === "missing_window" || panel.pacingAvailability === "awaiting_reset")
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: panel.pal_border
            opacity: 0.75
        }

        ColumnLayout {
            objectName: "timerPacingSection"
            visible: panel.pacingEnabled && (panel.pacingReady || panel.pacingAvailability === "missing_window" || panel.pacingAvailability === "awaiting_reset")
            Layout.fillWidth: true
            spacing: 5

            // Ready projection (matches mockup: "Atraso previsto de 2 min" + "Fim previsto 20:32 · Meta 20:30")
            Text {
                objectName: "timerPacingHeadline"
                visible: panel.pacingReady
                Layout.fillWidth: true
                text: panel.pacingHeadline
                color: panel.pacingColor
                font.pixelSize: 13
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }

            Text {
                objectName: "timerPacingSubline"
                visible: panel.pacingReady
                Layout.fillWidth: true
                text: panel.pacingSubline
                color: panel.pal_textMuted
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }

            // Missing schedule window guidance + direct shortcut
            Text {
                objectName: "timerPacingMissingWindowHint"
                visible: panel.pacingAvailability === "missing_window"
                Layout.fillWidth: true
                text: panel.scheduleKind === "meeting"
                      ? qsTr("Set the meeting time in Profile settings to track schedule pacing.")
                      : qsTr("Set start and end times in the menu (⋯) to track pacing.")
                color: panel.pal_textMuted
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }

            TimerButton {
                id: wideTiming
                objectName: "timerConfigureMeetingTimeButton"
                onActiveFocusChanged: panel.rememberFocus("timing", activeFocus)
                visible: panel.pacingAvailability === "missing_window"
                         && ((panel.pacing && panel.pacing.canConfigureMeetingWindow === true)
                             || (panel.scheduleKind === "custom" && !panel.isRunning))
                text: panel.scheduleKind === "meeting"
                      ? qsTr("Configure meeting time")
                      : qsTr("Set schedule time")
                iconName: "clock"
                iconSize: 13
                variant: "soft"
                accent: panel.pal_accent
                implicitHeight: 30
                onClicked: {
                    if (panel.scheduleKind === "meeting")
                        timer.configureMeetingSchedule()
                    else
                        panel.customTimingRequested()
                }
            }

            // Awaiting reset notice
            Text {
                objectName: "timerPacingAwaitingResetHint"
                visible: panel.pacingAvailability === "awaiting_reset"
                Layout.fillWidth: true
                text: qsTr("The configured time window will take effect after resetting results.")
                color: panel.pal_warning
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
        }

        // ── Up Next block (matches mockup) ────────────────────────────────────
        Rectangle {
            visible: panel.nextPartId.length > 0 && (panel.isRunning || panel.nextPartId !== panel.targetPartId)
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: panel.pal_border
            opacity: 0.75
        }

        RowLayout {
            objectName: "timerUpNextSection"
            visible: panel.nextPartId.length > 0 && (panel.isRunning || panel.nextPartId !== panel.targetPartId)
            Layout.fillWidth: true
            spacing: 8

            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 2

                Text {
                    text: qsTr("Up next")
                    color: panel.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.Medium
                }
                Text {
                    objectName: "timerUpNextTitle"
                    Layout.fillWidth: true
                    text: panel.nextPartTitle
                    textFormat: Text.PlainText
                    color: panel.pal_textPrimary
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Text {
                    visible: panel.nextPartPlannedLabel.length > 0
                    text: panel.nextPartPlannedLabel
                    color: panel.pal_textMuted
                    font.pixelSize: 11
                }
            }

            TimerButton {
                id: wideNext
                objectName: "timerStartNextButton"
                onActiveFocusChanged: panel.rememberFocus("next", activeFocus)
                iconName: "skip_next"
                iconSize: 14
                tip: qsTr("Start next part")
                variant: "soft"
                accent: panel.pal_accent
                Layout.preferredWidth: 34
                implicitHeight: 34
                onClicked: timer.startNextPart()
            }
        }
        }

        ScrollBar.vertical: ScrollBar {
            id: wideScrollBar
            policy: ScrollBar.AsNeeded
            width: 8
            visible: wideFlick.contentHeight > wideFlick.height + 1
            contentItem: Rectangle {
                implicitWidth: 4
                radius: 2
                color: wideScrollBar.hovered ? panel.pal_textMuted : panel.pal_borderStrong
                opacity: (wideScrollBar.active || wideScrollBar.hovered) ? 1.0 : 0.40
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // COMPACT MODE — Fixed top strip above scrollable list (< 760 px)
    // ═══════════════════════════════════════════════════════════════════════════
    ColumnLayout {
        id: compactCol
        visible: panel.showingCompactControls
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 10
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 2

                Text {
                    Layout.fillWidth: true
                    text: panel.statusBadgeText
                    color: panel.statusBadgeColor
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Text {
                    Layout.fillWidth: true
                    text: panel.isRunning
                          ? (panel.live.active_part_title || "")
                          : (panel.targetPartTitle || qsTr("No parts in this schedule"))
                    textFormat: Text.PlainText
                    color: panel.pal_textPrimary
                    font.pixelSize: 13
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                }
            }

            TimerDigits {
                objectName: "timerCompactClockDigits"
                Layout.preferredWidth: panel.narrowWidth ? 88 : 104
                text: panel.displayDigits
                color: (panel.isOverrun || panel.targetOverTarget) ? panel.pal_danger : panel.pal_textPrimary
                font.pixelSize: panel.narrowWidth ? 20 : 24
                font.weight: Font.Bold
                fontSizeMode: Text.HorizontalFit
                minimumPixelSize: 11
                horizontalAlignment: Text.AlignRight
                Behavior on color { ColorAnimation { duration: 160 } }
            }

            TimerButton {
                id: compactPrimary
                objectName: "timerCompactPrimaryActionButton"
                onActiveFocusChanged: panel.rememberFocus("primary", activeFocus)
                text: panel.narrowWidth ? "" : panel.primaryText
                iconName: panel.primaryIcon
                iconSize: 12
                tip: panel.primaryText
                variant: "primary"
                accent: panel.isOverrun ? panel.pal_danger : panel.pal_accent
                enabled: panel.primaryEnabled
                Layout.preferredWidth: panel.narrowWidth ? 36 : -1
                implicitHeight: 34
                onClicked: panel.activatePrimary()
            }
        }

        // Slim progress bar
        Rectangle {
            visible: panel.isRunning || panel.targetIsCompleted
            Layout.fillWidth: true
            Layout.preferredHeight: 4
            radius: 2
            color: panel.pal_surfaceAlt
            clip: true

            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Math.max(panel.progressRatio > 0 ? 4 : 0, parent.width * panel.progressRatio)
                radius: 2
                color: (panel.isOverrun || panel.targetOverTarget)
                       ? panel.pal_danger
                       : (panel.targetIsCompleted ? panel.pal_success : panel.pal_accent)
                Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
            }
        }

        // Secondary compact row: pacing + Adjust/Next/Reset
        RowLayout {
            visible: panel.isRunning || panel.targetIsCompleted || panel.pacingReady
                     || (!panel.isRunning && !!panel.selectedPart && !!panel.selectedPart.id)
                     || (panel.pacingEnabled && (panel.pacingAvailability === "missing_window" || panel.pacingAvailability === "awaiting_reset"))
            Layout.fillWidth: true
            spacing: 6

            Text {
                objectName: "timerCompactPacingStatus"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: {
                    if (panel.pacingReady)
                        return panel.pacingHeadline + " · " + (panel.pacing.projectedEndLabel || "") + "/" + (panel.pacing.targetEndLabel || "")
                    if (panel.pacingAvailability === "missing_window")
                        return panel.scheduleKind === "meeting" ? qsTr("Meeting time not set") : qsTr("Schedule time not set")
                    if (panel.pacingAvailability === "awaiting_reset")
                        return qsTr("Reset results to apply time window")
                    if (panel.targetHasAdjusted)
                        return qsTr("Target %1 · Planned %2").arg(panel.targetEffectiveLabel).arg(panel.targetPlannedLabel)
                    if (panel.targetIsCompleted)
                        return qsTr("Planned %1").arg(panel.targetPlannedLabel)
                    return ""
                }
                color: panel.pacingReady
                       ? panel.pacingColor
                       : (panel.pacingAvailability === "awaiting_reset" ? panel.pal_warning : panel.pal_textMuted)
                font.pixelSize: 11
                font.weight: panel.pacingReady ? Font.DemiBold : Font.Normal
                elide: Text.ElideRight
            }

            TimerButton {
                id: compactTiming
                visible: panel.pacingAvailability === "missing_window"
                         && ((panel.pacing && panel.pacing.canConfigureMeetingWindow === true)
                             || (panel.scheduleKind === "custom" && !panel.isRunning))
                onActiveFocusChanged: panel.rememberFocus("timing", activeFocus)
                iconName: "clock"
                iconSize: 12
                text: panel.narrowWidth ? "" : qsTr("Set time")
                tip: panel.scheduleKind === "meeting" ? qsTr("Configure meeting time") : qsTr("Set schedule time")
                variant: "soft"
                accent: panel.pal_accent
                Layout.preferredWidth: panel.narrowWidth ? 30 : -1
                implicitHeight: 28
                onClicked: {
                    if (panel.scheduleKind === "meeting")
                        timer.configureMeetingSchedule()
                    else
                        panel.customTimingRequested()
                }
            }

            TimerButton {
                id: compactAdjust
                visible: panel.isRunning
                iconName: "sliders"
                onActiveFocusChanged: panel.rememberFocus("adjust", activeFocus)
                iconSize: 12
                text: panel.narrowWidth ? "" : qsTr("Adjust")
                tip: qsTr("Adjust time")
                variant: panel.adjustOpen ? "soft" : "ghost"
                accent: panel.pal_accent
                Layout.preferredWidth: panel.narrowWidth ? 30 : -1
                implicitHeight: 28
                onClicked: panel.adjustOpen = !panel.adjustOpen
            }

            TimerButton {
                id: compactNext
                visible: panel.nextPartId.length > 0 && (panel.isRunning || panel.nextPartId !== panel.targetPartId)
                onActiveFocusChanged: panel.rememberFocus("next", activeFocus)
                iconName: "skip_next"
                iconSize: 12
                text: panel.narrowWidth ? "" : qsTr("Next")
                tip: qsTr("Start next part")
                variant: "soft"
                accent: panel.pal_accent
                Layout.preferredWidth: panel.narrowWidth ? 30 : -1
                implicitHeight: 28
                onClicked: timer.startNextPart()
            }

            TimerButton {
                id: compactEdit
                visible: !panel.isRunning && !!panel.selectedPart && !!panel.selectedPart.id
                onActiveFocusChanged: panel.rememberFocus("edit", activeFocus)
                iconName: "edit"
                iconSize: 12
                tip: qsTr("Edit part…")
                variant: "ghost"
                Layout.preferredWidth: 30
                implicitHeight: 28
                onClicked: panel.editSelectedPartRequested(panel.selectedPart)
            }

            TimerButton {
                id: compactReset
                visible: !panel.isRunning && panel.targetIsCompleted
                iconName: "reset"
                onActiveFocusChanged: panel.rememberFocus("reset", activeFocus)
                iconSize: 12
                text: panel.narrowWidth ? "" : qsTr("Reset")
                tip: qsTr("Reset this part")
                variant: "ghost"
                Layout.preferredWidth: panel.narrowWidth ? 30 : -1
                implicitHeight: 28
                onClicked: timer.resetPart(panel.targetPartId)
            }

            TimerButton {
                id: compactResetAll
                objectName: "timerCompactResetAllCompletedButton"
                visible: !panel.isRunning && panel.allCompleted && panel.scheduleKind === "custom"
                iconName: "reset"
                iconSize: 12
                tip: qsTr("Reset all results")
                variant: "ghost"
                Layout.preferredWidth: 30
                implicitHeight: 28
                onActiveFocusChanged: panel.rememberFocus("resetAll", activeFocus)
                onClicked: panel.resetAllRequested()
            }
        }

        // Compact adjust chips
        RowLayout {
            visible: panel.isRunning && panel.adjustOpen
            Layout.fillWidth: true
            spacing: 4

            Repeater {
                id: compactAdjustments
                model: [
                    { label: "−1 min", delta: -60 },
                    { label: "−1 s", delta: -1 },
                    { label: "+1 s", delta: 1 },
                    { label: "+1 min", delta: 60 }
                ]
                delegate: TimerButton {
                    required property var modelData
                    onActiveFocusChanged: panel.rememberFocus("delta:" + modelData.delta, activeFocus)
                    Layout.fillWidth: true
                    text: modelData.label
                    variant: "ghost"
                    implicitHeight: 28
                    onClicked: timer.adjustPart(panel.activePartId, modelData.delta)
                }
            }
        }
    }
}
