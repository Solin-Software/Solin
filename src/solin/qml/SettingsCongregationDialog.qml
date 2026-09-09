pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(520, parent ? parent.width - 32 : 520)
    height: Math.min(560, parent ? parent.height - 32 : 560)
    modal: true
    padding: 20
    property var domain: null
    property var state: domain ? domain.state : ({})

    function present(owner) {
        domain = owner
        search.text = String(state.congregationQuery || "")
        open()
        Qt.callLater(function() { search.forceActiveFocus() })
    }

    onClosed: debounce.stop()
    background: Rectangle {
        radius: 14
        color: appTheme.surface2
        border.color: appTheme.border
    }
    header: Text {
        text: qsTr("Fill in from jw.org")
        color: appTheme.textPrimary
        font.pixelSize: 20
        font.weight: Font.DemiBold
        padding: 20
        wrapMode: Text.Wrap
    }

    Timer {
        id: debounce
        interval: root.domain ? root.domain.searchDebounceMs : 900
        onTriggered: root.domain.searchCongregation(search.text)
    }

    contentItem: ColumnLayout {
        spacing: 12

        AppTextField {
            id: search
            objectName: "settingsCongregationSearch"
            Layout.fillWidth: true
            searchIcon: true
            clearActionVisible: text.length > 0
            placeholderText: qsTr("Search by congregation name")
            Accessible.name: placeholderText
            onTextEdited: debounce.restart()
            onAccepted: {
                debounce.stop()
                root.domain.searchCongregation(text)
            }
            onClearRequested: {
                text = ""
                debounce.stop()
                root.domain.searchCongregation("")
                forceActiveFocus()
            }
        }

        Text {
            Layout.fillWidth: true
            visible: text.length > 0
            text: String(root.state.congregationStatusText || "")
            color: root.state.congregationStatusKind === "error"
                   ? appTheme.dangerText
                   : root.state.congregationStatusKind === "filled"
                     ? appTheme.success : appTheme.textMuted
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }

        AppSelectionList {
            id: results
            objectName: "settingsCongregationResults"
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: count > 0
            spacing: 4
            model: root.state.congregationSuggestions || []

            delegate: ItemDelegate {
                id: option
                required property var modelData
                width: results.delegateWidth
                implicitHeight: Math.max(54, optionContent.implicitHeight + 20)
                objectName: "settingsCongregationOption_" + modelData.guid
                Accessible.name: modelData.label
                Accessible.description: modelData.description || ""
                AppPointerCursor {}

                contentItem: ColumnLayout {
                    id: optionContent
                    spacing: 3
                    Text {
                        Layout.fillWidth: true
                        text: option.modelData.label
                        color: appTheme.textPrimary
                        font.pixelSize: 14
                        font.weight: Font.Medium
                        wrapMode: Text.Wrap
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: option.modelData.description || ""
                        color: appTheme.textMuted
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                }
                background: Rectangle {
                    radius: 8
                    color: option.hovered || option.visualFocus
                           ? appTheme.hover : "transparent"
                    border.width: option.visualFocus ? 2 : 0
                    border.color: appTheme.accent
                }
                onClicked: {
                    search.text = modelData.name
                    root.domain.chooseCongregation(modelData.guid, modelData.name)
                }
            }
        }

        Item { Layout.fillHeight: true; visible: !results.visible }

        Flow {
            Layout.fillWidth: true
            spacing: 8
            layoutDirection: Qt.RightToLeft
            SettingsButton { text: qsTr("Close"); onClicked: root.close() }
        }
    }
}
