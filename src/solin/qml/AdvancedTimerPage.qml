// AdvancedTimerPage.qml — responsive week navigation and the colour-coded
// meeting-part workspace for the advanced timer.
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
    property color pal_textDim: appTheme.textDim
    property color pal_accent: appTheme.accent
    property color pal_warning: appTheme.warning
    property color pal_danger: appTheme.danger

    readonly property int sideInset: width < 420 ? 12 : 20
    readonly property int scrollGutter: 12
    readonly property int scrollGap: width < 420 ? 4 : 8
    readonly property bool compactNavigation: contentColumn.width < 820
    readonly property bool compactSectionHeaders: contentColumn.width < 620

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

    component WeekSummary: RowLayout {
        id: summary
        property bool showIcon: true
        spacing: 9

        Item { visible: summary.showIcon; Layout.fillWidth: true }
        Image {
            visible: summary.showIcon
            Layout.preferredWidth: 15; Layout.preferredHeight: 15
            sourceSize.width: 30; sourceSize.height: 30
            source: "image://timericons/calendar/30/8b949e"
            opacity: 0.85
        }
        ColumnLayout {
            Layout.fillWidth: !summary.showIcon
            Layout.minimumWidth: 0
            spacing: 0
            Text {
                Layout.fillWidth: true
                text: timer.weekLabel
                color: page.pal_textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
            }
            Text {
                Layout.fillWidth: true
                visible: !timer.isCurrentWeek
                text: qsTr("Not the current week")
                color: page.pal_textMuted
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
            }
        }
        Item { visible: summary.showIcon; Layout.fillWidth: true }
    }

    Flickable {
        id: scroll
        objectName: "advancedTimerViewport"
        anchors.fill: parent
        anchors.topMargin: 8
        anchors.bottomMargin: 4
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        maximumFlickVelocity: 5200
        flickDeceleration: 2800
        contentWidth: width
        contentHeight: contentColumn.implicitHeight + 20

        property real wheelTargetY: contentY
        property bool wheelScrolling: false

        function clampContentY(value) {
            return Math.max(0, Math.min(Math.max(0, contentHeight - height), value))
        }

        function smoothWheelScroll(delta) {
            if (!wheelAnimation.running)
                wheelTargetY = contentY
            wheelTargetY = clampContentY(wheelTargetY + delta)
            wheelAnimation.stop()
            wheelScrolling = true
            wheelAnimation.to = wheelTargetY
            wheelAnimation.start()
        }

        NumberAnimation {
            id: wheelAnimation
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
                scroll.smoothWheelScroll(hasPixel ? -raw : -raw * 0.35)
                event.accepted = true
            }
        }

        ColumnLayout {
            id: contentColumn
            objectName: "advancedTimerContent"
            x: page.sideInset
            width: Math.max(0, scroll.width - page.sideInset - page.scrollGutter - page.scrollGap)
            spacing: 14

            Card {
                id: navigationCard
                objectName: "timerWeekNavigationCard"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredWidth: contentColumn.width
                Layout.maximumWidth: contentColumn.width
                Layout.topMargin: 6
                implicitHeight: page.compactNavigation ? 112 : 60

                RowLayout {
                    objectName: "timerWeekNavigationWide"
                    visible: !page.compactNavigation
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 8

                    TimerButton {
                        objectName: "timerPreviousWeekButtonWide"
                        iconName: "chevron_left"; iconSize: 15
                        tip: qsTr("Previous week")
                        onClicked: timer.previousWeek()
                    }
                    WeekSummary { Layout.fillWidth: true }
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
                        accent: page.pal_accent
                        segWidth: 92
                        onPicked: function(i) { timer.setMeetingType(i === 1 ? "weekend" : "midweek") }
                    }
                    TimerButton {
                        iconName: "export"; iconSize: 15
                        tip: qsTr("Export PDF")
                        onClicked: timer.exportSchedulePdf()
                    }
                    TimerButton {
                        id: settingsButtonWide
                        objectName: "timerSettingsButtonWide"
                        iconName: "settings"; iconSize: 16
                        tip: qsTranslate("SettingsWidget", "Settings")
                        onClicked: settingsDialog.present(settingsButtonWide)
                    }
                }

                ColumnLayout {
                    objectName: "timerWeekNavigationCompact"
                    visible: page.compactNavigation
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.topMargin: 10
                    anchors.bottomMargin: 10
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        TimerButton {
                            objectName: "timerPreviousWeekButtonCompact"
                            iconName: "chevron_left"; iconSize: 15
                            tip: qsTr("Previous week")
                            onClicked: timer.previousWeek()
                        }
                        WeekSummary { showIcon: false; Layout.fillWidth: true; Layout.minimumWidth: 0 }
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
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        TimerSegment {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            options: [qsTr("Midweek"), qsTr("Weekend")]
                            current: timer.meetingType === "weekend" ? 1 : 0
                            accent: page.pal_accent
                            segWidth: 68
                            stretch: true
                            onPicked: function(i) { timer.setMeetingType(i === 1 ? "weekend" : "midweek") }
                        }
                        TimerButton {
                            iconName: "export"; iconSize: 15
                            tip: qsTr("Export PDF")
                            onClicked: timer.exportSchedulePdf()
                        }
                        TimerButton {
                            id: settingsButtonCompact
                            objectName: "timerSettingsButtonCompact"
                            iconName: "settings"; iconSize: 16
                            tip: qsTranslate("SettingsWidget", "Settings")
                            onClicked: settingsDialog.present(settingsButtonCompact)
                        }
                    }
                }
            }

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

            ColumnLayout {
                id: partsContainer
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredWidth: contentColumn.width
                Layout.maximumWidth: contentColumn.width
                spacing: 14
                property real slideX: 0
                transform: Translate { x: partsContainer.slideX }

                ParallelAnimation {
                    id: weekAnimation
                    property int direction: 0
                    NumberAnimation {
                        target: partsContainer; property: "slideX"
                        from: weekAnimation.direction * 40; to: 0
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
                        weekAnimation.stop()
                        weekAnimation.direction = direction
                        weekAnimation.start()
                    }
                }

                Repeater {
                    model: timer.parts
                    delegate: ColumnLayout {
                        id: partGroup
                        required property var modelData
                        readonly property color sectionAccent: modelData.sectionColor || appTheme.accent
                        readonly property color sectionText: modelData.sectionTextColor || modelData.sectionColor || appTheme.accentText
                        readonly property color sectionBadge: modelData.sectionBadgeBg || appTheme.accentTint
                        readonly property color sectionBorder: modelData.sectionBorderColor || modelData.sectionColor || appTheme.accent
                        readonly property string sectionIconHex: ("" + sectionText).replace("#", "")
                        Layout.fillWidth: true
                        spacing: 8

                        ColumnLayout {
                            visible: partGroup.modelData.showSectionHeader && partGroup.modelData.firstOfSection
                            Layout.fillWidth: true
                            Layout.topMargin: 8
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 11
                                Rectangle {
                                    Layout.preferredWidth: 34; Layout.preferredHeight: 34
                                    radius: 9
                                    color: partGroup.sectionBadge
                                    border.width: 1
                                    border.color: partGroup.sectionBorder
                                    Image {
                                        anchors.centerIn: parent
                                        width: 18; height: 18
                                        sourceSize.width: 36; sourceSize.height: 36
                                        source: "image://timericons/" + partGroup.modelData.sectionKey + "/36/" + partGroup.sectionIconHex
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    text: page.sectionName(partGroup.modelData.sectionKey)
                                    color: partGroup.sectionText
                                    font.pixelSize: 13
                                    font.weight: Font.Bold
                                    font.letterSpacing: 0.6
                                    font.capitalization: Font.AllUppercase
                                    elide: Text.ElideRight
                                }
                                CountControl { visible: !page.compactSectionHeaders && partGroup.modelData.configurableCount }
                                TotalBadge { visible: !page.compactSectionHeaders && partGroup.modelData.sectionTotalLabel.length > 0 }
                            }

                            RowLayout {
                                visible: page.compactSectionHeaders
                                         && (partGroup.modelData.configurableCount
                                             || partGroup.modelData.sectionTotalLabel.length > 0)
                                Layout.fillWidth: true
                                spacing: 8
                                CountControl { visible: partGroup.modelData.configurableCount }
                                Item { Layout.fillWidth: true }
                                TotalBadge { visible: partGroup.modelData.sectionTotalLabel.length > 0 }
                            }
                        }

                        MeetingPartRow {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.maximumWidth: partsContainer.width
                            compactLayout: page.compactSectionHeaders
                            partData: partGroup.modelData
                            live: timer.liveState
                            accent: partGroup.sectionAccent
                            number: partGroup.modelData.displayNumber
                            pal_surface: page.pal_surface
                            pal_surfaceAlt: page.pal_surfaceAlt
                            pal_border: page.pal_border
                            pal_textPrimary: page.pal_textPrimary
                            pal_textSecondary: page.pal_textSecondary
                            pal_textMuted: page.pal_textMuted
                            pal_textDim: page.pal_textDim
                            pal_danger: page.pal_danger
                        }

                        component CountControl: RowLayout {
                            spacing: 6
                            Text { text: qsTr("Parts"); color: page.pal_textMuted; font.pixelSize: 12 }
                            TimerStepper {
                                text: "" + partGroup.modelData.sectionCount
                                accent: partGroup.sectionAccent
                                fieldWidth: 30
                                decTip: qsTr("Fewer parts")
                                incTip: qsTr("More parts")
                                onDecremented: timer.setSectionCount(partGroup.modelData.section, Math.max(1, partGroup.modelData.sectionCount - 1))
                                onIncremented: timer.setSectionCount(partGroup.modelData.section, Math.min(8, partGroup.modelData.sectionCount + 1))
                            }
                        }

                        component TotalBadge: Rectangle {
                            radius: 7
                            color: partGroup.sectionBadge
                            border.color: partGroup.sectionBorder
                            border.width: 1
                            implicitHeight: 24
                            implicitWidth: totalLabel.implicitWidth + 18
                            Text {
                                id: totalLabel
                                anchors.centerIn: parent
                                text: qsTr("%1 total").arg(partGroup.modelData.sectionTotalLabel)
                                color: partGroup.sectionText
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }
                        }
                    }
                }
            }

            Item { Layout.fillWidth: true; implicitHeight: 8 }
        }

        ScrollBar.vertical: ScrollBar {
            id: verticalScroll
            policy: ScrollBar.AsNeeded
            width: page.scrollGutter
            contentItem: Rectangle {
                implicitWidth: 6
                radius: 3
                color: ((verticalScroll.active && !scroll.wheelScrolling) || verticalScroll.hovered)
                       ? page.pal_textMuted : page.pal_borderStrong
                opacity: (verticalScroll.active || verticalScroll.hovered || scroll.moving) ? 1.0 : 0.35
                anchors.horizontalCenter: parent.horizontalCenter
                Behavior on color { ColorAnimation { duration: 150 } }
                Behavior on opacity { NumberAnimation { duration: 180 } }
            }
        }
    }

    TimerSettingsDialog {
        id: settingsDialog
        pal_bg: page.pal_bg
        pal_surface: page.pal_surface
        pal_surfaceAlt: page.pal_surfaceAlt
        pal_border: page.pal_border
        pal_borderStrong: page.pal_borderStrong
        pal_textPrimary: page.pal_textPrimary
        pal_textSecondary: page.pal_textSecondary
        pal_textMuted: page.pal_textMuted
        pal_accent: page.pal_accent
        pal_warning: page.pal_warning
    }
}
