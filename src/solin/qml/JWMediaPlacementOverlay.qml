// JWMediaPlacementOverlay.qml — shared placement picker for JW media inserts.
import QtQuick 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    anchors.fill: parent
    color: Qt.rgba(0, 0, 0, 0.50)
    visible: hasBridge && placementBridge.showPlacement
    opacity: visible ? 1 : 0
    z: 100

    property var placementBridge: null
    readonly property bool hasBridge: placementBridge !== null
    readonly property string pendingTitle: hasBridge ? placementBridge.pendingItemTitle : ""
    readonly property string pendingThumb: hasBridge ? placementBridge.pendingItemThumb : ""

    property color surface: appTheme.surface
    property color surfaceElevated: appTheme.hoverStrong
    property color hover: appTheme.hover
    property color border_: appTheme.border_
    property color borderStrong: appTheme.borderStrong
    property color textPrimary: appTheme.textPrimary
    property color textSecondary: appTheme.textSecondary
    property color textMuted: appTheme.textMuted
    property color accent: appTheme.accent

    Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

    MouseArea {
        anchors.fill: parent
        onClicked: {
            if (root.hasBridge)
                root.placementBridge.cancelSelection()
        }
    }

    PlacementPopup {
        id: placementCard
        anchors.centerIn: parent
        width: Math.min(parent.width - 40, 340)
    }

    component PlacementPopup: Rectangle {
        id: popup
        height: popupContent.implicitHeight + 32
        radius: 12
        color: root.surface
        border.width: 1
        border.color: root.borderStrong

        MouseArea {
            anchors.fill: parent
            z: 0
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onClicked: function(mouse) { mouse.accepted = true }
            onPressed: function(mouse) { mouse.accepted = true }
            onReleased: function(mouse) { mouse.accepted = true }
        }

        transform: Translate {
            y: root.visible ? 0 : 24
            Behavior on y { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
        }

        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            z: -1
            radius: parent.radius + 1
            color: Qt.rgba(0, 0, 0, 0.3)
        }

        ColumnLayout {
            id: popupContent
            z: 1
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 16
            spacing: 12

            Text {
                text: qsTranslate("JWMediaCatalogBridge", "Where to add?")
                color: root.textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Rectangle {
                    Layout.preferredWidth: visible ? 72 : 0
                    Layout.preferredHeight: visible ? 40 : 0
                    radius: 6
                    color: root.surfaceElevated
                    clip: true
                    visible: root.pendingThumb !== ""

                    Image {
                        anchors.fill: parent
                        source: root.pendingThumb
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        visible: status === Image.Ready
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: root.pendingTitle
                    color: root.textSecondary
                    font.pixelSize: 12
                    font.weight: Font.Medium
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.Wrap
                    lineHeight: 1.2
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: root.border_
                opacity: 0.5
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4

                Repeater {
                    model: root.hasBridge ? root.placementBridge.placementOptions : []

                    Rectangle {
                        id: optionBtn
                        required property var modelData

                        Layout.fillWidth: true
                        height: 38
                        radius: 8
                        color: optionMa.containsMouse ? root.hover : "transparent"
                        border.width: optionMa.containsMouse ? 1 : 0
                        border.color: optionMa.containsMouse ? root.borderStrong : "transparent"

                        Behavior on color { ColorAnimation { duration: 100 } }

                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: 4
                            anchors.verticalCenter: parent.verticalCenter
                            width: 3
                            height: 18
                            radius: 1.5
                            color: optionBtn.modelData.color || root.accent
                            visible: optionBtn.modelData.type === "section"
                            opacity: 0.7
                        }

                        Text {
                            anchors.left: parent.left
                            anchors.leftMargin: optionBtn.modelData.type === "section" ? 16 : 12
                            anchors.right: parent.right
                            anchors.rightMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            text: optionBtn.modelData.label
                            color: optionMa.containsMouse ? root.textPrimary : root.textSecondary
                            font.pixelSize: 13
                            font.weight: Font.Medium
                            elide: Text.ElideRight

                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        MouseArea {
                            id: optionMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (root.hasBridge)
                                    root.placementBridge.confirmPlacement(optionBtn.modelData.id)
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: 4
                height: 34
                radius: 8
                color: cancelBtnMa.containsMouse ? root.hover : root.surfaceElevated
                border.width: 1
                border.color: root.border_

                Behavior on color { ColorAnimation { duration: 100 } }

                Text {
                    anchors.centerIn: parent
                    text: qsTranslate("JWMediaCatalogBridge", "Cancel")
                    color: cancelBtnMa.containsMouse ? root.textPrimary : root.textMuted
                    font.pixelSize: 13
                    font.weight: Font.Medium

                    Behavior on color { ColorAnimation { duration: 100 } }
                }

                MouseArea {
                    id: cancelBtnMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (root.hasBridge)
                            root.placementBridge.cancelSelection()
                    }
                }
            }
        }
    }
}
