import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: control
    property bool primary: false
    property bool destructive: false
    property string trailingIcon: ""
    implicitHeight: 44
    implicitWidth: Math.max(88, label.implicitWidth + (trailingIcon ? 50 : 28))
    font.pixelSize: 14
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    AppPointerCursor {}
    contentItem: RowLayout {
        spacing: 8
        Text {
            id: label
            Layout.fillWidth: true
            text: control.text
            font: control.font
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            color: !control.enabled ? appTheme.textMuted : control.primary ? appTheme.textOnAccent : control.destructive ? appTheme.dangerText : appTheme.textPrimary
            elide: Text.ElideRight
        }
        SettingsIcon {
            visible: control.trailingIcon.length > 0
            name: control.trailingIcon
            tint: !control.enabled ? appTheme.textMuted : control.primary ? appTheme.textOnAccent : appTheme.textSecondary
            width: 17
            height: 17
        }
    }
    background: Rectangle {
        radius: 8
        color: control.primary ? (control.down ? appTheme.accentPressed : appTheme.accent) : control.hovered ? appTheme.hover : appTheme.surfaceChrome
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? appTheme.accent : appTheme.border
        opacity: control.enabled ? 1 : 0.55
    }
}
