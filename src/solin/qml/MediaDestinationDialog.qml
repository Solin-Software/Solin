// MediaDestinationDialog.qml — unified Play / Add to… destination wizard.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    anchors.fill: parent
    color: appTheme.bg0
    focus: true

    readonly property color surface: appTheme.surface
    readonly property color surfaceElevated: appTheme.hoverStrong
    readonly property color hover: appTheme.hover
    readonly property color border_: appTheme.border_
    readonly property color borderStrong: appTheme.borderStrong
    readonly property color textPrimary: appTheme.textPrimary
    readonly property color textSecondary: appTheme.textSecondary
    readonly property color textMuted: appTheme.textMuted
    readonly property color textDim: appTheme.textDim
    readonly property color accent: appTheme.accent
    readonly property color accentTint: appTheme.accentTint

    function iconSource(name, size, color) {
        var hex = String(color).replace("#", "")
        return "image://destinationicons/" + name + "/" + size + "/" + hex
    }

    Keys.onEscapePressed: function(event) {
        if (destinationBridge.canGoBack)
            destinationBridge.back()
        else
            destinationBridge.cancel()
        event.accepted = true
    }

    Connections {
        target: destinationBridge
        function onChanged() {
            if (destinationBridge.step === "playlist")
                Qt.callLater(function() { playlistSearch.forceActiveFocus() })
        }
    }

    function stepIndex(step) {
        if (step === "action") return 0
        if (step === "preparing") return 1
        if (step === "destination") return 2
        if (step === "playlist") return 3
        if (step === "meeting" || step === "placement") return 4
        return 5
    }

    function filteredPlaylists(query) {
        var source = destinationBridge.playlists || []
        var needle = (query || "").trim().toLowerCase()
        if (needle === "") return source
        var result = []
        for (var index = 0; index < source.length; ++index) {
            if ((source[index].name || "").toLowerCase().indexOf(needle) >= 0)
                result.push(source[index])
        }
        return result
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            GhostButton {
                iconName: "chevron_left"
                accessibleName: qsTranslate("MediaDestinationDialog", "Back")
                visible: destinationBridge.canGoBack
                onClicked: destinationBridge.back()
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    Layout.fillWidth: true
                    text: qsTranslate("MediaDestinationDialog", "Media destination")
                    color: root.textPrimary
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: destinationBridge.itemSummary
                    color: root.textMuted
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
            }

            GhostButton {
                iconName: "close"
                accessibleName: qsTranslate("MediaDestinationDialog", "Cancel")
                onClicked: destinationBridge.cancel()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: mediaTitle.implicitHeight + 22
            radius: 10
            color: root.surfaceElevated
            border.width: 1
            border.color: root.border_

            Text {
                id: mediaTitle
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: 12
                text: destinationBridge.mediaTitle
                color: root.textSecondary
                font.pixelSize: 12
                font.weight: Font.Medium
                maximumLineCount: 2
                wrapMode: Text.Wrap
                elide: Text.ElideRight
            }
        }

        StackLayout {
            id: pages
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: root.stepIndex(destinationBridge.step)

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "What would you like to do?")
                subtitle: qsTranslate("MediaDestinationDialog", "Play now or organize this media for later.")

                ChoiceCard {
                    Layout.fillWidth: true
                    title: qsTranslate("MediaDestinationDialog", "Play now")
                    subtitle: qsTranslate("MediaDestinationDialog", "Send it directly to the projection.")
                    iconName: "play"
                    emphasized: true
                    enabled: destinationBridge.canPlay && !destinationBridge.busy
                    onClicked: destinationBridge.choosePlay()
                }

                ChoiceCard {
                    Layout.fillWidth: true
                    title: qsTranslate("MediaDestinationDialog", "Add to…")
                    subtitle: qsTranslate("MediaDestinationDialog", "Choose a playlist or a meeting.")
                    iconName: "plus"
                    enabled: !destinationBridge.busy
                    onClicked: destinationBridge.requestAdd()
                }
            }

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "Preparing media")
                subtitle: qsTranslate("MediaDestinationDialog", "This should only take a moment.")

                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    BusyRing {
                        anchors.centerIn: parent
                        running: true
                    }
                }
            }

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "Where should it go?")
                subtitle: qsTranslate("MediaDestinationDialog", "You can keep it in a playlist or prepare it for a meeting.")

                ChoiceCard {
                    Layout.fillWidth: true
                    title: qsTranslate("MediaDestinationDialog", "Playlists")
                    subtitle: qsTranslate("MediaDestinationDialog", "Add to an existing playlist or create a new one.")
                    iconName: "playlist"
                    onClicked: destinationBridge.showPlaylists()
                }

                ChoiceCard {
                    Layout.fillWidth: true
                    title: qsTranslate("MediaDestinationDialog", "Meetings")
                    subtitle: qsTranslate("MediaDestinationDialog", "Choose a week, meeting and position.")
                    iconName: "meeting"
                    onClicked: destinationBridge.showMeetings()
                }
            }

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "Choose a playlist")
                subtitle: qsTranslate("MediaDestinationDialog", "Select one below or create a new playlist.")

                TextField {
                    id: playlistSearch
                    Layout.fillWidth: true
                    Layout.preferredHeight: 38
                    placeholderText: qsTranslate("MediaDestinationDialog", "Search playlists…")
                    color: root.textPrimary
                    placeholderTextColor: root.textDim
                    selectByMouse: true
                    background: Rectangle {
                        radius: 8
                        color: root.surfaceElevated
                        border.width: playlistSearch.activeFocus ? 1 : 1
                        border.color: playlistSearch.activeFocus ? root.accent : root.border_
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 130
                    radius: 10
                    color: root.surface
                    border.width: 1
                    border.color: root.border_
                    clip: true

                    ListView {
                        id: playlistList
                        anchors.fill: parent
                        anchors.margins: 5
                        spacing: 3
                        model: root.filteredPlaylists(playlistSearch.text)
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: ScrollBar { }

                        delegate: Rectangle {
                            required property var modelData
                            width: playlistList.width
                            height: 40
                            radius: 8
                            color: playlistMouse.containsMouse ? root.hover : "transparent"
                            activeFocusOnTab: true
                            Keys.onReturnPressed: destinationBridge.choosePlaylist(
                                modelData.id,
                                modelData.name
                            )
                            Keys.onEnterPressed: destinationBridge.choosePlaylist(
                                modelData.id,
                                modelData.name
                            )

                            Text {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.margins: 12
                                text: modelData.name
                                color: playlistMouse.containsMouse ? root.textPrimary : root.textSecondary
                                font.pixelSize: 13
                                elide: Text.ElideRight
                            }

                            MouseArea {
                                id: playlistMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: destinationBridge.choosePlaylist(
                                    modelData.id,
                                    modelData.name
                                )
                            }
                        }

                        Text {
                            anchors.centerIn: parent
                            visible: playlistList.count === 0
                            text: playlistSearch.text === ""
                                  ? qsTranslate("MediaDestinationDialog", "No playlists yet")
                                  : qsTranslate("MediaDestinationDialog", "No matching playlists")
                            color: root.textMuted
                            font.pixelSize: 12
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    TextField {
                        id: newPlaylistName
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        placeholderText: qsTranslate("MediaDestinationDialog", "New playlist name…")
                        color: root.textPrimary
                        placeholderTextColor: root.textDim
                        selectByMouse: true
                        onAccepted: if (text.trim() !== "")
                            destinationBridge.createPlaylist(text)
                        background: Rectangle {
                            radius: 8
                            color: root.surfaceElevated
                            border.width: 1
                            border.color: newPlaylistName.activeFocus ? root.accent : root.border_
                        }
                    }

                    PrimaryButton {
                        text: qsTranslate("MediaDestinationDialog", "Create and add")
                        enabled: newPlaylistName.text.trim() !== ""
                                 && !destinationBridge.busy
                        onClicked: destinationBridge.createPlaylist(newPlaylistName.text)
                    }
                }
            }

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "Choose a meeting")
                subtitle: qsTranslate("MediaDestinationDialog", "Select the week first, then the meeting.")

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 58
                    radius: 10
                    color: root.surfaceElevated
                    border.width: 1
                    border.color: root.border_

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 8

                        GhostButton {
                            iconName: "chevron_left"
                            enabled: destinationBridge.canPreviousWeek
                            accessibleName: qsTranslate("MediaDestinationDialog", "Previous week")
                            onClicked: destinationBridge.previousWeek()
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Text {
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignHCenter
                                text: destinationBridge.weekLabel
                                color: root.textPrimary
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }

                            Text {
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignHCenter
                                text: destinationBridge.isCurrentWeek
                                      ? qsTranslate("MediaDestinationDialog", "This week")
                                      : qsTranslate("MediaDestinationDialog", "Return to this week")
                                color: destinationBridge.isCurrentWeek ? appTheme.success : root.accent
                                font.pixelSize: 10

                                MouseArea {
                                    anchors.fill: parent
                                    enabled: !destinationBridge.isCurrentWeek
                                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                    onClicked: destinationBridge.currentWeek()
                                }
                            }
                        }

                        GhostButton {
                            iconName: "chevron_right"
                            enabled: destinationBridge.canNextWeek
                            accessibleName: qsTranslate("MediaDestinationDialog", "Next week")
                            onClicked: destinationBridge.nextWeek()
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    Repeater {
                        model: destinationBridge.meetingTargets

                        MeetingCard {
                            required property var modelData
                            Layout.fillWidth: true
                            title: modelData.title || ""
                            subtitle: modelData.subtitle || ""
                            status: modelData.status || "loading"
                            accentColor: modelData.color || root.accent
                            selectable: !!modelData.available && !destinationBridge.busy
                            onClicked: destinationBridge.chooseMeeting(modelData.pub_type)
                            onRetry: destinationBridge.retryWeek()
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 164
                        radius: 12
                        color: root.surface
                        border.width: 1
                        border.color: root.border_
                        visible: destinationBridge.meetingTargets.length === 0

                        BusyRing {
                            anchors.centerIn: parent
                            running: parent.visible
                        }
                    }
                }

                Item { Layout.fillHeight: true }
            }

            WizardPage {
                title: qsTranslate("MediaDestinationDialog", "Could not continue")
                subtitle: destinationBridge.errorText

                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    PrimaryButton {
                        anchors.centerIn: parent
                        text: qsTranslate("MediaDestinationDialog", "Try again")
                        onClicked: destinationBridge.retry()
                    }
                }
            }
        }
    }

    MediaPlacementOverlay {
        anchors.fill: parent
        placementBridge: destinationBridge
        surface: root.surface
        surfaceElevated: root.surfaceElevated
        hover: root.hover
        border_: root.border_
        borderStrong: root.borderStrong
        textPrimary: root.textPrimary
        textSecondary: root.textSecondary
        textMuted: root.textMuted
        accent: root.accent
    }

    component WizardPage: ColumnLayout {
        property string title: ""
        property string subtitle: ""
        spacing: 12
        opacity: StackLayout.isCurrentItem ? 1 : 0

        Behavior on opacity {
            NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
        }

        Text {
            Layout.fillWidth: true
            text: parent.title
            color: root.textPrimary
            font.pixelSize: 15
            font.weight: Font.DemiBold
            wrapMode: Text.Wrap
        }

        Text {
            Layout.fillWidth: true
            text: parent.subtitle
            color: root.textMuted
            font.pixelSize: 11
            wrapMode: Text.Wrap
            visible: text !== ""
        }
    }

    component ChoiceCard: Rectangle {
        id: choice
        property string title: ""
        property string subtitle: ""
        property string iconName: ""
        property bool emphasized: false
        signal clicked()

        Layout.preferredHeight: 82
        radius: 12
        color: choiceMouse.containsMouse
               ? (emphasized ? root.accentTint : root.hover)
               : root.surface
        border.width: 1
        border.color: emphasized ? root.accent : root.border_
        opacity: enabled ? 1 : 0.45

        Behavior on color { ColorAnimation { duration: 120 } }

        RowLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 14

            Rectangle {
                Layout.preferredWidth: 42
                Layout.preferredHeight: 42
                radius: 12
                color: choice.emphasized ? root.accentTint : root.surfaceElevated

                Image {
                    anchors.centerIn: parent
                    width: 18
                    height: 18
                    source: root.iconSource(
                        choice.iconName,
                        18,
                        choice.emphasized ? root.accent : root.textSecondary
                    )
                    sourceSize: Qt.size(18, 18)
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3

                Text {
                    Layout.fillWidth: true
                    text: choice.title
                    color: root.textPrimary
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                Text {
                    Layout.fillWidth: true
                    text: choice.subtitle
                    color: root.textMuted
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }

            Image {
                width: 14
                height: 14
                source: root.iconSource("chevron_right", 14, root.textMuted)
                sourceSize: Qt.size(14, 14)
            }
        }

        MouseArea {
            id: choiceMouse
            anchors.fill: parent
            enabled: choice.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: choice.clicked()
        }
    }

    component MeetingCard: Rectangle {
        id: meetingCard
        property string title: ""
        property string subtitle: ""
        property string status: "loading"
        property color accentColor: root.accent
        property bool selectable: false
        signal clicked()
        signal retry()

        Layout.preferredHeight: 78
        radius: 12
        color: meetingMouse.containsMouse && enabled ? root.hover : root.surface
        border.width: 1
        border.color: selectable ? accentColor : root.border_
        opacity: status === "unavailable" ? 0.62 : 1

        Rectangle {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: 8
            width: 3
            height: 42
            radius: 1.5
            color: meetingCard.accentColor
        }

        Column {
            anchors.left: parent.left
            anchors.right: statusItem.left
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: 20
            anchors.rightMargin: 12
            spacing: 4

            Text {
                width: parent.width
                text: meetingCard.title
                color: root.textPrimary
                font.pixelSize: 13
                font.weight: Font.DemiBold
                elide: Text.ElideRight
            }

            Text {
                width: parent.width
                text: meetingCard.subtitle
                color: root.textMuted
                font.pixelSize: 10
                elide: Text.ElideRight
            }
        }

        Item {
            id: statusItem
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.rightMargin: 14
            width: 72
            height: 32

            BusyRing {
                anchors.centerIn: parent
                width: 20
                height: 20
                running: meetingCard.status === "loading"
                visible: running
            }

            Image {
                anchors.centerIn: parent
                visible: meetingCard.status === "ready"
                width: 14
                height: 14
                source: root.iconSource("chevron_right", 14, root.textMuted)
                sourceSize: Qt.size(14, 14)
            }

            Text {
                anchors.centerIn: parent
                visible: meetingCard.status === "error"
                text: qsTranslate("MediaDestinationDialog", "Retry")
                color: root.accent
                font.pixelSize: 10

                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -8
                    cursorShape: Qt.PointingHandCursor
                    onClicked: meetingCard.retry()
                }
            }
        }

        MouseArea {
            id: meetingMouse
            anchors.fill: parent
            enabled: meetingCard.selectable && meetingCard.status === "ready"
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: meetingCard.clicked()
        }
    }

    component GhostButton: Rectangle {
        id: ghost
        property string iconName: ""
        property string accessibleName: ""
        signal clicked()

        Layout.preferredWidth: 34
        Layout.preferredHeight: 34
        width: 34
        height: 34
        radius: 9
        color: ghostMouse.containsMouse && enabled ? root.hover : "transparent"
        opacity: enabled ? 1 : 0.35

        Image {
            anchors.centerIn: parent
            width: 15
            height: 15
            source: root.iconSource(ghost.iconName, 15, root.textSecondary)
            sourceSize: Qt.size(15, 15)
        }

        MouseArea {
            id: ghostMouse
            anchors.fill: parent
            enabled: ghost.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: ghost.clicked()
        }
    }

    component PrimaryButton: Rectangle {
        id: primary
        property string text: ""
        signal clicked()

        implicitWidth: primaryLabel.implicitWidth + 24
        implicitHeight: 38
        radius: 9
        color: enabled
               ? (primaryMouse.containsMouse ? appTheme.accentHover : root.accent)
               : root.surfaceElevated
        opacity: enabled ? 1 : 0.5

        Text {
            id: primaryLabel
            anchors.centerIn: parent
            text: primary.text
            color: enabled ? appTheme.textOnAccent : root.textMuted
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: primaryMouse
            anchors.fill: parent
            enabled: primary.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: primary.clicked()
        }
    }

    component BusyRing: Item {
        id: ring
        property bool running: false
        width: 28
        height: 28
        visible: running

        Canvas {
            anchors.fill: parent
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.lineWidth = 2.4
                ctx.lineCap = "round"
                ctx.strokeStyle = root.accent
                ctx.beginPath()
                ctx.arc(width / 2, height / 2, Math.min(width, height) * 0.34,
                        -Math.PI * 0.15, Math.PI * 1.35)
                ctx.stroke()
            }

            RotationAnimation on rotation {
                running: ring.running
                from: 0
                to: 360
                duration: 850
                loops: Animation.Infinite
            }
        }
    }
}
