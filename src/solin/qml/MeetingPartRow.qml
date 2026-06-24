// MeetingPartRow.qml — one meeting part: start/stop/reset, ±1 min steppers and
// inline duration editing, colour-coded by its section. Live time for the
// running part comes from `timer.liveState`; everything else from `partData`.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: row
    property var partData
    property var live
    property int number: 0
    property color accent: appTheme.accent
    property color pal_surface: appTheme.surface
    property color pal_surfaceAlt: appTheme.surfaceAlt
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_textDim: appTheme.textDim
    property color pal_danger: appTheme.danger

    readonly property string pid: partData ? partData.id : ""
    readonly property string state: partData ? partData.state : "idle"
    readonly property bool isActive: !!live && live.active === true && live.active_part_id === pid
    readonly property bool overrun: isActive && !!live && live.overrun === true
    readonly property int plannedColumnWidth: 138
    readonly property int durationColumnWidth: 74
    readonly property int actionColumnWidth: 88

    radius: 12
    implicitHeight: 64
    color: isActive
           ? Qt.rgba(accent.r, accent.g, accent.b, 0.07)
           : pal_surface
    border.width: isActive ? 1.5 : 1
    border.color: isActive ? accent : pal_border

    Behavior on color { ColorAnimation { duration: 180 } }
    Behavior on border.color { ColorAnimation { duration: 180 } }

    function fmt(sec) {
        var n = Number(sec)
        if (!isFinite(n)) n = 0
        var sign = n < 0 ? "-" : ""
        var t = Math.round(Math.abs(n))
        var h = Math.floor(t / 3600)
        var m = Math.floor((t % 3600) / 60)
        var s = t % 60
        function pad(n) { return ("0" + n).slice(-2) }
        return h > 0 ? sign + h + ":" + pad(m) + ":" + pad(s)
                     : sign + pad(m) + ":" + pad(s)
    }
    function liveSeconds() {
        if (!isActive) return 0
        if (live.display_seconds !== undefined && live.display_seconds !== null)
            return live.display_seconds
        if (row.state === "stopped")
            return live.elapsed_seconds
        return (live.direction === "up") ? live.elapsed_seconds : live.remaining_seconds
    }
    function parseToSeconds(text) {
        var parts = ("" + text).split(":").map(function(x) { return parseInt(x, 10) || 0 })
        if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if (parts.length === 2) return parts[0] * 60 + parts[1]
        return parts[0] * 60   // a bare number is treated as minutes
    }

    // Colour-coded section indicator down the left edge.
    Rectangle {
        anchors.left: parent.left
        anchors.leftMargin: 1
        anchors.verticalCenter: parent.verticalCenter
        width: 3
        radius: 1.5
        height: row.isActive ? parent.height - 14 : parent.height - 26
        color: row.accent
        opacity: row.isActive ? 1.0 : 0.7
        Behavior on height { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 12
        spacing: 12

        // ── Number badge ──────────────────────────────────────────────────────
        Rectangle {
            visible: row.number > 0
            Layout.preferredWidth: 24; Layout.preferredHeight: 24
            radius: 7
            color: row.isActive
                   ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.18)
                   : row.pal_surfaceAlt
            border.width: 1
            border.color: row.isActive
                          ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.5)
                          : row.pal_border
            Text {
                anchors.centerIn: parent
                text: "" + row.number
                color: row.isActive ? row.accent : row.pal_textMuted
                font.pixelSize: 12
                font.weight: Font.Bold
            }
        }

        // ── Title + started time ──────────────────────────────────────────────
        ColumnLayout {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            clip: true
            spacing: 2
            Text {
                text: row.partData ? row.partData.title : ""
                color: row.pal_textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.minimumWidth: 0
            }
            Text {
                visible: !!row.partData && row.partData.startedLabel.length > 0
                text: qsTr("Started %1").arg(row.partData ? row.partData.startedLabel : "")
                color: row.pal_textMuted
                font.pixelSize: 11
            }
        }

        Item {
            Layout.preferredWidth: row.plannedColumnWidth
            Layout.minimumWidth: row.plannedColumnWidth
            Layout.maximumWidth: row.plannedColumnWidth
            Layout.fillHeight: true

            // Planned duration stays visible after stop, but edits are idle-only.
            TimerStepper {
                visible: !row.isActive
                anchors.centerIn: parent
                text: row.partData ? row.partData.plannedLabel : "00:00"
                accent: row.accent
                fieldWidth: 70
                editable: row.state === "idle"
                buttonsVisible: row.state === "idle"
                decTip: qsTr("−1 min"); incTip: qsTr("+1 min")
                onDecremented: timer.adjustPart(row.pid, -60)
                onIncremented: timer.adjustPart(row.pid, +60)
                onEdited: function(value) { timer.setPartSeconds(row.pid, row.parseToSeconds(value)) }
            }

            // Live countdown for the running part.
            TimerDigits {
                visible: row.isActive
                anchors.centerIn: parent
                width: parent.width
                text: row.fmt(row.liveSeconds())
                color: row.overrun ? row.pal_danger : row.accent
                font.pixelSize: 23
                font.weight: Font.Bold
                horizontalAlignment: Text.AlignHCenter
            }
        }

        Item {
            Layout.preferredWidth: row.durationColumnWidth
            Layout.minimumWidth: row.durationColumnWidth
            Layout.maximumWidth: row.durationColumnWidth
            Layout.fillHeight: true

            // Frozen duration appears after the hold window; during hold the
            // live display is still the single source of visual focus.
            Column {
                visible: row.state === "stopped" && !row.isActive
                anchors.centerIn: parent
                width: parent.width
                spacing: 0
                Text {
                    width: parent.width
                    text: qsTr("duration")
                    color: row.pal_textDim
                    font.pixelSize: 9
                    font.weight: Font.DemiBold
                    font.letterSpacing: 0.8
                    font.capitalization: Font.AllUppercase
                    horizontalAlignment: Text.AlignHCenter
                }
                Text {
                    width: parent.width
                    text: row.partData ? row.partData.resultLabel : ""
                    color: row.pal_textSecondary
                    font.pixelSize: 15
                    font.weight: Font.Bold
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }

        // ── Action button ─────────────────────────────────────────────────────
        TimerButton {
            Layout.preferredWidth: row.actionColumnWidth
            Layout.minimumWidth: row.actionColumnWidth
            Layout.maximumWidth: row.actionColumnWidth
            text: row.state === "running" ? qsTr("Stop")
                : row.state === "stopped" ? qsTr("Reset")
                : qsTr("Start")
            iconName: row.state === "running" ? "stop"
                : row.state === "stopped" ? "reset"
                : "play"
            iconSize: row.state === "stopped" ? 13 : 12
            variant: row.state === "running" ? "soft"
                : row.state === "stopped" ? "ghost"
                : "primary"
            accent: row.state === "running" ? row.pal_danger : row.accent
            onClicked: {
                if (row.state === "running") timer.stopPart(row.pid)
                else if (row.state === "stopped") timer.resetPart(row.pid)
                else timer.startPart(row.pid)
            }
        }
    }
}
