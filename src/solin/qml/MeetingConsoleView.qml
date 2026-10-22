// MeetingConsoleView.qml — operator console for a Solin-hosted meeting.
// Bound to the `meetingConsole` context property (MeetingConsoleBridge).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: appTheme.bg

    readonly property int sideMargin: 20
    readonly property bool narrow: width < 760
    property string renameIdentity: ""
    property string removeIdentity: ""
    property string removeName: ""

    function statusText() {
        if (meetingConsole.failed)
            return qsTr("Meeting stopped working")
        if (meetingConsole.state === "starting")
            return qsTr("Starting…")
        if (meetingConsole.open)
            return meetingConsole.connected ? qsTr("Live") : qsTr("Hall reconnecting…")
        return qsTr("Not started")
    }

    function statusColor() {
        if (meetingConsole.failed)
            return appTheme.danger
        if (meetingConsole.open && meetingConsole.connected)
            return appTheme.success
        if (meetingConsole.open || meetingConsole.state === "starting")
            return appTheme.warning
        return appTheme.textMuted
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header ────────────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 70
            Layout.leftMargin: root.sideMargin
            Layout.rightMargin: root.sideMargin
            spacing: 14

            ColumnLayout {
                spacing: 2
                Text {
                    text: qsTr("Meeting")
                    color: appTheme.textPrimary
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                }
                RowLayout {
                    spacing: 6
                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: root.statusColor()
                    }
                    Text {
                        text: root.statusText()
                        color: appTheme.textSecondary
                        font.pixelSize: 12
                    }
                    Text {
                        visible: meetingConsole.open
                        text: "·  " + qsTr("%n watching remotely", "", meetingConsole.attendance)
                        color: appTheme.textSecondary
                        font.pixelSize: 12
                    }
                }
            }

            Item { Layout.fillWidth: true }

            SettingsButton {
                visible: meetingConsole.open
                enabled: !meetingConsole.busy
                text: meetingConsole.locked ? qsTr("Allow unmuting") : qsTr("Lock microphones")
                onClicked: meetingConsole.locked ? meetingConsole.unlock() : meetingConsole.lock()
            }
            SettingsButton {
                visible: meetingConsole.hosted && meetingConsole.available
                primary: !meetingConsole.open && meetingConsole.state !== "starting"
                destructive: meetingConsole.open
                text: meetingConsole.open || meetingConsole.state === "starting" || meetingConsole.failed
                      ? qsTr("End meeting") : qsTr("Start meeting")
                onClicked: (meetingConsole.open || meetingConsole.state === "starting" || meetingConsole.failed)
                           ? meetingConsole.stopMeeting() : meetingConsole.startMeeting()
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: appTheme.border_ }

        // ── Not hosted here / not available ─────────────────────────────────
        Item {
            visible: !meetingConsole.hosted || !meetingConsole.available
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                anchors.centerIn: parent
                width: Math.min(parent.width - 2 * root.sideMargin, 460)
                spacing: 14
                Text {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    color: appTheme.textPrimary
                    font.pixelSize: 15
                    text: !meetingConsole.hosted
                          ? qsTr("Meetings are set to use Zoom. To host the meeting from this computer, choose Solin in Settings.")
                          : qsTr("The meeting server is not included in this installation.")
                }
                SettingsButton {
                    visible: !meetingConsole.hosted
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("Open settings")
                    onClicked: meetingConsole.openSettings()
                }
            }
        }

        // ── Idle ─────────────────────────────────────────────────────────────
        Item {
            visible: meetingConsole.hosted && meetingConsole.available && !meetingConsole.open
            Layout.fillWidth: true
            Layout.fillHeight: true
            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width - 2 * root.sideMargin, 460)
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                color: appTheme.textSecondary
                font.pixelSize: 14
                text: meetingConsole.failed
                      ? qsTr("The meeting stopped working. End it and start it again.")
                      : qsTr("Start the meeting to let attendees join. They wait in the lobby until you admit them.")
            }
        }

        // ── Live ─────────────────────────────────────────────────────────────
        GridLayout {
            visible: meetingConsole.hosted && meetingConsole.open
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: root.sideMargin
            columns: root.narrow ? 1 : 2
            columnSpacing: 16
            rowSpacing: 16

            // Raised hands
            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                title: qsTr("Raised hands")
                count: meetingConsole.hands.length
                emptyText: qsTr("No hands raised")

                Repeater {
                    model: meetingConsole.hands
                    delegate: Row_ {
                        required property var modelData
                        leading: String(modelData.handPosition)
                        name: modelData.name
                        detail: modelData.status === "speaking" ? qsTr("Speaking") : ""
                        live: modelData.microphoneLive
                        SettingsButton {
                            visible: modelData.status !== "speaking" && meetingConsole.locked
                            primary: true
                            text: qsTr("Call on")
                            onClicked: meetingConsole.callOn(modelData.identity)
                        }
                        SettingsButton {
                            visible: modelData.status === "speaking"
                            text: qsTr("Done")
                            onClicked: meetingConsole.finish(modelData.identity)
                        }
                        SettingsButton {
                            visible: modelData.status !== "speaking"
                            text: qsTr("Lower")
                            onClicked: meetingConsole.lowerHand(modelData.identity)
                        }
                    }
                }
            }

            // Lobby and participants
            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                title: qsTr("Attendees")
                count: meetingConsole.participants.length - meetingConsole.lobbyCount
                emptyText: qsTr("Nobody has joined yet")
                headerAction: SettingsButton {
                    visible: meetingConsole.lobbyCount > 1
                    text: qsTr("Admit all (%1)").arg(meetingConsole.lobbyCount)
                    onClicked: meetingConsole.admitAll()
                }

                Repeater {
                    model: meetingConsole.participants
                    delegate: Row_ {
                        required property var modelData
                        readonly property bool waiting: modelData.status === "lobby"
                        leading: waiting ? "…" : "×" + modelData.attendance
                        name: modelData.name
                        detail: waiting ? qsTr("Waiting in the lobby")
                                        : modelData.status === "speaking" ? qsTr("Speaking") : ""
                        live: modelData.microphoneLive
                        highlighted: waiting
                        SettingsButton {
                            visible: waiting
                            primary: true
                            text: qsTr("Admit")
                            onClicked: meetingConsole.admit(modelData.identity)
                        }
                        SettingsButton {
                            text: "⋯"
                            Accessible.name: qsTr("More actions for %1").arg(modelData.name)
                            onClicked: menu.popup()
                            Menu {
                                id: menu
                                MenuItem {
                                    text: qsTr("Rename…")
                                    onTriggered: {
                                        root.renameIdentity = modelData.identity
                                        renameField.text = modelData.name
                                        renameDialog.open()
                                    }
                                }
                                MenuItem {
                                    visible: !waiting
                                    height: visible ? implicitHeight : 0
                                    text: qsTr("Back to the lobby")
                                    onTriggered: meetingConsole.toLobby(modelData.identity)
                                }
                                MenuItem {
                                    text: qsTr("Remove from meeting…")
                                    onTriggered: {
                                        root.removeIdentity = modelData.identity
                                        root.removeName = modelData.name
                                        removeDialog.open()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Dialogs ───────────────────────────────────────────────────────────────
    Dialog {
        id: renameDialog
        anchors.centerIn: parent
        modal: true
        title: qsTr("Rename attendee")
        standardButtons: Dialog.Ok | Dialog.Cancel
        AppTextField {
            id: renameField
            width: 280
            onAccepted: renameDialog.accept()
        }
        onAccepted: meetingConsole.rename(root.renameIdentity, renameField.text)
    }

    Dialog {
        id: removeDialog
        anchors.centerIn: parent
        modal: true
        title: qsTr("Remove from meeting")
        standardButtons: Dialog.Yes | Dialog.No
        Text {
            width: 320
            wrapMode: Text.WordWrap
            color: appTheme.textPrimary
            text: qsTr("Remove %1? They will not be able to join this meeting again.").arg(root.removeName)
        }
        onAccepted: meetingConsole.remove(root.removeIdentity)
    }

    // ── Local components ──────────────────────────────────────────────────────
    component Panel: Rectangle {
        id: panel
        property string title
        property int count: 0
        property string emptyText
        property alias headerAction: actionSlot.data
        default property alias rows: rowsColumn.data
        radius: 10
        color: appTheme.surface
        border.color: appTheme.border_

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 10
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                Text {
                    text: panel.title + (panel.count > 0 ? "  " + panel.count : "")
                    color: appTheme.textPrimary
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Item { Layout.fillWidth: true }
                Item {
                    id: actionSlot
                    Layout.preferredHeight: 36
                    implicitWidth: childrenRect.width
                }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ColumnLayout {
                    id: rowsColumn
                    width: parent ? parent.width : 0
                    spacing: 6
                }
            }
            Text {
                visible: rowsColumn.children.length <= 1
                text: panel.emptyText
                color: appTheme.textMuted
                font.pixelSize: 13
            }
        }
    }

    component Row_: Rectangle {
        id: row
        property string leading
        property string name
        property string detail
        property bool live: false
        property bool highlighted: false
        default property alias actions: actionsRow.data
        Layout.fillWidth: true
        implicitHeight: 56
        radius: 8
        color: highlighted ? appTheme.accentTint : appTheme.surfaceAlt

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 8
            spacing: 10
            Text {
                Layout.preferredWidth: 32
                text: row.leading
                color: appTheme.textSecondary
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text {
                    Layout.fillWidth: true
                    text: row.name
                    color: appTheme.textPrimary
                    font.pixelSize: 14
                    elide: Text.ElideRight
                }
                Text {
                    visible: row.detail.length > 0
                    text: row.detail
                    color: appTheme.textSecondary
                    font.pixelSize: 12
                }
            }
            Rectangle {
                visible: row.live
                width: 10; height: 10; radius: 5
                color: appTheme.success
                Accessible.name: qsTr("Microphone on")
            }
            RowLayout {
                id: actionsRow
                spacing: 6
            }
        }
    }
}
