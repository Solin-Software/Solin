pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    objectName: "libraryView"
    focus: true
    color: appTheme.bg

    readonly property int pagePadding: Math.max(16, Math.min(30, width * 0.035))
    property bool downloadsVisited: controller.activeSection === "downloads"

    onDownloadsVisitedChanged: {}
    Connections {
        target: controller
        function onStateChanged() {
            if (controller.activeSection === "downloads")
                root.downloadsVisited = true
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: root.pagePadding
        anchors.leftMargin: root.pagePadding
        anchors.rightMargin: root.pagePadding
        anchors.bottomMargin: root.pagePadding
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: pageSubtitle.visible ? 3 : 0

                Text {
                    Layout.fillWidth: true
                    text: controller.pageTitle
                    color: appTheme.textPrimary
                    font.pixelSize: Math.max(23, Math.min(28, root.width * 0.028))
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                }

                Text {
                    id: pageSubtitle
                    Layout.fillWidth: true
                    text: controller.pageSubtitle
                    visible: text !== ""
                    color: appTheme.textMuted
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
            }
        }

        Item { Layout.preferredHeight: 18 }

        Flickable {
            id: sectionFlick
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            contentWidth: sectionRow.width
            contentHeight: height
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.HorizontalFlick

            Row {
                id: sectionRow
                height: parent.height
                spacing: 6

                Repeater {
                    model: librarySectionModel

                    Rectangle {
                        id: sectionPill
                        required property string sectionId
                        required property string label

                        readonly property bool selected: controller.activeSection === sectionId
                        width: sectionLabel.implicitWidth + 24
                        height: 36
                        radius: 12
                        color: selected
                            ? appTheme.accentTint
                            : (sectionPointer.containsMouse ? appTheme.hover : "transparent")
                        border.width: selected || sectionPointer.containsMouse ? 1 : 0
                        border.color: selected ? appTheme.accent : appTheme.hoverBorder

                        Text {
                            id: sectionLabel
                            anchors.centerIn: parent
                            text: sectionPill.label
                            color: sectionPill.selected
                                ? appTheme.textPrimary
                                : appTheme.textSecondary
                            font.pixelSize: 12
                            font.weight: sectionPill.selected ? Font.DemiBold : Font.Medium
                        }

                        MouseArea {
                            id: sectionPointer
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onEntered: controller.pointerEnter()
                            onExited: controller.pointerExit()
                            onClicked: controller.setSection(sectionPill.sectionId)
                        }

                        Behavior on color { ColorAnimation { duration: 130 } }
                        Behavior on border.color { ColorAnimation { duration: 130 } }
                    }
                }
            }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse
                onWheel: function(event) {
                    var delta = event.pixelDelta.y !== 0
                        ? event.pixelDelta.y
                        : event.angleDelta.y * 0.35
                    sectionFlick.contentX = Math.max(
                        0,
                        Math.min(
                            sectionFlick.contentWidth - sectionFlick.width,
                            sectionFlick.contentX - delta
                        )
                    )
                    event.accepted = true
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: appTheme.border
            opacity: 0.72
        }

        Item { Layout.preferredHeight: 15 }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Loader {
                id: catalogLoader
                anchors.fill: parent
                visible: controller.activeSection !== "downloads"
                active: true
                sourceComponent: LibraryMediaSection {}
            }

            Loader {
                id: downloadsLoader
                anchors.fill: parent
                visible: controller.activeSection === "downloads"
                active: root.downloadsVisited
                sourceComponent: LibraryDownloadsSection {}
            }
        }
    }
}
