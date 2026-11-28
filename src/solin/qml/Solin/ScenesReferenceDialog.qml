pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root
    objectName: "scenesReferenceDialog"
    property var bridge: null
    property var theme: null
    property var editContext: ({})
    property string sceneId: ""
    property bool layerNameCustom: false
    property string validationError: ""
    property bool saveAttempted: false
    property string nameError: ""
    readonly property var choices: bridge ? bridge.referencedScenes : []
    readonly property bool canAdd: choices.some(function(choice) {
        return choice.id === root.sceneId && choice.available !== false
    })
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    modal: true
    anchors.centerIn: parent
    width: Math.min(520, parent ? parent.width - 28 : 520)
    height: Math.min(500, parent ? parent.height - 28 : 500)
    padding: 18
    closePolicy: Popup.CloseOnEscape

    function validateName() {
        nameError = bridge && visible ? bridge.layerNameError(nameField.text, "") : ""
        return !nameError.length
    }

    Connections {
        target: root.bridge
        function onChanged() { root.validateName() }
    }

    onOpened: validateName()

    function openForScene() {
        editContext = bridge ? bridge.sourceEditContext() : ({})
        sceneId = ""
        layerNameCustom = false
        nameField.text = ""
        validationError = ""
        saveAttempted = false
        selector.reset()
        open()
    }

    function selectScene(choice) {
        if (choice.available === false)
            return
        sceneId = choice.id
        if (!layerNameCustom)
            nameField.text = choice.name
    }

    function save() {
        saveAttempted = true
        validationError = ""
        if (!canAdd) {
            validationError = qsTr("Choose an available scene.")
            return
        }
        if (!nameField.text.trim().length) {
            validationError = qsTr("Enter a name for this source in the scene.")
            nameField.forceActiveFocus()
            return
        }
        if (!validateName()) {
            nameField.forceActiveFocus()
            return
        }
        if (bridge && bridge.addSceneReference(sceneId, nameField.text.trim(), editContext).length)
            close()
    }

    background: Rectangle {
        radius: 14
        color: root.theme ? root.theme.surface : "#172131"
        border.width: 1
        border.color: root.theme ? root.theme.border_ : "#2c394b"
    }
    header: Text {
        text: qsTr("Add scene")
        color: root.textPrimary
        font.pixelSize: 17
        font.weight: Font.DemiBold
        padding: 18
        bottomPadding: 0
    }
    contentItem: ScrollView {
        id: bodyScroll
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: bodyScroll.availableWidth
            spacing: 12
            ScenesSourceSelector {
                id: selector
                objectName: "scenesReferenceSelector"
                theme: root.theme
                Layout.fillWidth: true
                options: root.choices
                selectedId: root.sceneId
                searchPlaceholder: qsTr("Search scenes…")
                onChosen: function(choice) { root.selectScene(choice) }
            }
            Text { text: qsTr("Name in this scene"); color: root.textPrimary; font.pixelSize: 12 }
            ScenesTextField {
                id: nameField
                objectName: "scenesReferenceNameField"
                theme: root.theme
                Layout.fillWidth: true
                maximumLength: 120
                onTextChanged: {
                    root.validationError = ""
                    root.saveAttempted = false
                    root.validateName()
                }
                onTextEdited: root.layerNameCustom = true
                onAccepted: root.save()
            }
            Text {
                objectName: "scenesReferenceNameError"
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.nameError
                color: root.theme ? root.theme.danger : "#ef6a6a"
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Accessible.role: Accessible.AlertMessage
            }
        }
    }
    footer: ColumnLayout {
        spacing: 10
        Text {
            objectName: "scenesReferenceError"
            Layout.fillWidth: true
            visible: text.length > 0
            text: root.validationError || (root.saveAttempted && root.bridge ? root.bridge.sourceEditError : "")
            color: root.theme ? root.theme.danger : "#ef6a6a"
            font.pixelSize: 11
            wrapMode: Text.Wrap
            Accessible.role: Accessible.AlertMessage
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Item { Layout.fillWidth: true }
            ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
            ScenesDialogButton {
                objectName: "scenesReferenceAddButton"
                theme: root.theme
                text: qsTr("Add")
                primary: true
                enabled: root.canAdd && !root.nameError.length && nameField.text.trim().length > 0
                onClicked: root.save()
            }
        }
    }
}
