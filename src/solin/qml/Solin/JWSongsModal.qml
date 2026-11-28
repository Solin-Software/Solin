// JWSongsModal.qml — full-screen overlay for adding JW video songs.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: modalRoot
    anchors.fill: parent
    visible: false
    z: 1000
    color: "transparent"

    readonly property color bg:              appTheme.bg
    readonly property color surface:         appTheme.surface
    readonly property color surfaceElevated: appTheme.hoverStrong
    readonly property color surfaceInputFocus: appTheme.surfaceInputFocus
    readonly property color hover:           appTheme.hover
    readonly property color border_:         appTheme.border_
    readonly property color borderStrong:    appTheme.borderStrong
    readonly property color textPrimary:     appTheme.textPrimary
    readonly property color textSecondary:   appTheme.textSecondary
    readonly property color textMuted:       appTheme.textMuted
    readonly property color textDim:         appTheme.textDim
    readonly property color accent:          appTheme.accent
    readonly property color accentSelection: appTheme.accentSelection
    readonly property color accentTint:      appTheme.accentTint
    readonly property color success:         appTheme.success
    readonly property color dangerSubtle:    appTheme.dangerSubtle

    property bool isOpen: false

    readonly property bool hasBridge: typeof songsBridge !== "undefined" && songsBridge !== null

    function pointInsideSearchBox(x, y) {
        var p = searchBox.mapToItem(modalPanel, 0, 0)
        return x >= p.x && x <= p.x + searchBox.width
            && y >= p.y && y <= p.y + searchBox.height
    }

    function clearSearchFocusIfOutside(x, y) {
        if (searchField.activeFocus && !pointInsideSearchBox(x, y))
            modalPanel.forceActiveFocus()
    }

    function open() {
        if (isOpen) return
        visible = true
        isOpen = true
        openAnim.start()
        if (hasBridge) songsBridge.openModal()
        modalPanel.forceActiveFocus()
    }

    function close() {
        if (!isOpen) return
        isOpen = false
        closeAnim.start()
    }

    Connections {
        target: modalRoot.hasBridge ? songsBridge : null

        function onModalShouldClose() {
            modalRoot.close()
        }
    }

    Rectangle {
        id: overlayBg
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.65)
        opacity: 0

        MouseArea {
            anchors.fill: parent
            onClicked: modalRoot.close()
        }
    }

    Rectangle {
        id: modalPanel
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 620)
        height: Math.min(parent.height - 32, parent.height * 0.78)
        radius: 14
        color: modalRoot.surface
        border.width: 1
        border.color: modalRoot.border_
        clip: true
        opacity: 0
        scale: 0.95
        focus: true

        Keys.onEscapePressed: modalRoot.close()

        MouseArea {
            anchors.fill: parent
            z: 0
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onPressed: function(mouse) {
                modalRoot.clearSearchFocusIfOutside(mouse.x, mouse.y)
                mouse.accepted = true
            }
            onClicked: function(mouse) { mouse.accepted = true }
            onReleased: function(mouse) { mouse.accepted = true }
        }

        ParallelAnimation {
            id: openAnim
            NumberAnimation { target: overlayBg; property: "opacity"; from: 0; to: 1; duration: 200; easing.type: Easing.OutCubic }
            NumberAnimation { target: modalPanel; property: "opacity"; from: 0; to: 1; duration: 200; easing.type: Easing.OutCubic }
            NumberAnimation { target: modalPanel; property: "scale"; from: 0.95; to: 1; duration: 200; easing.type: Easing.OutCubic }
        }

        ParallelAnimation {
            id: closeAnim
            NumberAnimation { target: overlayBg; property: "opacity"; from: 1; to: 0; duration: 150; easing.type: Easing.InCubic }
            NumberAnimation { target: modalPanel; property: "opacity"; from: 1; to: 0; duration: 150; easing.type: Easing.InCubic }
            NumberAnimation { target: modalPanel; property: "scale"; from: 1; to: 0.95; duration: 150; easing.type: Easing.InCubic }

            onFinished: {
                modalRoot.visible = false
                if (modalRoot.hasBridge) songsBridge.reset()
                searchField.text = ""
            }
        }

        ColumnLayout {
            z: 1
            anchors.fill: parent
            spacing: 0

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 54

                Column {
                    anchors.left: parent.left
                    anchors.leftMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 2

                    Text {
                        text: qsTranslate("JWSongsBridge", "Add video song")
                        color: modalRoot.textPrimary
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }

                    Text {
                        text: qsTranslate("JWSongsBridge", "Songs from JW.org")
                        color: modalRoot.textDim
                        font.pixelSize: 11
                    }
                }

                Row {
                    anchors.right: closeBtn.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 8

                    BusyIndicator {
                        width: 22
                        height: 22
                        running: visible
                        visible: modalRoot.hasBridge && songsBridge.isLoading
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: modalRoot.hasBridge ? songsBridge.statusText : ""
                        color: modalRoot.textDim
                        font.pixelSize: 11
                        visible: modalRoot.hasBridge && songsBridge.isLoading
                        width: Math.min(132, implicitWidth)
                        elide: Text.ElideRight
                    }
                }

                Rectangle {
                    id: closeBtn
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    width: 32
                    height: 32
                    radius: 8
                    color: closeMa.containsMouse ? modalRoot.hover : "transparent"
                    border.width: closeMa.containsMouse ? 1 : 0
                    border.color: modalRoot.border_

                    Text {
                        anchors.centerIn: parent
                        text: "x"
                        color: closeMa.containsMouse ? modalRoot.textPrimary : modalRoot.textMuted
                        font.pixelSize: 15
                        font.weight: Font.Medium
                    }

                    MouseArea {
                        id: closeMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: modalRoot.close()
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: modalRoot.border_
                opacity: 0.6
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 54
                Layout.leftMargin: 16
                Layout.rightMargin: 16

                Rectangle {
                    id: searchBox
                    anchors.fill: parent
                    anchors.topMargin: 10
                    anchors.bottomMargin: 4
                    radius: 8
                    color: searchField.activeFocus ? modalRoot.surfaceInputFocus : modalRoot.bg
                    border.width: 1
                    border.color: searchField.activeFocus ? modalRoot.accent : modalRoot.border_

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 8
                        spacing: 8

                        Text {
                            Layout.preferredWidth: 16
                            text: "⌕"
                            color: searchField.activeFocus ? modalRoot.textMuted : modalRoot.textDim
                            font.pixelSize: 16
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true

                            Text {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                text: qsTranslate("JWSongsBridge", "Search by number or title...")
                                color: modalRoot.textDim
                                font.pixelSize: 13
                                visible: searchField.text.length === 0 && !searchField.activeFocus
                                elide: Text.ElideRight
                            }

                            TextInput {
                                id: searchField
                                anchors.fill: parent
                                color: modalRoot.textPrimary
                                selectionColor: modalRoot.accentSelection
                                selectedTextColor: appTheme.white
                                selectByMouse: true
                                font.pixelSize: 13
                                verticalAlignment: TextInput.AlignVCenter
                                clip: true
                                onTextChanged: searchDebounce.restart()
                            }
                        }

                        Rectangle {
                            Layout.preferredWidth: 22
                            Layout.preferredHeight: 22
                            radius: 6
                            visible: searchField.text.length > 0
                            color: clearSearchMa.containsMouse ? modalRoot.hover : "transparent"

                            Text {
                                anchors.centerIn: parent
                                text: "x"
                                color: clearSearchMa.containsMouse ? modalRoot.textSecondary : modalRoot.textMuted
                                font.pixelSize: 12
                            }

                            MouseArea {
                                id: clearSearchMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    searchField.text = ""
                                    searchField.forceActiveFocus()
                                    if (modalRoot.hasBridge) songsBridge.setSearchQuery("")
                                }
                            }
                        }
                    }
                }

                Timer {
                    id: searchDebounce
                    interval: 180
                    repeat: false
                    onTriggered: {
                        if (modalRoot.hasBridge)
                            songsBridge.setSearchQuery(searchField.text)
                    }
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                Layout.leftMargin: 18
                Layout.rightMargin: 18

                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: {
                        if (!modalRoot.hasBridge || songsBridge.isLoading)
                            return ""
                        var count = songsBridge.resultCount
                        if (count === 0)
                            return qsTranslate("JWSongsBridge", "No results")
                        return count + " " + qsTranslate("JWSongsBridge", "songs")
                    }
                    color: modalRoot.textDim
                    font.pixelSize: 11
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: 16
                Layout.rightMargin: 16
                Layout.preferredHeight: 1
                color: modalRoot.border_
                opacity: 0.4
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.topMargin: 6

                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: 10
                    visible: modalRoot.hasBridge && songsBridge.isLoading

                    BusyIndicator {
                        Layout.alignment: Qt.AlignHCenter
                        width: 32
                        height: 32
                        running: visible
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: modalRoot.hasBridge ? songsBridge.statusText : ""
                        color: modalRoot.textMuted
                        font.pixelSize: 13
                    }
                }

                Rectangle {
                    anchors.centerIn: parent
                    width: Math.min(parent.width - 48, 380)
                    height: errorText.implicitHeight + 34
                    radius: 10
                    color: "transparent"
                    border.width: 1
                    border.color: modalRoot.dangerSubtle
                    visible: modalRoot.hasBridge
                             && !songsBridge.isLoading
                             && songsBridge.errorMessage !== ""

                    Text {
                        id: errorText
                        anchors.centerIn: parent
                        width: Math.min(parent.width - 36, 320)
                        text: modalRoot.hasBridge ? songsBridge.errorMessage : ""
                        color: modalRoot.textSecondary
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                    }
                }

                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: 6
                    visible: modalRoot.hasBridge
                             && !songsBridge.isLoading
                             && songsBridge.errorMessage === ""
                             && songsBridge.resultCount === 0

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTranslate("JWSongsBridge", "No songs found")
                        color: modalRoot.textMuted
                        font.pixelSize: 13
                    }
                }

                ListView {
                    id: songsList
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.bottomMargin: 8
                    visible: modalRoot.hasBridge
                             && !songsBridge.isLoading
                             && songsBridge.errorMessage === ""
                             && songsBridge.resultCount > 0
                    clip: true
                    spacing: 2
                    boundsBehavior: Flickable.StopAtBounds
                    maximumFlickVelocity: 5200
                    flickDeceleration: 2800
                    cacheBuffer: 1000
                    model: modalRoot.hasBridge ? songsBridge.model : null

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
                        id: wheelAnim
                        target: songsList
                        property: "contentY"
                        duration: 180
                        easing.type: Easing.OutCubic
                    }

                    WheelHandler {
                        target: songsList
                        acceptedDevices: PointerDevice.Mouse
                        onWheel: function(event) {
                            var hasPixelDelta = event.pixelDelta.y !== 0
                            var rawDelta = hasPixelDelta ? event.pixelDelta.y : event.angleDelta.y
                            var scrollDelta = hasPixelDelta ? -rawDelta : -rawDelta * 0.50
                            songsList.smoothWheelScroll(scrollDelta)
                            event.accepted = true
                        }
                    }

                    ScrollBar.vertical: ScrollBar {
                        id: vScroll
                        policy: ScrollBar.AsNeeded
                        width: 8
                        contentItem: Rectangle {
                            implicitWidth: 4
                            radius: 2
                            color: (vScroll.active || vScroll.hovered)
                                   ? modalRoot.textMuted : modalRoot.borderStrong
                            opacity: (vScroll.active || vScroll.hovered || songsList.moving) ? 1.0 : 0.35
                            anchors.horizontalCenter: parent.horizontalCenter
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                        }
                    }

                    delegate: Item {
                        id: rowShell
                        required property int index
                        required property string numberText
                        required property string title
                        required property string durationText

                        width: songsList.width - 8
                        height: 48

                        Rectangle {
                            id: rowBg
                            anchors.fill: parent
                            radius: 7
                            color: rowMa.containsMouse ? modalRoot.hover : "transparent"
                            border.width: rowMa.containsMouse ? 1 : 0
                            border.color: rowMa.containsMouse ? modalRoot.borderStrong : "transparent"

                            Rectangle {
                                anchors.left: parent.left
                                anchors.leftMargin: 1
                                anchors.verticalCenter: parent.verticalCenter
                                width: 2
                                height: rowMa.containsMouse ? 20 : 0
                                radius: 1
                                color: modalRoot.accent
                                Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                            }

                            MouseArea {
                                id: rowMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (modalRoot.hasBridge)
                                        songsBridge.selectItem(rowShell.index)
                                }
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 10
                                spacing: 10

                                Rectangle {
                                    Layout.preferredWidth: 46
                                    Layout.preferredHeight: 28
                                    Layout.alignment: Qt.AlignVCenter
                                    radius: 7
                                    color: rowMa.containsMouse ? modalRoot.accentTint : modalRoot.bg
                                    border.width: 1
                                    border.color: rowMa.containsMouse ? modalRoot.accent : modalRoot.border_

                                    Text {
                                        anchors.centerIn: parent
                                        text: rowShell.numberText
                                        color: rowMa.containsMouse ? appTheme.accentText : modalRoot.textMuted
                                        font.pixelSize: 12
                                        font.family: "Consolas"
                                        font.weight: Font.DemiBold
                                    }
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: rowShell.title
                                    color: rowMa.containsMouse ? modalRoot.textPrimary : modalRoot.textSecondary
                                    font.pixelSize: 13
                                    font.weight: Font.Medium
                                    elide: Text.ElideRight
                                    verticalAlignment: Text.AlignVCenter
                                }

                                Text {
                                    Layout.preferredWidth: 54
                                    text: rowShell.durationText
                                    color: rowMa.containsMouse ? modalRoot.textMuted : modalRoot.textDim
                                    font.pixelSize: 11
                                    font.family: "Consolas"
                                    horizontalAlignment: Text.AlignRight
                                    verticalAlignment: Text.AlignVCenter
                                }

                                Rectangle {
                                    Layout.preferredWidth: 26
                                    Layout.preferredHeight: 26
                                    Layout.alignment: Qt.AlignVCenter
                                    radius: 7
                                    color: rowMa.containsMouse ? modalRoot.accentTint : "transparent"
                                    border.width: 1
                                    border.color: rowMa.containsMouse ? modalRoot.accent : modalRoot.border_

                                    Text {
                                        anchors.centerIn: parent
                                        text: "+"
                                        color: rowMa.containsMouse ? appTheme.accentText : modalRoot.textDim
                                        font.pixelSize: 15
                                        font.weight: Font.DemiBold
                                    }
                                }
                            }

                        }
                    }
                }
            }
        }

        MediaPlacementOverlay {
            anchors.fill: parent
            placementBridge: modalRoot.hasBridge ? songsBridge : null
            surface: modalRoot.surface
            surfaceElevated: modalRoot.surfaceElevated
            hover: modalRoot.hover
            border_: modalRoot.border_
            borderStrong: modalRoot.borderStrong
            textPrimary: modalRoot.textPrimary
            textSecondary: modalRoot.textSecondary
            textMuted: modalRoot.textMuted
            accent: modalRoot.accent
        }
    }
}
