import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property bool dismissTextFocusBoundary: true
    property color restingColor: appTheme.surfaceChrome
    property real cornerRadius: 8
    property real focusedBorderWidth: 2
    property bool searchIcon: false
    property bool clearActionVisible: false
    signal clearRequested()
    implicitHeight: 44
    font.pixelSize: 15
    color: appTheme.textPrimary
    placeholderTextColor: appTheme.textMuted
    selectionColor: appTheme.accentSelection
    selectedTextColor: appTheme.textOnAccent
    leftPadding: searchIcon ? 42 : 12
    rightPadding: clearActionVisible ? 44 : 12
    topPadding: 10
    bottomPadding: 10
    selectByMouse: true
    HoverHandler {
        objectName: "textFieldPointer"
        cursorShape: control.enabled ? Qt.IBeamCursor : Qt.ArrowCursor
    }
    background: Rectangle {
        radius: control.cornerRadius
        color: control.activeFocus ? appTheme.surfaceInputFocus : control.restingColor
        border.width: control.activeFocus ? control.focusedBorderWidth : 1
        border.color: control.activeFocus ? appTheme.accent : appTheme.border
        Canvas {
            id: magnifier
            property color strokeColor: appTheme.textMuted
            visible: control.searchIcon
            width: 20
            height: 20
            anchors.left: parent.left
            anchors.leftMargin: 13
            anchors.verticalCenter: parent.verticalCenter
            onPaint: {
                const context = getContext("2d")
                context.reset()
                context.strokeStyle = strokeColor
                context.lineWidth = 1.7
                context.lineCap = "round"
                context.beginPath()
                context.arc(8.5, 8.5, 5.5, 0, Math.PI * 2)
                context.stroke()
                context.beginPath()
                context.moveTo(12.5, 12.5)
                context.lineTo(17.5, 17.5)
                context.stroke()
            }
            onStrokeColorChanged: requestPaint()
        }
    }
    AbstractButton {
        z: 2
        visible: control.clearActionVisible
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: 44
        height: 44
        focusPolicy: Qt.StrongFocus
        Accessible.name: qsTranslate("SettingsWidget", "Clear")
        AppPointerCursor {}
        onClicked: control.clearRequested()
        contentItem: Text {
            text: "×"
            color: parent.hovered ? appTheme.textPrimary : appTheme.textMuted
            font.pixelSize: 20
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: control.cornerRadius
            color: parent.hovered ? appTheme.hover : "transparent"
            border.width: parent.visualFocus ? 2 : 0
            border.color: appTheme.accent
        }
    }
}
