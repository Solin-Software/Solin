import QtQuick 2.15
import QtQuick.Controls 2.15

MenuSeparator {
    id: root
    property var theme: null
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    implicitHeight: 9
    contentItem: Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        height: 1
        color: root.borderColor
    }
}
