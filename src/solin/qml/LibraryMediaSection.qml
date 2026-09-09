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
        running: controller.isLoading
    }

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
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
                    visible: text !== ""
                    elide: Text.ElideRight
                }
            }

            LibraryIconButton {
                iconName: "play_all"
                toolTipText: controller.playAllTooltip
                enabled: controller.hasItems && !controller.isLoading
                onClicked: controller.playAllClicked()
            }

            LibraryIconButton {
                iconName: "shuffle"
                toolTipText: controller.shuffleTooltip
                enabled: controller.hasItems && !controller.isLoading
                onClicked: controller.shuffleClicked()
            }

            LibraryIconButton {
                visible: controller.showDownloadAll
                Layout.preferredWidth: visible ? 36 : 0
                iconName: "cloud_download"
                toolTipText: controller.downloadAllTooltip
                enabled: controller.downloadAllEnabled
                activeState: controller.downloadAllActive
                onClicked: controller.downloadAllClicked()
            }

            LibraryIconButton {
                iconName: "refresh"
                toolTipText: controller.refreshTooltip
                enabled: controller.refreshEnabled
                onClicked: controller.refreshClicked()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            AppTextField {
                objectName: "librarySearchField"
                Layout.fillWidth: true
                placeholderText: controller.searchPlaceholder
                text: controller.searchText
                searchIcon: true
                clearActionVisible: text.length > 0
                restingColor: appTheme.surface2
                cornerRadius: 12
                font.pixelSize: 13
                onTextEdited: controller.setSearchText(text)
                onClearRequested: {
                    controller.setSearchText("")
                    forceActiveFocus()
                }
            }

            Rectangle {
                id: mediaMode
                visible: controller.supportsAudio
                Layout.preferredWidth: visible ? 82 : 0
                Layout.preferredHeight: 42
                radius: 12
                color: appTheme.surface2
                border.width: 1
                border.color: appTheme.border
                clip: true

                Rectangle {
                    width: 38
                    height: parent.height - 6
                    x: controller.audioMode ? parent.width - width - 3 : 3
                    y: 3
                    radius: 9
                    color: appTheme.accentTint
                    border.width: 1
                    border.color: appTheme.accent
                    Behavior on x {
                        NumberAnimation { duration: 190; easing.type: Easing.OutCubic }
                    }
                }

                Row {
                    anchors.fill: parent
                    anchors.margins: 3

                    Repeater {
                        model: [
                            { "mode": "video", "icon": "video", "tooltip": controller.videoTooltip },
                            { "mode": "audio", "icon": "music", "tooltip": controller.audioTooltip }
                        ]

                        Item {
                            id: modeItem
                            required property var modelData
                            width: 38
                            height: parent.height
                            readonly property bool checked: controller.audioMode
                                ? modelData.mode === "audio"
                                : modelData.mode === "video"

                            Image {
                                anchors.centerIn: parent
                                width: 14
                                height: 14
                                source: "image://libraryicons/" + modeItem.modelData.icon
                                    + "/14/" + root.iconHex(
                                        modeItem.checked ? appTheme.textSecondary : appTheme.textDim
                                    )
                            }

                            MouseArea {
                                id: modePointer
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onEntered: controller.pointerEnter()
                                onExited: controller.pointerExit()
                                onClicked: controller.setMode(modeItem.modelData.mode)
                            }

                            ToolTip {
                                visible: modePointer.containsMouse
                                text: modeItem.modelData.tooltip
                                delay: 420
                            }
                        }
                    }
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Column {
                anchors.fill: parent
                spacing: 5
                visible: controller.isLoading

                Repeater {
                    model: Math.max(4, Math.floor(parent.height / 58))
                    LibrarySkeletonRow {
                        width: parent.width
                        height: 53
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
                visible: !controller.isLoading && controller.statusText !== ""
            }

            ListView {
                id: catalogList
                anchors.fill: parent
                visible: !controller.isLoading && controller.statusText === ""
                model: catalogModel
                clip: true
                spacing: 3
                boundsBehavior: Flickable.StopAtBounds
                maximumFlickVelocity: 5200
                flickDeceleration: 2800
                cacheBuffer: 900

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
                        opacity: scrollBar.active || scrollBar.hovered || catalogList.moving ? 1 : 0.35
                        anchors.horizontalCenter: parent.horizontalCenter
                        Behavior on opacity { NumberAnimation { duration: 160 } }
                    }
                }

                delegate: Item {
                    id: row
                    required property int index
                    required property string numberText
                    required property string title
                    required property string durationText
                    required property bool cloudVisible
                    required property bool cloudDownloading
                    required property bool cloudQueued
                    required property real cloudProgress
                    required property string cloudTooltip
                    required property string mediaIcon

                    width: catalogList.width - 8
                    height: 54

                    Rectangle {
                        anchors.fill: parent
                        radius: 11
                        color: rowPointer.containsMouse ? appTheme.hover : "transparent"
                        border.width: rowPointer.containsMouse ? 1 : 0
                        border.color: appTheme.hoverBorder

                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: 1
                            anchors.verticalCenter: parent.verticalCenter
                            width: 3
                            height: rowPointer.containsMouse ? 20 : 0
                            radius: 2
                            color: appTheme.accent
                            Behavior on height { NumberAnimation { duration: 150 } }
                        }

                        MouseArea {
                            id: rowPointer
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onEntered: controller.pointerEnter()
                            onExited: controller.pointerExit()
                            onClicked: controller.playItem(row.index)
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 12
                            spacing: 9

                            Text {
                                visible: row.numberText !== ""
                                Layout.preferredWidth: visible ? 34 : 0
                                text: row.numberText
                                color: rowPointer.containsMouse ? appTheme.textMuted : appTheme.textDim
                                font.pixelSize: 11
                                font.family: "Consolas"
                                horizontalAlignment: Text.AlignRight
                            }

                            Image {
                                visible: row.numberText === ""
                                Layout.preferredWidth: visible ? 17 : 0
                                Layout.preferredHeight: 17
                                source: "image://libraryicons/" + row.mediaIcon + "/17/"
                                    + root.iconHex(appTheme.textDim)
                            }

                            Text {
                                Layout.fillWidth: true
                                text: row.title
                                color: rowPointer.containsMouse
                                    ? appTheme.textPrimary
                                    : appTheme.textSecondary
                                font.pixelSize: 13
                                font.weight: Font.Medium
                                elide: Text.ElideRight
                            }

                            Item {
                                Layout.preferredWidth: 88
                                Layout.preferredHeight: parent.height

                                CloudDownloadButton {
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 28
                                    height: 28
                                    iconSource: "image://libraryicons/cloud_download/13/"
                                        + root.iconHex(appTheme.textMuted)
                                    toolTipText: row.cloudTooltip
                                    active: row.cloudVisible
                                    downloading: row.cloudDownloading
                                    queued: row.cloudQueued
                                    progress: row.cloudProgress
                                    opacity: row.cloudVisible
                                        ? (rowPointer.containsMouse || downloading || queued ? 1 : 0.66)
                                        : 0
                                    onPointerEntered: controller.pointerEnter()
                                    onPointerExited: controller.pointerExit()
                                    onClicked: controller.downloadItem(row.index)
                                    Behavior on opacity { NumberAnimation { duration: 150 } }
                                }

                                Text {
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 52
                                    text: row.durationText
                                    color: rowPointer.containsMouse ? appTheme.textMuted : appTheme.textDim
                                    font.pixelSize: 11
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
}
