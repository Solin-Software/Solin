pragma Translator: "TalkThemeEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root

    property var bridge: null
    property var theme: null
    property color surface: theme ? theme.surface : "#172131"
    property color hover: theme ? theme.hover : "#233043"
    property color borderColor: theme ? theme.border_ : "#2c394b"
    property color borderStrong: theme ? theme.borderStrong : "#46566c"
    property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    property color textMuted: theme ? theme.textMuted : "#768397"
    property color accent: theme ? theme.accent : "#7367f0"
    signal deleteRequested(string presetId, string presetName)
    signal presetRequested(string presetId)

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function fontWeight(value) {
        if (value === "bold") return Font.Bold
        if (value === "semibold") return Font.DemiBold
        if (value === "medium") return Font.Medium
        return Font.Normal
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: root.width
            spacing: 10

            Text {
                Layout.leftMargin: 14
                Layout.rightMargin: 14
                Layout.topMargin: 14
                text: qsTr("Included styles")
                color: root.textSecondary
                font.pixelSize: 11
                font.weight: Font.DemiBold
                font.capitalization: Font.AllUppercase
                font.letterSpacing: 0.7
            }

            Repeater {
                model: root.bridge ? root.bridge.builtinPresets : []
                delegate: PresetCard {
                    required property var modelData
                    Layout.leftMargin: 12
                    Layout.rightMargin: 12
                    Layout.fillWidth: true
                    preset: modelData
                    onChosen: root.presetRequested(modelData.id)
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: 14
                Layout.rightMargin: 14
                Layout.topMargin: 5
                Layout.bottomMargin: 5
                Layout.preferredHeight: 1
                color: root.borderColor
                opacity: 0.75
            }

            RowLayout {
                Layout.leftMargin: 14
                Layout.rightMargin: 14
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: qsTr("My presets")
                    color: root.textSecondary
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.capitalization: Font.AllUppercase
                    font.letterSpacing: 0.7
                }
                Text {
                    visible: root.bridge && root.bridge.userPresets.length > 0
                    text: root.bridge ? String(root.bridge.userPresets.length) : ""
                    color: root.textMuted
                    font.pixelSize: 10
                }
            }

            ColumnLayout {
                visible: !root.bridge || root.bridge.userPresets.length === 0
                Layout.fillWidth: true
                Layout.leftMargin: 12
                Layout.rightMargin: 12
                Layout.preferredHeight: 86
                spacing: 4

                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("Your saved styles will appear here")
                    color: root.textSecondary
                    font.pixelSize: 11
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("Customize a theme and save it as a preset.")
                    color: root.textMuted
                    font.pixelSize: 10
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }

            Repeater {
                model: root.bridge ? root.bridge.userPresets : []
                delegate: PresetCard {
                    required property var modelData
                    Layout.leftMargin: 12
                    Layout.rightMargin: 12
                    Layout.fillWidth: true
                    preset: modelData
                    showDelete: true
                    onChosen: root.presetRequested(modelData.id)
                    onRemove: root.deleteRequested(modelData.id, modelData.name)
                }
            }

            Item { Layout.fillWidth: true; Layout.preferredHeight: 12 }
        }
    }

    component PresetCard: Rectangle {
        id: card
        property var preset: ({})
        readonly property var presetBackground: preset.background || ({})
        readonly property var presetPreview: preset.preview_layer || ({})
        property bool showDelete: false
        signal chosen()
        signal remove()

        Layout.preferredHeight: 88
        radius: 12
        clip: true
        color: root.surface
        border.width: card.preset.active ? 2 : 1
        border.color: card.preset.active ? root.accent
                      : cardMouse.containsMouse ? root.borderStrong : root.borderColor
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Rectangle {
            anchors.fill: parent
            color: card.presetBackground.base_color || root.surface
        }
        Image {
            anchors.fill: parent
            source: card.presetBackground.url || ""
            fillMode: Image.PreserveAspectCrop
            smooth: true
            asynchronous: true
        }
        Rectangle {
            anchors.fill: parent
            visible: Boolean(card.presetBackground.url)
            color: card.presetBackground.overlay_color || "#000000"
            opacity: Number(card.presetBackground.overlay_opacity || 0)
        }
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 35
            color: "#0d1523"
            opacity: 0.90
        }
        Text {
            anchors.left: parent.left
            anchors.right: activeMark.visible ? activeMark.left : parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: 12
            anchors.rightMargin: 6
            height: 35
            text: card.preset.name || ""
            color: "#f5f7fb"
            font.pixelSize: 11
            font.weight: Font.DemiBold
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        Item {
            id: previewDesign
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width
            height: width * 9 / 16

            Text {
                x: Number(card.presetPreview.x || 0) * parent.width
                y: Number(card.presetPreview.y || 0) * parent.height
                width: Number(card.presetPreview.width || 1) * parent.width
                text: card.presetPreview.text || ""
                color: card.presetPreview.color || "white"
                font.family: card.presetPreview.font_family || "Arial"
                font.pixelSize: Math.max(
                    7, Number(card.presetPreview.font_size || 0.08) * parent.height)
                font.weight: root.fontWeight(card.presetPreview.font_weight || "normal")
                font.letterSpacing: Number(card.presetPreview.letter_spacing || 0)
                horizontalAlignment: card.presetPreview.alignment === "center"
                    ? Text.AlignHCenter
                    : card.presetPreview.alignment === "right"
                        ? Text.AlignRight : Text.AlignLeft
                wrapMode: Text.Wrap
                lineHeightMode: Text.ProportionalHeight
                lineHeight: Number(card.presetPreview.line_height || 1.05)
                maximumLineCount: 3
                elide: Text.ElideRight
            }
        }
        Rectangle {
            id: activeMark
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: 9
            width: 18
            height: 18
            radius: 9
            visible: Boolean(card.preset.active)
            color: root.accent
            Text {
                anchors.centerIn: parent
                text: "✓"
                color: "white"
                font.pixelSize: 10
                font.weight: Font.Bold
            }
        }
        Rectangle {
            id: deleteButton
            z: 2
            visible: card.showDelete
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 7
            width: 22
            height: 22
            radius: 7
            color: deleteMouse.containsMouse ? (root.theme ? root.theme.dangerSubtle : "#492432") : "transparent"
            Image {
                anchors.centerIn: parent
                width: 13
                height: 13
                source: "image://talkthemeicons/trash/16/" + root.iconHex(
                    deleteMouse.containsMouse && root.theme ? root.theme.danger : root.textMuted)
            }
            MouseArea {
                id: deleteMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: function(mouse) {
                    mouse.accepted = true
                    card.remove()
                }
            }
            ToolTip.visible: deleteMouse.containsMouse
            ToolTip.text: qsTr("Delete preset")
        }
        MouseArea {
            id: cardMouse
            anchors.fill: parent
            z: 1
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: card.chosen()
        }
    }
}
