pragma ComponentBehavior: Bound
pragma Translator: "SettingsWidget"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    objectName: "settingsClockPicker"
    property int hour: 0
    property int minute: 0
    property string mode: "hour"
    property bool tracking: false
    readonly property real handAngle: mode === "hour" ? (hour % 12) * 30 : minute * 6
    spacing: 14

    function angleValue(x, y, steps) {
        const dx = x - face.width / 2
        const dy = y - face.height / 2
        let angle = Math.atan2(dx, -dy)
        if (angle < 0) angle += Math.PI * 2
        return Math.round(angle * steps / (Math.PI * 2)) % steps
    }

    function updateAt(x, y) {
        if (mode === "hour") {
            const dx = x - face.width / 2
            const dy = y - face.height / 2
            const ring = Math.sqrt(dx * dx + dy * dy) < 88 ? 12 : 0
            hour = angleValue(x, y, 12) + ring
        } else {
            minute = angleValue(x, y, 60)
        }
    }

    RowLayout {
        Layout.alignment: Qt.AlignHCenter
        spacing: 2
        AbstractButton {
            id: hourButton
            objectName: "clockHourMode"
            implicitWidth: hourText.implicitWidth + 16
            implicitHeight: 52
            onClicked: root.mode = "hour"
            Accessible.name: qsTr("Choose hour")
            AppPointerCursor {}
            contentItem: Text {
                id: hourText
                text: String(root.hour).padStart(2, "0")
                color: root.mode === "hour" ? appTheme.accentText : appTheme.textMuted
                font.pixelSize: 34
                font.weight: Font.Medium
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 8
                color: hourButton.hovered ? appTheme.hover : "transparent"
                border.width: hourButton.visualFocus ? 2 : 0
                border.color: appTheme.accent
            }
        }
        Text {
            text: ":"
            color: appTheme.textSecondary
            font.pixelSize: 34
            font.weight: Font.Medium
        }
        AbstractButton {
            id: minuteButton
            objectName: "clockMinuteMode"
            implicitWidth: minuteText.implicitWidth + 16
            implicitHeight: 52
            onClicked: root.mode = "minute"
            Accessible.name: qsTr("Choose minute")
            AppPointerCursor {}
            contentItem: Text {
                id: minuteText
                text: String(root.minute).padStart(2, "0")
                color: root.mode === "minute" ? appTheme.accentText : appTheme.textMuted
                font.pixelSize: 34
                font.weight: Font.Medium
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                radius: 8
                color: minuteButton.hovered ? appTheme.hover : "transparent"
                border.width: minuteButton.visualFocus ? 2 : 0
                border.color: appTheme.accent
            }
        }
    }

    Item {
        id: face
        objectName: "clockFace"
        Layout.alignment: Qt.AlignHCenter
        Layout.preferredWidth: 284
        Layout.preferredHeight: 284

        Rectangle {
            anchors.fill: parent
            radius: width / 2
            color: appTheme.surfaceAlt
            border.width: 1
            border.color: appTheme.border_
        }

        Rectangle {
            id: hand
            x: face.width / 2
            y: face.height / 2 - 1
            width: root.mode === "hour" && root.hour >= 12 ? 72 : 108
            height: 2
            radius: 1
            color: appTheme.accent
            transformOrigin: Item.Left
            rotation: root.handAngle - 90
            Behavior on rotation { enabled: !root.tracking; NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
            Behavior on width { enabled: !root.tracking; NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
        }

        Rectangle {
            anchors.centerIn: parent
            width: 7; height: 7; radius: 4
            color: appTheme.accent
        }

        Rectangle {
            readonly property real radiusFromCenter: root.mode === "hour" && root.hour >= 12 ? 72 : 108
            readonly property real radians: root.handAngle * Math.PI / 180
            x: face.width / 2 + Math.sin(radians) * radiusFromCenter - width / 2
            y: face.height / 2 - Math.cos(radians) * radiusFromCenter - height / 2
            width: 38; height: 38; radius: 19
            color: appTheme.accent
        }

        AppPointerCursor {
            anchors.fill: parent
            z: 10
            acceptedButtons: Qt.LeftButton
            preventStealing: true
            onPressed: mouse => {
                root.tracking = true
                root.updateAt(mouse.x, mouse.y)
            }
            onPositionChanged: mouse => {
                if (pressed)
                    root.updateAt(mouse.x, mouse.y)
            }
            onReleased: {
                root.tracking = false
                if (root.mode === "hour")
                    root.mode = "minute"
            }
            onCanceled: root.tracking = false
        }

        Repeater {
            model: root.mode === "hour" ? 24 : 12
            delegate: AbstractButton {
                id: numberButton
                required property int index
                readonly property int value: root.mode === "hour" ? index : index * 5
                readonly property int displayValue: value
                objectName: root.mode === "hour" ? "clockHour_" + value : "clockMinute_" + value
                readonly property real ringRadius: root.mode === "hour" && index >= 12 ? 72 : 108
                readonly property real angle: (root.mode === "hour" ? index % 12 : index) * Math.PI / 6
                readonly property bool selected: root.mode === "hour" ? root.hour === value : root.minute === value
                x: face.width / 2 + Math.sin(angle) * ringRadius - width / 2
                y: face.height / 2 - Math.cos(angle) * ringRadius - height / 2
                width: 40
                height: 40
                Accessible.name: root.mode === "hour"
                    ? qsTr("Hour %1").arg(String(value).padStart(2, "0"))
                    : qsTr("Minute %1").arg(String(value).padStart(2, "0"))
                onClicked: {
                    if (root.mode === "hour") {
                        root.hour = value
                        root.mode = "minute"
                    } else {
                        root.minute = value
                    }
                }
                contentItem: Text {
                    text: String(numberButton.value).padStart(2, "0")
                    color: numberButton.selected ? appTheme.textOnAccent
                        : numberButton.index >= 12 && root.mode === "hour" ? appTheme.textPrimary
                        : appTheme.textSecondary
                    font.pixelSize: root.mode === "hour" && numberButton.index >= 12 ? 12 : 13
                    font.weight: numberButton.selected ? Font.DemiBold : Font.Normal
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 20
                    color: numberButton.selected ? appTheme.accent
                        : numberButton.hovered ? appTheme.hoverStrong : "transparent"
                    border.width: numberButton.visualFocus ? 2 : 0
                    border.color: appTheme.accent
                }
            }
        }
    }

}
