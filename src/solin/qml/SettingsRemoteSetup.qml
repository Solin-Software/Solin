pragma Translator: "RemoteControlSetupDialog"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "settingsRemoteSetup"
    readonly property var remoteState: settingsRemote.state
    readonly property bool compact: width < 480

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(680, parent ? Math.max(0, parent.width - 24) : 680)
    height: Math.min(720, parent ? Math.max(0, parent.height - 24) : 720)
    padding: compact ? 16 : 24
    spacing: 16
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    visible: remoteState.setupVisible
    onClosed: {
        if (remoteState.setupVisible)
            settingsRemote.invoke("closeSetup")
    }
    onOpened: {
        remoteViewport.contentY = 0
        copyAddress.forceActiveFocus(Qt.PopupFocusReason)
    }

    background: Rectangle {
        radius: 16
        color: appTheme.surface
        border.color: appTheme.border
    }

    header: RowLayout {
        spacing: 12
        Text {
            Layout.fillWidth: true
            Layout.leftMargin: dialog.padding
            Layout.topMargin: dialog.padding
            text: qsTr("Set up Solin Remote")
            color: appTheme.textPrimary
            font.pixelSize: 21
            font.weight: Font.DemiBold
            wrapMode: Text.WordWrap
            Accessible.role: Accessible.Heading
        }
        SettingsButton {
            objectName: "remoteSetupClose"
            Layout.rightMargin: dialog.padding
            Layout.topMargin: dialog.padding
            Layout.preferredWidth: 44
            text: "×"
            font.pixelSize: 24
            Accessible.name: qsTranslate("SettingsWidget", "Close")
            onClicked: settingsRemote.invoke("closeSetup")
        }
    }

    contentItem: Item {
        id: viewportHost

        Flickable {
            id: remoteViewport
            objectName: "remoteSetupViewport"
            anchors.fill: parent
            anchors.rightMargin: 14
            clip: true
            contentWidth: width
            contentHeight: remoteBody.implicitHeight
            boundsBehavior: Flickable.DragAndOvershootBounds
            flickableDirection: Flickable.VerticalFlick

            ScrollBar.vertical: AppScrollBar {
                objectName: "remoteSetupScrollBar"
                parent: viewportHost
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.bottom: parent.bottom
            }

            ColumnLayout {
                id: remoteBody
                width: remoteViewport.width
                spacing: 20
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Scan once to open the secure setup on your phone or tablet.")
                    color: appTheme.textSecondary
                    font.pixelSize: 15
                    wrapMode: Text.WordWrap
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: dialog.compact ? 1 : 2
                    columnSpacing: 24
                    rowSpacing: 16
                    Rectangle {
                        Layout.preferredWidth: 188
                        Layout.preferredHeight: 188
                        Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                        radius: 10
                        // QR codes keep their required black-on-white scanning contrast.
                        color: "white"
                        Image {
                            anchors.fill: parent
                            anchors.margins: 6
                            source: dialog.remoteState.qrImage
                            fillMode: Image.PreserveAspectFit
                            smooth: false
                            visible: source.toString().length > 0
                            Accessible.name: qsTr("1  Scan the QR code")
                        }
                        Text {
                            anchors.fill: parent
                            anchors.margins: 14
                            text: dialog.remoteState.qrBusy ? qsTr("Generating QR code…")
                                                         : dialog.remoteState.qrError
                            visible: !dialog.remoteState.qrImage
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            wrapMode: Text.WordWrap
                            color: "#303740"
                            font.pixelSize: 13
                        }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignTop
                        spacing: 12
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("1  Scan the QR code")
                            color: appTheme.textPrimary
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                            wrapMode: Text.WordWrap
                        }
                        Text {
                            Layout.fillWidth: true
                            text: qsTr("Keep Solin open and connect both devices to the same local network. The guide explains the certificate and app installation.")
                            color: appTheme.textSecondary
                            font.pixelSize: 13
                            wrapMode: Text.WordWrap
                        }
                        AppTextField {
                            Layout.fillWidth: true
                            readOnly: true
                            text: dialog.remoteState.setupUrl
                            Accessible.name: qsTr("Copy setup address")
                        }
                        SettingsButton {
                            id: copyAddress
                            objectName: "remoteSetupCopy"
                            Layout.fillWidth: true
                            text: qsTr("Copy setup address")
                            onClicked: settingsRemote.invoke("copySetupAddress")
                        }
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: qsTr("On the first opening, the browser may show a privacy warning. Confirm that the local address matches, choose Advanced or Show Details, and continue only to this address. The warning disappears after the certificate is trusted.")
                    color: appTheme.textSecondary
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 1
                    color: appTheme.border
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("VERIFICATION CODE")
                        color: appTheme.textMuted
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                    }
                    AppTextField {
                        Layout.fillWidth: true
                        readOnly: true
                        text: dialog.remoteState.verificationCode
                        font.pixelSize: 22
                        font.family: "monospace"
                        Accessible.name: qsTr("VERIFICATION CODE")
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Compare this code on the device before installing the certificate.")
                        color: appTheme.textSecondary
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                    }
                    SettingsButton {
                        objectName: "remoteSetupFingerprint"
                        Layout.fillWidth: true
                        checkable: true
                        checked: dialog.remoteState.fingerprintVisible
                        text: qsTr("Show full SHA-256 fingerprint")
                        onClicked: settingsRemote.setValue("fingerprintVisible", checked)
                    }
                    TextEdit {
                        Layout.fillWidth: true
                        visible: dialog.remoteState.fingerprintVisible
                        text: dialog.remoteState.fingerprint
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                        color: appTheme.textSecondary
                        selectionColor: appTheme.accentSelection
                        selectedTextColor: appTheme.textOnAccent
                        font.pixelSize: 13
                        font.family: "monospace"
                        Accessible.name: qsTr("Show full SHA-256 fingerprint")
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: dialog.remoteState.feedback
                    color: dialog.remoteState.feedbackKind === "error"
                           ? appTheme.dangerText : appTheme.textSecondary
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                    Accessible.role: Accessible.AlertMessage
                }
            }
        }
    }

    footer: GridLayout {
        columns: dialog.compact ? 1 : 2
        columnSpacing: 12
        rowSpacing: 8
        SettingsButton {
            objectName: "remoteSetupSaveCertificate"
            Layout.fillWidth: true
            Layout.leftMargin: dialog.padding
            Layout.rightMargin: dialog.compact ? dialog.padding : 0
            Layout.bottomMargin: dialog.compact ? 0 : dialog.padding
            text: qsTr("Save certificate…")
            onClicked: settingsRemote.invoke("saveCertificate")
        }
        SettingsButton {
            objectName: "remoteSetupDone"
            Layout.fillWidth: true
            Layout.leftMargin: dialog.compact ? dialog.padding : 0
            Layout.rightMargin: dialog.padding
            Layout.bottomMargin: dialog.padding
            primary: true
            text: qsTr("Done")
            onClicked: settingsRemote.invoke("completeSetup")
        }
    }
}
