import QtQuick 2.15

Rectangle {
    id: root

    property string label: ""
    property var theme: null

    implicitWidth: Math.ceil(labelText.implicitWidth) + 10
    implicitHeight: 16
    radius: 5
    color: root.theme ? root.theme.hover : "#233043"
    border.width: 1
    border.color: root.theme ? root.theme.border_ : "#2c394b"

    Accessible.role: Accessible.StaticText
    Accessible.name: root.label

    Text {
        id: labelText
        anchors.centerIn: parent
        text: root.label
        color: root.theme ? root.theme.textMuted : "#768397"
        font.pixelSize: 8
        font.weight: Font.DemiBold
        font.capitalization: Font.AllUppercase
        font.letterSpacing: 0.5
    }
}
