pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: control
    objectName: "librarySearchField"
    property bool dismissTextFocusBoundary: true

    property string placeholderText: ""
    property string value: ""
    signal edited(string text)

    implicitHeight: 42
    radius: 12
    color: input.activeFocus ? appTheme.surfaceInputFocus : appTheme.surface2
    border.width: 1
    border.color: input.activeFocus ? appTheme.accent : appTheme.border
    opacity: enabled ? 1 : 0.58

    function colorHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    onValueChanged: {
        if (input.text !== value)
            input.text = value
    }

    TapHandler {
        acceptedButtons: Qt.LeftButton
        onTapped: input.forceActiveFocus()
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 13
        anchors.rightMargin: 9
        spacing: 9

        Image {
            Layout.preferredWidth: 15
            Layout.preferredHeight: 15
            source: "image://libraryicons/search/15/" + control.colorHex(
                input.activeFocus ? appTheme.textMuted : appTheme.textDim
            )
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Text {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: control.placeholderText
                color: appTheme.textDim
                font.pixelSize: 13
                visible: input.text.length === 0 && !input.activeFocus
                elide: Text.ElideRight
            }

            TextInput {
                id: input
                objectName: "librarySearchInput"
                anchors.fill: parent
                color: appTheme.textSecondary
                selectionColor: appTheme.accentSelection
                selectedTextColor: appTheme.white
                font.pixelSize: 13
                verticalAlignment: TextInput.AlignVCenter
                clip: true
                onTextEdited: control.edited(text)
            }
        }

        Rectangle {
            Layout.preferredWidth: 24
            Layout.preferredHeight: 24
            radius: 8
            visible: input.text.length > 0
            color: clearPointer.containsMouse ? appTheme.hover : "transparent"

            Image {
                anchors.centerIn: parent
                width: 13
                height: 13
                source: "image://libraryicons/close/13/" + control.colorHex(
                    clearPointer.containsMouse ? appTheme.textSecondary : appTheme.textMuted
                )
            }

            MouseArea {
                id: clearPointer
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onEntered: controller.pointerEnter()
                onExited: controller.pointerExit()
                onClicked: {
                    input.text = ""
                    control.edited("")
                    input.forceActiveFocus()
                }
            }
        }
    }

    Behavior on color { ColorAnimation { duration: 130 } }
    Behavior on border.color { ColorAnimation { duration: 130 } }
    Behavior on opacity { NumberAnimation { duration: 120 } }
}
