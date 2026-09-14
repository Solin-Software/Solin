pragma Translator: "AdvancedTimerPage"

// TimerSettingsDialog.qml — focused settings surface for the advanced timer.
// Changes are applied immediately through TimerBridge, matching the existing
// persistence contract while keeping the meeting-part workspace uncluttered.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Dialog {
    id: dialog

    property color pal_bg: appTheme.bg
    property color pal_surface: appTheme.surface
    property color pal_surfaceAlt: appTheme.surfaceAlt
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_accent: appTheme.accent
    property color pal_warning: appTheme.warning

    property var returnFocusItem: null
    property int pendingReserveIndex: -1

    readonly property bool compact: width < 520
    readonly property int outerMargin: compact ? 16 : 20
    readonly property int cardPadding: compact ? 14 : 18
    readonly property var cfg: timer.clockConfig
    readonly property bool clockHasAnalog: cfg.mode === "analog" || cfg.mode === "analog_digital"
    readonly property bool clockHasDigital: cfg.mode === "digital" || cfg.mode === "analog_digital"
    readonly property bool digitalClockOptionsAvailable: clockHasDigital
    readonly property bool ampmOptionAvailable: digitalClockOptionsAvailable && !cfg.hour_format_24h
    readonly property var clockModeRows: optionRows(timer.clockModes, ["digital", "analog", "analog_digital"])
    readonly property var clockModeOptions: optionLabels(clockModeRows, clockModeLabel)
    readonly property int clockModeCurrent: optionIndex(clockModeRows, cfg.mode, "digital")
    readonly property var analogStyleRows: optionRows(timer.analogClockStyles, ["signature", "classic"])
    readonly property var analogStyleOptions: optionLabels(analogStyleRows, analogStyleLabel)
    readonly property int analogStyleCurrent: optionIndex(analogStyleRows, cfg.analog_style, "signature")
    readonly property var partDisplayRows: optionRows(timer.partTimerDisplays, ["timer", "clock", "clock_timer"])
    readonly property var partDisplayOptions: optionLabels(partDisplayRows, partDisplayLabel)
    readonly property int partDisplayCurrent: optionIndex(partDisplayRows, cfg.part_timer_display, "timer")

    objectName: "timerSettingsDialog"
    parent: Overlay.overlay
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 2) : 0
    width: Math.max(0, Math.min(640, parent ? parent.width - 32 : 640))
    height: Math.max(0, Math.min(720, parent ? parent.height - 32 : 720))
    padding: 0
    modal: true
    dim: true
    focus: true
    closePolicy: Popup.CloseOnEscape

    function present(focusItem) {
        returnFocusItem = focusItem
        open()
    }

    function optionOpacity(available) {
        return available ? 1.0 : 0.68
    }

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function optionRows(rows, fallback) {
        return rows && rows.length > 0 ? rows : fallback
    }

    function optionId(row) {
        if (typeof row === "string")
            return row
        return row && row.id ? row.id : ""
    }

    function optionLabels(rows, labelFunc) {
        var labels = []
        for (var i = 0; i < rows.length; ++i)
            labels.push(labelFunc(optionId(rows[i])))
        return labels
    }

    function optionIndex(rows, value, fallbackId) {
        var id = value ? value : fallbackId
        for (var i = 0; i < rows.length; ++i) {
            if (optionId(rows[i]) === id)
                return i
        }
        return 0
    }

    function optionIdAt(rows, index, fallbackId) {
        return index >= 0 && index < rows.length ? optionId(rows[index]) : fallbackId
    }

    function clockModeLabel(id) {
        if (id === "digital") return qsTr("Digital")
        if (id === "analog") return qsTr("Analog")
        if (id === "analog_digital") return qsTr("Analog + digital")
        return id
    }

    function analogStyleLabel(id) {
        //: "Signature" is the proper name of this clock face. Keep it unchanged.
        if (id === "signature") return qsTr("Signature")
        if (id === "classic") return qsTr("Classic")
        return id
    }

    function partDisplayLabel(id) {
        if (id === "timer") return qsTr("Timer")
        if (id === "clock") return qsTr("Clock")
        if (id === "clock_timer") return qsTr("Clock + timer")
        return id
    }

    onClosed: {
        if (returnFocusItem)
            returnFocusItem.forceActiveFocus(Qt.PopupFocusReason)
    }

    enter: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 170; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.97; to: 1.0; duration: 210; easing.type: Easing.OutCubic }
        }
    }
    exit: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 120 }
            NumberAnimation { property: "scale"; from: 1.0; to: 0.98; duration: 120 }
        }
    }

    Overlay.modal: Rectangle {
        color: appTheme.black
        opacity: 0.56
    }

    background: Rectangle {
        radius: 18
        color: dialog.pal_surface
        border.color: dialog.pal_borderStrong
        border.width: 1
    }

    contentItem: ColumnLayout {
        spacing: 0
        Accessible.role: Accessible.Dialog
        Accessible.name: qsTr("Timer") + " · " + qsTranslate("SettingsWidget", "Settings")

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: dialog.compact ? 64 : 70

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: dialog.outerMargin
                anchors.rightMargin: dialog.outerMargin - 4
                spacing: 12

                Rectangle {
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    radius: 10
                    color: Qt.rgba(dialog.pal_accent.r, dialog.pal_accent.g, dialog.pal_accent.b, 0.11)
                    border.color: Qt.rgba(dialog.pal_accent.r, dialog.pal_accent.g, dialog.pal_accent.b, 0.30)
                    border.width: 1
                    Image {
                        anchors.centerIn: parent
                        width: 19; height: 19
                        sourceSize.width: 38; sourceSize.height: 38
                        source: "image://timericons/settings/38/" + dialog.iconHex(dialog.pal_accent)
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Timer") + " · " + qsTranslate("SettingsWidget", "Settings")
                        color: dialog.pal_textPrimary
                        font.pixelSize: 18
                        font.weight: Font.Bold
                        elide: Text.ElideRight
                    }
                }
                TimerButton {
                    objectName: "timerSettingsCloseButton"
                    iconName: "close"
                    iconSize: 14
                    tip: qsTranslate("SettingsWidget", "Close")
                    onClicked: dialog.close()
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: dialog.pal_border
            opacity: 0.8
        }

        Flickable {
            id: viewport
            objectName: "timerSettingsViewport"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            contentWidth: width
            contentHeight: settingsColumn.implicitHeight + dialog.outerMargin * 2

            property real wheelTargetY: contentY

            function clampContentY(value) {
                return Math.max(0, Math.min(Math.max(0, contentHeight - height), value))
            }

            function smoothWheelScroll(delta) {
                if (!settingsWheelAnimation.running)
                    wheelTargetY = contentY
                wheelTargetY = clampContentY(wheelTargetY + delta)
                settingsWheelAnimation.stop()
                settingsWheelAnimation.to = wheelTargetY
                settingsWheelAnimation.start()
            }

            NumberAnimation {
                id: settingsWheelAnimation
                target: viewport
                property: "contentY"
                duration: 190
                easing.type: Easing.OutCubic
            }

            WheelHandler {
                target: viewport
                acceptedDevices: PointerDevice.Mouse
                onWheel: function(event) {
                    var hasPixel = event.pixelDelta.y !== 0
                    var raw = hasPixel ? event.pixelDelta.y : event.angleDelta.y
                    viewport.smoothWheelScroll(hasPixel ? -raw : -raw * 0.35)
                    event.accepted = true
                }
            }

            ColumnLayout {
                id: settingsColumn
                x: dialog.outerMargin
                y: dialog.outerMargin
                width: Math.max(0, viewport.width - dialog.outerMargin * 2 - 10)
                spacing: 14

                SettingsCard {
                    objectName: "timerSettingsClockCard"
                    Layout.fillWidth: true
                    title: qsTr("Clock")

                    GridLayout {
                        Layout.fillWidth: true
                        columns: dialog.compact ? 1 : 2
                        columnSpacing: 18
                        rowSpacing: dialog.compact ? 7 : 13

                        SettingLabel { text: qsTr("Clock face") }
                        TimerSelect {
                            objectName: "timerClockModeSelect"
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            options: dialog.clockModeOptions
                            current: dialog.clockModeCurrent
                            accent: dialog.pal_accent
                            onPicked: function(i) {
                                timer.updateClock("mode", dialog.optionIdAt(dialog.clockModeRows, i, "digital"))
                            }
                        }

                        SettingLabel {
                            text: qsTr("Analog style")
                            opacity: dialog.optionOpacity(dialog.clockHasAnalog)
                        }
                        TimerSelect {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            opacity: dialog.optionOpacity(dialog.clockHasAnalog)
                            options: dialog.analogStyleOptions
                            current: dialog.analogStyleCurrent
                            accent: dialog.pal_accent
                            enabled: dialog.clockHasAnalog
                            onPicked: function(i) {
                                timer.updateClock("analog_style", dialog.optionIdAt(dialog.analogStyleRows, i, "signature"))
                            }
                        }

                        SettingLabel {
                            text: qsTr("Format")
                            opacity: dialog.optionOpacity(dialog.digitalClockOptionsAvailable)
                        }
                        TimerSegment {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            options: [qsTr("24-hour"), qsTr("12-hour")]
                            current: dialog.cfg.hour_format_24h ? 0 : 1
                            accent: dialog.pal_accent
                            stretch: true
                            enabled: dialog.digitalClockOptionsAvailable
                            onPicked: function(i) { timer.updateClock("hour_format_24h", i === 0) }
                        }

                        SettingLabel {
                            text: qsTr("Show seconds")
                            opacity: dialog.optionOpacity(dialog.digitalClockOptionsAvailable)
                        }
                        TimerToggle {
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            checked: dialog.cfg.show_seconds
                            accent: dialog.pal_accent
                            accessibleName: qsTr("Show seconds")
                            enabled: dialog.digitalClockOptionsAvailable
                            onToggled: function(v) { timer.updateClock("show_seconds", v) }
                        }

                        SettingLabel {
                            text: qsTr("AM / PM")
                            opacity: dialog.optionOpacity(dialog.ampmOptionAvailable)
                        }
                        TimerToggle {
                            checked: dialog.cfg.show_ampm
                            accent: dialog.pal_accent
                            accessibleName: qsTr("AM / PM")
                            enabled: dialog.ampmOptionAvailable
                            onToggled: function(v) { timer.updateClock("show_ampm", v) }
                        }
                    }
                }

                SettingsCard {
                    objectName: "timerSettingsPartCard"
                    Layout.fillWidth: true
                    title: qsTr("Part timer")

                    GridLayout {
                        Layout.fillWidth: true
                        columns: dialog.compact ? 1 : 2
                        columnSpacing: 18
                        rowSpacing: dialog.compact ? 7 : 13

                        SettingLabel { text: qsTr("During parts") }
                        TimerSelect {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            options: dialog.partDisplayOptions
                            current: dialog.partDisplayCurrent
                            accent: dialog.pal_accent
                            onPicked: function(i) {
                                timer.updateClock("part_timer_display", dialog.optionIdAt(dialog.partDisplayRows, i, "timer"))
                            }
                        }

                        SettingLabel { text: qsTr("Count direction") }
                        TimerSegment {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.bottomMargin: dialog.compact ? 5 : 0
                            options: [qsTr("Count down"), qsTr("Count up")]
                            current: dialog.cfg.direction === "up" ? 1 : 0
                            accent: dialog.pal_accent
                            stretch: true
                            onPicked: function(i) { timer.updateClock("direction", i === 1 ? "up" : "down") }
                        }

                        SettingLabel { text: qsTr("Hold duration when stopped") }
                        TimerStepper {
                            text: dialog.cfg.freeze_seconds + " s"
                            accent: dialog.pal_accent
                            fieldWidth: 52
                            decTip: qsTr("Less")
                            incTip: qsTr("More")
                            onDecremented: timer.updateClock("freeze_seconds", Math.max(0, dialog.cfg.freeze_seconds - 1))
                            onIncremented: timer.updateClock("freeze_seconds", Math.min(30, dialog.cfg.freeze_seconds + 1))
                        }
                    }
                }

                SettingsCard {
                    objectName: "timerSettingsDisplayCard"
                    Layout.fillWidth: true
                    title: qsTr("Display")

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 9
                        RowLayout {
                            Layout.fillWidth: true
                            SettingLabel { text: qsTr("Display size"); Layout.fillWidth: true }
                            Text {
                                text: Math.round(sizeSlider.value) + "%"
                                color: dialog.pal_textMuted
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                        }
                        TimerSlider {
                            id: sizeSlider
                            Layout.fillWidth: true
                            from: 10; to: 100; step: 5
                            value: dialog.cfg.text_scale_pct
                            accent: dialog.pal_accent
                            onMoved: function(v) { timer.updateClock("text_scale_pct", Math.round(v)) }
                        }
                    }
                }

                SettingsCard {
                    objectName: "timerSettingsMonitorsCard"
                    Layout.fillWidth: true
                    title: qsTr("Monitors")
                    iconName: "monitor"

                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: qsTr("Show timer"); Layout.fillWidth: true }
                        TimerToggle {
                            checked: timer.timerVisible
                            accent: dialog.pal_accent
                            accessibleName: qsTr("Show timer")
                            onToggled: function(v) { timer.setTimerVisible(v) }
                        }
                    }

                    Repeater {
                        model: timer.monitors
                        delegate: Rectangle {
                            id: monitorRow
                            required property var modelData
                            Layout.fillWidth: true
                            radius: 11
                            color: modelData.reserved
                                   ? Qt.rgba(dialog.pal_accent.r, dialog.pal_accent.g, dialog.pal_accent.b, 0.08)
                                   : dialog.pal_surfaceAlt
                            border.color: modelData.reserved ? dialog.pal_accent : dialog.pal_border
                            border.width: 1
                            implicitHeight: dialog.compact ? 104 : 58

                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: dialog.compact ? 10 : 12
                                spacing: 8

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 11
                                    Rectangle {
                                        Layout.preferredWidth: 34; Layout.preferredHeight: 34
                                        radius: 8
                                        color: modelData.reserved
                                               ? Qt.rgba(dialog.pal_accent.r, dialog.pal_accent.g, dialog.pal_accent.b, 0.14)
                                               : dialog.pal_bg
                                        border.color: modelData.reserved ? dialog.pal_accent : dialog.pal_border
                                        border.width: 1
                                        Image {
                                            anchors.centerIn: parent
                                            width: 16; height: 16
                                            sourceSize.width: 32; sourceSize.height: 32
                                            source: "image://timericons/monitor/32/"
                                                    + dialog.iconHex(modelData.reserved ? dialog.pal_accent : dialog.pal_textMuted)
                                        }
                                    }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        spacing: 2
                                        Text {
                                            Layout.fillWidth: true
                                            text: modelData.name
                                            color: dialog.pal_textPrimary
                                            font.pixelSize: 14
                                            font.weight: Font.DemiBold
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: modelData.reserved
                                                  ? qsTr("Reserved for the timer · %1").arg(modelData.resolution)
                                                  : modelData.resolution
                                            color: modelData.reserved ? dialog.pal_accent : dialog.pal_textMuted
                                            font.pixelSize: 12
                                            elide: Text.ElideRight
                                        }
                                    }
                                    MonitorAction { visible: !dialog.compact }
                                }
                                MonitorAction {
                                    visible: dialog.compact
                                    Layout.fillWidth: true
                                }
                            }

                            component MonitorAction: TimerButton {
                                text: monitorRow.modelData.reserved ? qsTr("Unreserve") : qsTr("Reserve")
                                variant: monitorRow.modelData.reserved ? "ghost" : "soft"
                                accent: dialog.pal_accent
                                tip: monitorRow.modelData.reserved ? "" : qsTr("Reserve this monitor for the timer")
                                onClicked: {
                                    if (monitorRow.modelData.reserved) {
                                        timer.unreserveMonitor(monitorRow.modelData.index)
                                    } else {
                                        var result = timer.requestReserve(monitorRow.modelData.index)
                                        if (result && result.conflict) {
                                            dialog.pendingReserveIndex = result.index
                                            conflictDialog.screenName = result.screenName
                                            conflictDialog.open()
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Rectangle {
                        visible: timer.monitors.length === 0
                        Layout.fillWidth: true
                        implicitHeight: 56
                        radius: 11
                        color: dialog.pal_surfaceAlt
                        border.color: dialog.pal_border
                        border.width: 1
                        Text {
                            anchors.fill: parent
                            anchors.margins: 12
                            text: qsTr("No secondary monitors detected")
                            color: dialog.pal_textMuted
                            font.pixelSize: 13
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                Item { Layout.fillWidth: true; Layout.preferredHeight: 1 }
            }

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                width: 10
                contentItem: Rectangle {
                    implicitWidth: 5
                    radius: 3
                    color: dialog.pal_borderStrong
                    opacity: 0.75
                }
            }
        }
    }

    component SettingsCard: Rectangle {
        id: card
        property string title: ""
        property string iconName: ""
        default property alias cardContent: cardColumn.data

        radius: 14
        color: dialog.pal_surfaceAlt
        border.color: dialog.pal_border
        border.width: 1
        implicitHeight: cardColumn.implicitHeight + dialog.cardPadding * 2

        ColumnLayout {
            id: cardColumn
            anchors.fill: parent
            anchors.margins: dialog.cardPadding
            spacing: 13

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Image {
                    visible: card.iconName !== ""
                    Layout.preferredWidth: 15; Layout.preferredHeight: 15
                    sourceSize.width: 30; sourceSize.height: 30
                    source: card.iconName === "" ? "" : "image://timericons/" + card.iconName + "/30/" + dialog.iconHex(dialog.pal_textMuted)
                }
                Text {
                    Layout.fillWidth: true
                    text: card.title
                    color: dialog.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.1
                    font.capitalization: Font.AllUppercase
                    elide: Text.ElideRight
                }
            }
        }
    }

    component SettingLabel: Text {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        color: dialog.pal_textSecondary
        font.pixelSize: 13
        wrapMode: dialog.compact ? Text.WordWrap : Text.NoWrap
        elide: dialog.compact ? Text.ElideNone : Text.ElideRight
    }

    Dialog {
        id: conflictDialog
        property string screenName: ""

        parent: Overlay.overlay
        x: parent ? Math.round((parent.width - width) / 2) : 0
        y: parent ? Math.round((parent.height - height) / 2) : 0
        width: Math.max(0, Math.min(400, parent ? parent.width - 32 : 400))
        padding: 24
        modal: true
        dim: true
        focus: true
        closePolicy: Popup.CloseOnEscape

        Overlay.modal: Rectangle { color: appTheme.black; opacity: 0.62 }
        background: Rectangle {
            radius: 16
            color: dialog.pal_surface
            border.color: dialog.pal_borderStrong
            border.width: 1
        }
        contentItem: ColumnLayout {
            spacing: 14
            Rectangle {
                Layout.preferredWidth: 46; Layout.preferredHeight: 46
                radius: 12
                color: Qt.rgba(dialog.pal_warning.r, dialog.pal_warning.g, dialog.pal_warning.b, 0.14)
                border.color: Qt.rgba(dialog.pal_warning.r, dialog.pal_warning.g, dialog.pal_warning.b, 0.34)
                border.width: 1
                Image {
                    anchors.centerIn: parent
                    width: 22; height: 22
                    sourceSize.width: 44; sourceSize.height: 44
                    source: "image://timericons/monitor/44/d29922"
                }
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Monitor in use by media")
                color: dialog.pal_textPrimary
                font.pixelSize: 16
                font.weight: Font.Bold
                wrapMode: Text.WordWrap
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Media is currently using %1. Reserve it for the timer and move media off this monitor?")
                      .arg(conflictDialog.screenName)
                color: dialog.pal_textMuted
                font.pixelSize: 13
                lineHeight: 1.25
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                spacing: 10
                Item { Layout.fillWidth: true }
                TimerButton {
                    text: qsTr("Cancel")
                    onClicked: conflictDialog.close()
                }
                TimerButton {
                    text: qsTr("Reserve")
                    variant: "primary"
                    accent: dialog.pal_accent
                    onClicked: {
                        if (dialog.pendingReserveIndex >= 0)
                            timer.confirmReserve(dialog.pendingReserveIndex)
                        conflictDialog.close()
                    }
                }
            }
        }
    }
}
