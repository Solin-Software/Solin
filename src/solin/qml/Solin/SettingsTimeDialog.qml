pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(440, parent ? parent.width - 32 : 440)
    height: Math.min(520, parent ? parent.height - 32 : 520)
    modal: true
    padding: 20
    property var spec: ({label: "", key: ""})
    property var domain: null

    function present(row, owner) {
        spec = row
        domain = owner
        const parts = String(owner.state[row.key] || "00:00").split(":")
        clock.hour = Math.max(0, Math.min(23, Number(parts[0]) || 0))
        clock.minute = Math.max(0, Math.min(59, Number(parts[1]) || 0))
        clock.mode = "hour"
        open()
    }

    background: Rectangle {
        radius: 14
        color: appTheme.surface2
        border.width: 1
        border.color: appTheme.border
    }
    header: Text {
        text: root.spec.label
        color: appTheme.textPrimary
        font.pixelSize: 20
        font.weight: Font.DemiBold
        padding: 20
        wrapMode: Text.Wrap
    }
    contentItem: ScrollView {
        id: scroll
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: scroll.availableWidth
            spacing: 16
            SettingsClockPicker {
                id: clock
                Layout.alignment: Qt.AlignHCenter
            }
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 8
                SettingsButton { text: qsTr("Cancel"); onClicked: root.close() }
                SettingsButton {
                    text: qsTr("Apply")
                    primary: true
                    onClicked: {
                        root.domain.setValue(root.spec.key,
                            String(clock.hour).padStart(2, "0") + ":" + String(clock.minute).padStart(2, "0"))
                        root.close()
                    }
                }
            }
        }
    }
}
