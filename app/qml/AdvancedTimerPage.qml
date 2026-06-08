// AdvancedTimerPage.qml — clock configuration, monitor reservation, week
// navigation and the colour-coded meeting-part list for the advanced timer.
// Scrolling mirrors PlaylistTreeView: a Flickable (mouse drag-to-scroll) with a
// smooth wheel animation and a thin auto-hiding scrollbar in its own gutter.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: page

    property color pal_bg: "#0d1117"
    property color pal_surface: "#13161c"
    property color pal_surfaceAlt: "#0f131a"
    property color pal_hover: "#1a1f2a"
    property color pal_border: "#1e2430"
    property color pal_borderStrong: "#2a3040"
    property color pal_textPrimary: "#e6edf3"
    property color pal_textSecondary: "#c9d1d9"
    property color pal_textMuted: "#8b949e"
    property color pal_textDim: "#484f58"
    property color pal_accent: "#388bfd"
    property color pal_danger: "#f85149"

    readonly property var cfg: timer.clockConfig
    readonly property bool clockHasAnalog: page.cfg.mode === "analog" || page.cfg.mode === "analog_digital"
    readonly property bool clockHasDigital: page.cfg.mode === "digital" || page.cfg.mode === "analog_digital"
    readonly property bool digitalClockOptionsAvailable: page.clockHasDigital
    readonly property bool ampmOptionAvailable: page.digitalClockOptionsAvailable && !page.cfg.hour_format_24h
    readonly property var clockModeRows: page.optionRows(timer.clockModes, ["digital", "analog", "analog_digital"])
    readonly property var clockModeOptions: page.optionLabels(page.clockModeRows, page.clockModeLabel)
    readonly property int clockModeCurrent: page.optionIndex(page.clockModeRows, page.cfg.mode, "digital")
    readonly property var analogStyleRows: page.optionRows(timer.analogClockStyles, ["signature", "classic"])
    readonly property var analogStyleOptions: page.optionLabels(page.analogStyleRows, page.analogStyleLabel)
    readonly property int analogStyleCurrent: page.optionIndex(page.analogStyleRows, page.cfg.analog_style, "signature")
    property int pendingReserveIndex: -1

    readonly property int sideInset: 20
    readonly property int scrollGutter: 12
    readonly property int scrollGap: 8
    readonly property int controlWidth: 206   // shared compact width for timer controls

    function optionOpacity(available) {
        return available ? 1.0 : 0.68
    }

    function optionRows(rows, fallback) {
        if (rows && rows.length > 0)
            return rows
        return fallback
    }

    function optionId(row) {
        if (typeof row === "string")
            return row
        return row && row.id ? row.id : ""
    }

    function optionLabels(rows, labelFunc) {
        var labels = []
        for (var i = 0; i < rows.length; ++i)
            labels.push(labelFunc(page.optionId(rows[i])))
        return labels
    }

    function optionIndex(rows, optionId, fallbackId) {
        var id = optionId ? optionId : fallbackId
        for (var i = 0; i < rows.length; ++i) {
            if (page.optionId(rows[i]) === id)
                return i
        }
        return 0
    }

    function optionIdAt(rows, index, fallbackId) {
        if (index >= 0 && index < rows.length)
            return page.optionId(rows[index])
        return fallbackId
    }

    function clockModeLabel(id) {
        if (id === "digital")
            return qsTr("Digital")
        if (id === "analog")
            return qsTr("Analog")
        if (id === "analog_digital")
            return qsTr("Analog + digital")
        return id
    }

    function analogStyleLabel(id) {
        if (id === "signature")
            return qsTr("Signature")
        if (id === "classic")
            return qsTr("Classic")
        return id
    }

    // Reuse the shared meeting-section translations (matches the Meetings tab).
    function sectionName(key) {
        if (key === "treasures")   return qsTranslate("_Section", "TREASURES FROM GOD'S WORD")
        if (key === "ministry")    return qsTranslate("_Section", "APPLY YOURSELF TO THE FIELD MINISTRY")
        if (key === "living")      return qsTranslate("_Section", "LIVING AS CHRISTIANS")
        if (key === "public_talk") return qsTranslate("_Section", "PUBLIC TALK")
        if (key === "watchtower")  return qsTranslate("_Section", "Watchtower Study")
        return key
    }

    component Card: Rectangle {
        radius: 14
        color: page.pal_surface
        border.color: page.pal_border
        border.width: 1
    }

    component CardTitle: RowLayout {
        id: ctRoot
        property string label: ""
        property string icon: ""
        spacing: 9
        Image {
            visible: ctRoot.icon !== ""
            Layout.preferredWidth: 15; Layout.preferredHeight: 15
            sourceSize.width: 30; sourceSize.height: 30
            source: ctRoot.icon === "" ? "" : "image://timericons/" + ctRoot.icon + "/30/8b949e"
        }
        Text {
            text: ctRoot.label
            color: page.pal_textMuted
            font.pixelSize: 11
            font.weight: Font.DemiBold
            font.letterSpacing: 1.2
            font.capitalization: Font.AllUppercase
        }
    }

    component RowLabel: Text {
        color: page.pal_textSecondary
        font.pixelSize: 13
        Layout.fillWidth: true
        elide: Text.ElideRight
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
        contentHeight: contentCol.implicitHeight + 20

        property real wheelTargetY: contentY
        property bool wheelScrolling: false

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
            target: scroll
            property: "contentY"
            duration: 200
            easing.type: Easing.OutCubic
            onStopped: scroll.wheelScrolling = false
        }

        WheelHandler {
            target: scroll
            acceptedDevices: PointerDevice.Mouse
            onWheel: function(event) {
                var hasPixel = event.pixelDelta.y !== 0
                var raw = hasPixel ? event.pixelDelta.y : event.angleDelta.y
                var delta = hasPixel ? -raw : -raw * 0.35
                scroll.smoothWheelScroll(delta)
                event.accepted = true
            }
        }

        ColumnLayout {
            id: contentCol
            x: page.sideInset
            width: Math.max(0, scroll.width - page.sideInset - page.scrollGutter - page.scrollGap)
            spacing: 14

            // ── Clock configuration ───────────────────────────────────────────
            Card {
                Layout.fillWidth: true
                Layout.topMargin: 6
                implicitHeight: clockCol.implicitHeight + 36
                ColumnLayout {
                    id: clockCol
                    anchors.fill: parent
                    anchors.margins: 18
                    spacing: 14

                    CardTitle { label: qsTr("Clock") }

                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        RowLabel { text: qsTr("Clock face") }
                        TimerSelect {
                            Layout.preferredWidth: page.controlWidth
                            options: page.clockModeOptions
                            current: page.clockModeCurrent
                            accent: page.pal_accent
                            onPicked: function(i) {
                                timer.updateClock("mode", page.optionIdAt(page.clockModeRows, i, "digital"))
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        opacity: page.optionOpacity(page.clockHasAnalog)
                        Behavior on opacity { NumberAnimation { duration: 140 } }
                        RowLabel { text: qsTr("Analog style") }
                        TimerSelect {
                            Layout.preferredWidth: page.controlWidth
                            options: page.analogStyleOptions
                            current: page.analogStyleCurrent
                            accent: page.pal_accent
                            enabled: page.clockHasAnalog
                            onPicked: function(i) {
                                timer.updateClock("analog_style", page.optionIdAt(page.analogStyleRows, i, "signature"))
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        opacity: page.optionOpacity(page.digitalClockOptionsAvailable)
                        Behavior on opacity { NumberAnimation { duration: 140 } }
                        RowLabel { text: qsTr("Format") }
                        TimerSegment {
                            Layout.preferredWidth: Math.max(page.controlWidth, implicitWidth)
                            options: [qsTr("24-hour"), qsTr("12-hour")]
                            current: page.cfg.hour_format_24h ? 0 : 1
                            accent: page.pal_accent; stretch: true
                            enabled: page.digitalClockOptionsAvailable
                            onPicked: function(i) { timer.updateClock("hour_format_24h", i === 0) }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        opacity: page.optionOpacity(page.digitalClockOptionsAvailable)
                        Behavior on opacity { NumberAnimation { duration: 140 } }
                        RowLabel { text: qsTr("Show seconds") }
                        TimerToggle {
                            checked: page.cfg.show_seconds; accent: page.pal_accent
                            enabled: page.digitalClockOptionsAvailable
                            onToggled: function(v) { timer.updateClock("show_seconds", v) }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        opacity: page.optionOpacity(page.ampmOptionAvailable)
                        Behavior on opacity { NumberAnimation { duration: 140 } }
                        RowLabel { text: qsTr("AM / PM") }
                        TimerToggle {
                            checked: page.cfg.show_ampm; accent: page.pal_accent
                            enabled: page.ampmOptionAvailable
                            onToggled: function(v) { timer.updateClock("show_ampm", v) }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

                    CardTitle { label: qsTr("Part timer") }

                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        RowLabel { text: qsTr("Count direction") }
                        TimerSegment {
                            Layout.preferredWidth: Math.max(page.controlWidth, implicitWidth)
                            options: [qsTr("Count down"), qsTr("Count up")]
                            current: page.cfg.direction === "up" ? 1 : 0
                            accent: page.pal_accent; stretch: true
                            onPicked: function(i) { timer.updateClock("direction", i === 1 ? "up" : "down") }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        RowLabel { text: qsTr("Hold duration when stopped") }
                        TimerStepper {
                            text: page.cfg.freeze_seconds + " s"
                            accent: page.pal_accent; fieldWidth: 52
                            decTip: qsTr("Less"); incTip: qsTr("More")
                            onDecremented: timer.updateClock("freeze_seconds", Math.max(0, page.cfg.freeze_seconds - 1))
                            onIncremented: timer.updateClock("freeze_seconds", Math.min(30, page.cfg.freeze_seconds + 1))
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

                    CardTitle { label: qsTr("Display") }

                    RowLayout {
                        Layout.fillWidth: true; spacing: 14
                        Text {
                            text: qsTr("Display size"); color: page.pal_textSecondary
                            font.pixelSize: 13; Layout.fillWidth: true
                        }
                        TimerSlider {
                            id: sizeSlider
                            Layout.preferredWidth: page.controlWidth - 52
                            from: 10; to: 100; step: 5
                            value: page.cfg.text_scale_pct
                            accent: page.pal_accent
                            onMoved: function(v) { timer.updateClock("text_scale_pct", Math.round(v)) }
                        }
                        Text {
                            text: Math.round(sizeSlider.value) + "%"
                            color: page.pal_textMuted; font.pixelSize: 13
                            horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 44
                        }
                    }
                }
            }

            // ── Monitors ──────────────────────────────────────────────────────
            Card {
                Layout.fillWidth: true
                implicitHeight: monCol.implicitHeight + 36
                ColumnLayout {
                    id: monCol
                    anchors.fill: parent
                    anchors.margins: 18
                    spacing: 13

                    RowLayout {
                        Layout.fillWidth: true
                        CardTitle { label: qsTr("Monitors"); icon: "monitor"; Layout.fillWidth: true }
                        Text {
                            text: qsTr("Show timer"); color: page.pal_textSecondary; font.pixelSize: 13
                        }
                        TimerToggle {
                            Layout.leftMargin: 4
                            checked: timer.timerVisible; accent: page.pal_accent
                            onToggled: function(v) { timer.setTimerVisible(v) }
                        }
                    }

                    Repeater {
                        model: timer.monitors
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            radius: 11
                            color: modelData.reserved
                                   ? Qt.rgba(0.22, 0.55, 0.99, 0.08) : page.pal_surfaceAlt
                            border.color: modelData.reserved ? page.pal_accent : page.pal_border
                            border.width: 1
                            implicitHeight: 58
                            Behavior on border.color { ColorAnimation { duration: 150 } }
                            Behavior on color { ColorAnimation { duration: 150 } }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 16
                                anchors.rightMargin: 12
                                spacing: 12

                                Rectangle {
                                    Layout.preferredWidth: 34; Layout.preferredHeight: 34
                                    radius: 8
                                    color: modelData.reserved ? Qt.rgba(0.22, 0.55, 0.99, 0.14) : page.pal_bg
                                    border.color: modelData.reserved ? page.pal_accent : page.pal_border
                                    border.width: 1
                                    Image {
                                        anchors.centerIn: parent
                                        width: 16; height: 16
                                        sourceSize.width: 32; sourceSize.height: 32
                                        source: "image://timericons/monitor/32/" + (modelData.reserved ? "79c0ff" : "8b949e")
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    Text {
                                        text: modelData.name
                                        color: page.pal_textPrimary
                                        font.pixelSize: 14; font.weight: Font.DemiBold
                                        elide: Text.ElideRight; Layout.fillWidth: true
                                    }
                                    Text {
                                        text: modelData.reserved
                                              ? qsTr("Reserved for the timer · %1").arg(modelData.resolution)
                                              : modelData.resolution
                                        color: modelData.reserved ? page.pal_accent : page.pal_textMuted
                                        font.pixelSize: 12
                                    }
                                }
                                TimerButton {
                                    text: modelData.reserved ? qsTr("Unreserve") : qsTr("Reserve")
                                    variant: modelData.reserved ? "ghost" : "soft"
                                    accent: page.pal_accent
                                    tip: modelData.reserved ? "" : qsTr("Reserve this monitor for the timer")
                                    onClicked: {
                                        if (modelData.reserved) {
                                            timer.unreserveMonitor(modelData.index)
                                        } else {
                                            var res = timer.requestReserve(modelData.index)
                                            if (res && res.conflict) {
                                                page.pendingReserveIndex = res.index
                                                conflictModal.screenName = res.screenName
                                                conflictModal.shown = true
                                            }
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
                        color: page.pal_surfaceAlt
                        border.color: page.pal_border
                        border.width: 1
                        Text {
                            anchors.centerIn: parent
                            text: qsTr("No secondary monitors detected")
                            color: page.pal_textMuted; font.pixelSize: 13
                        }
                    }
                }
            }

            // ── Week navigation + meeting type ────────────────────────────────
            Card {
                Layout.fillWidth: true
                implicitHeight: 60
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 8

                    TimerButton {
                        iconName: "chevron_left"; iconSize: 15
                        tip: qsTr("Previous week")
                        onClicked: timer.previousWeek()
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 9
                        Item { Layout.fillWidth: true }
                        Image {
                            Layout.preferredWidth: 15; Layout.preferredHeight: 15
                            sourceSize.width: 30; sourceSize.height: 30
                            source: "image://timericons/calendar/30/8b949e"
                            opacity: 0.85
                        }
                        ColumnLayout {
                            spacing: 0
                            Text {
                                text: timer.weekLabel
                                color: page.pal_textPrimary
                                font.pixelSize: 14; font.weight: Font.DemiBold
                                horizontalAlignment: Text.AlignHCenter
                            }
                            Text {
                                visible: !timer.isCurrentWeek
                                text: qsTr("Not the current week")
                                color: page.pal_textMuted; font.pixelSize: 11
                                horizontalAlignment: Text.AlignHCenter
                                Layout.alignment: Qt.AlignHCenter
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                    TimerButton {
                        iconName: "chevron_right"; iconSize: 15
                        tip: qsTr("Next week")
                        onClicked: timer.nextWeek()
                    }
                    TimerButton {
                        iconName: "home"; iconSize: 14
                        enabled: !timer.isCurrentWeek
                        tip: qsTr("Back to this week")
                        onClicked: timer.goToCurrentWeek()
                    }
                    Rectangle {
                        Layout.preferredWidth: 1
                        Layout.fillHeight: true
                        Layout.topMargin: 14; Layout.bottomMargin: 14
                        color: page.pal_border
                    }
                    TimerSegment {
                        options: [qsTr("Midweek"), qsTr("Weekend")]
                        current: timer.meetingType === "weekend" ? 1 : 0
                        accent: page.pal_accent; segWidth: 92
                        onPicked: function(i) { timer.setMeetingType(i === 1 ? "weekend" : "midweek") }
                    }
                    TimerButton {
                        iconName: "pdf"; iconSize: 15
                        tip: qsTr("Export PDF")
                        onClicked: timer.exportSchedulePdf()
                    }
                }
            }

            // ── "Meeting parts" sub-header ────────────────────────────────────
            Text {
                Layout.topMargin: 4
                Layout.leftMargin: 2
                text: qsTr("Meeting parts")
                color: page.pal_textMuted
                font.pixelSize: 11
                font.weight: Font.DemiBold
                font.letterSpacing: 1.2
                font.capitalization: Font.AllUppercase
            }

            // ── Meeting parts (grouped + colour-coded per section) ────────────
            // Animated container: week navigation slides the list in from the
            // side, switching meeting type cross-fades it (via timer.weekShift).
            ColumnLayout {
                id: partsContainer
                Layout.fillWidth: true
                spacing: 14
                property real slideX: 0
                transform: Translate { x: partsContainer.slideX }

                ParallelAnimation {
                    id: weekAnim
                    property int dir: 0
                    NumberAnimation {
                        target: partsContainer; property: "slideX"
                        from: weekAnim.dir * 40; to: 0
                        duration: 340; easing.type: Easing.OutCubic
                    }
                    NumberAnimation {
                        target: partsContainer; property: "opacity"
                        from: 0.0; to: 1.0
                        duration: 280; easing.type: Easing.OutCubic
                    }
                }
                Connections {
                    target: timer
                    function onWeekShift(direction) {
                        weekAnim.stop()
                        weekAnim.dir = direction
                        weekAnim.start()
                    }
                }

                Repeater {
                    model: timer.parts
                    delegate: ColumnLayout {
                    id: partGroup
                    required property var modelData
                    readonly property color secAccent: modelData.sectionColor || "#388bfd"
                    readonly property color secTextColor: modelData.sectionTextColor || modelData.sectionColor || "#79c0ff"
                    readonly property color secBadgeBg: modelData.sectionBadgeBg || "#0d2035"
                    readonly property color secBorderColor: modelData.sectionBorderColor || modelData.sectionColor || "#388bfd"
                    readonly property string secIconHex: ("" + (modelData.sectionTextColor || modelData.sectionColor || "#79c0ff")).replace("#", "")
                    Layout.fillWidth: true
                    spacing: 8

                    RowLayout {
                        visible: partGroup.modelData.showSectionHeader && partGroup.modelData.firstOfSection
                        Layout.fillWidth: true
                        Layout.topMargin: 8
                        spacing: 11

                        Rectangle {
                            Layout.preferredWidth: 34; Layout.preferredHeight: 34
                            radius: 9
                            color: partGroup.secBadgeBg
                            border.width: 1
                            border.color: partGroup.secBorderColor
                            Image {
                                anchors.centerIn: parent
                                width: 18; height: 18
                                sourceSize.width: 36; sourceSize.height: 36
                                source: "image://timericons/" + partGroup.modelData.sectionKey + "/36/" + partGroup.secIconHex
                            }
                        }
                        Text {
                            text: page.sectionName(partGroup.modelData.sectionKey)
                            color: partGroup.secTextColor
                            font.pixelSize: 13; font.weight: Font.Bold
                            font.letterSpacing: 0.6
                            font.capitalization: Font.AllUppercase
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        RowLayout {
                            visible: partGroup.modelData.configurableCount
                            spacing: 6
                            Text { text: qsTr("Parts"); color: page.pal_textMuted; font.pixelSize: 12 }
                            TimerStepper {
                                text: "" + partGroup.modelData.sectionCount
                                accent: partGroup.secAccent
                                fieldWidth: 30
                                decTip: qsTr("Fewer parts"); incTip: qsTr("More parts")
                                onDecremented: timer.setSectionCount(partGroup.modelData.section, Math.max(1, partGroup.modelData.sectionCount - 1))
                                onIncremented: timer.setSectionCount(partGroup.modelData.section, Math.min(8, partGroup.modelData.sectionCount + 1))
                            }
                        }
                        Rectangle {
                            visible: partGroup.modelData.sectionTotalLabel.length > 0
                            radius: 7
                            color: partGroup.secBadgeBg
                            border.color: partGroup.secBorderColor
                            border.width: 1
                            implicitHeight: 24
                            implicitWidth: totalLbl.implicitWidth + 18
                            Text {
                                id: totalLbl
                                anchors.centerIn: parent
                                text: qsTr("%1 total").arg(partGroup.modelData.sectionTotalLabel)
                                color: partGroup.secTextColor
                                font.pixelSize: 11; font.weight: Font.DemiBold
                            }
                        }
                    }

                    MeetingPartRow {
                        Layout.fillWidth: true
                        partData: partGroup.modelData
                        live: timer.liveState
                        accent: partGroup.secAccent
                        number: partGroup.modelData.displayNumber
                        pal_surface: page.pal_surface
                        pal_surfaceAlt: page.pal_surfaceAlt
                        pal_border: page.pal_border
                        pal_borderStrong: page.pal_borderStrong
                        pal_textPrimary: page.pal_textPrimary
                        pal_textSecondary: page.pal_textSecondary
                        pal_textMuted: page.pal_textMuted
                        pal_textDim: page.pal_textDim
                        pal_danger: page.pal_danger
                    }
                    }
                }
            }

            Item { Layout.fillWidth: true; implicitHeight: 8 }
        }

        ScrollBar.vertical: ScrollBar {
            id: vScroll
            policy: ScrollBar.AsNeeded
            width: page.scrollGutter
            contentItem: Rectangle {
                implicitWidth: 6
                radius: 3
                color: ((vScroll.active && !scroll.wheelScrolling) || vScroll.hovered)
                       ? page.pal_textMuted : page.pal_borderStrong
                opacity: (vScroll.active || vScroll.hovered || scroll.moving) ? 1.0 : 0.35
                anchors.horizontalCenter: parent.horizontalCenter
                Behavior on color { ColorAnimation { duration: 150 } }
                Behavior on opacity { NumberAnimation { duration: 180 } }
            }
        }
    }

    // ── Conflict confirmation (custom premium modal) ──────────────────────────
    Item {
        id: conflictModal
        anchors.fill: parent
        z: 100
        property bool shown: false
        property string screenName: ""
        visible: opacity > 0.01
        opacity: shown ? 1.0 : 0.0
        Behavior on opacity { NumberAnimation { duration: 170; easing.type: Easing.OutCubic } }

        Rectangle {
            anchors.fill: parent
            color: "#000000"
            opacity: 0.55
            MouseArea {
                anchors.fill: parent
                onClicked: conflictModal.shown = false
            }
        }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(400, page.width - 48)
            radius: 16
            color: page.pal_surface
            border.color: page.pal_borderStrong
            border.width: 1
            implicitHeight: modalCol.implicitHeight + 48
            scale: conflictModal.shown ? 1.0 : 0.94
            Behavior on scale { NumberAnimation { duration: 200; easing.type: Easing.OutBack } }

            MouseArea {
                anchors.fill: parent
                z: 0
                acceptedButtons: Qt.AllButtons
                hoverEnabled: true
                onPressed: function(mouse) { mouse.accepted = true }
                onReleased: function(mouse) { mouse.accepted = true }
                onClicked: function(mouse) { mouse.accepted = true }
            }

            ColumnLayout {
                id: modalCol
                anchors.fill: parent
                anchors.margins: 24
                spacing: 14

                Rectangle {
                    Layout.preferredWidth: 46; Layout.preferredHeight: 46
                    radius: 12
                    color: Qt.rgba(0.82, 0.6, 0.13, 0.14)
                    border.color: Qt.rgba(0.82, 0.6, 0.13, 0.34)
                    border.width: 1
                    Image {
                        anchors.centerIn: parent
                        width: 22; height: 22
                        sourceSize.width: 44; sourceSize.height: 44
                        source: "image://timericons/monitor/44/d29922"
                    }
                }
                Text {
                    text: qsTr("Monitor in use by media")
                    color: page.pal_textPrimary
                    font.pixelSize: 16; font.weight: Font.Bold
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }
                Text {
                    text: qsTr("Media is currently using %1. Reserve it for the timer and move media off this monitor?")
                          .arg(conflictModal.screenName)
                    color: page.pal_textMuted
                    font.pixelSize: 13
                    lineHeight: 1.25
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                RowLayout {
                    Layout.topMargin: 4
                    Layout.fillWidth: true
                    spacing: 10
                    Item { Layout.fillWidth: true }
                    TimerButton {
                        text: qsTr("Cancel")
                        variant: "ghost"
                        accent: page.pal_accent
                        onClicked: conflictModal.shown = false
                    }
                    TimerButton {
                        text: qsTr("Reserve")
                        variant: "primary"
                        accent: page.pal_accent
                        onClicked: {
                            if (page.pendingReserveIndex >= 0)
                                timer.confirmReserve(page.pendingReserveIndex)
                            conflictModal.shown = false
                        }
                    }
                }
            }
        }
    }
}
