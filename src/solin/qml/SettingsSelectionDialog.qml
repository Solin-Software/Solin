pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(480, parent ? parent.width - 32 : 480)
    height: Math.min(560, parent ? parent.height - 32 : 560)
    modal: true
    padding: 20
    property var spec: ({label: "", kind: "select", options: "", key: ""})
    property var domain: null
    property var choices: domain ? domain.state[spec.options] || [] : []
    property var filtered: choices.filter(item => item.label.toLocaleLowerCase().indexOf(filter.text.toLocaleLowerCase()) >= 0)
    property string draftTime: "00:00"
    function present(row, owner) {
        spec = row; domain = owner
        filter.text = ""
        draftTime = String(owner.state[row.key] || "00:00")
        open()
        Qt.callLater(function() { if (spec.kind === "time") timeField.forceActiveFocus(); else filter.forceActiveFocus() })
    }
    background: Rectangle { radius: 14; color: appTheme.surface2; border.color: appTheme.border }
    header: Text { text: root.spec.label; color: appTheme.textPrimary; font.pixelSize: 20; font.weight: Font.DemiBold; padding: 20; wrapMode: Text.Wrap }
    contentItem: ColumnLayout {
        spacing: 12
        AppTextField { id: filter; searchIcon: true; Layout.fillWidth: true; visible: root.spec.kind !== "time"; placeholderText: qsTr("Search…"); Accessible.name: placeholderText }
        AppTextField {
            id: timeField
            Layout.fillWidth: true; visible: root.spec.kind === "time"
            text: root.draftTime; inputMask: "99:99"; inputMethodHints: Qt.ImhTime
            Accessible.name: root.spec.label
            onTextEdited: root.draftTime = text
        }
        Text { visible: root.spec.kind === "time"; text: qsTr("HH:mm"); color: appTheme.textMuted; font.pixelSize: 13 }
        AppSelectionList {
            id: options
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: root.spec.kind !== "time"
            clip: true; spacing: 4; model: root.filtered
            delegate: ItemDelegate {
                required property var modelData
                width: options.delegateWidth; implicitHeight: Math.max(44, label.implicitHeight + 20)
                Accessible.name: modelData.label
                AppPointerCursor {}
                contentItem: Text { id: label; text: modelData.label; color: appTheme.textPrimary; font.pixelSize: 15; wrapMode: Text.Wrap }
                background: Rectangle { radius: 8; color: parent.hovered || parent.visualFocus ? appTheme.hover : "transparent"; border.color: parent.visualFocus ? appTheme.accent : "transparent" }
                onClicked: { root.domain.setValue(root.spec.key, modelData.value); root.close() }
            }
        }
        Text { Layout.fillWidth: true; visible: root.spec.kind !== "time" && !root.filtered.length; text: qsTr("No results found."); color: appTheme.textMuted; wrapMode: Text.Wrap }
        Item { Layout.fillHeight: true; visible: root.spec.kind === "time" }
        Flow {
            Layout.fillWidth: true; spacing: 8
            SettingsButton {
                visible: root.spec.kind === "time"
                text: qsTr("Apply"); primary: true
                enabled: /^([01]\d|2[0-3]):[0-5]\d$/.test(root.draftTime)
                onClicked: { root.domain.setValue(root.spec.key, root.draftTime); root.close() }
            }
            SettingsButton { text: qsTr("Cancel"); onClicked: root.close() }
        }
    }
}
