import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Button {
    id: root
    objectName: "meetingFolderControl"

    property var bridge: null
    property color accent: appTheme.accent
    readonly property string folderState: bridge ? bridge.folderState : "checking"
    readonly property bool folderPresent: folderState === "present"
    readonly property string folderPath: bridge ? bridge.folderPath : ""
    readonly property bool syncEnabled: bridge ? bridge.syncEnabled : false
    readonly property bool pending: bridge ? bridge.syncPending : false
    readonly property bool busy: bridge ? bridge.syncBusy : false
    readonly property bool mutable: bridge && bridge.folderManagementAvailable
                                    && !busy && !pending
                                    && (folderState === "present" || folderState === "missing")
    readonly property string folderLabel: {
        switch (folderState) {
        case "present": return qsTranslate("MeetingSync", "Folder present")
        case "missing": return qsTranslate("MeetingSync", "Folder missing")
        case "unavailable": return qsTranslate("MeetingSync", "Folder unavailable")
        default: return qsTranslate("MeetingSync", "Checking folder…")
        }
    }
    readonly property string syncLabel: syncEnabled
        ? qsTranslate("MeetingSync", "Sync enabled")
        : qsTranslate("MeetingSync", "Sync disabled")
    readonly property string operationLabel: {
        if (busy && bridge.syncBusyMessage.length > 0)
            return bridge.syncBusyMessage
        if (pending || busy)
            return syncEnabled ? qsTranslate("MeetingSync", "Synchronizing…")
                               : qsTranslate("MeetingSync", "Finishing…")
        return ""
    }
    readonly property string summary: folderLabel + " · " + syncLabel
    readonly property string toolTipText: text + "\n" + summary
        + (operationLabel.length ? "\n" + operationLabel : "")

    text: qsTranslate("MeetingSync", "Meeting folder")
    implicitWidth: 32
    implicitHeight: 32
    padding: 0
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    Accessible.name: text
    Accessible.description: summary + (operationLabel.length ? " · " + operationLabel : "")
    AppPointerCursor {}

    AppToolTip {
        visible: (root.hovered || root.visualFocus) && !folderPopup.visible
        text: root.toolTipText
    }

    onClicked: folderPopup.visible ? folderPopup.close() : folderPopup.open()
    Keys.onDownPressed: folderPopup.open()
    onVisibleChanged: if (!visible) folderPopup.close()
    onBridgeChanged: folderPopup.close()

    background: Rectangle {
        radius: 7
        color: root.hovered || folderPopup.visible
            ? (root.folderPresent ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.26) : appTheme.hover)
            : (root.folderPresent ? appTheme.accentTint : "transparent")
        border.width: root.visualFocus ? 2 : 1
        border.color: root.visualFocus ? root.accent
            : root.folderPresent
                ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b,
                          root.hovered || folderPopup.visible ? 0.55 : 0.35)
                : root.hovered || folderPopup.visible ? appTheme.borderStrong : appTheme.border_
    }

    contentItem: Item {
        Image {
            objectName: "meetingFolderIcon"
            anchors.centerIn: parent
            width: 16
            height: 16
            source: "image://playlisticons/" + (root.syncEnabled ? "folder_link" : "folder") + "/16/"
                + String(root.folderPresent ? appTheme.success : appTheme.textMuted).replace("#", "")
        }
    }

    Popup {
        id: folderPopup
        objectName: "meetingFolderPopup"
        parent: root
        readonly property Item viewport: Overlay.overlay
        readonly property point anchorPosition: viewport
            ? root.parent.mapToItem(viewport, root.x, root.y) : Qt.point(0, 0)
        width: Math.min(320, viewport ? viewport.width - 24 : 320)
        height: Math.min(implicitHeight, viewport ? viewport.height - 24 : implicitHeight)
        x: viewport ? Math.max(12 - anchorPosition.x,
                               Math.min(root.width - width, viewport.width - width - 12 - anchorPosition.x)) : 0
        y: viewport ? Math.max(12 - anchorPosition.y,
                               Math.min(root.height + 6, viewport.height - height - 12 - anchorPosition.y)) : root.height + 6
        padding: 12
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent
        onOpened: {
            if (createFolder.visible && createFolder.enabled)
                createFolder.forceActiveFocus(Qt.PopupFocusReason)
            else if (openFolder.visible && openFolder.enabled)
                openFolder.forceActiveFocus(Qt.PopupFocusReason)
            else
                folderPathField.forceActiveFocus(Qt.PopupFocusReason)
        }
        onClosed: if (root.visible) root.forceActiveFocus(Qt.PopupFocusReason)

        background: Rectangle {
            radius: 10
            color: appTheme.surface
            border.width: 1
            border.color: appTheme.borderStrong
        }

        contentItem: Flickable {
            id: panelViewport
            objectName: "meetingFolderViewport"
            implicitHeight: panel.implicitHeight
            contentWidth: width
            contentHeight: panel.implicitHeight
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            ScrollBar.vertical: AppScrollBar {
                id: panelScroll
                visible: size < 1
            }

            function reveal(control) {
                var top = control.mapToItem(panel, 0, 0).y
                if (top < contentY)
                    contentY = top
                else if (top + control.height > contentY + height)
                    contentY = Math.min(contentHeight - height, top + control.height - height)
            }

            ColumnLayout {
                id: panel
                width: parent.width - (panelScroll.visible ? panelScroll.width + 4 : 0)
                spacing: 8

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: root.text
                        color: appTheme.textPrimary
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        wrapMode: Text.Wrap
                    }
                    Button {
                        id: helpLink
                        objectName: "meetingFolderHelp"
                        text: qsTranslate("MeetingSync", "Help")
                        implicitWidth: contentItem.implicitWidth + leftPadding + rightPadding
                        implicitHeight: Math.max(24, contentItem.implicitHeight + topPadding + bottomPadding)
                        padding: 4
                        focusPolicy: Qt.StrongFocus
                        hoverEnabled: true
                        Accessible.role: Accessible.Link
                        Accessible.name: text
                        onActiveFocusChanged: if (activeFocus) panelViewport.reveal(this)
                        onClicked: Qt.openUrlExternally("https://solinav.vercel.app/guide/#linked-folder-meeting-sync")
                        Keys.onReturnPressed: clicked()
                        Keys.onEnterPressed: clicked()
                        AppPointerCursor {}
                        contentItem: Text {
                            text: helpLink.text
                            color: appTheme.accent
                            font.pixelSize: 11
                            font.underline: true
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 4
                            color: "transparent"
                            border.width: helpLink.visualFocus ? 2 : 0
                            border.color: appTheme.accent
                        }
                    }
                }
                TextArea {
                    id: folderPathField
                    objectName: "meetingFolderPath"
                    Layout.fillWidth: true
                    text: root.folderPath
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    padding: 4
                    color: appTheme.textSecondary
                    selectionColor: appTheme.accentSelection
                    selectedTextColor: appTheme.textOnAccent
                    font.pixelSize: 11
                    Accessible.name: qsTranslate("MeetingSync", "Folder path")
                    onActiveFocusChanged: if (activeFocus) panelViewport.reveal(this)
                    background: Rectangle {
                        radius: 4
                        color: "transparent"
                        border.width: folderPathField.activeFocus ? 2 : 0
                        border.color: appTheme.accent
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.folderState === "unavailable"
                    text: qsTranslate("MeetingSync", "The linked folder is unavailable. Check its location and access permissions.")
                    color: appTheme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
                FolderAction {
                    id: createFolder
                    objectName: "meetingFolderCreate"
                    visible: root.folderState === "missing"
                    enabled: root.mutable
                    text: qsTranslate("MeetingSync", "Create folder")
                    onClicked: if (root.mutable) root.bridge.createMeetingFolder()
                }
                FolderAction {
                    id: openFolder
                    objectName: "meetingFolderOpen"
                    visible: root.folderState === "present"
                    text: qsTranslate("MeetingSync", "Open folder")
                    onClicked: if (root.bridge && root.folderState === "present") root.bridge.openMeetingFolder()
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 1
                    color: appTheme.border_
                }
                Switch {
                    id: syncSwitch
                    objectName: "meetingFolderSync"
                    Layout.fillWidth: true
                    implicitHeight: Math.max(40, contentItem.implicitHeight + 12)
                    leftPadding: 0
                    rightPadding: 56
                    enabled: root.mutable
                    text: qsTranslate("MeetingSync", "Meeting synchronization")
                    focusPolicy: Qt.StrongFocus
                    Accessible.name: text
                    Accessible.description: syncHelper.text + " " + root.syncLabel
                    onActiveFocusChanged: if (activeFocus) panelViewport.reveal(this)
                    AppPointerCursor {}
                    Binding { target: syncSwitch; property: "checked"; value: root.syncEnabled }
                    onClicked: {
                        if (root.mutable)
                            root.bridge.setSyncEnabled(!root.syncEnabled)
                        checked = root.syncEnabled
                    }
                    indicator: Rectangle {
                        x: syncSwitch.width - width
                        y: (syncSwitch.height - height) / 2
                        width: 46
                        height: 26
                        radius: 13
                        color: syncSwitch.checked ? appTheme.accent : appTheme.secondaryControl
                        border.width: syncSwitch.visualFocus ? 2 : 1
                        border.color: syncSwitch.visualFocus ? appTheme.textPrimary : appTheme.borderStrong
                        Rectangle {
                            x: syncSwitch.checked ? 23 : 3
                            y: 3
                            width: 20
                            height: 20
                            radius: 10
                            color: appTheme.white
                            Behavior on x { NumberAnimation { duration: 120 } }
                        }
                    }
                    contentItem: Text {
                        text: syncSwitch.text
                        color: syncSwitch.enabled ? appTheme.textPrimary : appTheme.textSecondary
                        font.pixelSize: 12
                        font.weight: Font.Medium
                        wrapMode: Text.Wrap
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                Text {
                    id: syncHelper
                    Layout.fillWidth: true
                    text: qsTranslate("MeetingSync", "This setting applies to all computers linked to this meeting folder.")
                    color: appTheme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
                Text {
                    objectName: "meetingFolderOperation"
                    Layout.fillWidth: true
                    visible: root.operationLabel.length > 0
                    text: root.operationLabel
                    color: appTheme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                    Accessible.role: Accessible.StaticText
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 1
                    visible: deleteFolder.visible
                    color: appTheme.border_
                }
                FolderAction {
                    id: deleteFolder
                    objectName: "meetingFolderDelete"
                    visible: root.folderState === "present"
                    enabled: root.mutable && !root.syncEnabled
                    destructive: true
                    text: qsTranslate("MeetingSync", "Delete folder…")
                    Accessible.description: deleteReason.visible ? deleteReason.text : ""
                    onClicked: {
                        if (root.mutable && !root.syncEnabled) {
                            folderPopup.close()
                            root.bridge.deleteMeetingFolder()
                        }
                    }
                }
                Text {
                    id: deleteReason
                    objectName: "meetingFolderDeleteReason"
                    Layout.fillWidth: true
                    visible: deleteFolder.visible && (root.syncEnabled || root.pending)
                    text: qsTranslate("MeetingSync", "Turn off synchronization to delete this folder")
                    color: appTheme.textSecondary
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
        }
    }

    component FolderAction: Button {
        id: action
        property bool destructive: false
        Layout.fillWidth: true
        implicitHeight: Math.max(34, contentItem.implicitHeight + 16)
        padding: 8
        focusPolicy: Qt.StrongFocus
        Accessible.name: text
        onActiveFocusChanged: if (activeFocus) panelViewport.reveal(action)
        AppPointerCursor {}
        contentItem: Text {
            text: action.text
            color: !action.enabled ? appTheme.textMuted
                : action.destructive ? appTheme.dangerText : appTheme.textPrimary
            font.pixelSize: 12
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 7
            color: action.hovered ? (action.destructive ? appTheme.dangerSubtle : appTheme.hover)
                                  : "transparent"
            border.width: action.visualFocus ? 2 : 1
            border.color: action.visualFocus ? appTheme.accent : appTheme.border_
        }
    }
}
