import QtQuick
import QtQuick.Layouts

Item {
    id: root

    property var alert: ({})
    property bool reduceMotion: false
    property var readoutFace: ({})

    readonly property bool hasNames: alert.kind === "automatic" && alert.count > 0
    property var presentedAlert: ({})
    property var presentedReadout: ({})

    onAlertChanged: {
        if (alert.kind === "automatic")
            presentedAlert = alert
    }
    onReadoutFaceChanged: {
        if (alert.kind === "automatic" && readoutFace.primary_text)
            presentedReadout = readoutFace
    }
    Component.onCompleted: {
        if (hasNames) {
            presentedAlert = alert
            presentedReadout = readoutFace
        }
    }

    component PresenceLayer: Loader {
        required property bool shown
        opacity: shown ? 1 : 0
        active: shown || opacity > 0
        visible: active
        z: shown ? 1 : 0

        Behavior on opacity {
            enabled: !root.reduceMotion
            NumberAnimation { id: fade; duration: 150; easing.type: Easing.OutCubic }
        }
        Connections {
            target: root
            function onReduceMotionChanged() {
                if (root.reduceMotion && fade.running)
                    fade.complete()
            }
        }
    }

    PresenceLayer {
        anchors.fill: parent
        shown: root.hasNames
        sourceComponent: automatic
        onActiveChanged: {
            if (!active) {
                root.presentedAlert = ({})
                root.presentedReadout = ({})
            }
        }
    }

    PresenceLayer {
        anchors.fill: parent
        shown: root.alert.kind === "manual"
        sourceComponent: manual
    }

    component HandIcon: Image {
        required property color tint
        property string iconName: "raised_hand"
        readonly property int rasterSize: Math.ceil(width * Screen.devicePixelRatio)

        height: width
        fillMode: Image.PreserveAspectFit
        source: rasterSize > 0
                ? "image://timericons/" + iconName + "/" + rasterSize + "/" + String(tint).slice(1)
                : ""
        sourceSize: Qt.size(rasterSize, rasterSize)
    }

    Component {
        id: manual

        Item {
            HandIcon {
                id: hand
                objectName: "clockHandAlertManual"
                readonly property real span: Math.min(parent.width, parent.height)

                anchors.centerIn: parent
                width: Math.round(span * 0.60)
                iconName: "raised_hand_filled"
                tint: "#ffd75e"
                opacity: 0.81

                SequentialAnimation on scale {
                    running: hand.visible && !root.reduceMotion
                    loops: Animation.Infinite
                    onStopped: hand.scale = 1.0

                    NumberAnimation { from: 1.0; to: 1.05; duration: 1600; easing.type: Easing.InOutSine }
                    NumberAnimation { from: 1.05; to: 1.0; duration: 1600; easing.type: Easing.InOutSine }
                }
            }
        }
    }

    Component {
        id: automatic

        Item {
            id: overlay
            readonly property real unit: Math.max(0.6, Math.min(width / 960, height / 540))
            readonly property var names: root.presentedAlert.names || []
            readonly property int total: root.presentedAlert.count || 0
            readonly property color foreground: "#f6f8fc"
            readonly property color accent: "#a9d3ff"
            readonly property color divider: "#2f4055"
            readonly property int inset: Math.round(20 * unit)
            readonly property int headerHeight: Math.round(57 * unit)
            readonly property int rowHeight: Math.round(50 * unit)
            readonly property int footerHeight: Math.round(35 * unit)
            readonly property int shownRows: {
                const available = Math.min(7, names.length, total)
                const room = height * 0.90 - headerHeight
                if (available === total && available * rowHeight <= room)
                    return available
                return Math.max(0, Math.min(available, Math.floor((room - footerHeight) / rowHeight)))
            }
            readonly property int remaining: Math.max(0, total - shownRows)

            Rectangle {
                anchors.fill: parent
                color: "#000000"
                opacity: 0.32
            }

            Rectangle {
                objectName: "clockHandAlertCard"
                anchors.centerIn: parent
                width: Math.min(parent.width * 0.92,
                                Math.max(420 * overlay.unit, Math.min(540 * overlay.unit, parent.width * 0.54)))
                height: Math.min(parent.height, content.implicitHeight)
                radius: Math.round(16 * overlay.unit)
                antialiasing: true
                border.color: "#526782"
                clip: true
                gradient: Gradient {
                    GradientStop { position: 0; color: "#1b2839" }
                    GradientStop { position: 1; color: "#0e1623" }
                }

                Column {
                    id: content
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: overlay.inset

                    Item {
                        width: parent.width
                        height: overlay.headerHeight

                        RowLayout {
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: Math.min(implicitWidth, parent.width - (readout.visible ? readout.width + spacing : 0))
                            spacing: Math.round(10 * overlay.unit)

                            HandIcon {
                                Layout.preferredWidth: Math.round(24 * overlay.unit)
                                Layout.preferredHeight: Layout.preferredWidth
                                tint: overlay.accent
                            }

                            Text {
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                text: qsTranslate("ZoomPanel", "Raised hands")
                                color: overlay.foreground
                                font.pixelSize: Math.round(22 * overlay.unit)
                                font.weight: Font.DemiBold
                            }

                            Rectangle {
                                implicitWidth: Math.max(height, count.implicitWidth + Math.round(14 * overlay.unit))
                                implicitHeight: Math.round(28 * overlay.unit)
                                radius: height / 2
                                color: "#253c55"

                                TimerDigits {
                                    id: count
                                    anchors.centerIn: parent
                                    text: overlay.total.toLocaleString(Qt.locale(), "f", 0)
                                    color: overlay.accent
                                    font.pixelSize: Math.round(16 * overlay.unit)
                                    font.weight: Font.DemiBold
                                }
                            }
                        }

                        DigitalClockReadout {
                            id: readout
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            visible: primaryText.length > 0 && width <= parent.width / 2
                            primaryText: root.presentedReadout.primary_text || ""
                            secondsText: root.presentedReadout.seconds_text || ""
                            secondaryText: root.presentedReadout.secondary_text || ""
                            digitColor: root.presentedReadout.overrun ? "#ff5555" : "#e1e9f6"
                            primaryPx: Math.round(33 * overlay.unit)
                            secondaryRatio: 0.36
                        }
                    }

                    Repeater {
                        model: overlay.shownRows

                        Item {
                            id: row
                            required property int index
                            width: content.width
                            height: overlay.rowHeight

                            Rectangle {
                                width: parent.width
                                height: 1
                                color: overlay.divider
                            }

                            Text {
                                anchors.fill: parent
                                verticalAlignment: Text.AlignVCenter
                                text: overlay.names[row.index] || ""
                                textFormat: Text.PlainText
                                maximumLineCount: 1
                                elide: Text.ElideRight
                                color: overlay.foreground
                                font.pixelSize: Math.round(30 * overlay.unit)
                                font.weight: Font.DemiBold
                            }
                        }
                    }

                    Item {
                        width: parent.width
                        height: overlay.footerHeight
                        visible: overlay.remaining > 0

                        Rectangle {
                            width: parent.width
                            height: 1
                            color: overlay.divider
                        }

                        TimerDigits {
                            anchors.centerIn: parent
                            text: "+" + overlay.remaining.toLocaleString(Qt.locale(), "f", 0)
                            color: "#b3c4db"
                            font.pixelSize: Math.round(16 * overlay.unit)
                            font.weight: Font.DemiBold
                        }
                    }
                }
            }
        }
    }
}
