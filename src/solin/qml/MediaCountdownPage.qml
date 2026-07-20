// MediaCountdownPage.qml — contextual manual and meeting-aware countdown setup.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: page
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

    property var automation: timer.mediaCountdownAutomation
    property string nowText: "--:--:--"
    property int hourValue: 0
    property int minValue: 0
    property bool targetDirty: false

    readonly property int leadSeconds: automation && automation.leadSeconds
                                       ? automation.leadSeconds : 600
    readonly property int leadHours: Math.floor(leadSeconds / 3600)
    readonly property int leadMinutes: Math.floor((leadSeconds % 3600) / 60)
    readonly property int leadRemainderSeconds: leadSeconds % 60

    function pad(n) { return ("0" + n).slice(-2) }

    function applySuggestedTarget() {
        if (!automation || targetDirty)
            return
        hourValue = automation.suggestedTargetHour
        minValue = automation.suggestedTargetMinute
    }

    function enterPage() {
        targetDirty = false
        timer.refreshMediaCountdownPage()
        Qt.callLater(applySuggestedTarget)
    }

    function updateLead(seconds) {
        timer.setAutomaticCountdownLeadSeconds(Math.max(10, Math.min(7200, seconds)))
    }

    function statusTitle() {
        if (!automation)
            return ""
        switch (automation.status) {
        case "disabled": return qsTr("Automation off")
        case "schedule_required": return qsTr("Configuration required")
        case "ready": return qsTr("Ready")
        case "waiting_for_projection": return qsTr("Waiting for the media window")
        case "active": return qsTr("Countdown active")
        case "suppressed": return qsTr("Interrupted for this meeting")
        case "missed": return qsTr("Not started")
        default: return ""
        }
    }

    function statusDescription() {
        if (!automation)
            return ""
        if (automation.status === "disabled")
            return qsTr("Turn it on to show a countdown before configured meetings.")
        if (automation.status === "schedule_required")
            return qsTr("No meeting time is configured. The countdown will remain ready until a schedule is added.")
        if (automation.status === "waiting_for_projection") {
            if (automation.blockingReason === "no_window")
                return qsTr("No media window is available. Solin will keep trying only until the meeting starts.")
            if (automation.blockingReason === "in_use")
                return qsTr("The media window is in use. Solin will keep trying only until the meeting starts.")
            if (automation.blockingReason === "automation_unavailable")
                return qsTr("Automatic projection is temporarily unavailable. Solin will keep trying only until the meeting starts.")
            return qsTr("Waiting for the media window")
        }
        if (automation.status === "active" && automation.next && automation.next.startTime) {
            if (automation.activeAutomatic)
                return qsTr("Started automatically · reaches zero at %1").arg(automation.next.startTime)
            return qsTr("Reach zero at") + " " + automation.next.startTime
        }
        if (automation.status === "suppressed")
            return qsTr("It will not start again automatically for this occurrence.")
        if (automation.status === "missed")
            return qsTr("The meeting time was reached before the countdown could start.")
        if (automation.next && automation.next.startTime) {
            return qsTr("Next: %1, %2 · automatic start at %3")
                .arg(automation.next.weekdayLabel)
                .arg(automation.next.startTime)
                .arg(automation.next.triggerTime)
        }
        return qsTr("Waiting for the next configured meeting.")
    }

    function statusColor() {
        if (!automation)
            return pal_textMuted
        if (automation.status === "active" || automation.status === "ready")
            return pal_accent
        if (automation.status === "schedule_required" ||
                automation.status === "waiting_for_projection" ||
                automation.status === "missed")
            return appTheme.warning
        return pal_textMuted
    }

    onVisibleChanged: if (visible) enterPage()

    Connections {
        target: timer
        function onMediaCountdownAutomationChanged() {
            if (page.visible && !page.targetDirty)
                Qt.callLater(page.applySuggestedTarget)
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: page.nowText = Qt.formatTime(new Date(), "HH:mm:ss")
    }

    Flickable {
        id: scroll
        anchors.fill: parent
        anchors.topMargin: 8
        anchors.bottomMargin: 4
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        maximumFlickVelocity: 5200
        flickDeceleration: 2800
        contentWidth: width
        contentHeight: contentCol.implicitHeight + 28

        property real wheelTargetY: contentY

        function clampContentY(value) {
            return Math.max(0, Math.min(Math.max(0, contentHeight - height), value))
        }

        function smoothWheelScroll(delta) {
            if (!wheelAnim.running)
                wheelTargetY = contentY
            wheelTargetY = clampContentY(wheelTargetY + delta)
            wheelAnim.stop()
            wheelAnim.to = wheelTargetY
            wheelAnim.start()
        }

        NumberAnimation {
            id: wheelAnim
            target: scroll
            property: "contentY"
            duration: 200
            easing.type: Easing.OutCubic
        }

        WheelHandler {
            target: scroll
            acceptedDevices: PointerDevice.Mouse
            onWheel: function(event) {
                var raw = event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y
                scroll.smoothWheelScroll(event.pixelDelta.y !== 0 ? -raw : -raw * 0.35)
                event.accepted = true
            }
        }

        GridLayout {
            id: contentCol
            width: Math.max(0, Math.min(560, scroll.width - 48))
            x: Math.round((scroll.width - width) / 2)
            columns: 1
            columnSpacing: 0
            rowSpacing: 16

            Rectangle {
                objectName: "automaticCountdownCard"
                Layout.row: 1
                Layout.fillWidth: true
                implicitHeight: automationCol.implicitHeight + 40
                radius: 14
                color: page.pal_surface
                border.color: page.pal_border
                border.width: 1

                ColumnLayout {
                    id: automationCol
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 16

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            spacing: 3
                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: qsTr("Automatic countdown")
                                color: page.pal_textPrimary
                                font.pixelSize: 16
                                font.weight: Font.Bold
                            }
                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: qsTr("Shows a countdown in the media window before configured meetings.")
                                color: page.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }

                        TimerToggle {
                            checked: page.automation ? page.automation.enabled : false
                            accent: page.pal_accent
                            onToggled: function(value) { timer.setAutomaticCountdownEnabled(value) }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: statusCol.implicitHeight + 22
                        radius: 10
                        color: Qt.rgba(page.statusColor().r, page.statusColor().g,
                                       page.statusColor().b, 0.08)
                        border.width: 1
                        border.color: Qt.rgba(page.statusColor().r, page.statusColor().g,
                                              page.statusColor().b, 0.28)

                        ColumnLayout {
                            id: statusCol
                            anchors.fill: parent
                            anchors.margins: 11
                            spacing: 4
                            RowLayout {
                                spacing: 7
                                Rectangle {
                                    width: 7; height: 7; radius: 4
                                    color: page.statusColor()
                                }
                                Text {
                                    text: page.statusTitle()
                                    color: page.pal_textPrimary
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                            }
                            Text {
                                objectName: "countdownStatusDescription"
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: page.statusDescription()
                                color: page.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: qsTr("How long before the meeting should the countdown start?")
                                color: page.pal_textSecondary
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                wrapMode: Text.WordWrap
                            }
                            Text {
                                text: qsTr("10 seconds – 2 hours")
                                color: page.pal_textMuted
                                font.pixelSize: 10
                            }
                        }

                        RowLayout {
                            Layout.alignment: Qt.AlignHCenter
                            spacing: 6

                            ColumnLayout {
                                spacing: 3
                                TimerStepper {
                                    text: page.pad(page.leadHours)
                                    accent: page.pal_accent
                                    fieldWidth: 34
                                    editable: true
                                    decTip: qsTr("−1 h"); incTip: qsTr("+1 h")
                                    onDecremented: page.updateLead(page.leadSeconds - 3600)
                                    onIncremented: page.updateLead(page.leadSeconds + 3600)
                                    onEdited: function(v) {
                                        var n = parseInt(v, 10)
                                        if (!isNaN(n)) page.updateLead(n * 3600 + page.leadMinutes * 60 + page.leadRemainderSeconds)
                                    }
                                }
                                Text { Layout.alignment: Qt.AlignHCenter; text: qsTr("h"); color: page.pal_textMuted; font.pixelSize: 10 }
                            }
                            Text { text: ":"; color: page.pal_textSecondary; font.pixelSize: 18; Layout.alignment: Qt.AlignTop; Layout.topMargin: 4 }
                            ColumnLayout {
                                spacing: 3
                                TimerStepper {
                                    text: page.pad(page.leadMinutes)
                                    accent: page.pal_accent
                                    fieldWidth: 34
                                    editable: true
                                    decTip: qsTr("−1 min"); incTip: qsTr("+1 min")
                                    onDecremented: page.updateLead(page.leadSeconds - 60)
                                    onIncremented: page.updateLead(page.leadSeconds + 60)
                                    onEdited: function(v) {
                                        var n = parseInt(v, 10)
                                        if (!isNaN(n)) page.updateLead(page.leadHours * 3600 + Math.max(0, Math.min(59, n)) * 60 + page.leadRemainderSeconds)
                                    }
                                }
                                Text { Layout.alignment: Qt.AlignHCenter; text: qsTr("min"); color: page.pal_textMuted; font.pixelSize: 10 }
                            }
                            Text { text: ":"; color: page.pal_textSecondary; font.pixelSize: 18; Layout.alignment: Qt.AlignTop; Layout.topMargin: 4 }
                            ColumnLayout {
                                spacing: 3
                                TimerStepper {
                                    text: page.pad(page.leadRemainderSeconds)
                                    accent: page.pal_accent
                                    fieldWidth: 34
                                    editable: true
                                    decTip: qsTr("−10 s"); incTip: qsTr("+10 s")
                                    onDecremented: page.updateLead(page.leadSeconds - 10)
                                    onIncremented: page.updateLead(page.leadSeconds + 10)
                                    onEdited: function(v) {
                                        var n = parseInt(v, 10)
                                        if (!isNaN(n)) page.updateLead(page.leadHours * 3600 + page.leadMinutes * 60 + Math.max(0, Math.min(59, n)))
                                    }
                                }
                                Text { Layout.alignment: Qt.AlignHCenter; text: qsTr("s"); color: page.pal_textMuted; font.pixelSize: 10 }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Text {
                            text: qsTr("Default appearance")
                            color: page.pal_textSecondary
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        TimerSegment {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 36
                            options: [qsTr("Circular timer"), qsTr("Annual text")]
                            current: page.automation ? page.automation.presentationIndex : 0
                            accent: page.pal_accent
                            stretch: true
                            segHeight: 36
                            onPicked: function(index) { timer.setMediaCountdownPresentation(index) }
                        }
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Used for automatic and manual countdowns.")
                            color: page.pal_textMuted
                            font.pixelSize: 10
                            wrapMode: Text.WordWrap
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: qsTr("Meeting times")
                                color: page.pal_textSecondary
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                            }
                            TimerButton {
                                text: page.automation && page.automation.configuredCount === 0
                                      ? qsTr("Configure meetings")
                                      : page.automation && page.automation.configuredCount === 1
                                        ? qsTr("Complete times") : qsTr("Edit times")
                                variant: "ghost"
                                accent: page.pal_accent
                                onClicked: timer.configureMeetingSchedule()
                            }
                        }

                        Repeater {
                            model: page.automation ? page.automation.slots : []
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 8
                                Text {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    text: modelData.label
                                    color: page.pal_textMuted
                                    font.pixelSize: 11
                                }
                                Text {
                                    text: modelData.configured
                                          ? modelData.weekdayLabel + ", " + modelData.timeText
                                          : qsTr("Not configured")
                                    color: modelData.configured ? page.pal_textSecondary : page.pal_textMuted
                                    font.pixelSize: 11
                                    font.weight: modelData.configured ? Font.DemiBold : Font.Normal
                                }
                            }
                        }
                    }
                }
            }

            Rectangle {
                objectName: "manualCountdownCard"
                Layout.row: 0
                Layout.fillWidth: true
                implicitHeight: manualCol.implicitHeight + 40
                radius: 14
                color: page.pal_surface
                border.color: page.pal_border
                border.width: 1

                ColumnLayout {
                    id: manualCol
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 14

                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            spacing: 3
                            Text {
                                text: qsTr("Start manually")
                                color: page.pal_textPrimary
                                font.pixelSize: 16
                                font.weight: Font.Bold
                            }
                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                text: qsTr("Start or restart a countdown at any time.")
                                color: page.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                        RowLayout {
                            spacing: 7
                            Text {
                                text: qsTr("Now")
                                color: page.pal_textMuted
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                font.capitalization: Font.AllUppercase
                            }
                            Text {
                                text: page.nowText
                                color: page.pal_textSecondary
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: qsTr("Reach zero at")
                            color: page.pal_textSecondary
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Rectangle {
                            visible: page.automation && page.automation.suggestedFromMeeting && !page.targetDirty
                            implicitWidth: selectedMeetingText.implicitWidth + 16
                            implicitHeight: 24
                            radius: 7
                            color: Qt.rgba(page.pal_accent.r, page.pal_accent.g, page.pal_accent.b, 0.10)
                            border.color: Qt.rgba(page.pal_accent.r, page.pal_accent.g, page.pal_accent.b, 0.28)
                            Text {
                                id: selectedMeetingText
                                anchors.centerIn: parent
                                text: qsTr("Meeting time selected")
                                color: page.pal_accent
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }
                        }
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 8
                        TimerStepper {
                            text: page.pad(page.hourValue)
                            accent: page.pal_accent
                            fieldWidth: 50
                            editable: true
                            decTip: qsTr("−1 h"); incTip: qsTr("+1 h")
                            onDecremented: { page.targetDirty = true; page.hourValue = (page.hourValue + 23) % 24 }
                            onIncremented: { page.targetDirty = true; page.hourValue = (page.hourValue + 1) % 24 }
                            onEdited: function(v) {
                                var n = parseInt(v, 10)
                                if (!isNaN(n)) { page.targetDirty = true; page.hourValue = ((n % 24) + 24) % 24 }
                            }
                        }
                        Text { text: ":"; color: page.pal_textPrimary; font.pixelSize: 22; font.weight: Font.Bold }
                        TimerStepper {
                            text: page.pad(page.minValue)
                            accent: page.pal_accent
                            fieldWidth: 50
                            editable: true
                            decTip: qsTr("−1 min"); incTip: qsTr("+1 min")
                            onDecremented: { page.targetDirty = true; page.minValue = (page.minValue + 59) % 60 }
                            onIncremented: { page.targetDirty = true; page.minValue = (page.minValue + 1) % 60 }
                            onEdited: function(v) {
                                var n = parseInt(v, 10)
                                if (!isNaN(n)) { page.targetDirty = true; page.minValue = ((n % 60) + 60) % 60 }
                            }
                        }
                    }

                    TimerButton {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 44
                        text: qsTr("Start countdown to this time")
                        variant: "primary"
                        accent: page.pal_accent
                        iconName: "play"
                        iconSize: 13
                        onClicked: timer.startCountdownToTime(page.hourValue, page.minValue)
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTr("Or count down for")
                        color: page.pal_textMuted
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.2
                        font.capitalization: Font.AllUppercase
                    }

                    Grid {
                        id: presetGrid
                        Layout.fillWidth: true
                        Layout.preferredHeight: implicitHeight
                        columns: width < 390 ? 3 : 5
                        spacing: 8
                        Repeater {
                            model: [5, 10, 15, 20, 30]
                            delegate: Rectangle {
                                required property int modelData
                                width: Math.max(0, (presetGrid.width
                                                   - presetGrid.spacing * (presetGrid.columns - 1))
                                                  / presetGrid.columns)
                                height: 40
                                radius: 8
                                color: chipMa.containsMouse
                                       ? Qt.rgba(page.pal_accent.r, page.pal_accent.g, page.pal_accent.b, 0.14)
                                       : page.pal_surfaceAlt
                                border.width: 1
                                border.color: chipMa.containsMouse ? page.pal_accent : page.pal_border
                                scale: chipMa.pressed ? 0.95 : 1.0
                                Behavior on color { ColorAnimation { duration: 130 } }
                                Behavior on border.color { ColorAnimation { duration: 130 } }
                                Behavior on scale { NumberAnimation { duration: 90 } }
                                ColumnLayout {
                                    anchors.centerIn: parent
                                    spacing: -1
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: "" + modelData
                                        color: chipMa.containsMouse ? appTheme.accentText : page.pal_textPrimary
                                        font.pixelSize: 17
                                        font.weight: Font.Bold
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: qsTr("min")
                                        color: page.pal_textMuted
                                        font.pixelSize: 10
                                    }
                                }
                                TimerPointerArea {
                                    id: chipMa
                                    anchors.fill: parent
                                    onClicked: timer.startCountdownMinutes(modelData)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
