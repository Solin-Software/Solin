pragma Translator: "TalkThemeEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    objectName: "talkThemeSidebar"

    property var bridge: null
    property var theme: null
    property string currentTab: "styles"
    property bool navigationVisible: true
    property color panelColor: theme ? theme.surface : "#172131"
    property color subtlePanel: theme ? theme.bg : "#111927"
    property color borderColor: theme ? theme.border_ : "#2c394b"
    property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    property color accent: theme ? theme.accent : "#7367f0"
    signal deletePresetRequested(string presetId, string presetName)
    signal presetRequested(string presetId)

    color: root.panelColor
    border.width: 1
    border.color: root.borderColor

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            id: navigationHeader
            objectName: "talkThemeSidebarNavigation"
            visible: root.navigationVisible
            Layout.fillWidth: true
            Layout.preferredHeight: root.navigationVisible ? 58 : 0
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: root.borderColor
            }
            Rectangle {
                anchors.fill: parent
                anchors.margins: 10
                radius: 10
                color: root.subtlePanel
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 3
                    spacing: 3
                    TabButton { value: "styles"; label: qsTr("Styles") }
                    TabButton { value: "layers"; label: qsTr("Layers") }
                }
            }
        }

        TalkThemePresetLibrary {
            visible: root.currentTab === "styles"
            Layout.fillWidth: true
            Layout.fillHeight: true
            bridge: root.bridge
            theme: root.theme
            onPresetRequested: function(presetId) { root.presetRequested(presetId) }
            onDeleteRequested: function(presetId, presetName) {
                root.deletePresetRequested(presetId, presetName)
            }
        }

        TalkThemeLayerPanel {
            visible: root.currentTab === "layers"
            Layout.fillWidth: true
            Layout.fillHeight: true
            bridge: root.bridge
            theme: root.theme
        }
    }

    component TabButton: Rectangle {
        id: tab
        property string value: ""
        property string label: ""
        Layout.fillWidth: true
        Layout.fillHeight: true
        radius: 8
        color: root.currentTab === value ? root.panelColor
              : tabMouse.containsMouse ? (root.theme ? root.theme.hover : "#233043") : "transparent"
        Text {
            anchors.centerIn: parent
            text: tab.label
            color: root.currentTab === tab.value ? root.textPrimary : root.textSecondary
            font.pixelSize: 11
            font.weight: root.currentTab === tab.value ? Font.DemiBold : Font.Medium
        }
        MouseArea {
            id: tabMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.currentTab = tab.value
        }
    }
}
