pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root
    objectName: "scenesRenameLayerDialog"
    property var bridge: null
    property var theme: null
    property var editContext: ({})
    property string layerId: ""
    property string validationError: ""
    property bool saveAttempted: false
    property string nameError: ""
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    modal: true
    anchors.centerIn: parent
    width: Math.min(420, parent ? parent.width - 28 : 420)
    padding: 18
    closePolicy: Popup.CloseOnEscape

    function validateName() {
        nameError = bridge && visible ? bridge.layerNameError(nameField.text, layerId) : ""
        return !nameError.length
    }

    Connections {
        target: root.bridge
        function onChanged() { root.validateName() }
    }

    function openForLayer() {
        if (!bridge || !bridge.selectedLayerId.length)
            return
        editContext = bridge.sourceEditContext()
        layerId = bridge.selectedLayerId
        nameField.text = bridge.selectedLayer.name || ""
        validationError = ""
        saveAttempted = false
        open()
    }

    function save() {
        saveAttempted = true
        validationError = ""
        if (!nameField.text.trim().length) {
            validationError = qsTr("Enter a name for this source in the scene.")
            nameField.forceActiveFocus()
            return
        }
        if (!validateName()) {
            nameField.forceActiveFocus()
            return
        }
        if (bridge && bridge.renameLayer(layerId, nameField.text.trim(), editContext))
            close()
    }

    onOpened: { validateName(); nameField.forceActiveFocus(); nameField.selectAll() }
    background: Rectangle {
        radius: 14
        color: root.theme ? root.theme.surface : "#172131"
        border.width: 1
        border.color: root.theme ? root.theme.border_ : "#2c394b"
    }
    header: Text {
        text: qsTr("Rename source")
        color: root.textPrimary
        font.pixelSize: 17
        font.weight: Font.DemiBold
        padding: 18
        bottomPadding: 0
    }
    contentItem: ColumnLayout {
        spacing: 12
        Text { text: qsTr("Name in this scene"); color: root.textPrimary; font.pixelSize: 12 }
        ScenesTextField {
            id: nameField
            objectName: "scenesRenameLayerNameField"
            theme: root.theme
            Layout.fillWidth: true
            maximumLength: 120
            onTextChanged: {
                root.validationError = ""
                root.saveAttempted = false
                root.validateName()
            }
            onAccepted: root.save()
        }
        Text {
            objectName: "scenesRenameLayerNameError"
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.nameError
            color: root.theme ? root.theme.danger : "#ef6a6a"
            font.pixelSize: 11
            wrapMode: Text.Wrap
            Accessible.role: Accessible.AlertMessage
        }
        Text {
            objectName: "scenesRenameLayerError"
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.validationError || (root.saveAttempted && root.bridge ? root.bridge.sourceEditError : "")
            color: root.theme ? root.theme.danger : "#ef6a6a"
            font.pixelSize: 11
            wrapMode: Text.Wrap
            Accessible.role: Accessible.AlertMessage
        }
    }
    footer: Rectangle {
        implicitHeight: actions.implicitHeight + 36
        color: "transparent"
        RowLayout {
            id: actions
            anchors.fill: parent
            anchors.margins: 18
            spacing: 10
            Item { Layout.fillWidth: true }
            ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
            ScenesDialogButton {
                objectName: "scenesRenameLayerSaveButton"
                theme: root.theme
                text: qsTr("Save")
                primary: true
                enabled: !root.nameError.length && nameField.text.trim().length > 0
                onClicked: root.save()
            }
        }
    }
}
