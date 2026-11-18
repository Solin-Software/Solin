// MeetingConsoleView.qml — the Live meeting console for meetings Solin hosts itself.
// Bound to the `meeting` context property (MeetingConsoleBridge).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    // Docked it is window chrome, so it matches the bottom bar (#StatusBar).
    color: docked ? appTheme.surfaceChrome : appTheme.bg

    readonly property bool docked: meeting ? meeting.docked : false

    readonly property var info: meeting ? meeting.info : ({})
    readonly property var snap: meeting ? meeting.snapshot : ({})
    readonly property bool componentsAvailable: meeting ? meeting.available : false
    readonly property string state_: info.state || "idle"
    readonly property bool live: state_ === "live"
    readonly property bool busy: state_ === "starting" || state_ === "ending"
    property string renameDevice: ""
    // Thin (docked as the main window's right panel, or a narrowed window): everything stacks.
    readonly property bool compact: width < 560

    // Stacked, the lobby and hands fit their rows (up to three, then they scroll), so
    // the attendee list keeps the remaining height.
    function stackedPanelHeight(rows, hasAdmitAll) {
        return 52 + Math.min(rows, 3) * 36 + (hasAdmitAll ? 38 : 0)
    }

    function statusText(key, value) {
        const labels = {
            idle: qsTr("Off"), starting: qsTr("Starting"), live: qsTr("Live"), reconnecting: qsTr("Reconnecting"),
            failed: qsTr("Failed"), off: qsTr("Off"), connecting: qsTr("Connecting"), online: qsTr("Online"),
            stopped: qsTr("Stopped"), running: qsTr("Running"), restarting: qsTr("Restarting")
        }
        return labels[value] || value || "—"
    }
    function statusColor(value) {
        if (value === "live" || value === "online" || value === "running") return "#2e9e5b"
        if (value === "failed") return appTheme.danger
        if (value === "starting" || value === "connecting" || value === "reconnecting" || value === "restarting") return "#c9a227"
        return appTheme.textMuted
    }

    // The meeting information card (links, password, meetings ahead, approved devices) folds
    // away: open between meetings, closed during one so the people lists keep the room.
    property bool infoExpanded: true
    onLiveChanged: infoExpanded = !live
    readonly property bool perMeeting: info.linkMode === "per_meeting"
    readonly property var upcoming: info.upcoming || []
    readonly property var devicesList: (live && snap.approvedDevices) ? snap.approvedDevices : (info.devices || [])

    function meetingDate(iso) {
        return iso ? Qt.locale().toString(new Date(iso), "ddd dd/MM HH:mm") : ""
    }
    function kindText(kind) {
        return kind === "midweek" ? qsTr("Midweek meeting") : kind === "weekend" ? qsTr("Weekend meeting")
             : kind === "unscheduled" ? qsTr("Unscheduled meeting") : qsTr("Special meeting")
    }
    function roleText(role) { return role === "moderator" ? qsTr("Moderator") : qsTr("Stage") }
    function countText(count) { return count === 0 ? qsTr("already counted") : String(count) }
    // The text to paste in the congregation's group: one line per meeting.
    function shareLine(m) {
        return qsTr("%1 · %2 · Password: %3").arg(meetingDate(m.startsAt)).arg(m.url).arg(m.password)
    }
    function shareMonth() {
        const lines = []
        for (let i = 0; i < upcoming.length; i++)
            if (!upcoming[i].cancelled && upcoming[i].url) lines.push(shareLine(upcoming[i]))
        return lines.join("\n")
    }
    function shareSame() {
        return qsTr("%1 · Password: %2").arg(info.invitationUrl || "").arg(info.password || "")
    }
    // One line for the folded card: the link in use, or the next meeting in per-meeting mode.
    function infoSummary() {
        if (info.invitationUrl && (!perMeeting || live || busy))
            return info.invitationUrl.replace(/^https?:\/\//, "") + "  ·  " + qsTr("password %1").arg(info.password || "")
        for (let i = 0; i < upcoming.length; i++)
            if (!upcoming[i].cancelled)
                return qsTr("Next: %1").arg(meetingDate(upcoming[i].startsAt)) + (upcoming[i].code ? "  ·  " + upcoming[i].code : "")
        return ""
    }

    component Badge: Rectangle {
        property string role
        implicitWidth: badgeText.implicitWidth + 12; implicitHeight: 18; radius: 9
        color: role === "moderator" ? appTheme.accent : "#2e9e5b"
        Text { id: badgeText; anchors.centerIn: parent; text: root.roleText(parent.role); color: "white"; font.pixelSize: 11; font.bold: true }
    }

    component ActionButton: Button {
        id: control
        property bool primary: false
        property bool danger: false
        implicitHeight: 32
        padding: 10
        font.pixelSize: 13
        contentItem: Text {
            text: control.text
            font: control.font
            color: control.primary || control.danger ? "white" : appTheme.textPrimary
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 7
            opacity: control.enabled ? 1 : 0.45
            color: control.danger ? appTheme.danger : control.primary ? appTheme.accent
                 : control.hovered ? appTheme.hover : appTheme.surfaceAlt
            border.color: control.primary || control.danger ? "transparent" : appTheme.border_
        }
    }

    // Toggles between a floating window and the main window's right panel.
    component DockButton: Button {
        id: dockControl
        implicitWidth: 32; implicitHeight: 32
        padding: 0
        hoverEnabled: true
        readonly property string description: meeting && meeting.docked
            ? qsTr("Attached to the right of the window. Click to detach.")
            : qsTr("Attach this panel to the right of the window.")
        Accessible.name: description
        ToolTip.visible: hovered
        ToolTip.text: description
        ToolTip.delay: 500
        contentItem: Item {
            Rectangle {
                anchors.centerIn: parent
                width: 16; height: 14; radius: 2; color: "transparent"; border.width: 1.5
                border.color: meeting && meeting.docked ? appTheme.accent : appTheme.textMuted
                Rectangle {
                    anchors { top: parent.top; bottom: parent.bottom; right: parent.right }
                    width: 6; radius: 2; color: parent.border.color
                }
            }
        }
        background: Rectangle { radius: 7; color: dockControl.hovered ? appTheme.hover : "transparent" }
    }

    component Chip: RowLayout {
        property string label
        property string value
        spacing: 6
        Rectangle { width: 8; height: 8; radius: 4; color: root.statusColor(parent.value) }
        Text { text: parent.label + ": " + root.statusText("", parent.value); color: appTheme.textSecondary; font.pixelSize: 12 }
    }

    component Panel: Rectangle {
        property string title
        property int count: -1
        default property alias content: body.data
        color: appTheme.surface
        radius: 10
        border.color: appTheme.border_
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 8
            Text {
                text: parent.parent.title + (parent.parent.count >= 0 ? "  (" + parent.parent.count + ")" : "")
                color: appTheme.textPrimary; font.pixelSize: 14; font.bold: true
            }
            Item { id: body; Layout.fillWidth: true; Layout.fillHeight: true }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.compact ? 10 : 16
        spacing: root.compact ? 8 : 12

        // ── Header ────────────────────────────────────────────────────────
        GridLayout {
            Layout.fillWidth: true
            columns: root.compact ? 2 : 3
            columnSpacing: 12; rowSpacing: 10
            ColumnLayout {
                Layout.row: 0; Layout.column: 0
                Layout.fillWidth: true
                spacing: 2
                Text { Layout.fillWidth: true; text: qsTr("Live meeting"); color: appTheme.textPrimary; font.pixelSize: root.compact ? 16 : 20; font.bold: true; elide: Text.ElideRight }
                Text {
                    Layout.fillWidth: true; wrapMode: Text.Wrap
                    text: !root.componentsAvailable ? qsTr("Meeting components are not installed.")
                        : root.live ? qsTr("Online: %1 people on %2 devices").arg(snap.people || 0).arg(snap.devices || 0)
                        : root.busy ? qsTr("Please wait…") : qsTr("Not started")
                    color: appTheme.textMuted; font.pixelSize: 13
                }
            }
            GridLayout {
                Layout.row: root.compact ? 1 : 0; Layout.column: root.compact ? 0 : 1
                Layout.columnSpan: root.compact ? 2 : 1
                Layout.fillWidth: root.compact
                columns: root.compact ? 1 : 3
                columnSpacing: 12; rowSpacing: 6
                ActionButton {
                    Layout.fillWidth: root.compact
                    visible: root.live
                    text: qsTr("Copy count")
                    onClicked: meeting.copyCount()
                }
                ActionButton {
                    Layout.fillWidth: root.compact
                    visible: root.live
                    text: snap.micsLocked === false ? qsTr("Lock microphones") : qsTr("Unlock microphones")
                    onClicked: meeting.lockMics(snap.micsLocked === false)
                }
                ActionButton {
                    Layout.fillWidth: root.compact
                    text: root.live || root.busy ? qsTr("End meeting") : qsTr("Open meeting now")
                    primary: !root.live && !root.busy
                    danger: root.live
                    enabled: root.componentsAvailable && !root.busy
                    onClicked: root.live ? meeting.end() : meeting.start()
                }
            }
            DockButton {
                Layout.row: 0; Layout.column: root.compact ? 1 : 2
                Layout.alignment: Qt.AlignTop
                visible: meeting && meeting.dockable
                onClicked: meeting.requestDock(!meeting.docked)
            }
        }

        // ── Official count (scheduled meetings, at 0.75 of the meeting) ──
        Rectangle {
            Layout.fillWidth: true
            visible: root.live && !!snap.officialCount
            color: appTheme.accent; radius: 10
            implicitHeight: countRow.implicitHeight + 20
            RowLayout {
                id: countRow
                anchors.fill: parent; anchors.margins: 10; spacing: 10
                Text {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; color: "white"; font.pixelSize: 14; font.bold: true
                    text: snap.officialCount ? qsTr("Attendance: %1 people on %2 devices")
                        .arg(snap.officialCount.people).arg(snap.officialCount.devices) : ""
                }
                ActionButton { text: qsTr("Copy"); onClicked: meeting.copy(String(snap.officialCount.people)) }
            }
        }

        // ── Stage devices and moderators asking to join (operator approval) ──
        Rectangle {
            Layout.fillWidth: true
            readonly property var requests: snap.staffRequests || []
            visible: root.live && requests.length > 0
            color: appTheme.surface; radius: 10; border.color: "#c9a227"; border.width: 2
            implicitHeight: requestColumn.implicitHeight + 20
            ColumnLayout {
                id: requestColumn
                anchors.fill: parent; anchors.margins: 10; spacing: 6
                Repeater {
                    model: parent.parent.requests
                    RowLayout {
                        Layout.fillWidth: true; spacing: 8
                        Text {
                            Layout.fillWidth: true; wrapMode: Text.Wrap; color: appTheme.textPrimary; font.pixelSize: 14
                            textFormat: Text.StyledText
                            text: (modelData.role === "moderator" ? qsTr("<b>Wants to be moderator</b>: %1")
                                                                   : qsTr("<b>Stage device waiting</b>: %1"))
                                  .arg(modelData.name.replace(/&/g, "&amp;").replace(/</g, "&lt;"))
                        }
                        ActionButton { text: qsTr("Allow"); primary: true; onClicked: meeting.approveStaff(modelData.device) }
                        ActionButton { text: qsTr("Refuse"); onClicked: meeting.refuseStaff(modelData.device) }
                    }
                }
            }
        }

        // ── Meeting information: links, password, meetings ahead, devices ──
        Rectangle {
            Layout.fillWidth: true
            visible: !!info.invitationUrl || root.perMeeting
            color: appTheme.surface; radius: 10; border.color: appTheme.border_
            implicitHeight: infoColumn.implicitHeight + 16
            ColumnLayout {
                id: infoColumn
                anchors.fill: parent; anchors.margins: 8; spacing: 8

                // The header folds the card; folded, it still shows the link and password.
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: infoHeader.implicitHeight + 8
                    radius: 7
                    color: infoHeaderArea.containsMouse ? appTheme.hover : "transparent"
                    MouseArea {
                        id: infoHeaderArea
                        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: root.infoExpanded = !root.infoExpanded
                    }
                    RowLayout {
                        id: infoHeader
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: 4; rightMargin: 0 }
                        spacing: 8
                        Text {
                            text: "\u25B8"; color: appTheme.textMuted; font.pixelSize: 13
                            rotation: root.infoExpanded ? 90 : 0
                            Behavior on rotation { NumberAnimation { duration: 120 } }
                        }
                        Text {
                            text: qsTr("Meeting information"); color: appTheme.textPrimary; font.pixelSize: 14; font.bold: true
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: !root.infoExpanded
                            text: (info.lowOnCodes || info.hubError ? "⚠ " : "") + root.infoSummary()
                            color: appTheme.textMuted; font.pixelSize: 12; elide: Text.ElideRight
                        }
                        Item { Layout.fillWidth: true; visible: root.infoExpanded }
                        ActionButton {
                            visible: !root.infoExpanded && !!info.invitationUrl && (!root.perMeeting || root.live || root.busy)
                            text: qsTr("Copy invitation")
                            onClicked: meeting.copy(root.shareSame())
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 4; Layout.rightMargin: 4; Layout.bottomMargin: 4
                    visible: root.infoExpanded
                    spacing: 8

                    Text {
                        Layout.fillWidth: true
                        visible: root.perMeeting && !root.live && !root.busy
                        text: qsTr("Each meeting has its own link and password.")
                        color: appTheme.textSecondary; font.pixelSize: 13; wrapMode: Text.Wrap
                    }
                    Repeater {
                        // The link in use (live) or the permanent one; per-meeting mode lists them below.
                        model: (!root.perMeeting || root.live || root.busy) && info.invitationUrl ? [
                            { label: qsTr("Invitation"), value: info.invitationUrl || "" },
                            { label: qsTr("Password"), value: info.password || "" },
                            { label: qsTr("Stage page"), value: info.stageUrl || "" },
                            { label: qsTr("Moderator"), value: info.moderatorUrl || "" }
                        ] : []
                        RowLayout {
                            Layout.fillWidth: true; spacing: 8
                            Text { text: modelData.label; color: appTheme.textMuted; font.pixelSize: 12; Layout.preferredWidth: root.compact ? 62 : 90; elide: Text.ElideRight }
                            TextField {
                                Layout.fillWidth: true; readOnly: true; selectByMouse: true
                                text: modelData.value; font.pixelSize: 13; color: appTheme.textPrimary
                                background: Rectangle { color: appTheme.surfaceAlt; radius: 6; border.color: appTheme.border_ }
                            }
                            ActionButton { text: qsTr("Copy"); enabled: !!modelData.value; onClicked: meeting.copy(modelData.value) }
                        }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        visible: !root.perMeeting && !!info.invitationUrl
                        ActionButton { text: qsTr("Copy invitation and password"); visible: !root.perMeeting; onClicked: meeting.copy(root.shareSame()) }
                        ActionButton { text: qsTr("Change password…"); visible: !root.perMeeting; onClicked: { passwordDialog.meetingId = ""; passwordField.text = info.password || ""; passwordDialog.open() } }
                        ActionButton { text: qsTr("New link"); visible: !root.perMeeting && !root.live && !root.busy; onClicked: { newLinkDialog.meetingId = ""; newLinkDialog.open() } }
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: !root.live && !root.busy && !root.perMeeting
                        text: info.linkScope === "hub"
                            ? qsTr("This link and password stay the same for every meeting, so you can share them in advance.")
                            : info.linkScope === "pending"
                            ? qsTr("Getting the public link from %1… Until then this link only works on this network.").arg(info.server || "")
                            : qsTr("Without a meeting server this link only works on this network, while a meeting is open, and it changes if this computer's network address changes.")
                        color: appTheme.textMuted; font.pixelSize: 12; wrapMode: Text.Wrap
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: !!info.lowOnCodes
                        text: "⚠ " + qsTr("Fewer than two weeks of meeting links are prepared. Open Solin while it is online to prepare more.")
                        color: "#c9a227"; font.pixelSize: 12; wrapMode: Text.Wrap
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: !!info.hubError
                        text: "⚠ " + qsTr("No public links for now: %1. Solin keeps trying; meanwhile links work on this network only.").arg(info.hubError || "")
                        color: "#c9a227"; font.pixelSize: 12; wrapMode: Text.Wrap
                    }

                    // Upcoming meetings: the schedule (both modes), with links in per-meeting mode.
                    Text {
                        visible: root.upcoming.length > 0
                        text: qsTr("Upcoming meetings")
                        color: appTheme.textPrimary; font.pixelSize: 13; font.bold: true
                    }
                    Repeater {
                        model: root.upcoming
                        RowLayout {
                            Layout.fillWidth: true; spacing: 8
                            opacity: modelData.cancelled ? 0.5 : 1
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 13
                                font.strikeout: modelData.cancelled
                                color: modelData.running ? "#2e9e5b" : appTheme.textPrimary
                                text: root.meetingDate(modelData.startsAt) + "  ·  " + root.kindText(modelData.kind)
                                      + (root.perMeeting && modelData.code ? "  ·  " + modelData.code + "  ·  " + qsTr("password %1").arg(modelData.password) : "")
                            }
                            ActionButton {
                                visible: root.perMeeting && !modelData.cancelled && !!modelData.url
                                text: qsTr("Copy"); onClicked: meeting.copy(root.shareLine(modelData))
                            }
                            ActionButton { text: "⋯"; onClicked: rowMenu.popup() }
                            Menu {
                                id: rowMenu
                                MenuItem {
                                    text: qsTr("Change password…"); visible: root.perMeeting; height: visible ? implicitHeight : 0
                                    onTriggered: { passwordDialog.meetingId = modelData.id; passwordField.text = modelData.password; passwordDialog.open() }
                                }
                                MenuItem {
                                    text: qsTr("New link"); visible: root.perMeeting && !modelData.running; height: visible ? implicitHeight : 0
                                    onTriggered: { newLinkDialog.meetingId = modelData.id; newLinkDialog.open() }
                                }
                                MenuItem {
                                    text: qsTr("Change time…"); enabled: !modelData.running
                                    onTriggered: { timeDialog.meetingId = modelData.id; dateField.text = Qt.locale().toString(new Date(modelData.startsAt), "dd/MM/yyyy"); timeField.text = Qt.locale().toString(new Date(modelData.startsAt), "HH:mm"); timeDialog.open() }
                                }
                                MenuItem {
                                    text: modelData.cancelled ? qsTr("Restore meeting") : qsTr("Cancel meeting"); enabled: !modelData.running
                                    visible: modelData.kind === "midweek" || modelData.kind === "weekend"; height: visible ? implicitHeight : 0
                                    onTriggered: meeting.setMeetingCancelled(modelData.id, !modelData.cancelled)
                                }
                                MenuItem {
                                    text: qsTr("Remove meeting"); enabled: !modelData.running
                                    visible: modelData.kind !== "midweek" && modelData.kind !== "weekend"; height: visible ? implicitHeight : 0
                                    onTriggered: meeting.removeMeeting(modelData.id)
                                }
                            }
                        }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        ActionButton { text: qsTr("Share the month"); visible: root.perMeeting && root.upcoming.length > 0; onClicked: meeting.copy(root.shareMonth()) }
                        ActionButton { text: qsTr("Renew all upcoming meetings"); visible: root.perMeeting && root.upcoming.length > 0; onClicked: { newLinkDialog.meetingId = "*"; newLinkDialog.open() } }
                        ActionButton { text: qsTr("Add meeting…"); onClicked: { timeDialog.meetingId = ""; dateField.text = ""; timeField.text = ""; timeDialog.open() } }
                    }

                    // Approved stage devices and moderators (remembered across meetings).
                    Text {
                        visible: root.devicesList.length > 0
                        text: qsTr("Approved devices")
                        color: appTheme.textPrimary; font.pixelSize: 13; font.bold: true
                    }
                    Repeater {
                        model: root.devicesList
                        RowLayout {
                            Layout.fillWidth: true; spacing: 8
                            Badge { role: modelData.role }
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 13; color: appTheme.textPrimary
                                text: modelData.name + (modelData.online ? "  ·  " + qsTr("online")
                                      : modelData.lastSeen ? "  ·  " + qsTr("last seen %1").arg(Qt.locale().toString(new Date(modelData.lastSeen * 1000), "dd/MM HH:mm")) : "")
                            }
                            ActionButton { text: qsTr("Remove"); onClicked: meeting.removeDevice(modelData.device) }
                        }
                    }
                }
            }
        }

        // ── Status of the meeting's parts ─────────────────────────────────
        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 4
            spacing: 6
            visible: root.live || root.busy || (info.warnings || []).length > 0
            Flow {
                Layout.fillWidth: true; spacing: 16
                visible: root.live || root.busy
                Chip { label: qsTr("Program"); value: info.output || "idle" }
                Chip { label: qsTr("Hub"); value: info.tunnel || "off" }
                Chip { label: qsTr("Media server"); value: info.galene || "stopped" }
                Chip { label: qsTr("Commenters' audio"); value: info.commenterAudio || "stopped" }
            }
            Text {
                // Which remote part-holder is in each scene video slot ("Remote participant N").
                readonly property var slots: info.participantVideo || ({})
                Layout.fillWidth: true
                visible: Object.keys(slots).length > 0
                text: Object.keys(slots).sort().map(function(slot) {
                    return qsTr("Remote participant %1: %2").arg(slot).arg(slots[slot] || qsTr("unnamed"))
                }).join("   ·   ")
                color: appTheme.textSecondary; font.pixelSize: 12; wrapMode: Text.Wrap
            }
            Repeater {
                model: info.warnings || []
                Text { Layout.fillWidth: true; text: "⚠ " + modelData; color: "#c9a227"; font.pixelSize: 12; wrapMode: Text.Wrap }
            }
        }
        Text {
            id: root_failed
            Layout.fillWidth: true
            visible: root.state_ === "failed"
            text: qsTr("The meeting could not start: %1").arg(info.error || "")
            color: appTheme.danger; wrapMode: Text.Wrap; font.pixelSize: 13
        }

        // ── Lobby · Hands · Attendees ─────────────────────────────────────
        // Stable list models (MeetingRowsModel): a snapshot updates rows in place,
        // so delegates, a pressed button and an open ⋯ menu survive it.
        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: root.compact ? 1 : 3
            columnSpacing: 12; rowSpacing: 8
            visible: root.live

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: !root.compact; Layout.preferredWidth: 1
                Layout.preferredHeight: root.compact ? root.stackedPanelHeight(count, count > 1) : -1
                title: qsTr("Lobby"); count: meeting ? meeting.lobby.count : 0
                ColumnLayout {
                    anchors.fill: parent; spacing: 6
                    ActionButton {
                        text: qsTr("Admit all"); primary: true
                        visible: meeting && meeting.lobby.count > 1
                        onClicked: meeting.admitAll()
                    }
                    ListView {
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 4
                        model: meeting ? meeting.lobby : null
                        delegate: RowLayout {
                            width: ListView.view.width; spacing: 6
                            Text { Layout.fillWidth: true; text: model.name + "  · " + root.countText(model.count); color: appTheme.textPrimary; elide: Text.ElideRight; font.pixelSize: 14 }
                            ActionButton { text: qsTr("Admit"); primary: true; onClicked: meeting.admit(model.device) }
                            ActionButton { text: qsTr("Remove"); onClicked: meeting.remove(model.device) }
                        }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: !root.compact; Layout.preferredWidth: 1
                Layout.preferredHeight: root.compact ? root.stackedPanelHeight(count, false) : -1
                title: qsTr("Raised hands"); count: meeting ? meeting.hands.count : 0
                ListView {
                    anchors.fill: parent; clip: true; spacing: 4
                    model: meeting ? meeting.hands : null
                    delegate: RowLayout {
                        width: ListView.view.width; spacing: 6
                        Text { text: (index + 1) + "."; color: appTheme.textMuted; font.pixelSize: 14 }
                        Text { Layout.fillWidth: true; text: model.name; color: appTheme.textPrimary; elide: Text.ElideRight; font.pixelSize: 14 }
                        ActionButton { text: qsTr("Call on"); primary: true; enabled: model.connected; onClicked: meeting.callOn(model.device) }
                        ActionButton { text: qsTr("Lower"); onClicked: meeting.lowerHand(model.device) }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1.3
                Layout.minimumHeight: root.compact ? 120 : 0
                title: qsTr("Attendees"); count: meeting ? meeting.attendees.count : 0
                Column {
                    id: staffOnline
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    spacing: 4
                    Repeater {
                        model: snap.staff || []
                        RowLayout {
                            width: staffOnline.width; spacing: 6
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 14; color: appTheme.textPrimary
                                text: modelData.name + (modelData.networkChanged ? "  · " + qsTr("new network") : "")
                            }
                            Badge { role: modelData.role }
                        }
                    }
                }
                ListView {
                    anchors { left: parent.left; right: parent.right; top: staffOnline.bottom; bottom: parent.bottom; topMargin: staffOnline.height > 0 ? 6 : 0 }
                    clip: true; spacing: 4
                    model: meeting ? meeting.attendees : null
                    delegate: ColumnLayout {
                        width: ListView.view.width; spacing: 2
                        RowLayout {
                            Layout.fillWidth: true; spacing: 6
                            Rectangle { width: 8; height: 8; radius: 4; color: model.live ? "#2e9e5b" : model.connected ? appTheme.textSecondary : appTheme.border_ }
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 14
                                text: model.name + "  · " + root.countText(model.count) + (model.partHolder ? "  · " + qsTr("part") : "") + (model.hand ? "  ✋" : "")
                                color: model.connected ? appTheme.textPrimary : appTheme.textMuted
                            }
                            ActionButton {
                                text: model.live ? qsTr("Done") : qsTr("Call on")
                                primary: !model.live; danger: model.live
                                enabled: model.connected || model.live
                                onClicked: model.live ? meeting.done(model.device) : meeting.callOn(model.device)
                            }
                            ActionButton { text: "⋯"; onClicked: menu.popup() }
                            Menu {
                                id: menu
                                MenuItem { text: model.partHolder ? qsTr("Not a part-holder") : qsTr("Part-holder (camera)"); onTriggered: meeting.setPartHolder(model.device, !model.partHolder) }
                                MenuItem { text: qsTr("Rename…"); onTriggered: { root.renameDevice = model.device; renameField.text = model.name; renameDialog.open() } }
                                MenuItem { text: qsTr("Remove from meeting"); onTriggered: meeting.remove(model.device) }
                            }
                        }
                    }
                }
            }
        }

        Item { Layout.fillHeight: true; visible: !root.live }
    }

    // Docked: the seam against the page content.
    Rectangle {
        objectName: "dockSeam"
        visible: root.docked
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        width: 1; color: appTheme.borderChrome
    }

    Dialog {
        id: newLinkDialog
        property string meetingId: ""
        modal: true
        anchors.centerIn: parent
        title: meetingId === "*" ? qsTr("Renew every upcoming link?") : qsTr("Replace the link?")
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: Math.min(380, root.width - 80)
            text: qsTr("Everyone will need the new link and password. Moderators and stage devices will need to be approved again.")
            color: appTheme.textPrimary; font.pixelSize: 13; wrapMode: Text.Wrap
        }
        onAccepted: meetingId === "*" ? meeting.renewUpcoming() : meeting.newLink(meetingId)
    }

    Dialog {
        id: passwordDialog
        property string meetingId: ""
        modal: true
        anchors.centerIn: parent
        title: qsTr("Meeting password")
        standardButtons: Dialog.Ok | Dialog.Cancel
        ColumnLayout {
            width: Math.min(340, root.width - 80); spacing: 8
            TextField { id: passwordField; Layout.fillWidth: true; selectByMouse: true; maximumLength: 32 }
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap; color: appTheme.textMuted; font.pixelSize: 12
                text: qsTr("4 to 32 characters, without spaces. The link stays the same, and approved devices are kept.")
            }
        }
        onAccepted: meeting.setPassword(passwordField.text.trim(), meetingId)
    }

    Dialog {
        id: timeDialog
        property string meetingId: ""
        modal: true
        anchors.centerIn: parent
        title: meetingId ? qsTr("Change the meeting time") : qsTr("Add a meeting")
        standardButtons: Dialog.Ok | Dialog.Cancel
        GridLayout {
            width: Math.min(340, root.width - 80); columns: 2; columnSpacing: 8; rowSpacing: 8
            Text { text: qsTr("Date"); color: appTheme.textSecondary; font.pixelSize: 13 }
            TextField { id: dateField; Layout.fillWidth: true; placeholderText: qsTr("DD/MM/YYYY"); selectByMouse: true }
            Text { text: qsTr("Time"); color: appTheme.textSecondary; font.pixelSize: 13 }
            TextField { id: timeField; Layout.fillWidth: true; placeholderText: "HH:MM"; selectByMouse: true }
        }
        onAccepted: timeDialog.meetingId ? meeting.moveMeeting(timeDialog.meetingId, dateField.text, timeField.text)
                                         : meeting.addMeeting(dateField.text, timeField.text)
    }

    Dialog {
        id: renameDialog
        modal: true
        anchors.centerIn: parent
        title: qsTr("Rename attendee")
        standardButtons: Dialog.Ok | Dialog.Cancel
        TextField { id: renameField; width: Math.min(320, root.width - 80); selectByMouse: true }
        onAccepted: if (renameField.text.trim().length > 0) meeting.rename(root.renameDevice, renameField.text.trim())
    }
}
