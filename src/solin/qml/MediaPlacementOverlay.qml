// Shared placement picker for structured media inserts.
import QtQuick 2.15
import QtQuick.Controls 2.15
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

    Popup {
        id: placementCard
        objectName: "mediaPlacementPopup"
        anchors.centerIn: parent
        width: Math.min(root.width - 40, 340)
        height: Math.min(implicitHeight, Math.max(0, root.height - 40))
        padding: 16
        modal: true
        dim: false
        focus: true
        visible: root.visible
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        function focusInitialOption() {
            if (!opened)
                return
            var first = options.itemAt(0)
            if (first)
                first.forceActiveFocus(Qt.PopupFocusReason)
            else
                cancelButton.forceActiveFocus(Qt.PopupFocusReason)
        }
        onOpened: Qt.callLater(focusInitialOption)
        onClosed: {
            if (root.visible && root.hasBridge)
                root.placementBridge.cancelSelection()
        }

        background: Rectangle {
            radius: 12
            color: root.surface
            border.width: 1
            border.color: root.borderStrong
        }

        contentItem: ColumnLayout {
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

            Flickable {
                id: optionList
                objectName: "mediaPlacementOptions"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 0
                implicitHeight: optionColumn.implicitHeight
                contentHeight: optionColumn.implicitHeight
                contentWidth: width
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                function ensureVisible(item) {
                    var top = item.mapToItem(optionColumn, 0, 0).y
                    var next = contentY
                    if (top < contentY)
                        next = top
                    else if (top + item.height > contentY + height)
                        next = top + item.height - height
                    contentY = Math.max(0, Math.min(next, contentHeight - height))
                }

                ScrollBar.vertical: ScrollBar {
                    id: optionScrollBar
                    policy: ScrollBar.AsNeeded
                    activeFocusOnTab: false
                }

                ColumnLayout {
                    id: optionColumn
                    width: optionList.width - (optionScrollBar.visible ? optionScrollBar.width + 4 : 0)
                    spacing: 4

                    Repeater {
                        id: options
                        model: root.hasBridge ? root.placementBridge.placementOptions : []

                        PlacementButton {
                            required property var modelData
                            objectName: "mediaPlacementOption"
                            text: modelData.label
                            sectionOption: modelData.type === "section"
                            markerColor: modelData.color || root.accent
                            onClicked: {
                                if (root.hasBridge)
                                    root.placementBridge.confirmPlacement(modelData.id)
                            }
                        }
                    }
                }
            }

            PlacementButton {
                id: cancelButton
                objectName: "mediaPlacementCancel"
                Layout.topMargin: 4
                implicitHeight: 34
                secondary: true
                text: qsTranslate("JWMediaCatalogBridge", "Cancel")
                onClicked: {
                    if (root.hasBridge)
                        root.placementBridge.cancelSelection()
                }
            }
        }
    }

    component PlacementButton: Button {
        id: control
        property bool secondary: false
        property bool sectionOption: false
        property color markerColor: root.accent

        Layout.fillWidth: true
        implicitHeight: 38
        leftPadding: sectionOption ? 16 : 12
        rightPadding: 12
        activeFocusOnTab: true
        hoverEnabled: true
        Keys.onReturnPressed: clicked()
        Keys.onEnterPressed: clicked()
        onActiveFocusChanged: {
            if (activeFocus && !secondary)
                optionList.ensureVisible(control)
        }

        contentItem: Text {
            text: control.text
            color: control.hovered || control.activeFocus ? root.textPrimary
                   : control.secondary ? root.textMuted : root.textSecondary
            font.pixelSize: 13
            font.weight: Font.Medium
            verticalAlignment: Text.AlignVCenter
            horizontalAlignment: control.secondary ? Text.AlignHCenter : Text.AlignLeft
            elide: Text.ElideRight
        }

        background: Rectangle {
            radius: 8
            color: control.hovered || control.activeFocus ? root.hover
                   : control.secondary ? root.surfaceElevated : "transparent"
            border.width: control.hovered || control.activeFocus || control.secondary ? 1 : 0
            border.color: control.activeFocus ? root.accent
                          : control.secondary ? root.border_ : root.borderStrong
            Behavior on color { ColorAnimation { duration: 100 } }

            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: 4
                anchors.verticalCenter: parent.verticalCenter
                width: 3
                height: 18
                radius: 1.5
                color: control.markerColor
                visible: control.sectionOption
                opacity: 0.7
            }
        }
    }
}
