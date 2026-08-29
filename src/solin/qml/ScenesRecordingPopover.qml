pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Popup {
    id: root
    objectName: "scenesRecordingPopover"

    property var bridge: null
    property var theme: null
    property int elapsedTick: 0

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"
    readonly property bool recording: bridge && bridge.recordingStatus === "recording"
    readonly property bool folderMutable: bridge && !bridge.recordingBusy

    width: Math.min(382, parent ? parent.width - 24 : 382)
    padding: 16
    modal: false
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    function openFor(anchor) {
        if (!anchor || !root.bridge)
            return
        root.bridge.refreshRecordingAudioDevices()
        var point = anchor.mapToItem(root.parent, 0, anchor.height + 6)
        root.x = Math.max(12, Math.min(point.x, root.parent.width - root.width - 12))
        root.y = point.y
        root.open()
    }

    onOpened: microphoneCombo.forceActiveFocus()

    function indexForKey(model, key) {
        for (var index = 0; index < model.length; ++index) {
            if (model[index].key === key)
                return index
        }
        return -1
    }

    function elapsedText() {
        root.elapsedTick
        var total = root.bridge ? root.bridge.recordingElapsedSeconds : 0
        var hours = Math.floor(total / 3600)
        var minutes = Math.floor((total % 3600) / 60)
        var seconds = total % 60
        var mm = minutes < 10 ? "0" + minutes : String(minutes)
        var ss = seconds < 10 ? "0" + seconds : String(seconds)
        return hours > 0 ? String(hours) + ":" + mm + ":" + ss : mm + ":" + ss
    }

    Timer {
        interval: 1000
        repeat: true
        running: root.visible && root.bridge && root.bridge.recordingActive
        onTriggered: root.elapsedTick += 1
    }

    background: Rectangle {
        color: root.surface
        radius: 14
        border.width: 1
        border.color: root.borderColor
    }

    contentItem: Flickable {
        implicitHeight: Math.min(
            recordingForm.implicitHeight,
            root.parent ? Math.max(260, root.parent.height - 104) : 520)
        contentWidth: width
        contentHeight: recordingForm.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollIndicator.vertical: ScrollIndicator { }

        ColumnLayout {
            id: recordingForm
            width: parent.width
            spacing: 13

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Rectangle {
                Layout.preferredWidth: 30
                Layout.preferredHeight: 30
                radius: 9
                color: Qt.rgba(root.danger.r, root.danger.g, root.danger.b, 0.12)
                Image {
                    anchors.centerIn: parent
                    width: 14
                    height: 14
                    source: "image://sceneicons/record/16/" + String(root.danger).replace("#", "")
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Recording")
                    color: root.textPrimary
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: root.bridge ? root.bridge.recordingStatusLabel : ""
                    color: root.bridge && root.bridge.recordingStatus === "failed"
                        ? root.danger : root.textMuted
                    font.pixelSize: 9
                    elide: Text.ElideRight
                }
            }
            Text {
                visible: root.bridge && root.bridge.recordingActive
                text: root.elapsedText()
                color: root.recording ? root.danger : root.textSecondary
                font.pixelSize: 12
                font.weight: Font.DemiBold
                font.family: "monospace"
            }
        }

        Text {
            Layout.fillWidth: true
            visible: root.bridge && root.bridge.recordingMessage.length > 0
            text: root.bridge ? root.bridge.recordingMessage : ""
            color: root.bridge && root.bridge.recordingStatus === "failed"
                ? root.danger : root.textMuted
            font.pixelSize: 9
            wrapMode: Text.Wrap
        }

        Rectangle {
            objectName: "scenesRecordingAudioWarning"
            Layout.fillWidth: true
            Layout.preferredHeight: audioWarningText.implicitHeight + 16
            visible: root.bridge && root.bridge.recordingAudioWarning.length > 0
            radius: 8
            color: Qt.rgba(1.0, 0.72, 0.32, 0.08)
            border.width: 1
            border.color: Qt.rgba(1.0, 0.72, 0.32, 0.20)
            Text {
                id: audioWarningText
                anchors.fill: parent
                anchors.margins: 8
                text: root.bridge ? root.bridge.recordingAudioWarning : ""
                color: root.textMuted
                font.pixelSize: 8
                wrapMode: Text.Wrap
            }
        }

        ScenesDialogButton {
            objectName: "scenesRecordingPopoverToggle"
            Layout.fillWidth: true
            theme: root.theme
            text: !root.bridge ? qsTr("Record")
                : root.bridge.recordingStatus === "recording" ? qsTr("Stop recording")
                : root.bridge.recordingStatus === "failed" ? qsTr("Try again")
                : root.bridge.recordingStatus === "starting" ? qsTr("Starting…")
                : root.bridge.recordingStatus === "stopping" ? qsTr("Finishing…")
                : qsTr("Start recording")
            primary: root.bridge && root.bridge.recordingStatus !== "recording"
            danger: root.bridge && root.bridge.recordingStatus === "recording"
            enabled: root.bridge && root.bridge.recordingCanToggle
            onClicked: root.bridge.toggleProgramRecording()
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: root.borderColor
        }

        Text {
            Layout.fillWidth: true
            visible: root.bridge && root.bridge.recordingAudioDeviceStatus.length > 0
            text: root.bridge ? root.bridge.recordingAudioDeviceStatus : ""
            color: root.textMuted
            font.pixelSize: 8
            wrapMode: Text.Wrap
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            Text {
                text: qsTr("Microphone")
                color: root.textSecondary
                font.pixelSize: 9
                font.weight: Font.DemiBold
            }
            ScenesComboBox {
                id: microphoneCombo
                objectName: "scenesRecordingMicrophone"
                Layout.fillWidth: true
                theme: root.theme
                model: root.bridge ? root.bridge.recordingMicrophoneChoices : []
                textRole: "name"
                currentIndex: root.indexForKey(
                    model, root.bridge ? root.bridge.recordingMicrophoneSelection : "")
                enabled: root.bridge !== null
                Accessible.name: qsTr("Microphone")
                onActivated: function(index) {
                    if (root.bridge && index >= 0)
                        root.bridge.setRecordingMicrophone(model[index].key)
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            Text {
                text: qsTr("System audio")
                color: root.textSecondary
                font.pixelSize: 9
                font.weight: Font.DemiBold
            }
            ScenesComboBox {
                id: systemAudioCombo
                objectName: "scenesRecordingSystemAudio"
                Layout.fillWidth: true
                theme: root.theme
                model: root.bridge ? root.bridge.recordingSystemAudioChoices : []
                textRole: "name"
                currentIndex: root.indexForKey(
                    model, root.bridge ? root.bridge.recordingSystemAudioSelection : "")
                enabled: root.bridge !== null
                Accessible.name: qsTr("System audio")
                onActivated: function(index) {
                    if (root.bridge && index >= 0)
                        root.bridge.setRecordingSystemAudio(model[index].key)
                }
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Includes sounds from every app using the selected output.")
                color: root.textMuted
                font.pixelSize: 8
                wrapMode: Text.Wrap
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                text: qsTr("Save recordings to")
                color: root.textSecondary
                font.pixelSize: 9
                font.weight: Font.DemiBold
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                radius: 9
                color: root.surfaceSoft
                border.width: 1
                border.color: root.borderColor
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 9
                    spacing: 8
                    Image {
                        Layout.preferredWidth: 14
                        Layout.preferredHeight: 14
                        source: "image://sceneicons/folder/16/" + String(root.textMuted).replace("#", "")
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.bridge ? root.bridge.recordingOutputDirectory : ""
                        color: root.textSecondary
                        font.pixelSize: 9
                        elide: Text.ElideMiddle
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 7
                ScenesDialogButton {
                    objectName: "scenesRecordingChooseFolder"
                    Layout.fillWidth: true
                    theme: root.theme
                    text: qsTr("Change…")
                    enabled: root.folderMutable
                    onClicked: root.bridge.chooseRecordingDirectory()
                }
                ScenesDialogButton {
                    Layout.fillWidth: true
                    theme: root.theme
                    text: qsTr("Use default")
                    enabled: root.folderMutable && root.bridge
                        && !root.bridge.recordingOutputDirectoryIsDefault
                    onClicked: root.bridge.useDefaultRecordingDirectory()
                }
                ScenesDialogButton {
                    Layout.fillWidth: true
                    theme: root.theme
                    text: qsTr("Open folder")
                    enabled: root.bridge !== null
                    onClicked: root.bridge.openRecordingDirectory()
                }
            }
        }
        }
    }
}
