// Shared QML view for Songs and Original Songs.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Rectangle {
    id: root
    color: "#0d1117"

    readonly property int pagePad: Math.max(16, Math.min(28, width * 0.035))
    readonly property color bg:            "#0d1117"
    readonly property color surface:       "#161b22"
    readonly property color surface2:      "#11161d"
    readonly property color hover:         "#1b2130"
    readonly property color hoverBorder:   "#252d3d"
    readonly property color border:        "#21262d"
    readonly property color borderStrong:  "#30363d"
    readonly property color textPrimary:   "#e6edf3"
    readonly property color textSecondary: "#b8c0cc"
    readonly property color textMuted:     "#8b949e"
    readonly property color textDim:       "#4d5866"
    readonly property color accent:        "#388bfd"
    readonly property color accentSubtle:  "#1a3358"
    readonly property color amber:         "#d29922"

    readonly property bool hasController: typeof controller !== "undefined" && controller !== null
    readonly property string titleText: root.hasController ? controller.titleText : ""
    readonly property string subtitleText: root.hasController ? controller.subtitleText : ""
    readonly property string refreshTooltip: root.hasController ? controller.refreshTooltip : ""
    readonly property bool refreshEnabled: root.hasController && controller.refreshEnabled
    readonly property bool supportsAudio: root.hasController && controller.supportsAudio
    readonly property string countText: root.hasController ? controller.countText : ""
    readonly property string cacheText: root.hasController ? controller.cacheText : ""
    readonly property bool hasItems: root.hasController && controller.hasItems
    readonly property string playAllTooltip: root.hasController ? controller.playAllTooltip : ""
    readonly property string shuffleTooltip: root.hasController ? controller.shuffleTooltip : ""
    readonly property bool showDownloadAll: root.hasController && controller.showDownloadAll
    readonly property bool downloadAllEnabled: root.hasController && controller.downloadAllEnabled
    readonly property bool downloadAllActive: root.hasController && controller.downloadAllActive
    readonly property string downloadAllTooltip: root.hasController ? controller.downloadAllTooltip : ""
    readonly property bool isLoading: root.hasController && controller.isLoading
    readonly property string statusText: root.hasController ? controller.statusText : ""
    readonly property string searchPlaceholder: root.hasController ? controller.searchPlaceholder : ""
    readonly property bool audioMode: root.hasController && controller.audioMode
    readonly property string videoTooltip: root.hasController ? controller.videoTooltip : ""
    readonly property string audioTooltip: root.hasController ? controller.audioTooltip : ""

    function refreshClicked() {
        if (root.hasController) controller.refreshClicked()
    }

    function playAllClicked() {
        if (root.hasController) controller.playAllClicked()
    }

    function shuffleClicked() {
        if (root.hasController) controller.shuffleClicked()
    }

    function downloadAllClicked() {
        if (root.hasController) controller.downloadAllClicked()
    }

    function pointerEnter() {
        if (root.hasController) controller.pointerEnter()
    }

    function pointerExit() {
        if (root.hasController) controller.pointerExit()
    }

    function playItem(index) {
        if (root.hasController) controller.playItem(index)
    }

    function downloadItem(index) {
        if (root.hasController) controller.downloadItem(index)
    }

    function setSearchText(text) {
        if (root.hasController) controller.setSearchText(text)
    }

    function setMode(mode) {
        if (root.hasController) controller.setMode(mode)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin:    root.pagePad
        anchors.leftMargin:   root.pagePad
        anchors.rightMargin:  root.pagePad
        anchors.bottomMargin: root.pagePad
        spacing: 14

        // ── Header ──────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3

                Text {
                    Layout.fillWidth: true
                    text: root.titleText
                    color: root.textPrimary
                    font.pixelSize: Math.max(20, Math.min(24, root.width * 0.026))
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: root.subtitleText
                    color: root.textMuted
                    font.pixelSize: 12
                    visible: text !== ""
                    elide: Text.ElideRight
                }
            }

            RoundIconButton {
                visible:     root.showDownloadAll
                Layout.preferredWidth: visible ? 32 : 0
                iconName:    "cloud_download"
                toolTipText: root.downloadAllTooltip
                enabled:     root.downloadAllEnabled
                activeState: root.downloadAllActive
                onClicked:   root.downloadAllClicked()
            }

            RoundIconButton {
                iconName:    "refresh"
                toolTipText: root.refreshTooltip
                enabled:     root.refreshEnabled
                onClicked:   root.refreshClicked()
            }
        }

        // ── Search ───────────────────────────────────────────────────────
        SearchField {
            Layout.fillWidth: true
            Layout.preferredHeight: 40
        }

        // ── Toolbar row ──────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            MediaSegment {
                visible: root.supportsAudio
                Layout.preferredWidth:  visible ? 80 : 0
                Layout.preferredHeight: 30
            }

            // Push count + buttons to the right
            Item { Layout.fillWidth: true }

            // Count + cache block (right-aligned)
            ColumnLayout {
                spacing: 1
                Layout.alignment: Qt.AlignVCenter

                Text {
                    Layout.alignment: Qt.AlignRight
                    text:  root.countText
                    color: root.textMuted
                    font.pixelSize: 12
                }

                Text {
                    Layout.alignment: Qt.AlignRight
                    text:    root.cacheText
                    color:   root.textDim
                    font.pixelSize: 10
                    visible: text !== ""
                }
            }

            // Slim vertical divider before action buttons
            Rectangle {
                width:  1
                height: 16
                color:  root.border
                Layout.alignment: Qt.AlignVCenter
                visible: root.hasItems
            }

            RoundIconButton {
                iconName:    "play_all"
                toolTipText: root.playAllTooltip
                enabled:     root.hasItems && !root.isLoading
                onClicked:   root.playAllClicked()
            }

            RoundIconButton {
                iconName:    "shuffle"
                toolTipText: root.shuffleTooltip
                enabled:     root.hasItems && !root.isLoading
                onClicked:   root.shuffleClicked()
            }
        }

        // ── Separator ────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color:   root.border
            opacity: 0.7
        }

        // ── List area ────────────────────────────────────────────────────
        Item {
            Layout.fillWidth:  true
            Layout.fillHeight: true

            // Empty / loading state
            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width - 32, 520)
                text:  root.statusText
                color: root.textMuted
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: root.isLoading || root.statusText !== ""
            }

            ListView {
                id: mediaList
                anchors.fill: parent
                visible:  !root.isLoading
                clip:     true
                spacing:  2
                model:    typeof libraryModel !== "undefined" ? libraryModel : null
                boundsBehavior:       Flickable.StopAtBounds
                maximumFlickVelocity: 5200
                flickDeceleration:    2800
                cacheBuffer:          1200

                property real wheelTargetY: contentY

                function clampContentY(value) {
                    var maxY = Math.max(0, contentHeight - height)
                    return Math.max(0, Math.min(maxY, value))
                }

                function smoothWheelScroll(delta) {
                    if (!wheelAnim.running) wheelTargetY = contentY
                    wheelTargetY = clampContentY(wheelTargetY + delta)
                    wheelAnim.stop()
                    wheelAnim.to = wheelTargetY
                    wheelAnim.start()
                }

                NumberAnimation {
                    id:       wheelAnim
                    target:   mediaList
                    property: "contentY"
                    duration: 180
                    easing.type: Easing.OutCubic
                }

                WheelHandler {
                    target:          mediaList
                    acceptedDevices: PointerDevice.Mouse
                    onWheel: function(event) {
                        var hasPixelDelta = event.pixelDelta.y !== 0
                        var rawDelta      = hasPixelDelta ? event.pixelDelta.y : event.angleDelta.y
                        var scrollDelta   = hasPixelDelta ? -rawDelta : -rawDelta * 0.50
                        mediaList.smoothWheelScroll(scrollDelta)
                        event.accepted = true
                    }
                }

                ScrollBar.vertical: ScrollBar {
                    id: vScroll
                    policy: ScrollBar.AsNeeded
                    width:  8
                    contentItem: Rectangle {
                        implicitWidth: 4
                        radius: 2
                        color: (vScroll.active || vScroll.hovered) ? root.textMuted : root.borderStrong
                        opacity: (vScroll.active || vScroll.hovered || mediaList.moving) ? 1.0 : 0.35
                        Behavior on opacity { NumberAnimation { duration: 180 } }
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                }

                // ── Row Delegate ─────────────────────────────────────────
                delegate: Item {
                    id: rowShell
                    required property int    index
                    required property string numberText
                    required property string title
                    required property string durationText
                    required property bool   cloudVisible
                    required property bool   cloudDownloading
                    required property bool   cloudQueued
                    required property real   cloudProgress
                    required property string cloudTooltip
                    required property string mediaIcon

                    // Leave 8px gap on right for the scrollbar
                    width:  mediaList.width - 8
                    height: 52

                    Rectangle {
                        id: rowBg
                        anchors.fill: parent
                        radius: 7
                        color:        rowMa.containsMouse ? root.hover : "transparent"
                        border.width: rowMa.containsMouse ? 1 : 0
                        border.color: rowMa.containsMouse ? root.hoverBorder : "transparent"

                        Behavior on color        { ColorAnimation { duration: 120 } }
                        Behavior on border.color { ColorAnimation { duration: 120 } }

                        // Left accent bar — animates height on hover
                        Rectangle {
                            anchors.left:           parent.left
                            anchors.leftMargin:     1
                            anchors.verticalCenter: parent.verticalCenter
                            width:  2
                            height: rowMa.containsMouse ? 20 : 0
                            radius: 1
                            color:  root.accent
                            Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                        }

                        MouseArea {
                            id: rowMa
                            anchors.fill: parent
                            hoverEnabled: true
                            onEntered:  root.pointerEnter()
                            onExited:   root.pointerExit()
                            onClicked:  root.playItem(rowShell.index)
                        }

                        RowLayout {
                            anchors.fill:        parent
                            anchors.leftMargin:  12
                            anchors.rightMargin: 10
                            spacing: 0

                            // ── Track number (fixed 34px, hidden when empty) ──
                            Item {
                                Layout.preferredWidth:  rowShell.numberText !== "" ? 34 : 0
                                Layout.preferredHeight: parent.height
                                visible: rowShell.numberText !== ""

                                Text {
                                    anchors.right:          parent.right
                                    anchors.rightMargin:    8
                                    anchors.verticalCenter: parent.verticalCenter
                                    text:  rowShell.numberText
                                    color: rowMa.containsMouse ? root.textMuted : root.textDim
                                    font.pixelSize: 12
                                    font.family:    "Consolas"
                                    horizontalAlignment: Text.AlignRight
                                    Behavior on color { ColorAnimation { duration: 120 } }
                                }
                            }

                            // ── Title (fills remaining space) ─────────────
                            Text {
                                Layout.fillWidth:  true
                                Layout.leftMargin: rowShell.numberText === "" ? 0 : 6
                                text:  rowShell.title
                                color: rowMa.containsMouse ? root.textPrimary : root.textSecondary
                                font.pixelSize: 13
                                font.weight:    Font.Medium
                                elide:          Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                                Behavior on color { ColorAnimation { duration: 120 } }
                            }

                            // ── Right metadata cluster (ALWAYS 80px fixed) ────
                            //
                            // KEY FIX: Cloud icon and duration live inside a single
                            // fixed-width Item. The cloud button uses opacity animation
                            // instead of visible/width toggling — so the duration text
                            // is ALWAYS at the same x position regardless of cloud state.
                            Item {
                                Layout.preferredWidth:  80
                                Layout.preferredHeight: parent.height

                                RowLayout {
                                    anchors.fill: parent
                                    spacing:      4

                                    // Cloud download — fades in/out, never collapses layout
                                    CloudDownloadButton {
                                        Layout.preferredWidth:  24
                                        Layout.preferredHeight: 24
                                        Layout.alignment:       Qt.AlignVCenter
                                        iconSource:  "image://mediaicons/cloud_download/13/8b949e"
                                        toolTipText: rowShell.cloudTooltip
                                        downloading: rowShell.cloudDownloading
                                        queued:      rowShell.cloudQueued
                                        progress:    rowShell.cloudProgress
                                        active:      rowShell.cloudVisible
                                        opacity:     rowShell.cloudVisible ? 1.0 : 0.0
                                        onPointerEntered: root.pointerEnter()
                                        onPointerExited:  root.pointerExit()
                                        onClicked: root.downloadItem(rowShell.index)
                                        Behavior on opacity { NumberAnimation { duration: 160 } }
                                    }

                                    // Duration — locked to right edge, never shifts
                                    Text {
                                        Layout.preferredWidth:    52
                                        text:  rowShell.durationText
                                        color: rowMa.containsMouse ? root.textMuted : root.textDim
                                        font.pixelSize: 11
                                        font.family:    "Consolas"
                                        horizontalAlignment: Text.AlignRight
                                        verticalAlignment:   Text.AlignVCenter
                                        Behavior on color { ColorAnimation { duration: 120 } }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Search Field Component ────────────────────────────────────────────────
    component SearchField: Rectangle {
        id: searchBox
        radius: 8
        color:        searchInput.activeFocus ? "#0e1720" : root.surface2
        border.width: 1
        border.color: searchInput.activeFocus ? root.accent : root.border

        Behavior on color        { ColorAnimation { duration: 140 } }
        Behavior on border.color { ColorAnimation { duration: 140 } }

        RowLayout {
            anchors.fill:        parent
            anchors.leftMargin:  12
            anchors.rightMargin: 8
            spacing: 8

            Image {
                Layout.preferredWidth:  14
                Layout.preferredHeight: 14
                source: "image://mediaicons/search/14/"
                        + (searchInput.activeFocus ? "6e7a8a" : "4d5866")
            }

            Item {
                Layout.fillWidth:  true
                Layout.fillHeight: true

                Text {
                    anchors.left:           parent.left
                    anchors.right:          parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text:    root.searchPlaceholder
                    color:   root.textDim
                    font.pixelSize: 13
                    visible: searchInput.text.length === 0 && !searchInput.activeFocus
                    elide:   Text.ElideRight
                }

                TextInput {
                    id: searchInput
                    anchors.fill:      parent
                    color:             root.textSecondary
                    selectionColor:    "#1f6feb"
                    selectedTextColor: "#ffffff"
                    font.pixelSize:    13
                    verticalAlignment: TextInput.AlignVCenter
                    clip: true
                    onTextChanged: root.setSearchText(text)
                }
            }

            // Clear button
            Rectangle {
                Layout.preferredWidth:  22
                Layout.preferredHeight: 22
                radius: 5
                visible:      searchInput.text.length > 0
                color:        clearMa.containsMouse ? root.hover : "transparent"
                border.width: clearMa.containsMouse ? 1 : 0
                border.color: root.border

                Text {
                    anchors.centerIn: parent
                    text:  "×"
                    color: clearMa.containsMouse ? root.textSecondary : root.textMuted
                    font.pixelSize: 15
                    font.weight:    Font.Light
                    Behavior on color { ColorAnimation { duration: 100 } }
                }

                MouseArea {
                    id: clearMa
                    anchors.fill: parent
                    hoverEnabled: true
                    onEntered:  root.pointerEnter()
                    onExited:   root.pointerExit()
                    onClicked: {
                        searchInput.text = ""
                        searchInput.forceActiveFocus()
                    }
                }
            }
        }
    }

    // ── Media Segment Toggle ──────────────────────────────────────────────────
    component MediaSegment: Rectangle {
        id: segment
        radius: 8
        color: root.surface2
        border.width: 1
        border.color: root.border
        clip: true

        Rectangle {
            id: activePill
            width:  38
            height: parent.height - 4
            x:      root.audioMode ? parent.width - width - 2 : 2
            y:      2
            radius: 6
            color:  root.accentSubtle
            border.width: 1
            border.color: "#2a5a9e"

            Behavior on x {
                NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
            }
        }

        Row {
            anchors.fill:    parent
            anchors.margins: 2

            SegmentHalf {
                width:       38
                height:      parent.height
                iconName:    "video"
                checked:     !root.audioMode
                toolTipText: root.videoTooltip
                onClicked:   root.setMode("video")
            }

            SegmentHalf {
                width:       38
                height:      parent.height
                iconName:    "music"
                checked:     root.audioMode
                toolTipText: root.audioTooltip
                onClicked:   root.setMode("audio")
            }
        }
    }

    // ── Segment Half ─────────────────────────────────────────────────────────
    component SegmentHalf: Item {
        id: half
        property string iconName:    ""
        property bool   checked:     false
        property string toolTipText: ""
        signal clicked()

        Image {
            anchors.centerIn: parent
            width:  14
            height: 14
            source: "image://mediaicons/" + half.iconName + "/14/"
                    + (half.checked ? "c9d1d9" : "4d5866")
        }

        MouseArea {
            id: halfMa
            anchors.fill: parent
            hoverEnabled: true
            onEntered:  root.pointerEnter()
            onExited:   root.pointerExit()
            onClicked:  half.clicked()
        }

        ToolTip {
            visible: halfMa.containsMouse && half.toolTipText !== ""
            text:    half.toolTipText
            delay:   400
        }
    }

    // ── Round Icon Button ─────────────────────────────────────────────────────
    component RoundIconButton: Rectangle {
        id: iconButton
        property string iconName:    ""
        property string toolTipText: ""
        property bool activeState: false
        signal clicked()

        implicitWidth:  32
        implicitHeight: 32
        radius: 8
        // Transparent by default — border + bg only on hover (more modern feel)
        color:        enabled && btnMa.containsMouse ? root.hover : "transparent"
        border.width: 1
        border.color: iconButton.activeState ? root.amber : (enabled && btnMa.containsMouse ? root.borderStrong : root.border)
        opacity: enabled ? 1.0 : 0.38

        Behavior on color        { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Image {
            anchors.centerIn: parent
            width:  14
            height: 14
            source: "image://mediaicons/" + iconButton.iconName + "/14/"
                    + (iconButton.activeState ? "d29922" : (btnMa.containsMouse ? "c9d1d9" : "8b949e"))
        }

        MouseArea {
            id: btnMa
            anchors.fill: parent
            enabled:      iconButton.enabled
            hoverEnabled: true
            onEntered:  root.pointerEnter()
            onExited:   root.pointerExit()
            onClicked:  iconButton.clicked()
        }

        ToolTip {
            visible: btnMa.containsMouse && iconButton.toolTipText !== ""
            text:    iconButton.toolTipText
            delay:   400
        }
    }

}
