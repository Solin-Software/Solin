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
        anchors.margins: 16
        spacing: 12

        // ── Header ────────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text { text: qsTr("Live meeting"); color: appTheme.textPrimary; font.pixelSize: 20; font.bold: true }
                Text {
                    text: !root.componentsAvailable ? qsTr("Meeting components are not installed.")
                        : root.live ? qsTr("%1 connected · %2 attended").arg(snap.connected || 0).arg(snap.attendance || 0)
                        : root.busy ? qsTr("Please wait…") : qsTr("Not started")
                    color: appTheme.textMuted; font.pixelSize: 13
                }
            }
            ActionButton {
                visible: root.live
                text: qsTr("Copy attendance")
                onClicked: meeting.copyAttendance()
            }
            ActionButton {
                visible: root.live
                text: snap.micsLocked === false ? qsTr("Lock microphones") : qsTr("Unlock microphones")
                onClicked: meeting.lockMics(snap.micsLocked === false)
            }
            ActionButton {
                text: root.live || root.busy ? qsTr("End meeting") : qsTr("Start meeting")
                primary: !root.live && !root.busy
                danger: root.live
                enabled: root.componentsAvailable && !root.busy
                onClicked: root.live ? meeting.end() : meeting.start()
            }
        }

        // ── Links and status ──────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            visible: root.live || root.busy || root_failed.visible
            color: appTheme.surface; radius: 10; border.color: appTheme.border_
            implicitHeight: linkColumn.implicitHeight + 24
            ColumnLayout {
                id: linkColumn
                anchors.fill: parent; anchors.margins: 12; spacing: 8
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Text { text: qsTr("Invitation"); color: appTheme.textMuted; font.pixelSize: 12; Layout.preferredWidth: 90 }
                    TextField {
                        Layout.fillWidth: true; readOnly: true; selectByMouse: true
                        text: info.invitationUrl || ""; font.pixelSize: 13; color: appTheme.textPrimary
                        background: Rectangle { color: appTheme.surfaceAlt; radius: 6; border.color: appTheme.border_ }
                    }
                    ActionButton { text: qsTr("Copy"); enabled: !!info.invitationUrl; onClicked: meeting.copy(info.invitationUrl) }
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Text { text: qsTr("Stage page"); color: appTheme.textMuted; font.pixelSize: 12; Layout.preferredWidth: 90 }
                    TextField {
                        Layout.fillWidth: true; readOnly: true; selectByMouse: true; echoMode: TextInput.Normal
                        text: info.stageUrl || ""; font.pixelSize: 13; color: appTheme.textPrimary
                        background: Rectangle { color: appTheme.surfaceAlt; radius: 6; border.color: appTheme.border_ }
                    }
                    ActionButton { text: qsTr("Copy"); enabled: !!info.stageUrl; onClicked: meeting.copy(info.stageUrl) }
                }
                Flow {
                    Layout.fillWidth: true; spacing: 16
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
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 12
            visible: root.live

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                title: qsTr("Lobby"); count: (snap.lobby || []).length
                ColumnLayout {
                    anchors.fill: parent; spacing: 6
                    ActionButton {
                        text: qsTr("Admit all"); primary: true
                        visible: (snap.lobby || []).length > 1
                        onClicked: meeting.admitAll()
                    }
                    ListView {
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 4
                        model: snap.lobby || []
                        delegate: RowLayout {
                            width: ListView.view.width; spacing: 6
                            Text { Layout.fillWidth: true; text: modelData.name; color: appTheme.textPrimary; elide: Text.ElideRight; font.pixelSize: 14 }
                            ActionButton { text: qsTr("Admit"); primary: true; onClicked: meeting.admit(modelData.device) }
                            ActionButton { text: qsTr("Remove"); onClicked: meeting.remove(modelData.device) }
                        }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                title: qsTr("Raised hands"); count: (snap.hands || []).length
                ListView {
                    anchors.fill: parent; clip: true; spacing: 4
                    model: snap.hands || []
                    delegate: RowLayout {
                        width: ListView.view.width; spacing: 6
                        Text { text: (index + 1) + "."; color: appTheme.textMuted; font.pixelSize: 14 }
                        Text { Layout.fillWidth: true; text: modelData.name; color: appTheme.textPrimary; elide: Text.ElideRight; font.pixelSize: 14 }
                        ActionButton { text: qsTr("Call on"); primary: true; enabled: modelData.connected; onClicked: meeting.callOn(modelData.device) }
                        ActionButton { text: qsTr("Lower"); onClicked: meeting.lowerHand(modelData.device) }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1.3
                title: qsTr("Attendees"); count: (snap.attendees || []).length
                ListView {
                    anchors.fill: parent; clip: true; spacing: 4
                    model: snap.attendees || []
                    delegate: ColumnLayout {
                        width: ListView.view.width; spacing: 2
                        RowLayout {
                            Layout.fillWidth: true; spacing: 6
                            Rectangle { width: 8; height: 8; radius: 4; color: modelData.live ? "#2e9e5b" : modelData.connected ? appTheme.textSecondary : appTheme.border_ }
                            Text {
                                Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 14
                                text: modelData.name + (modelData.partHolder ? "  · " + qsTr("part") : "") + (modelData.hand ? "  ✋" : "")
                                color: modelData.connected ? appTheme.textPrimary : appTheme.textMuted
                            }
                            ActionButton {
                                text: modelData.live ? qsTr("Done") : qsTr("Call on")
                                primary: !modelData.live; danger: modelData.live
                                enabled: modelData.connected || modelData.live
                                onClicked: modelData.live ? meeting.done(modelData.device) : meeting.callOn(modelData.device)
                            }
                            ActionButton { text: "⋯"; onClicked: menu.popup() }
                            Menu {
                                id: menu
                                MenuItem { text: modelData.partHolder ? qsTr("Not a part-holder") : qsTr("Part-holder (camera)"); onTriggered: meeting.setPartHolder(modelData.device, !modelData.partHolder) }
                                MenuItem { text: qsTr("Rename…"); onTriggered: { root.renameDevice = modelData.device; renameField.text = modelData.name; renameDialog.open() } }
                                MenuItem { text: qsTr("Remove from meeting"); onTriggered: meeting.remove(modelData.device) }
                            }
                        }
                    }
                }
            }
        }

        Item { Layout.fillHeight: true; visible: !root.live }
    }

    Dialog {
        id: renameDialog
        modal: true
        anchors.centerIn: parent
        title: qsTr("Rename attendee")
        standardButtons: Dialog.Ok | Dialog.Cancel
        TextField { id: renameField; width: 320; selectByMouse: true }
        onAccepted: if (renameField.text.trim().length > 0) meeting.rename(root.renameDevice, renameField.text.trim())
    }
}
