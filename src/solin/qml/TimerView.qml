// TimerView.qml — root of the Timer tab. A header with a segmented switch flips
// between the advanced clock/meeting timer (default) and the legacy media-window
// countdown, with a crossfade + subtle slide between the two pages.
// Bound to the `timer` context property (TimerBridge).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: "#0d1117"

    // ── Shared palette (mirrors MeetingDetailView for one cohesive design) ────
    readonly property color bg: "#0d1117"
    readonly property color surface: "#13161c"
    readonly property color surfaceAlt: "#0f131a"
    readonly property color hover: "#1a1f2a"
    readonly property color border_: "#1e2430"
    readonly property color borderStrong: "#2a3040"
    readonly property color textPrimary: "#e6edf3"
    readonly property color textSecondary: "#c9d1d9"
    readonly property color textMuted: "#8b949e"
    readonly property color textDim: "#484f58"
    readonly property color accent: "#388bfd"
    readonly property color danger: "#f85149"

    readonly property int sideMargin: 20

    property int mode: 0  // 0 = advanced, 1 = media countdown

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header ────────────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 70

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: root.sideMargin
                anchors.rightMargin: root.sideMargin
                spacing: 14

                Rectangle {
                    Layout.preferredWidth: 40
                    Layout.preferredHeight: 40
                    Layout.alignment: Qt.AlignVCenter
                    radius: 11
                    color: Qt.rgba(0.22, 0.55, 0.99, 0.10)
                    border.color: Qt.rgba(0.22, 0.55, 0.99, 0.28)
                    border.width: 1
                    Image {
                        anchors.centerIn: parent
                        width: 20; height: 20
                        sourceSize.width: 40; sourceSize.height: 40
                        source: "image://timericons/clock/40/79c0ff"
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 2
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Timer")
                        color: root.textPrimary
                        font.pixelSize: 20
                        font.weight: Font.Bold
                        horizontalAlignment: Text.AlignLeft
                        elide: Text.ElideRight
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.mode === 0
                              ? qsTr("Clock & meeting parts on a dedicated monitor")
                              : qsTr("Project a countdown on the media window")
                        color: root.textMuted
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignLeft
                        elide: Text.ElideRight
                    }
                }

                TimerSegment {
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    options: [qsTr("Advanced"), qsTr("Media countdown")]
                    current: root.mode
                    accent: root.accent
                    segWidth: 140
                    segHeight: 36
                    onPicked: function(index) { root.mode = index }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: root.border_
            opacity: 0.75
        }

        // ── Pages (crossfade + slide) ─────────────────────────────────────────
        Item {
            id: pageHost
            Layout.fillWidth: true
            Layout.fillHeight: true

            AdvancedTimerPage {
                anchors.fill: parent
                opacity: root.mode === 0 ? 1.0 : 0.0
                visible: opacity > 0.01
                enabled: root.mode === 0
                Behavior on opacity { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                transform: Translate {
                    y: root.mode === 0 ? 0 : 14
                    Behavior on y { NumberAnimation { duration: 280; easing.type: Easing.OutCubic } }
                }

                pal_bg: root.bg
                pal_surface: root.surface
                pal_surfaceAlt: root.surfaceAlt
                pal_hover: root.hover
                pal_border: root.border_
                pal_borderStrong: root.borderStrong
                pal_textPrimary: root.textPrimary
                pal_textSecondary: root.textSecondary
                pal_textMuted: root.textMuted
                pal_textDim: root.textDim
                pal_accent: root.accent
                pal_danger: root.danger
            }

            MediaCountdownPage {
                anchors.fill: parent
                opacity: root.mode === 1 ? 1.0 : 0.0
                visible: opacity > 0.01
                enabled: root.mode === 1
                Behavior on opacity { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                transform: Translate {
                    y: root.mode === 1 ? 0 : 14
                    Behavior on y { NumberAnimation { duration: 280; easing.type: Easing.OutCubic } }
                }

                pal_bg: root.bg
                pal_surface: root.surface
                pal_surfaceAlt: root.surfaceAlt
                pal_hover: root.hover
                pal_border: root.border_
                pal_borderStrong: root.borderStrong
                pal_textPrimary: root.textPrimary
                pal_textSecondary: root.textSecondary
                pal_textMuted: root.textMuted
                pal_accent: root.accent
            }
        }
    }
}
