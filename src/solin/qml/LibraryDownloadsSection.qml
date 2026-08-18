pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: root
    property real shimmerPhase: 0

    NumberAnimation on shimmerPhase {
        from: 0
        to: 1
        duration: 1900
        loops: Animation.Infinite
        easing.type: Easing.Linear
        running: controller.downloadsLoading && !controller.downloadsHasItems
    }

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function fileNameRepeatsTitle(title, fileName) {
        var dot = fileName.lastIndexOf(".")
        var stem = dot > 0 ? fileName.slice(0, dot) : fileName
        return stem.trim().toLowerCase() === title.trim().toLowerCase()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    Layout.fillWidth: true
                    text: controller.sectionTitle
                    color: appTheme.textPrimary
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: controller.sectionSubtitle
                    color: appTheme.textMuted
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
            }

            Text {
                text: controller.downloadsSummary
                color: appTheme.textMuted
                font.pixelSize: 11
                visible: !controller.downloadsLoading
            }

            LibraryIconButton {
                iconName: "refresh"
                toolTipText: controller.refreshTooltip
                enabled: !controller.downloadsLoading && !controller.downloadsDeleting
                onClicked: controller.refreshDownloads()
            }
        }

        LibrarySearchField {
            Layout.fillWidth: true
            enabled: !controller.downloadsDeleting
            placeholderText: controller.searchPlaceholder
            value: controller.searchText
            onEdited: function(text) { controller.setDownloadsSearchText(text) }
        }

        Flickable {
            id: filterFlick
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            contentWidth: filterRow.width
            contentHeight: height
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.HorizontalFlick

            Row {
                id: filterRow
                height: parent.height
                spacing: 6

                Repeater {
                    model: [
                        { "id": "all", "label": controller.downloadsAllLabel, "visible": true },
                        { "id": "video", "label": controller.downloadsVideoLabel, "visible": controller.downloadsVideoLabel.split("  ").pop() !== "0" },
                        { "id": "audio", "label": controller.downloadsAudioLabel, "visible": controller.downloadsAudioLabel.split("  ").pop() !== "0" },
                        { "id": "image", "label": controller.downloadsImageLabel, "visible": controller.downloadsImageLabel.split("  ").pop() !== "0" }
                    ]

                    Rectangle {
                        id: chip
                        required property var modelData
                        readonly property bool selected: controller.downloadsFilter === modelData.id
                        visible: modelData.visible
                        width: visible ? chipLabel.implicitWidth + 22 : 0
                        height: 30
                        radius: 10
                        color: selected
                            ? appTheme.accentTint
                            : (chipPointer.containsMouse ? appTheme.hover : "transparent")
                        border.width: selected || chipPointer.containsMouse ? 1 : 0
                        border.color: selected ? appTheme.accent : appTheme.hoverBorder

                        Text {
                            id: chipLabel
                            anchors.centerIn: parent
                            text: chip.modelData.label
                            color: chip.selected ? appTheme.textPrimary : appTheme.textMuted
                            font.pixelSize: 11
                            font.weight: chip.selected ? Font.DemiBold : Font.Medium
                        }

                        MouseArea {
                            id: chipPointer
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: !controller.downloadsDeleting
                            cursorShape: Qt.PointingHandCursor
                            onEntered: controller.pointerEnter()
                            onExited: controller.pointerExit()
                            onClicked: controller.setDownloadsFilter(chip.modelData.id)
                        }
                    }
                }
            }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse
                onWheel: function(event) {
                    var delta = event.pixelDelta.y !== 0
                        ? event.pixelDelta.y
                        : event.angleDelta.y * 0.35
                    filterFlick.contentX = Math.max(
                        0,
                        Math.min(
                            filterFlick.contentWidth - filterFlick.width,
                            filterFlick.contentX - delta
                        )
                    )
                    event.accepted = true
                }
            }
        }

        Rectangle {
            id: selectionToolbar
            objectName: "downloadsSelectionToolbar"
            readonly property bool compactActions: width < 500
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 46 : 0
            visible: controller.downloadsHasSelection
            enabled: !controller.downloadsDeleting
            opacity: visible ? (enabled ? 1 : 0.58) : 0
            scale: visible ? 1 : 0.992
            radius: 13
            color: appTheme.surface2
            border.width: 1
            border.color: appTheme.borderStrong
            clip: true

            Behavior on opacity { NumberAnimation { duration: 150 } }
            Behavior on scale {
                NumberAnimation { duration: 170; easing.type: Easing.OutCubic }
            }

            Item {
                anchors.fill: parent

                Rectangle {
                    id: selectionBadge
                    anchors.left: parent.left
                    anchors.leftMargin: 9
                    anchors.verticalCenter: parent.verticalCenter
                    width: 28
                    height: 28
                    radius: 9
                    color: appTheme.accentTint
                    border.width: 1
                    border.color: appTheme.accentMuted

                    Image {
                        anchors.centerIn: parent
                        width: 13
                        height: 13
                        source: "image://libraryicons/check/13/"
                            + root.iconHex(appTheme.accent)
                    }
                }

                Row {
                    anchors.left: selectionBadge.right
                    anchors.leftMargin: 8
                    anchors.right: selectionActions.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    height: Math.max(selectionCount.implicitHeight, selectionSize.implicitHeight)
                    spacing: 6
                    clip: true

                    Text {
                        id: selectionCount
                        objectName: "downloadsSelectionCount"
                        text: controller.downloadsSelectionText
                        color: appTheme.textPrimary
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }

                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 3
                        height: 3
                        radius: 2
                        color: appTheme.textFaint
                    }

                    Text {
                        id: selectionSize
                        objectName: "downloadsSelectionSize"
                        text: controller.downloadsSelectionSize
                        color: appTheme.textDim
                        font.pixelSize: 10
                    }
                }

                Row {
                    id: selectionActions
                    anchors.right: parent.right
                    anchors.rightMargin: 7
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6

                    Rectangle {
                        id: selectVisibleAction
                    objectName: "downloadsSelectVisibleAction"
                    readonly property real actionWidth: selectionToolbar.compactActions
                        ? 32
                        : selectVisibleLabel.implicitWidth + 39
                    width: actionWidth
                    height: 32
                    radius: 10
                    color: selectPointer.containsMouse ? appTheme.hover : appTheme.surface
                    border.width: 1
                    border.color: selectPointer.containsMouse
                        ? appTheme.hoverBorder
                        : appTheme.border

                    RowLayout {
                        anchors.centerIn: parent
                        spacing: 6

                        Image {
                            Layout.preferredWidth: 13
                            Layout.preferredHeight: 13
                            source: "image://libraryicons/select_all/13/"
                                + root.iconHex(
                                    selectPointer.containsMouse
                                        ? appTheme.textSecondary
                                        : appTheme.textMuted
                                )
                        }

                        Text {
                            id: selectVisibleLabel
                            visible: !selectionToolbar.compactActions
                            text: controller.downloadsSelectAllText
                            color: appTheme.textSecondary
                            font.pixelSize: 10
                            font.weight: Font.Medium
                        }
                    }

                    MouseArea {
                        id: selectPointer
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onEntered: controller.pointerEnter()
                        onExited: controller.pointerExit()
                        onClicked: controller.toggleSelectAllDownloads()
                    }

                    ToolTip {
                        id: selectTooltip
                        visible: selectPointer.containsMouse
                            && selectionToolbar.compactActions
                        text: controller.downloadsSelectAllText
                        delay: 420
                        padding: 7
                        contentItem: Text {
                            text: selectTooltip.text
                            color: appTheme.textPrimary
                            font.pixelSize: 12
                        }
                        background: Rectangle {
                            color: appTheme.surfaceChrome
                            radius: 7
                            border.width: 1
                            border.color: appTheme.border
                        }
                    }

                    Behavior on color { ColorAnimation { duration: 120 } }
                    Behavior on border.color { ColorAnimation { duration: 120 } }
                }

                    Rectangle {
                        id: deleteAction
                    objectName: "downloadsDeleteAction"
                    readonly property real actionWidth: selectionToolbar.compactActions
                        ? 32
                        : deleteLabel.implicitWidth + 39
                    width: actionWidth
                    height: 32
                    radius: 10
                    color: deletePointer.containsMouse
                        ? appTheme.dangerSubtle
                        : appTheme.surface
                    border.width: 1
                    border.color: deletePointer.containsMouse
                        ? appTheme.danger
                        : appTheme.border

                    RowLayout {
                        anchors.centerIn: parent
                        spacing: 6
                        Image {
                            Layout.preferredWidth: 13
                            Layout.preferredHeight: 13
                            source: "image://libraryicons/trash/13/"
                                + root.iconHex(appTheme.danger)
                        }
                        Text {
                            id: deleteLabel
                            objectName: "downloadsDeleteLabel"
                            visible: !selectionToolbar.compactActions
                            text: controller.downloadsDeleteLabel
                            color: appTheme.dangerText
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                        }
                    }

                    MouseArea {
                        id: deletePointer
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onEntered: controller.pointerEnter()
                        onExited: controller.pointerExit()
                        onClicked: controller.deleteSelectedDownloads()
                    }

                    ToolTip {
                        id: deleteTooltip
                        visible: deletePointer.containsMouse
                        text: controller.downloadsDeleteTooltip
                        delay: 420
                        padding: 7
                        contentItem: Text {
                            text: deleteTooltip.text
                            color: appTheme.textPrimary
                            font.pixelSize: 12
                        }
                        background: Rectangle {
                            color: appTheme.surfaceChrome
                            radius: 7
                            border.width: 1
                            border.color: appTheme.border
                        }
                    }

                    Behavior on color { ColorAnimation { duration: 120 } }
                    Behavior on border.color { ColorAnimation { duration: 120 } }
                }

                    LibraryIconButton {
                        objectName: "downloadsClearSelectionAction"
                        width: 32
                        height: 32
                        radius: 10
                        iconName: "close"
                        toolTipText: controller.downloadsClearSelectionTooltip
                        onClicked: controller.clearDownloadsSelection()
                    }
                }
            }
        }

        Rectangle {
            objectName: "downloadsRefreshErrorBanner"
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 36 : 0
            visible: !controller.downloadsLoading
                && controller.downloadsHasItems
                && controller.statusText !== ""
            radius: 10
            color: appTheme.warningSurface
            border.width: 1
            border.color: appTheme.warningBorder

            Text {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                text: controller.statusText
                color: appTheme.textSecondary
                font.pixelSize: 10
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Column {
                anchors.fill: parent
                spacing: 5
                visible: controller.downloadsLoading && !controller.downloadsHasItems
                Repeater {
                    model: Math.max(4, Math.floor(parent.height / 60))
                    LibrarySkeletonRow {
                        width: parent.width
                        height: 61
                        showThumbnail: true
                        shimmerPhase: root.shimmerPhase
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width - 32, 520)
                text: controller.statusText
                color: appTheme.textMuted
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: !controller.downloadsLoading && !controller.downloadsHasItems
            }

            ListView {
                id: downloadsList
                anchors.fill: parent
                visible: controller.downloadsHasItems
                model: downloadsModel
                clip: true
                spacing: 3
                boundsBehavior: Flickable.StopAtBounds
                maximumFlickVelocity: 5200
                flickDeceleration: 2800
                cacheBuffer: 760

                ScrollBar.vertical: ScrollBar {
                    id: scrollBar
                    policy: ScrollBar.AsNeeded
                    width: 8
                    contentItem: Rectangle {
                        implicitWidth: 4
                        radius: 2
                        color: scrollBar.active || scrollBar.hovered
                            ? appTheme.textMuted
                            : appTheme.borderStrong
                        opacity: scrollBar.active || scrollBar.hovered || downloadsList.moving ? 1 : 0.35
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }

                delegate: Item {
                    id: row
                    required property string title
                    required property string fileName
                    required property string sizeText
                    required property string mediaIcon
                    required property string mediaType
                    required property bool selected
                    required property string sourcePath
                    required property string thumbnailSource

                    width: downloadsList.width - 8
                    height: 64
                    scale: rowPointer.pressed ? 0.994 : 1
                    transformOrigin: Item.Center

                    Behavior on scale {
                        NumberAnimation { duration: 105; easing.type: Easing.OutCubic }
                    }

                    Component.onCompleted: controller.requestDownloadedInfo(row.sourcePath)

                    Rectangle {
                        anchors.fill: parent
                        radius: 11
                        color: row.selected
                            ? appTheme.accentTint
                            : (rowPointer.containsMouse ? appTheme.hover : "transparent")
                        border.width: row.selected || rowPointer.containsMouse ? 1 : 0
                        border.color: row.selected ? appTheme.accent : appTheme.hoverBorder

                        Behavior on color { ColorAnimation { duration: 145 } }
                        Behavior on border.color { ColorAnimation { duration: 145 } }

                        MouseArea {
                            id: rowPointer
                            anchors.fill: parent
                            property bool heldForSelection: false
                            hoverEnabled: true
                            enabled: !controller.downloadsDeleting
                            cursorShape: Qt.PointingHandCursor
                            onEntered: controller.pointerEnter()
                            onExited: controller.pointerExit()
                            onPressed: heldForSelection = false
                            onCanceled: heldForSelection = false
                            onPressAndHold: function(mouse) {
                                heldForSelection = true
                                controller.toggleDownloadedItem(row.sourcePath)
                                mouse.accepted = true
                            }
                            onClicked: function(mouse) {
                                if (heldForSelection) {
                                    mouse.accepted = true
                                    return
                                }
                                if (controller.downloadsHasSelection)
                                    controller.toggleDownloadedItem(row.sourcePath)
                                else
                                    controller.playDownloadedItem(row.sourcePath)
                            }
                        }

                        Rectangle {
                            anchors.fill: parent
                            radius: parent.radius
                            color: appTheme.accent
                            opacity: rowPointer.pressed && !row.selected ? 0.055 : 0
                            Behavior on opacity { NumberAnimation { duration: 100 } }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 13
                            spacing: 10

                            Rectangle {
                                Layout.preferredWidth: 62
                                Layout.preferredHeight: 38
                                radius: 9
                                color: appTheme.surface2
                                border.width: 1
                                border.color: row.selected ? appTheme.accent : appTheme.border
                                clip: true

                                Image {
                                    id: thumbnail
                                    anchors.fill: parent
                                    source: row.thumbnailSource
                                    sourceSize.width: 124
                                    sourceSize.height: 76
                                    fillMode: Image.PreserveAspectCrop
                                    visible: source !== "" && status === Image.Ready
                                }

                                Image {
                                    anchors.centerIn: parent
                                    width: 15
                                    height: 15
                                    visible: !thumbnail.visible
                                    source: "image://libraryicons/" + row.mediaIcon
                                        + "/15/" + root.iconHex(appTheme.textMuted)
                                }

                                Rectangle {
                                    anchors.fill: parent
                                    visible: row.selected
                                    color: appTheme.accentTint
                                    opacity: 0.82
                                }

                                Image {
                                    id: selectedCheck
                                    anchors.centerIn: parent
                                    width: 16
                                    height: 16
                                    visible: row.selected
                                    opacity: row.selected ? 1 : 0
                                    scale: row.selected ? 1 : 0.78
                                    source: "image://libraryicons/check/16/"
                                        + root.iconHex(appTheme.accent)

                                    Behavior on opacity { NumberAnimation { duration: 145 } }
                                    Behavior on scale {
                                        NumberAnimation {
                                            duration: 175
                                            easing.type: Easing.OutCubic
                                        }
                                    }
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                Text {
                                    Layout.fillWidth: true
                                    text: row.title
                                    color: rowPointer.containsMouse || row.selected
                                        ? appTheme.textPrimary
                                        : appTheme.textSecondary
                                    font.pixelSize: 12
                                    font.weight: Font.Medium
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: row.fileName
                                    color: appTheme.textDim
                                    font.pixelSize: 9
                                    visible: text !== "" && !root.fileNameRepeatsTitle(
                                        row.title,
                                        row.fileName
                                    )
                                    elide: Text.ElideMiddle
                                }
                            }

                            Text {
                                Layout.preferredWidth: 66
                                text: row.sizeText
                                color: rowPointer.containsMouse ? appTheme.textMuted : appTheme.textDim
                                font.pixelSize: 10
                                font.family: "Consolas"
                                horizontalAlignment: Text.AlignRight
                            }
                        }
                    }
                }
            }
        }
    }
}
