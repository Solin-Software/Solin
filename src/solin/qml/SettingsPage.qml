pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "settingsPage"
    required property var section
    property bool compactNavigation: false
    readonly property var domains: ({
        general: settingsGeneral,
        integrations: settingsIntegrations,
        remote: settingsRemote
    })
    property bool restoring: true
    signal choose(var row, var owner)
    signal backRequested()

    function restorePosition() {
        Qt.callLater(function() {
            if (settingsNavigation.targetKey) {
                reveal(settingsNavigation.targetKey)
            } else {
                viewport.contentY = Math.min(
                    settingsNavigation.scrollPosition(section.id),
                    Math.max(0, viewport.contentHeight - viewport.height)
                )
            }
            restoring = false
        })
    }

    function reveal(key) {
        for (let i = 0; i < groups.count; i++) {
            const group = groups.itemAt(i)
            const row = group.reveal(key)
            if (row) {
                Qt.callLater(function() {
                    const pos = row.mapToItem(body, 0, 0)
                    viewport.contentY = Math.max(0, Math.min(
                        pos.y - 20, viewport.contentHeight - viewport.height
                    ))
                    row.focusControl()
                })
                return
            }
        }
    }

    Component.onCompleted: restorePosition()

    Connections {
        target: viewport
        function onContentYChanged() {
            if (!root.restoring)
                settingsNavigation.rememberScroll(root.section.id, viewport.contentY)
        }
    }

    Rectangle {
        id: compactHeader
        objectName: "settingsCompactHeader"
        visible: root.compactNavigation
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: visible ? 60 : 0
        color: appTheme.bg
        z: 2

        Loader {
            anchors.fill: parent
            anchors.leftMargin: 8
            anchors.rightMargin: 16
            anchors.topMargin: 8
            anchors.bottomMargin: 8
            active: compactHeader.visible
            sourceComponent: SettingsSectionHeader {
                section: root.section
                compact: true
                onBackRequested: root.backRequested()
            }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: appTheme.border
        }
    }

    Flickable {
        id: viewport
        objectName: "settingsPageViewport"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: compactHeader.visible ? compactHeader.bottom : parent.top
        anchors.bottom: parent.bottom
        anchors.rightMargin: 14
        clip: true
        contentWidth: width
        contentHeight: body.implicitHeight
        boundsBehavior: Flickable.DragAndOvershootBounds
        flickableDirection: Flickable.VerticalFlick

        ScrollBar.vertical: AppScrollBar {
            parent: root
            anchors.top: viewport.top
            anchors.right: parent.right
            anchors.bottom: parent.bottom
        }

        ColumnLayout {
            id: body
            width: viewport.width
            spacing: 0

            ColumnLayout {
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: Math.min(840, viewport.width)
                Layout.fillWidth: true
                Layout.maximumWidth: 840
                Layout.margins: viewport.width < 480 ? 16 : 28
                spacing: 16

                Loader {
                    Layout.fillWidth: true
                    active: !root.compactNavigation
                    visible: active
                    Layout.preferredHeight: active && item ? item.implicitHeight : 0
                    sourceComponent: SettingsSectionHeader {
                        section: root.section
                        compact: false
                    }
                }

                Repeater {
                    id: groups
                    model: root.section.groups
                    delegate: SettingsGroup {
                        required property var modelData
                        Layout.fillWidth: true
                        spec: modelData
                        domain: root.domains[spec.domain]
                        onChoose: (row, owner) => root.choose(row, owner)
                    }
                }

                Repeater {
                    model: root.section.id === "appearance"
                        || root.section.id === "media"
                        || root.section.id === "meetings"
                        || root.section.id === "projection" ? [settingsGeneral]
                        : root.section.id === "remote" ? [settingsRemote]
                        : root.section.id === "about" ? [] : [settingsIntegrations]
                    delegate: Text {
                        required property var modelData
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: modelData.state.feedback || ""
                        color: modelData.state.feedbackKind === "error"
                            ? appTheme.dangerText : appTheme.textMuted
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                        Accessible.role: Accessible.AlertMessage
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: root.section.id === "about"
                    spacing: 20
                    Text {
                        text: "Solin · " + settingsNavigation.version
                        color: appTheme.textPrimary
                        font.pixelSize: 20
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Audio & Video app for Kingdom Hall meetings.")
                        color: appTheme.textSecondary
                        font.pixelSize: 15
                        wrapMode: Text.Wrap
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 12
                        SettingsButton {
                            text: qsTr("Official Website")
                            onClicked: settingsNavigation.openLink("website")
                        }
                        SettingsButton {
                            text: qsTr("Changelog")
                            onClicked: settingsNavigation.openLink("changelog")
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("This app is independent and is not affiliated with or endorsed by the Watch Tower Bible and Tract Society of Pennsylvania or any of its associated organizations.")
                        color: appTheme.textMuted
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }
}
