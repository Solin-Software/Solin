import QtQuick 2.15
import QtQuick.Controls 2.15

ToolTip {
    id: root
    delay: 400
    padding: 6
    implicitWidth: Math.min(360, contentItem.implicitWidth + leftPadding + rightPadding)

    contentItem: Text {
        text: root.text
        textFormat: Text.PlainText
        color: appTheme.textPrimary
        font.pixelSize: 12
        wrapMode: Text.Wrap
    }
    background: Rectangle {
        color: appTheme.surface
        radius: 6
        border.width: 1
        border.color: appTheme.border_
    }
}
