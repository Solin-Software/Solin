// MeetingConsoleView.qml — the Live meeting console for meetings Solin hosts itself.
// Bound to the `meeting` context property (MeetingConsoleBridge).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: appTheme.bg

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
                        : root.live ? qsTr("%1 connected · %2 attended").arg(snap.connected || 0).arg(snap.attendance || 0)
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
                    text: qsTr("Copy attendance")
                    onClicked: meeting.copyAttendance()
                }
                ActionButton {
                    Layout.fillWidth: root.compact
                    visible: root.live
                    text: snap.micsLocked === false ? qsTr("Lock microphones") : qsTr("Unlock microphones")
                    onClicked: meeting.lockMics(snap.micsLocked === false)
                }
                ActionButton {
                    Layout.fillWidth: root.compact
                    text: root.live || root.busy ? qsTr("End meeting") : qsTr("Start meeting")
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

        // ── Links and status ──────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            visible: !!info.invitationUrl
            color: appTheme.surface; radius: 10; border.color: appTheme.border_
            implicitHeight: linkColumn.implicitHeight + 24
            ColumnLayout {
                id: linkColumn
                anchors.fill: parent; anchors.margins: 12; spacing: 8
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Text { text: qsTr("Invitation"); color: appTheme.textMuted; font.pixelSize: 12; Layout.preferredWidth: root.compact ? 62 : 90; elide: Text.ElideRight }
                    TextField {
                        Layout.fillWidth: true; readOnly: true; selectByMouse: true
                        text: info.invitationUrl || ""; font.pixelSize: 13; color: appTheme.textPrimary
                        background: Rectangle { color: appTheme.surfaceAlt; radius: 6; border.color: appTheme.border_ }
                    }
                    ActionButton { text: qsTr("Copy"); enabled: !!info.invitationUrl; onClicked: meeting.copy(info.invitationUrl) }
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Text { text: qsTr("Stage page"); color: appTheme.textMuted; font.pixelSize: 12; Layout.preferredWidth: root.compact ? 62 : 90; elide: Text.ElideRight }
                    TextField {
                        Layout.fillWidth: true; readOnly: true; selectByMouse: true; echoMode: TextInput.Normal
                        text: info.stageUrl || ""; font.pixelSize: 13; color: appTheme.textPrimary
                        background: Rectangle { color: appTheme.surfaceAlt; radius: 6; border.color: appTheme.border_ }
                    }
                    ActionButton { text: qsTr("Copy"); enabled: !!info.stageUrl; onClicked: meeting.copy(info.stageUrl) }
                }
                // Before a meeting: where this permanent link works, and replacing it.
                Text {
                    Layout.fillWidth: true
                    visible: !root.live && !root.busy
                    text: info.linkScope === "hub"
                        ? qsTr("This link stays the same for every meeting, so you can share it in advance. Anyone who opens it before you start sees a page that keeps retrying.")
                        : qsTr("This link stays the same for every meeting, but without a hub it only works on this network, while a meeting is running, and it changes if this computer's network address changes.")
                    color: appTheme.textMuted; font.pixelSize: 12; wrapMode: Text.Wrap
                }
                ActionButton {
                    visible: !root.live && !root.busy
                    text: qsTr("New link")
                    onClicked: newLinkDialog.open()
                }
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
                            Text { Layout.fillWidth: true; text: model.name; color: appTheme.textPrimary; elide: Text.ElideRight; font.pixelSize: 14 }
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
                ListView {
                    anchors.fill: parent; clip: true; spacing: 4
                    model: meeting ? meeting.attendees : null
                    delegate: ColumnLayout {
                        width: ListView.view.width; spacing: 2
                        RowLayout {
                            Layout.fillWidth: true; spacing: 6
                            Rectangle { width: 8; height: 8; radius: 4; color: model.live ? "#2e9e5b" : model.connected ? appTheme.textSecondary : appTheme.border_ }
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 14
                                text: model.name + (model.partHolder ? "  · " + qsTr("part") : "") + (model.hand ? "  ✋" : "")
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
        visible: meeting && meeting.docked
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        width: 1; color: appTheme.border_
    }

    Dialog {
        id: newLinkDialog
        modal: true
        anchors.centerIn: parent
        title: qsTr("Replace the link?")
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: Math.min(380, root.width - 80)
            text: qsTr("The link and stage page you already shared will stop working.")
            color: appTheme.textPrimary; font.pixelSize: 13; wrapMode: Text.Wrap
        }
        onAccepted: meeting.newLink()
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
