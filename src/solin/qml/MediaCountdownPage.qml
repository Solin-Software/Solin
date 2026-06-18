// MediaCountdownPage.qml — the media-window countdown: pick a target time-of-day
// or a manual duration, projected on the media window.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: page
    property color pal_bg: "#0d1117"
    property color pal_surface: "#13161c"
    property color pal_surfaceAlt: "#0f131a"
    property color pal_hover: "#1a1f2a"
    property color pal_border: "#1e2430"
    property color pal_borderStrong: "#2a3040"
    property color pal_textPrimary: "#e6edf3"
    property color pal_textSecondary: "#c9d1d9"
    property color pal_textMuted: "#8b949e"
    property color pal_accent: "#388bfd"

    property string nowText: "--:--:--"
    property int hourValue: (new Date().getHours() + 1) % 24
    property int minValue: 0

    function pad(n) { return ("0" + n).slice(-2) }

    Timer {
        interval: 1000; running: true; repeat: true; triggeredOnStart: true
        onTriggered: page.nowText = Qt.formatTime(new Date(), "HH:mm:ss")
    }

    Rectangle {
        anchors.centerIn: parent
        width: Math.max(0, Math.min(500, parent.width - 48))
        radius: 14
        color: page.pal_surface
        border.color: page.pal_border
        border.width: 1
        implicitHeight: col.implicitHeight + 48

        ColumnLayout {
            id: col
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            // ── Wall clock ────────────────────────────────────────────────────
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 10
                Text {
                    text: qsTr("Now")
                    color: page.pal_textMuted
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.2
                    font.capitalization: Font.AllUppercase
                }
                Text {
                    text: page.nowText
                    color: page.pal_textSecondary
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
            }

            // ── Target time-of-day ────────────────────────────────────────────
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: qsTr("Reach zero at")
                color: page.pal_textMuted
                font.pixelSize: 11
                font.weight: Font.DemiBold
                font.letterSpacing: 1.4
                font.capitalization: Font.AllUppercase
            }

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 8

                TimerStepper {
                    text: page.pad(page.hourValue)
                    accent: page.pal_accent
                    fieldWidth: 50
                    editable: true
                    decTip: qsTr("−1 h"); incTip: qsTr("+1 h")
                    onDecremented: page.hourValue = (page.hourValue + 23) % 24
                    onIncremented: page.hourValue = (page.hourValue + 1) % 24
                    onEdited: function(v) {
                        var n = parseInt(v, 10); if (!isNaN(n)) page.hourValue = ((n % 24) + 24) % 24
                    }
                }
                Text {
                    text: ":"
                    color: page.pal_textPrimary
                    font.pixelSize: 22; font.weight: Font.Bold
                    Layout.alignment: Qt.AlignVCenter
                }
                TimerStepper {
                    text: page.pad(page.minValue)
                    accent: page.pal_accent
                    fieldWidth: 50
                    editable: true
                    decTip: qsTr("−1 min"); incTip: qsTr("+1 min")
                    onDecremented: page.minValue = (page.minValue + 59) % 60
                    onIncremented: page.minValue = (page.minValue + 1) % 60
                    onEdited: function(v) {
                        var n = parseInt(v, 10); if (!isNaN(n)) page.minValue = ((n % 60) + 60) % 60
                    }
                }
            }

            TimerButton {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                text: qsTr("Start countdown to this time")
                variant: "primary"
                accent: page.pal_accent
                iconName: "play"; iconSize: 13
                onClicked: timer.startCountdownToTime(page.hourValue, page.minValue)
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: page.pal_border; opacity: 0.7 }

            // ── Manual-duration presets ───────────────────────────────────────
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: qsTr("Or count down for")
                color: page.pal_textMuted
                font.pixelSize: 11
                font.weight: Font.DemiBold
                font.letterSpacing: 1.4
                font.capitalization: Font.AllUppercase
            }

            GridLayout {
                Layout.fillWidth: true
                columns: width < 390 ? 3 : 5
                columnSpacing: 8
                rowSpacing: 8
                Repeater {
                    model: [5, 10, 15, 20, 30]
                    delegate: Rectangle {
                        required property int modelData
                        Layout.fillWidth: true
                        Layout.preferredHeight: 40
                        radius: 8
                        color: chipMa.containsMouse
                               ? Qt.rgba(0.22, 0.55, 0.99, 0.14) : page.pal_surfaceAlt
                        border.width: 1
                        border.color: chipMa.containsMouse ? page.pal_accent : page.pal_border
                        scale: chipMa.pressed ? 0.95 : 1.0
                        Behavior on color { ColorAnimation { duration: 130 } }
                        Behavior on border.color { ColorAnimation { duration: 130 } }
                        Behavior on scale { NumberAnimation { duration: 90 } }

                        ColumnLayout {
                            anchors.centerIn: parent
                            spacing: -1
                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                text: "" + modelData
                                color: chipMa.containsMouse ? "#79c0ff" : page.pal_textPrimary
                                font.pixelSize: 17; font.weight: Font.Bold
                                Behavior on color { ColorAnimation { duration: 130 } }
                            }
                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                text: qsTr("min")
                                color: page.pal_textMuted
                                font.pixelSize: 10
                            }
                        }
                        MouseArea {
                            id: chipMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: timer.startCountdownMinutes(modelData)
                        }
                    }
                }
            }
        }
    }
}
