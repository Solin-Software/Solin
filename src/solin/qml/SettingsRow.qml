pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FocusScope {
    id: root
    required property var spec
    required property var domain
    property var state: domain ? domain.state : ({})
    property bool narrow: width < 480
    property bool highlighted: false
    readonly property var value: state[spec.key]
    readonly property bool available: !spec.enabledWhen || !!state[spec.enabledWhen]
    readonly property string help: spec.descriptionKey ? String(state[spec.descriptionKey] || "") : spec.description
    readonly property bool fullWidthEditor: ["multiline", "screens", "shortcuts"].indexOf(spec.kind) >= 0
    signal choose(var row, var owner)
    implicitHeight: content.implicitHeight + 24
    visible: !spec.visibleWhen || !!state[spec.visibleWhen]
    objectName: "setting_" + spec.key
    Accessible.name: spec.label
    Accessible.description: help

    function focusControl() {
        forceActiveFocus(Qt.TabFocusReason)
        if (editor.item && editor.item.focusControl)
            editor.item.focusControl()
    }

    Rectangle {
        anchors.fill: parent
        color: root.highlighted ? appTheme.accentTint : "transparent"
        border.width: root.activeFocus ? 2 : 0
        border.color: appTheme.accent
    }

    GridLayout {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 12
        columns: (root.narrow && root.spec.kind !== "toggle") || root.fullWidthEditor ? 1 : 2
        columnSpacing: 24
        rowSpacing: 12

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 4
            Text {
                Layout.fillWidth: true
                text: root.spec.label
                color: appTheme.textPrimary
                font.pixelSize: 15
                font.weight: Font.Medium
                wrapMode: Text.Wrap
            }
            Text {
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.help
                color: appTheme.textMuted
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
        }

        Loader {
            id: editor
            Layout.fillWidth: root.narrow || root.fullWidthEditor
            Layout.preferredWidth: root.narrow || root.fullWidthEditor ? -1 : 252
            Layout.minimumWidth: root.narrow || root.fullWidthEditor ? 0 : 220
            Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
            sourceComponent: {
                switch (root.spec.kind) {
                case "toggle": return toggleComponent
                case "input": case "password": return fieldComponent
                case "multiline": return multilineComponent
                case "select": case "time": return selectComponent
                case "congregation": return congregationComponent
                case "folder": return folderComponent
                case "screens": return screensComponent
                case "shortcuts": return shortcutsComponent
                default: return actionComponent
                }
            }
        }
    }

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        height: 1
        color: appTheme.border_
        opacity: 0.72
    }

    Component {
        id: toggleComponent
        Item {
            implicitWidth: 48
            implicitHeight: 44
            function focusControl() { toggle.forceActiveFocus() }
            Switch {
                id: toggle
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                enabled: root.available
                implicitWidth: 48
                implicitHeight: 44
                Accessible.name: root.spec.label
                Accessible.description: root.help
                AppPointerCursor {}
                onClicked: {
                    root.domain.setValue(root.spec.key, checked)
                    checked = !!root.domain.state[root.spec.key]
                }
                Binding {
                    target: toggle
                    property: "checked"
                    value: !!root.value
                }
                indicator: Rectangle {
                    x: toggle.width - width
                    y: (toggle.height - height) / 2
                    width: 46; height: 26; radius: 13
                    color: toggle.checked ? appTheme.accent : appTheme.secondaryControl
                    border.color: toggle.visualFocus ? appTheme.textPrimary : "transparent"
                    border.width: 2
                    Rectangle {
                        x: toggle.checked ? 23 : 3
                        y: 3; width: 20; height: 20; radius: 10
                        color: appTheme.white
                        Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
                    }
                }
            }
        }
    }

    Component {
        id: fieldComponent
        AppTextField {
            enabled: root.available
            text: String(root.value === undefined ? "" : root.value)
            echoMode: root.spec.kind === "password" ? TextInput.Password : TextInput.Normal
            Accessible.name: root.spec.label
            inputMethodHints: root.spec.kind === "password" ? Qt.ImhSensitiveData | Qt.ImhNoPredictiveText : Qt.ImhNone
            onTextEdited: root.domain.setValue(root.spec.key, text)
            function focusControl() { forceActiveFocus() }
        }
    }

    Component {
        id: multilineComponent
        TextArea {
            text: String(root.value || "")
            implicitHeight: Math.max(96, contentHeight + 24)
            wrapMode: TextEdit.Wrap
            selectByMouse: true
            padding: 12
            font.pixelSize: 15
            color: appTheme.textPrimary
            selectionColor: appTheme.accentSelection
            selectedTextColor: appTheme.textOnAccent
            Accessible.name: root.spec.label
            onTextChanged: if (activeFocus && text !== root.value) root.domain.setValue(root.spec.key, text)
            background: Rectangle {
                radius: 8
                color: appTheme.surfaceChrome
                border.color: parent.activeFocus ? appTheme.accent : appTheme.border
                border.width: parent.activeFocus ? 2 : 1
            }
            function focusControl() { forceActiveFocus() }
        }
    }

    Component {
        id: selectComponent
        SettingsButton {
            enabled: root.available
            text: {
                if (root.spec.kind === "time") return String(root.value || "00:00")
                const options = root.state[root.spec.options] || []
                for (let i = 0; i < options.length; i++)
                    if (String(options[i].value) === String(root.value)) return options[i].label
                return root.value ? String(root.value) : qsTr("Select…")
            }
            trailingIcon: root.spec.kind === "time" ? "clock" : "down"
            Accessible.name: root.spec.label + ": " + text
            onClicked: root.choose(root.spec, root.domain)
            function focusControl() { forceActiveFocus() }
        }
    }

    Component {
        id: actionComponent
        ColumnLayout {
            spacing: 8
            Text {
                Layout.fillWidth: true
                visible: typeof root.value === "string" && root.value.length > 0
                text: typeof root.value === "string" ? root.value : ""
                color: appTheme.textMuted
                font.pixelSize: 13
                wrapMode: Text.WrapAnywhere
                horizontalAlignment: root.narrow ? Text.AlignLeft : Text.AlignRight
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                layoutDirection: root.narrow ? Qt.LeftToRight : Qt.RightToLeft
                SettingsButton {
                    width: root.narrow ? parent.width : implicitWidth
                    text: root.spec.label
                    enabled: root.available
                    primary: ["saveCredentials", "saveYeartext"].indexOf(root.spec.action) >= 0
                    onClicked: root.domain.invoke(root.spec.action)
                }
                SettingsButton {
                    width: root.narrow ? parent.width : implicitWidth
                    visible: !!root.spec.secondaryAction
                    text: root.spec.secondaryLabel
                    onClicked: root.domain.invoke(root.spec.secondaryAction)
                }
            }
        }
    }

    Component {
        id: congregationComponent
        SettingsButton {
            enabled: root.available
            text: qsTr("Search…")
            Accessible.name: root.spec.label
            Accessible.description: root.help
            onClicked: root.choose(root.spec, root.domain)
            function focusControl() { forceActiveFocus() }
        }
    }

    Component {
        id: folderComponent
        ColumnLayout {
            spacing: 8
            Text {
                Layout.fillWidth: true
                text: root.value || qsTr("No folder selected")
                color: appTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WrapAnywhere
                horizontalAlignment: root.narrow ? Text.AlignLeft : Text.AlignRight
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                layoutDirection: root.narrow ? Qt.LeftToRight : Qt.RightToLeft
                SettingsButton {
                    width: root.narrow ? parent.width : implicitWidth
                    text: qsTr("Choose…")
                    onClicked: root.domain.invoke(root.spec.action)
                }
                SettingsButton {
                    width: root.narrow ? parent.width : implicitWidth
                    visible: !!root.value
                    text: root.spec.secondaryLabel
                    onClicked: root.domain.invoke(root.spec.secondaryAction)
                }
            }
        }
    }

    Component {
        id: screensComponent
        ColumnLayout {
            spacing: 8
            Repeater {
                model: root.value || []
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 12
                    SettingsIcon { name: "projection" }
                    Text {
                        Layout.fillWidth: true
                        text: modelData.label + "\n" + modelData.name + " · " + modelData.width + " × " + modelData.height
                        color: appTheme.textSecondary
                        font.pixelSize: 14
                        wrapMode: Text.Wrap
                    }
                }
            }
            Text {
                Layout.fillWidth: true
                visible: (root.value || []).length < 2
                text: qsTr("No secondary screen detected. Connect an external monitor.")
                color: appTheme.textMuted
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
        }
    }

    Component {
        id: shortcutsComponent
        ColumnLayout {
            spacing: 10
            Repeater {
                model: root.value || []
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        text: modelData.eventLabel + " · " + modelData.sequence
                        color: appTheme.textSecondary
                        font.pixelSize: 14
                        wrapMode: Text.Wrap
                    }
                    SettingsButton { text: qsTr("Edit"); onClicked: root.domain.invoke("editShortcut:" + modelData.id) }
                    SettingsButton { text: qsTr("Delete"); destructive: true; onClicked: root.domain.invoke("deleteShortcut:" + modelData.id) }
                }
            }
            Text {
                Layout.fillWidth: true
                visible: !(root.value || []).length
                text: qsTr("No shortcuts configured.")
                color: appTheme.textMuted
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }
            SettingsButton { text: root.spec.label; onClicked: root.domain.invoke(root.spec.action) }
        }
    }
}
