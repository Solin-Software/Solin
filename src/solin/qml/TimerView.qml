// TimerView.qml — root of the Timer tab. A header with a segmented switch flips
// between the advanced clock/meeting timer (default) and the legacy media-window
// countdown, with a crossfade + subtle slide between the two pages.
// Bound to the `timer` context property (TimerBridge).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: appTheme.bg

    readonly property color bg: appTheme.bg
    readonly property color surface: appTheme.surface
    readonly property color surfaceAlt: appTheme.surfaceAlt
    readonly property color hover: appTheme.hover
    readonly property color border_: appTheme.border_
    readonly property color borderStrong: appTheme.borderStrong
    readonly property color textPrimary: appTheme.textPrimary
    readonly property color textSecondary: appTheme.textSecondary
    readonly property color textMuted: appTheme.textMuted
    readonly property color textDim: appTheme.textDim
    readonly property color accent: appTheme.accent
    readonly property color accentText: appTheme.accentText
    readonly property color danger: appTheme.danger

    readonly property int sideMargin: 20
    readonly property bool compactHeader: width < 560

    property int mode: 0  // 0 = advanced, 1 = media countdown

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header ────────────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: root.compactHeader ? 116 : 70

            RowLayout {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: 70
                anchors.leftMargin: root.sideMargin
                anchors.rightMargin: root.sideMargin
                spacing: 14

                Rectangle {
                    Layout.preferredWidth: 40
                    Layout.preferredHeight: 40
                    Layout.alignment: Qt.AlignVCenter
                    radius: 11
                    color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.10)
                    border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.28)
                    border.width: 1
                    Image {
                        anchors.centerIn: parent
                        width: 20; height: 20
                        sourceSize.width: 40; sourceSize.height: 40
                        source: "image://timericons/clock/40/" + root.iconHex(root.accentText)
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
                    objectName: "timerModeSelectorWide"
                    visible: !root.compactHeader
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    options: [qsTr("Advanced"), qsTr("Media countdown")]
                    current: root.mode
                    accent: root.accent
                    segWidth: 140
                    segHeight: 44
                    onPicked: function(index) { root.mode = index }
                }
            }

            TimerSegment {
                objectName: "timerModeSelectorCompact"
                visible: root.compactHeader
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.leftMargin: root.sideMargin
                anchors.rightMargin: root.sideMargin
                anchors.bottomMargin: 8
                options: [qsTr("Advanced"), qsTr("Media countdown")]
                current: root.mode
                accent: root.accent
                stretch: true
                segHeight: 40
                onPicked: function(index) { root.mode = index }
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
