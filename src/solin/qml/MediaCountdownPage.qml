// MediaCountdownPage.qml — clear manual operation with meeting-aware automation.
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

    property var countdown: timer.mediaCountdown
    readonly property var manualSuggestion: countdown && countdown.manualSuggestion
                                                     ? countdown.manualSuggestion : ({})
    readonly property var automation: countdown && countdown.automation
                                                ? countdown.automation : ({})
    readonly property var schedule: countdown && countdown.schedule
                                              ? countdown.schedule : ({})
    readonly property var nextMeeting: schedule && schedule.next ? schedule.next : ({})

    readonly property int presentationIndex: countdown && countdown.presentationIndex !== undefined
                                             ? countdown.presentationIndex : 0
    readonly property int leadSeconds: automation && automation.leadSeconds !== undefined
                                       ? automation.leadSeconds : 600
    readonly property int configuredCount: schedule && schedule.configuredCount !== undefined
                                           ? schedule.configuredCount : 0
    readonly property int gutter: width < 480 ? 16 : width < 720 ? 22 : 32
    readonly property bool narrowContent: contentColumn.width < 540

    property var nowValue: new Date()
    property int hourValue: 0
    property int minuteValue: 0
    property bool targetDirty: false
    property int manualMode: 0
    property int selectedDurationSeconds: 600
    property bool customDurationVisible: false
    property bool leadCustomVisible: false
    property bool automationExpanded: false

    function pad(value) {
        return ("0" + value).slice(-2)
    }

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value))
    }

    function applySuggestedTarget() {
        if (!manualSuggestion || targetDirty)
            return
        hourValue = manualSuggestion.hour !== undefined ? manualSuggestion.hour : 0
        minuteValue = manualSuggestion.minute !== undefined ? manualSuggestion.minute : 0
    }

    function isLeadPreset(seconds) {
        return seconds === 300 || seconds === 600 || seconds === 900 || seconds === 1800
    }

    function enterPage() {
        targetDirty = false
        manualMode = 0
        customDurationVisible = false
        automationExpanded = false
        timer.refreshMediaCountdownPage()
        Qt.callLater(function() {
            page.applySuggestedTarget()
            page.leadCustomVisible = !page.isLeadPreset(page.leadSeconds)
            page.automationExpanded = !!page.automation.enabled && page.configuredCount === 0
        })
    }

    function updateLead(seconds) {
        timer.setAutomaticCountdownLeadSeconds(clamp(seconds, 10, 7200))
    }

    function selectedTargetIsTomorrow() {
        var selectedMinutes = hourValue * 60 + minuteValue
        var nowMinutes = nowValue.getHours() * 60 + nowValue.getMinutes()
        return selectedMinutes <= nowMinutes
    }

    function selectedTargetText() {
        var time = pad(hourValue) + ":" + pad(minuteValue)
        return selectedTargetIsTomorrow()
               ? qsTr("Tomorrow at %1").arg(time)
               : qsTr("Today at %1").arg(time)
    }

    function durationText(seconds) {
        var hours = Math.floor(seconds / 3600)
        var minutes = Math.floor((seconds % 3600) / 60)
        var remainder = seconds % 60
        return pad(hours) + ":" + pad(minutes) + ":" + pad(remainder)
    }

    function selectManualDuration(seconds) {
        selectedDurationSeconds = clamp(seconds, 10, 86399)
        customDurationVisible = false
    }

    function automationSummary() {
        if (!automation || !automation.enabled)
            return qsTr("Turn it on to show a countdown before configured meetings.")
        if (automation.status === "schedule_required" || configuredCount === 0)
            return qsTr("Add meeting days and times to finish setting up the automation.")
        if (automation.status === "waiting_for_projection") {
            if (automation.blockingReason === "no_window")
                return qsTr("No media window is available. Solin will keep trying until the meeting starts.")
            if (automation.blockingReason === "in_use")
                return qsTr("The media window is in use. Solin will resume the countdown when it becomes available.")
            return qsTr("Automatic projection is temporarily unavailable. Solin will keep trying until the meeting starts.")
        }
        if (automation.status === "active" && automation.activeAutomatic && nextMeeting.startTime)
            return qsTr("Started automatically · reaches zero at %1").arg(nextMeeting.startTime)
        if (automation.status === "suppressed")
            return qsTr("It will not start again automatically for this occurrence.")
        if (automation.status === "missed")
            return qsTr("The meeting time was reached before the countdown could start.")
        if (nextMeeting.startTime)
            return qsTr("Next: %1 at %2 · starts automatically at %3")
                .arg(nextMeeting.weekdayLabel)
                .arg(nextMeeting.startTime)
                .arg(nextMeeting.triggerTime)
        return qsTr("Waiting for the next configured meeting.")
    }

    onVisibleChanged: if (visible) enterPage()

    Connections {
        target: timer
        function onMediaCountdownChanged() {
            if (page.visible && !page.targetDirty)
                Qt.callLater(page.applySuggestedTarget)
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            page.nowValue = new Date()
        }
    }

    component SurfaceCard: Rectangle {
        id: card
        default property alias contents: body.data
        property alias contentSpacing: body.spacing
        property int contentMargins: page.narrowContent ? 18 : 24

        implicitHeight: body.implicitHeight + contentMargins * 2
        radius: 18
        color: page.pal_surface
        border.width: 1
        border.color: page.pal_border

        ColumnLayout {
            id: body
            anchors.fill: parent
            anchors.margins: card.contentMargins
            spacing: 18
        }
    }

    component PillTabs: Rectangle {
        id: tabs
        property var options: []
        property int current: 0
        signal picked(int index)

        implicitHeight: 44
        radius: height / 2
        color: page.pal_surfaceAlt
        border.width: 1
        border.color: page.pal_border

        Rectangle {
            x: 4 + tabs.current * width
            y: 4
            width: (tabs.width - 8) / Math.max(1, tabs.options.length)
            height: tabs.height - 8
            radius: height / 2
            color: page.pal_accent
            Behavior on x { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
        }

        Row {
            anchors.fill: parent
            anchors.margins: 4
            Repeater {
                model: tabs.options
                delegate: Item {
                    id: tab
                    required property int index
                    required property string modelData
                    width: (tabs.width - 8) / Math.max(1, tabs.options.length)
                    height: tabs.height - 8
                    activeFocusOnTab: true

                    Accessible.role: Accessible.RadioButton
                    Accessible.name: modelData
                    Accessible.checked: index === tabs.current

                    Keys.onPressed: function(event) {
                        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                            tabs.picked(index)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Left && index > 0) {
                            tabs.picked(index - 1)
                            event.accepted = true
                        } else if (event.key === Qt.Key_Right && index + 1 < tabs.options.length) {
                            tabs.picked(index + 1)
                            event.accepted = true
                        }
                    }

                    Rectangle {
                        anchors.fill: parent
                        radius: height / 2
                        color: "transparent"
                        border.width: tab.activeFocus ? 2 : 0
                        border.color: appTheme.white
                    }

                    Text {
                        anchors.centerIn: parent
                        text: tab.modelData
                        color: tab.index === tabs.current ? appTheme.white : page.pal_textMuted
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }

                    TimerPointerArea {
                        anchors.fill: parent
                        onClicked: tabs.picked(tab.index)
                    }
                }
            }
        }
    }

    component ChoiceChip: Rectangle {
        id: chip
        property string text: ""
        property bool selected: false
        signal clicked()

        implicitWidth: chipLabel.implicitWidth + 30
        implicitHeight: 44
        radius: height / 2
        color: selected
               ? Qt.rgba(page.pal_accent.r, page.pal_accent.g, page.pal_accent.b, 0.14)
               : (chipMouse.containsMouse ? page.pal_hover : page.pal_surfaceAlt)
        border.width: activeFocus ? 2 : 1
        border.color: selected || activeFocus ? page.pal_accent
                                             : chipMouse.containsMouse
                                               ? page.pal_borderStrong : page.pal_border
        activeFocusOnTab: true
        scale: chipMouse.pressed ? 0.96 : 1

        Accessible.role: Accessible.RadioButton
        Accessible.name: text
        Accessible.checked: selected

        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
        Behavior on scale { NumberAnimation { duration: 80 } }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                chip.clicked()
                event.accepted = true
            }
        }

        Text {
            id: chipLabel
            anchors.centerIn: parent
            text: chip.text
            color: chip.selected ? page.pal_accent : page.pal_textSecondary
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }

        TimerPointerArea {
            id: chipMouse
            anchors.fill: parent
            onClicked: chip.clicked()
        }
    }

    component DurationEditor: Item {
        id: editor
        property int value: 600
        property int minimumValue: 10
        property int maximumValue: 86399
        signal valueRequested(int seconds)

        readonly property int hours: Math.floor(value / 3600)
        readonly property int minutes: Math.floor((value % 3600) / 60)
        readonly property int seconds: value % 60

        implicitHeight: durationRow.implicitHeight

        function requestValue(nextValue) {
            valueRequested(page.clamp(nextValue, minimumValue, maximumValue))
        }

        RowLayout {
            id: durationRow
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: page.narrowContent ? 4 : 8

            ColumnLayout {
                spacing: 5
                TimerStepper {
                    text: page.pad(editor.hours)
                    accent: page.pal_accent
                    fieldWidth: page.narrowContent ? 34 : 44
                    editable: true
                    decTip: qsTr("−1 h")
                    incTip: qsTr("+1 h")
                    onDecremented: editor.requestValue(editor.value - 3600)
                    onIncremented: editor.requestValue(editor.value + 3600)
                    onEdited: function(text) {
                        var parsed = parseInt(text, 10)
                        if (!isNaN(parsed))
                            editor.requestValue(parsed * 3600 + editor.minutes * 60 + editor.seconds)
                    }
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("hours")
                    color: page.pal_textMuted
                    font.pixelSize: 11
                }
            }

            Text {
                text: ":"
                color: page.pal_textSecondary
                font.pixelSize: 22
                font.weight: Font.DemiBold
                Layout.alignment: Qt.AlignTop
                Layout.topMargin: 4
            }

            ColumnLayout {
                spacing: 5
                TimerStepper {
                    text: page.pad(editor.minutes)
                    accent: page.pal_accent
                    fieldWidth: page.narrowContent ? 34 : 44
                    editable: true
                    decTip: qsTr("−1 min")
                    incTip: qsTr("+1 min")
                    onDecremented: editor.requestValue(editor.value - 60)
                    onIncremented: editor.requestValue(editor.value + 60)
                    onEdited: function(text) {
                        var parsed = parseInt(text, 10)
                        if (!isNaN(parsed))
                            editor.requestValue(editor.hours * 3600 + page.clamp(parsed, 0, 59) * 60 + editor.seconds)
                    }
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("minutes")
                    color: page.pal_textMuted
                    font.pixelSize: 11
                }
            }

            Text {
                text: ":"
                color: page.pal_textSecondary
                font.pixelSize: 22
                font.weight: Font.DemiBold
                Layout.alignment: Qt.AlignTop
                Layout.topMargin: 4
            }

            ColumnLayout {
                spacing: 5
                TimerStepper {
                    text: page.pad(editor.seconds)
                    accent: page.pal_accent
                    fieldWidth: page.narrowContent ? 34 : 44
                    editable: true
                    decTip: qsTr("−10 s")
                    incTip: qsTr("+10 s")
                    onDecremented: editor.requestValue(editor.value - 10)
                    onIncremented: editor.requestValue(editor.value + 10)
                    onEdited: function(text) {
                        var parsed = parseInt(text, 10)
                        if (!isNaN(parsed))
                            editor.requestValue(editor.hours * 3600 + editor.minutes * 60 + page.clamp(parsed, 0, 59))
                    }
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("seconds")
                    color: page.pal_textMuted
                    font.pixelSize: 11
                }
            }
        }
    }

    Flickable {
        id: scroll
        anchors.fill: parent
        anchors.topMargin: 10
        anchors.bottomMargin: 6
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        maximumFlickVelocity: 5200
        flickDeceleration: 2800
        contentWidth: width
        contentHeight: contentColumn.implicitHeight + 34

        property real wheelTargetY: contentY

        function clampContentY(value) {
            return Math.max(0, Math.min(Math.max(0, contentHeight - height), value))
        }

        function smoothWheelScroll(delta) {
            if (!wheelAnimation.running)
                wheelTargetY = contentY
            wheelTargetY = clampContentY(wheelTargetY + delta)
            wheelAnimation.stop()
            wheelAnimation.to = wheelTargetY
            wheelAnimation.start()
        }

        NumberAnimation {
            id: wheelAnimation
            target: scroll
            property: "contentY"
            duration: 190
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

        ScrollBar.vertical: ScrollBar {
            id: verticalBar
            policy: ScrollBar.AsNeeded
            contentItem: Rectangle {
                implicitWidth: 5
                radius: width / 2
                color: page.pal_borderStrong
                opacity: verticalBar.active ? 0.85 : 0.42
                Behavior on opacity { NumberAnimation { duration: 140 } }
            }
            background: Item {}
        }

        ColumnLayout {
            id: contentColumn
            width: Math.max(0, Math.min(800, scroll.width - page.gutter * 2))
            x: Math.round((scroll.width - width) / 2)
            spacing: 18

            Item {
                objectName: "countdownAppearanceRow"
                Layout.fillWidth: true
                implicitHeight: appearanceLayout.implicitHeight

                GridLayout {
                    id: appearanceLayout
                    anchors.left: parent.left
                    anchors.right: parent.right
                    columns: page.narrowContent ? 1 : 2
                    columnSpacing: 24
                    rowSpacing: 10

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 3
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Projection appearance")
                            color: page.pal_textPrimary
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Used for future manual and automatic countdowns. An active projection is not changed.")
                            color: page.pal_textMuted
                            font.pixelSize: 10
                            maximumLineCount: page.narrowContent ? 2 : 1
                            elide: Text.ElideRight
                            wrapMode: page.narrowContent ? Text.WordWrap : Text.NoWrap
                        }
                    }

                    TimerSegment {
                        objectName: "presentationSelector"
                        Layout.fillWidth: page.narrowContent
                        Layout.preferredWidth: page.narrowContent ? 0 : 284
                        Layout.alignment: page.narrowContent ? Qt.AlignLeft : Qt.AlignRight
                        options: [qsTr("Circular"), qsTr("Annual text")]
                        current: page.presentationIndex
                        accent: page.pal_accent
                        stretch: true
                        segHeight: 42
                        labelPixelSize: 12
                        onPicked: function(index) { timer.setMediaCountdownPresentation(index) }
                    }
                }
            }

            SurfaceCard {
                objectName: "manualCountdownCard"
                Layout.fillWidth: true
                contentSpacing: 14

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Start manually")
                        color: page.pal_textPrimary
                        font.pixelSize: 17
                        font.weight: Font.Bold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Choose a target, review it, then start the countdown.")
                        color: page.pal_textMuted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }
                }

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 44

                    PillTabs {
                        objectName: "manualModeTabs"
                        anchors.left: parent.left
                        width: page.narrowContent ? parent.width : Math.min(340, parent.width)
                        options: [qsTr("Until a time"), qsTr("For a duration")]
                        current: page.manualMode
                        onPicked: function(index) { page.manualMode = index }
                    }
                }

                ColumnLayout {
                    objectName: "manualTargetTimePanel"
                    Layout.fillWidth: true
                    visible: page.manualMode === 0
                    spacing: 14

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: targetLayout.implicitHeight + 28
                        radius: 14
                        color: page.pal_surfaceAlt
                        border.width: 1
                        border.color: page.pal_border

                        GridLayout {
                            id: targetLayout
                            anchors.fill: parent
                            anchors.margins: 14
                            columns: page.narrowContent ? 1 : 2
                            columnSpacing: 22
                            rowSpacing: 12

                            RowLayout {
                                Layout.alignment: page.narrowContent ? Qt.AlignHCenter : Qt.AlignLeft
                                spacing: 6
                                TimerStepper {
                                    text: page.pad(page.hourValue)
                                    accent: page.pal_accent
                                    fieldWidth: 48
                                    valueFontSize: 27
                                    editable: true
                                    decTip: qsTr("−1 h")
                                    incTip: qsTr("+1 h")
                                    onDecremented: {
                                        page.targetDirty = true
                                        page.hourValue = (page.hourValue + 23) % 24
                                    }
                                    onIncremented: {
                                        page.targetDirty = true
                                        page.hourValue = (page.hourValue + 1) % 24
                                    }
                                    onEdited: function(text) {
                                        var parsed = parseInt(text, 10)
                                        if (!isNaN(parsed)) {
                                            page.targetDirty = true
                                            page.hourValue = ((parsed % 24) + 24) % 24
                                        }
                                    }
                                }
                                Text {
                                    text: ":"
                                    color: page.pal_textPrimary
                                    font.pixelSize: 27
                                    font.weight: Font.Bold
                                }
                                TimerStepper {
                                    text: page.pad(page.minuteValue)
                                    accent: page.pal_accent
                                    fieldWidth: 48
                                    valueFontSize: 27
                                    editable: true
                                    decTip: qsTr("−1 min")
                                    incTip: qsTr("+1 min")
                                    onDecremented: {
                                        page.targetDirty = true
                                        page.minuteValue = (page.minuteValue + 59) % 60
                                    }
                                    onIncremented: {
                                        page.targetDirty = true
                                        page.minuteValue = (page.minuteValue + 1) % 60
                                    }
                                    onEdited: function(text) {
                                        var parsed = parseInt(text, 10)
                                        if (!isNaN(parsed)) {
                                            page.targetDirty = true
                                            page.minuteValue = ((parsed % 60) + 60) % 60
                                        }
                                    }
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                Layout.alignment: Qt.AlignVCenter
                                text: page.selectedTargetText()
                                color: page.pal_textPrimary
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                horizontalAlignment: page.narrowContent ? Text.AlignHCenter : Text.AlignLeft
                                elide: Text.ElideRight
                            }
                        }
                    }

                    TimerButton {
                        objectName: "startTargetTimeButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 46
                        text: qsTr("Start countdown")
                        variant: "primary"
                        accent: page.pal_accent
                        iconName: "play"
                        iconSize: 14
                        onClicked: timer.startCountdownToTime(page.hourValue, page.minuteValue)
                    }
                }

                ColumnLayout {
                    objectName: "manualDurationPanel"
                    Layout.fillWidth: true
                    visible: page.manualMode === 1
                    spacing: 16

                    Text {
                        Layout.fillWidth: true
                        text: qsTr("How long should the countdown run?")
                        color: page.pal_textSecondary
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }

                    Flow {
                        id: manualPresetFlow
                        Layout.fillWidth: true
                        Layout.preferredHeight: childrenRect.height
                        spacing: 8

                        Repeater {
                            model: [300, 600, 900, 1200, 1800]
                            delegate: ChoiceChip {
                                required property int modelData
                                objectName: "durationPreset" + modelData
                                text: qsTr("%1 min").arg(modelData / 60)
                                selected: !page.customDurationVisible && page.selectedDurationSeconds === modelData
                                onClicked: page.selectManualDuration(modelData)
                            }
                        }

                        ChoiceChip {
                            objectName: "customDurationChip"
                            text: qsTr("Custom")
                            selected: page.customDurationVisible
                            onClicked: page.customDurationVisible = true
                        }
                    }

                    DurationEditor {
                        objectName: "customDurationEditor"
                        Layout.fillWidth: true
                        visible: page.customDurationVisible
                        value: page.selectedDurationSeconds
                        minimumValue: 10
                        maximumValue: 86399
                        onValueRequested: function(seconds) {
                            page.selectedDurationSeconds = seconds
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Duration: %1").arg(page.durationText(page.selectedDurationSeconds))
                        color: page.pal_textMuted
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignRight
                    }

                    TimerButton {
                        objectName: "startDurationButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 46
                        text: qsTr("Start countdown")
                        variant: "primary"
                        accent: page.pal_accent
                        iconName: "play"
                        iconSize: 14
                        onClicked: timer.startCountdownDuration(page.selectedDurationSeconds)
                    }
                }
            }

            SurfaceCard {
                objectName: "automaticCountdownCard"
                Layout.fillWidth: true
                contentSpacing: 12
                contentMargins: page.narrowContent ? 16 : 20

                GridLayout {
                    Layout.fillWidth: true
                    columns: page.narrowContent ? 1 : 2
                    columnSpacing: 18
                    rowSpacing: 10

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 5

                        Text {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: qsTr("Before meetings")
                            color: page.pal_textPrimary
                            font.pixelSize: 15
                            font.weight: Font.Bold
                            elide: Text.ElideRight
                        }

                        Text {
                            objectName: "countdownStatusDescription"
                            Layout.fillWidth: true
                            text: page.automationSummary()
                            color: page.pal_textMuted
                            font.pixelSize: 11
                            wrapMode: Text.WordWrap
                            maximumLineCount: page.automationExpanded ? 3 : 2
                            elide: Text.ElideRight
                        }
                    }

                    RowLayout {
                        Layout.alignment: page.narrowContent ? Qt.AlignRight : Qt.AlignVCenter
                        spacing: 8

                        TimerToggle {
                            objectName: "automationToggle"
                            checked: !!page.automation.enabled
                            accent: page.pal_accent
                            accessibleName: qsTr("Automatic countdown before meetings")
                            onToggled: function(value) {
                                timer.setAutomaticCountdownEnabled(value)
                                if (value)
                                    page.automationExpanded = true
                            }
                        }

                        Rectangle {
                            id: automationExpander
                            objectName: "toggleAutomationSettingsButton"
                            signal clicked()
                            Layout.preferredWidth: 44
                            Layout.preferredHeight: 44
                            radius: height / 2
                            color: expanderMouse.containsMouse ? page.pal_hover : "transparent"
                            border.width: activeFocus ? 2 : 1
                            border.color: activeFocus ? page.pal_accent : page.pal_border
                            activeFocusOnTab: true

                            Accessible.role: Accessible.Button
                            Accessible.name: page.automationExpanded ? qsTr("Hide settings") : qsTr("Configure")

                            onClicked: page.automationExpanded = !page.automationExpanded
                            Keys.onPressed: function(event) {
                                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                    automationExpander.clicked()
                                    event.accepted = true
                                }
                            }

                            Image {
                                anchors.centerIn: parent
                                width: 16
                                height: 16
                                sourceSize: Qt.size(32, 32)
                                source: "image://timericons/chevron_down/32/" +
                                        String(page.pal_textSecondary).replace("#", "")
                                rotation: page.automationExpanded ? 180 : 0
                                Behavior on rotation {
                                    NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
                                }
                            }

                            TimerPointerArea {
                                id: expanderMouse
                                anchors.fill: parent
                                onClicked: automationExpander.clicked()
                            }
                        }
                    }
                }

                ColumnLayout {
                    objectName: "automationSettingsPanel"
                    Layout.fillWidth: true
                    visible: page.automationExpanded
                    enabled: page.automationExpanded
                    opacity: page.automationExpanded ? 1 : 0
                    spacing: 16
                    Behavior on opacity {
                        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: page.pal_border
                        opacity: 0.8
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        visible: page.configuredCount === 0
                        implicitHeight: emptyScheduleColumn.implicitHeight + 32
                        radius: 14
                        color: page.pal_surfaceAlt
                        border.width: 1
                        border.color: appTheme.warning

                        ColumnLayout {
                            id: emptyScheduleColumn
                            anchors.fill: parent
                            anchors.margins: 16
                            spacing: 10
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Meeting schedule required")
                                color: page.pal_textPrimary
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Define at least one meeting day and time so this automation can run.")
                                color: page.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                            TimerButton {
                                Layout.alignment: Qt.AlignLeft
                                Layout.preferredHeight: 42
                                text: qsTr("Configure meeting times")
                                variant: "soft"
                                accent: page.pal_accent
                                onClicked: timer.configureMeetingSchedule()
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: page.configuredCount > 0
                        spacing: 16

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 5
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("How long before the meeting should the countdown start?")
                                color: page.pal_textSecondary
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                wrapMode: Text.WordWrap
                            }
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Choose a common interval or set a precise time between 10 seconds and 2 hours.")
                                color: page.pal_textMuted
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }

                        Flow {
                            id: leadPresetFlow
                            Layout.fillWidth: true
                            Layout.preferredHeight: childrenRect.height
                            spacing: 8

                            Repeater {
                                model: [300, 600, 900, 1800]
                                delegate: ChoiceChip {
                                    required property int modelData
                                    objectName: "leadPreset" + modelData
                                    text: qsTr("%1 min").arg(modelData / 60)
                                    selected: !page.leadCustomVisible && page.leadSeconds === modelData
                                    onClicked: {
                                        page.leadCustomVisible = false
                                        page.updateLead(modelData)
                                    }
                                }
                            }

                            ChoiceChip {
                                text: qsTr("Custom")
                                selected: page.leadCustomVisible
                                onClicked: page.leadCustomVisible = true
                            }
                        }

                        DurationEditor {
                            Layout.fillWidth: true
                            visible: page.leadCustomVisible
                            value: page.leadSeconds
                            minimumValue: 10
                            maximumValue: 7200
                            onValueRequested: function(seconds) { page.updateLead(seconds) }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                color: page.pal_border
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Text {
                                    Layout.fillWidth: true
                                    text: qsTr("Next meeting")
                                    color: page.pal_textMuted
                                    font.pixelSize: 11
                                }
                                Text {
                                    text: page.nextMeeting.startTime
                                          ? page.nextMeeting.weekdayLabel + ", " + page.nextMeeting.startTime
                                          : qsTr("Not configured")
                                    color: page.pal_textPrimary
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Text {
                                    Layout.fillWidth: true
                                    text: qsTr("Countdown starts")
                                    color: page.pal_textMuted
                                    font.pixelSize: 11
                                }
                                Text {
                                    text: page.nextMeeting.triggerTime || "--:--:--"
                                    color: page.pal_accent
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                            }

                            TimerButton {
                                Layout.alignment: Qt.AlignRight
                                Layout.preferredHeight: 40
                                text: qsTr("Edit days and times")
                                variant: "ghost"
                                accent: page.pal_accent
                                onClicked: timer.configureMeetingSchedule()
                            }
                        }
                    }
                }
            }
        }
    }
}
