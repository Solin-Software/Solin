pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var section
    property bool compact: false
    signal backRequested()

    implicitHeight: compact ? 44 : titleBlock.implicitHeight

    RowLayout {
        anchors.fill: parent
        spacing: root.compact ? 4 : 12

        ToolButton {
            id: backButton
            objectName: "settingsBackButton"
            visible: root.compact
            Layout.preferredWidth: visible ? 44 : 0
            Layout.preferredHeight: 44
            Layout.alignment: Qt.AlignVCenter
            padding: 0
            hoverEnabled: true
            focusPolicy: Qt.StrongFocus
            Accessible.name: qsTr("Back to sections")
            AppPointerCursor {}
            onClicked: root.backRequested()

            contentItem: Item {
                SettingsIcon {
                    anchors.centerIn: parent
                    name: "back"
                    width: 15
                    height: 15
                    tint: backButton.enabled
                        ? appTheme.textSecondary : appTheme.textMuted
                }
            }

            background: Item {
                Rectangle {
                    anchors.centerIn: parent
                    width: 32
                    height: 32
                    radius: 16
                    scale: backButton.down ? 0.94 : 1
                    color: backButton.down
                        ? appTheme.accentTint
                        : backButton.hovered ? appTheme.hover : appTheme.surfaceChrome
                    border.width: backButton.visualFocus ? 1 : 0
                    border.color: appTheme.accent
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Behavior on scale { NumberAnimation { duration: 90 } }
                }
            }

            ToolTip {
                id: backToolTip
                visible: backButton.hovered
                text: qsTr("Back to sections")
                delay: 450
                padding: 7
                contentItem: Text {
                    text: backToolTip.text
                    color: appTheme.textPrimary
                    font.pixelSize: 12
                }
                background: Rectangle {
                    color: appTheme.surfaceChrome
                    radius: 7
                    border.width: 1
                    border.color: appTheme.border
                }
            }
        }

        Loader {
            active: !root.compact
            visible: active
            Layout.minimumWidth: active ? 24 : 0
            Layout.preferredWidth: active ? 24 : 0
            Layout.maximumWidth: active ? 24 : 0
            Layout.preferredHeight: 24
            Layout.alignment: Qt.AlignVCenter
            sourceComponent: SettingsIcon {
                objectName: "settingsSectionTitleIcon"
                width: 24
                height: 24
                name: root.section.icon
            }
        }

        ColumnLayout {
            id: titleBlock
            Layout.fillWidth: true
            spacing: root.compact ? 2 : 6
            Text {
                Layout.fillWidth: true
                text: root.section.title
                color: appTheme.textPrimary
                font.pixelSize: root.compact ? 18 : 25
                font.weight: Font.DemiBold
                wrapMode: Text.Wrap
                verticalAlignment: Text.AlignVCenter
            }
            Text {
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.section.description
                color: appTheme.textMuted
                font.pixelSize: root.compact ? 11 : 13
                wrapMode: Text.Wrap
            }
        }
    }
}
