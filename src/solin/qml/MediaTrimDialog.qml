import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15
import QtMultimedia

Dialog {
    id: dialog
    objectName: "mediaTrimDialog"

    property var controller: null
    property string itemId: ""
    property string mediaTitle: ""
    property string mediaSource: ""
    property string mediaType: "video"
    property real initialStartTicks: 0
    property real initialEndTicks: 0
    property real initialBaseTicks: 0
    property real durationMs: 0
    property real startMs: 0
    property real endMs: 0
    property string preparationError: ""
    property bool rangeDirty: false
    property bool previewAudioEnabled: true
    property int pointerHoverCount: 0
    property bool previewPrepared: false
    property bool stalledLongEnough: false

    readonly property real minimumRangeMs: 100
    readonly property real handleWidth: 16
    readonly property real maximumDialogWidth: 820
    readonly property real maximumDialogHeight: 700
    readonly property color previewSurfaceColor: "#0b0d12"
    readonly property color previewSurfaceText: "#cbd5e1"
    readonly property color previewSurfaceWarning: "#f2b84b"
    readonly property bool sourceReady: durationMs > 0 && previewPlayer.seekable
    readonly property bool rangeValid: sourceReady
                                       && startMs >= 0
                                       && endMs <= durationMs
                                       && endMs - startMs >= minimumRangeMs
    readonly property bool initialPreviewLoading: !previewPrepared
                                                  && preparationError === ""
                                                  && (previewPlayer.mediaStatus === MediaPlayer.LoadingMedia
                                                      || previewPlayer.mediaStatus === MediaPlayer.BufferingMedia)
    readonly property bool previewActuallyStalled: previewPrepared
                                                   && stalledLongEnough
                                                   && preparationError === ""
                                                   && previewPlayer.playbackState === MediaPlayer.PlayingState
                                                   && previewPlayer.mediaStatus === MediaPlayer.StalledMedia

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(maximumDialogWidth,
                    parent ? Math.max(0, parent.width - 32) : maximumDialogWidth,
                    parent ? parent.width * 0.85 : maximumDialogWidth)
    height: Math.min(maximumDialogHeight,
                     parent ? Math.max(0, parent.height - 32) : maximumDialogHeight,
                     parent ? parent.height * 0.75 : maximumDialogHeight)
    modal: true
    closePolicy: Popup.CloseOnEscape
    padding: 0

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value))
    }

    function formatTime(milliseconds) {
        var total = Math.max(0, Math.round(milliseconds))
        var hours = Math.floor(total / 3600000)
        var minutes = Math.floor((total % 3600000) / 60000)
        var seconds = Math.floor((total % 60000) / 1000)
        var millis = total % 1000
        function two(value) { return value < 10 ? "0" + value : String(value) }
        function three(value) {
            if (value < 10) return "00" + value
            if (value < 100) return "0" + value
            return String(value)
        }
        return (hours > 0 ? two(hours) + ":" : "")
                + two(minutes) + ":" + two(seconds) + "." + three(millis)
    }

    function iconSource(name, size, colorValue) {
        return "image://playlisticons/" + name + "/" + size + "/"
                + String(colorValue).replace("#", "")
    }

    function timelinePositionFor(milliseconds, timelineWidth) {
        if (durationMs <= 0)
            return handleWidth
        return handleWidth
                + clamp(milliseconds / durationMs, 0, 1)
                  * Math.max(0, timelineWidth - handleWidth * 2)
    }

    function millisecondsAtTimelineX(positionX, timelineWidth) {
        var usableWidth = Math.max(1, timelineWidth - handleWidth * 2)
        var ratio = (positionX - handleWidth) / usableWidth
        return clamp(ratio, 0, 1) * durationMs
    }

    function beginPointerHover() {
        pointerHoverCount += 1
        if (pointerHoverCount === 1 && controller !== null
                && controller.pointerEnter !== undefined)
            controller.pointerEnter()
    }

    function endPointerHover() {
        if (pointerHoverCount <= 0)
            return
        pointerHoverCount -= 1
        if (pointerHoverCount === 0 && controller !== null
                && controller.pointerExit !== undefined)
            controller.pointerExit()
    }

    function resetPointerHover() {
        if (pointerHoverCount > 0 && controller !== null
                && controller.pointerExit !== undefined)
            controller.pointerExit()
        pointerHoverCount = 0
    }

    function openFor(node) {
        if (!node || (node.mediaType !== "audio" && node.mediaType !== "video"))
            return
        previewPlayer.stop()
        itemId = String(node.id || "")
        mediaTitle = String(node.title || "")
        mediaSource = String(node.trimSource || node.url || "")
        mediaType = String(node.mediaType || "video")
        initialStartTicks = Number(node.startTrimTicks || 0)
        initialEndTicks = Number(node.endTrimTicks || 0)
        initialBaseTicks = Number(node.baseDurationTicks || 0)
        durationMs = initialBaseTicks > 0 ? initialBaseTicks / 10000 : 0
        startMs = initialStartTicks / 10000
        endMs = durationMs > 0 ? Math.max(startMs, durationMs - initialEndTicks / 10000) : 0
        preparationError = ""
        rangeDirty = false
        previewPrepared = false
        stalledLongEnough = false
        if (node.trimAvailable === false || mediaSource === "") {
            preparationError = qsTr("This media is not available for editing.")
            open()
            return
        }
        previewPlayer.source = mediaSource
        open()
    }

    function resetRange() {
        startMs = 0
        endMs = durationMs
        rangeDirty = true
        previewPlayer.pause()
        previewPlayer.position = 0
    }

    function togglePreview() {
        if (!rangeValid)
            return
        if (previewPlayer.playbackState === MediaPlayer.PlayingState) {
            previewPlayer.pause()
            return
        }
        if (previewPlayer.position < startMs
                || previewPlayer.position >= endMs - 40)
            previewPlayer.position = startMs
        previewPlayer.play()
    }

    onClosed: {
        resetPointerHover()
        previewPrepared = false
        stalledLongEnough = false
        previewPlayer.stop()
        previewPlayer.source = ""
    }

    background: Rectangle {
        radius: 14
        color: appTheme.surface
        border.width: 1
        border.color: appTheme.borderStrong
    }

    Overlay.modal: Rectangle { color: Qt.rgba(0, 0, 0, 0.58) }

    MediaPlayer {
        id: previewPlayer
        objectName: "trimPreviewPlayer"
        audioOutput: AudioOutput {
            volume: 0.72
            muted: !dialog.previewAudioEnabled
        }
        videoOutput: previewOutput

        onDurationChanged: function(duration) {
            if (duration <= 0)
                return
            dialog.durationMs = duration
            if (!dialog.rangeDirty) {
                dialog.startMs = dialog.clamp(dialog.initialStartTicks / 10000,
                                              0, Math.max(0, duration - dialog.minimumRangeMs))
                dialog.endMs = dialog.clamp(
                            duration - dialog.initialEndTicks / 10000,
                            dialog.startMs + dialog.minimumRangeMs,
                            duration)
            } else {
                dialog.startMs = dialog.clamp(dialog.startMs, 0,
                                              Math.max(0, duration - dialog.minimumRangeMs))
                dialog.endMs = dialog.clamp(dialog.endMs,
                                            dialog.startMs + dialog.minimumRangeMs,
                                            duration)
            }
            if (previewPlayer.seekable)
                dialog.previewPrepared = true
        }
        onPositionChanged: function(position) {
            if (playbackState === MediaPlayer.PlayingState
                    && dialog.endMs > 0 && position >= dialog.endMs) {
                pause()
                previewPlayer.position = dialog.endMs
            }
        }
        onErrorOccurred: function(error, errorString) {
            dialog.preparationError = errorString || qsTr("The media could not be opened.")
        }
        onSeekableChanged: function(seekable) {
            if (seekable && dialog.durationMs > 0)
                dialog.previewPrepared = true
            if ((mediaStatus === MediaPlayer.LoadedMedia
                 || mediaStatus === MediaPlayer.BufferedMedia) && !seekable)
                dialog.preparationError = qsTr("This source does not support reliable seeking. Download it for offline use before setting custom times.")
        }
        onMediaStatusChanged: function(status) {
            dialog.stalledLongEnough = false
            if (status === MediaPlayer.LoadedMedia
                    || status === MediaPlayer.BufferedMedia)
                dialog.previewPrepared = true
            if (status === MediaPlayer.InvalidMedia)
                dialog.preparationError = qsTr("The media could not be opened.")
        }
        onPlaybackStateChanged: function(state) {
            if (state !== MediaPlayer.PlayingState)
                dialog.stalledLongEnough = false
        }
    }

    Timer {
        interval: 350
        running: dialog.visible
                 && dialog.previewPrepared
                 && !dialog.stalledLongEnough
                 && dialog.preparationError === ""
                 && previewPlayer.playbackState === MediaPlayer.PlayingState
                 && previewPlayer.mediaStatus === MediaPlayer.StalledMedia
        onTriggered: dialog.stalledLongEnough = true
    }

    Timer {
        interval: 10000
        running: dialog.visible && !dialog.sourceReady
                 && dialog.preparationError === ""
        onTriggered: dialog.preparationError = qsTr("Timed out while checking whether this source supports seeking.")
    }

    component SoftButton: Button {
        id: softButton
        required property var pointerOwner
        property string variant: "secondary"

        implicitWidth: Math.max(78, softButtonText.implicitWidth + 30)
        implicitHeight: 38
        leftPadding: 15
        rightPadding: 15
        opacity: enabled ? 1 : 0.45

        contentItem: Text {
            id: softButtonText
            text: softButton.text
            color: softButton.variant === "primary"
                   ? appTheme.textOnAccent : appTheme.textSecondary
            font.pixelSize: 12
            font.weight: Font.Medium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            radius: 10
            color: {
                if (softButton.variant === "primary") {
                    if (softButton.down)
                        return appTheme.accentPressed
                    return softButton.hovered ? appTheme.accentHover : appTheme.accent
                }
                if (softButton.variant === "ghost")
                    return softButton.hovered ? appTheme.hover : "transparent"
                return softButton.hovered ? appTheme.hoverStrong : appTheme.surfaceAlt
            }
            border.width: softButton.variant === "secondary" ? 1 : 0
            border.color: softButton.hovered ? appTheme.borderStrong : appTheme.border_

            Behavior on color { ColorAnimation { duration: 120 } }
        }

        PointerHover {
            owner: softButton.pointerOwner
            enabled: softButton.enabled
        }
    }

    component PointerHover: HoverHandler {
        id: pointerHover
        required property var owner
        property bool registered: false

        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor

        function syncRegistration() {
            var shouldRegister = enabled && hovered
            if (shouldRegister === registered)
                return
            registered = shouldRegister
            if (registered)
                owner.beginPointerHover()
            else
                owner.endPointerHover()
        }

        onHoveredChanged: syncRegistration()
        onEnabledChanged: syncRegistration()
        Component.onDestruction: {
            if (registered)
                owner.endPointerHover()
        }
    }

    contentItem: ColumnLayout {
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 66

            Column {
                anchors.left: parent.left
                anchors.right: closeButton.left
                anchors.leftMargin: 20
                anchors.rightMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                spacing: 3

                Label {
                    width: parent.width
                    text: qsTr("Start and end times")
                    color: appTheme.textPrimary
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Label {
                    width: parent.width
                    text: dialog.mediaTitle
                    color: appTheme.textSecondary
                    elide: Text.ElideRight
                    font.pixelSize: 12
                }
            }

            Button {
                id: closeButton
                objectName: "trimCloseButton"
                anchors.right: parent.right
                anchors.rightMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                width: 34
                height: 34
                padding: 0
                onClicked: dialog.reject()

                ToolTip.visible: hovered
                ToolTip.delay: 450
                ToolTip.text: qsTr("Close")

                contentItem: Item {
                    Rectangle {
                        anchors.centerIn: parent
                        width: 13
                        height: 1.6
                        radius: 1
                        rotation: 45
                        color: closeButton.hovered ? appTheme.textPrimary : appTheme.textMuted
                    }
                    Rectangle {
                        anchors.centerIn: parent
                        width: 13
                        height: 1.6
                        radius: 1
                        rotation: -45
                        color: closeButton.hovered ? appTheme.textPrimary : appTheme.textMuted
                    }
                }

                background: Rectangle {
                    radius: 9
                    color: closeButton.hovered ? appTheme.hover : "transparent"
                    border.width: closeButton.hovered ? 1 : 0
                    border.color: appTheme.border_
                    Behavior on color { ColorAnimation { duration: 120 } }
                }

                PointerHover {
                    owner: dialog
                    enabled: closeButton.enabled
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: appTheme.border_
            opacity: 0.65
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 18
                anchors.topMargin: 14
                anchors.bottomMargin: 12
                spacing: 12

                Rectangle {
                    id: previewSurface
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: dialog.mediaType === "video" ? 112 : 96
                    Layout.preferredHeight: dialog.mediaType === "video" ? 280 : 150
                    radius: 11
                    color: dialog.previewSurfaceColor
                    border.width: 1
                    border.color: appTheme.border_
                    clip: true

                    VideoOutput {
                        id: previewOutput
                        anchors.fill: parent
                        visible: dialog.mediaType === "video"
                        fillMode: VideoOutput.PreserveAspectFit
                    }

                    Column {
                        anchors.centerIn: parent
                        visible: dialog.mediaType === "audio"
                        spacing: 9

                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: 54
                            height: 54
                            radius: 18
                            color: appTheme.accentTint

                            Image {
                                anchors.centerIn: parent
                                width: 25
                                height: 25
                                source: dialog.iconSource("media_audio", 25, appTheme.accent)
                                sourceSize.width: 25
                                sourceSize.height: 25
                                fillMode: Image.PreserveAspectFit
                            }
                        }
                        Label {
                            objectName: "trimAudioPreviewLabel"
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: qsTr("Audio preview")
                            color: dialog.previewSurfaceText
                            font.pixelSize: 12
                        }
                    }

                    BusyIndicator {
                        objectName: "trimBusyIndicator"
                        anchors.centerIn: parent
                        running: dialog.initialPreviewLoading
                                 || dialog.previewActuallyStalled
                        visible: running
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 12
                        height: errorLabel.implicitHeight + 14
                        radius: 8
                        visible: dialog.preparationError !== ""
                        color: Qt.rgba(0.04, 0.05, 0.07, 0.88)

                        Label {
                            id: errorLabel
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.margins: 9
                            text: dialog.preparationError
                            color: dialog.previewSurfaceWarning
                            wrapMode: Text.Wrap
                            horizontalAlignment: Text.AlignHCenter
                            font.pixelSize: 11
                        }
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.bottom: parent.bottom
                        anchors.margins: 12
                        width: Math.min(parent.width - 24, mutedLabel.implicitWidth + 18)
                        height: 28
                        radius: 9
                        visible: !dialog.previewAudioEnabled
                                 && dialog.preparationError === ""
                        color: Qt.rgba(0.04, 0.05, 0.07, 0.82)

                        Label {
                            id: mutedLabel
                            objectName: "trimMutedPreviewLabel"
                            anchors.centerIn: parent
                            width: parent.width - 18
                            text: qsTr("Preview audio is muted while another media item is active.")
                            color: dialog.previewSurfaceText
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 76
                    spacing: 8

                    Button {
                        id: previewButton
                        objectName: "trimPreviewButton"
                        Layout.minimumWidth: 20
                        Layout.preferredWidth: 20
                        Layout.maximumWidth: 20
                        Layout.minimumHeight: 20
                        Layout.preferredHeight: 20
                        Layout.maximumHeight: 20
                        Layout.alignment: Qt.AlignVCenter
                        enabled: dialog.rangeValid
                        padding: 0
                        onClicked: dialog.togglePreview()

                        ToolTip.visible: hovered
                        ToolTip.delay: 450
                        ToolTip.text: previewPlayer.playbackState === MediaPlayer.PlayingState
                                      ? qsTr("Pause") : qsTr("Play")

                        contentItem: Image {
                            anchors.centerIn: parent
                            width: 10
                            height: 10
                            source: dialog.iconSource(
                                        previewPlayer.playbackState === MediaPlayer.PlayingState
                                        ? "pause" : "play",
                                        20,
                                        previewButton.enabled
                                        ? (previewButton.hovered
                                           ? appTheme.textPrimary : appTheme.textSecondary)
                                        : appTheme.textDim)
                            sourceSize.width: 20
                            sourceSize.height: 20
                            fillMode: Image.PreserveAspectFit
                        }

                        background: Item {}

                        PointerHover {
                            owner: dialog
                            enabled: previewButton.enabled
                        }
                    }

                    Item {
                        id: timeline
                        objectName: "trimTimeline"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        enabled: dialog.sourceReady
                        opacity: enabled ? 1 : 0.52

                        Rectangle {
                            id: track
                            objectName: "trimTrack"
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.leftMargin: dialog.handleWidth
                            anchors.rightMargin: dialog.handleWidth
                            anchors.verticalCenter: parent.verticalCenter
                            height: 7
                            radius: 3.5
                            color: appTheme.borderStrong
                        }

                        Rectangle {
                            id: selectedRange
                            objectName: "trimSelectedRange"
                            z: 1
                            x: dialog.timelinePositionFor(dialog.startMs, timeline.width)
                            y: track.y
                            width: Math.max(
                                       0,
                                       dialog.timelinePositionFor(dialog.endMs, timeline.width) - x)
                            height: track.height
                            radius: track.radius
                            color: appTheme.accent
                        }

                        Item {
                            id: seekArea
                            objectName: "trimSeekArea"
                            z: 2
                            x: selectedRange.x
                            y: track.y - 10
                            width: selectedRange.width
                            height: track.height + 20
                            enabled: timeline.enabled && width > 0

                            function seekToPosition(positionX, positionY) {
                                var point = mapToItem(timeline, positionX, positionY)
                                previewPlayer.position = dialog.clamp(
                                            dialog.millisecondsAtTimelineX(
                                                point.x, timeline.width),
                                            dialog.startMs,
                                            dialog.endMs)
                            }

                            MouseArea {
                                anchors.fill: parent
                                enabled: seekArea.enabled
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                preventStealing: true

                                onPressed: function(mouse) {
                                    seekArea.seekToPosition(mouse.x, mouse.y)
                                    mouse.accepted = true
                                }
                                onPositionChanged: function(mouse) {
                                    if (pressed)
                                        seekArea.seekToPosition(mouse.x, mouse.y)
                                }
                            }

                            PointerHover {
                                owner: dialog
                                enabled: seekArea.enabled
                            }
                        }

                        Rectangle {
                            id: playhead
                            z: 3
                            x: dialog.timelinePositionFor(
                                   previewPlayer.position, timeline.width) - width / 2
                            y: track.y - 4
                            width: 2
                            height: track.height + 8
                            radius: 1
                            color: appTheme.textPrimary
                            visible: dialog.sourceReady
                                     && previewPlayer.position >= dialog.startMs
                                     && previewPlayer.position <= dialog.endMs
                            opacity: 0.85
                        }

                        Rectangle {
                            id: startHandle
                            z: 4
                            objectName: "trimStartHandle"
                            x: dialog.timelinePositionFor(dialog.startMs, timeline.width) - width
                            y: track.y + track.height / 2 - height / 2
                            width: dialog.handleWidth
                            height: 28
                            radius: 6
                            color: startDrag.pressed ? appTheme.accentHover : appTheme.accent
                            border.width: 2
                            border.color: appTheme.surface

                            MouseArea {
                                id: startDrag
                                property real grabOffsetX: 0
                                anchors.fill: parent
                                anchors.margins: -8
                                hoverEnabled: true
                                cursorShape: Qt.SizeHorCursor
                                onPressed: function(mouse) {
                                    previewPlayer.pause()
                                    var point = mapToItem(timeline, mouse.x, mouse.y)
                                    grabOffsetX = point.x - dialog.timelinePositionFor(
                                                dialog.startMs, timeline.width)
                                    mouse.accepted = true
                                }
                                onPositionChanged: function(mouse) {
                                    if (!pressed)
                                        return
                                    var point = mapToItem(timeline, mouse.x, mouse.y)
                                    dialog.startMs = dialog.clamp(
                                                dialog.millisecondsAtTimelineX(
                                                    point.x - grabOffsetX,
                                                    timeline.width),
                                                0,
                                                dialog.endMs - dialog.minimumRangeMs)
                                    dialog.rangeDirty = true
                                }
                                onReleased: function(mouse) {
                                    previewPlayer.position = dialog.startMs
                                    mouse.accepted = true
                                }
                            }
                        }

                        Rectangle {
                            id: endHandle
                            z: 4
                            objectName: "trimEndHandle"
                            x: dialog.timelinePositionFor(dialog.endMs, timeline.width)
                            y: track.y + track.height / 2 - height / 2
                            width: dialog.handleWidth
                            height: 28
                            radius: 6
                            color: endDrag.pressed ? appTheme.accentHover : appTheme.accent
                            border.width: 2
                            border.color: appTheme.surface

                            MouseArea {
                                id: endDrag
                                property real grabOffsetX: 0
                                anchors.fill: parent
                                anchors.margins: -8
                                hoverEnabled: true
                                cursorShape: Qt.SizeHorCursor
                                onPressed: function(mouse) {
                                    previewPlayer.pause()
                                    var point = mapToItem(timeline, mouse.x, mouse.y)
                                    grabOffsetX = point.x - dialog.timelinePositionFor(
                                                dialog.endMs, timeline.width)
                                    mouse.accepted = true
                                }
                                onPositionChanged: function(mouse) {
                                    if (!pressed)
                                        return
                                    var point = mapToItem(timeline, mouse.x, mouse.y)
                                    dialog.endMs = dialog.clamp(
                                                dialog.millisecondsAtTimelineX(
                                                    point.x - grabOffsetX,
                                                    timeline.width),
                                                dialog.startMs + dialog.minimumRangeMs,
                                                dialog.durationMs)
                                    dialog.rangeDirty = true
                                }
                                onReleased: function(mouse) {
                                    previewPlayer.position = dialog.endMs
                                    mouse.accepted = true
                                }
                            }
                        }

                        Rectangle {
                            id: startBubble
                            objectName: "trimStartBubble"
                            z: 5
                            x: dialog.clamp(
                                   startHandle.x + startHandle.width / 2 - width / 2,
                                   0,
                                   Math.max(0, timeline.width - width))
                            y: -17
                            width: startBubbleText.implicitWidth + 16
                            height: 26
                            radius: 8
                            color: appTheme.hoverStrong
                            border.width: 1
                            border.color: appTheme.borderStrong
                            opacity: startDrag.pressed ? 1 : 0
                            visible: opacity > 0

                            Label {
                                id: startBubbleText
                                anchors.centerIn: parent
                                text: qsTr("Start") + "  " + dialog.formatTime(dialog.startMs)
                                color: appTheme.textPrimary
                                font.pixelSize: 10
                                font.weight: Font.Medium
                            }
                            Rectangle {
                                anchors.horizontalCenter: parent.horizontalCenter
                                anchors.bottom: parent.bottom
                                anchors.bottomMargin: -4
                                width: 8
                                height: 8
                                rotation: 45
                                color: parent.color
                                border.width: 1
                                border.color: parent.border.color
                            }
                            Behavior on opacity { NumberAnimation { duration: 90 } }
                        }

                        Rectangle {
                            id: endBubble
                            objectName: "trimEndBubble"
                            z: 5
                            x: dialog.clamp(
                                   endHandle.x + endHandle.width / 2 - width / 2,
                                   0,
                                   Math.max(0, timeline.width - width))
                            y: -17
                            width: endBubbleText.implicitWidth + 16
                            height: 26
                            radius: 8
                            color: appTheme.hoverStrong
                            border.width: 1
                            border.color: appTheme.borderStrong
                            opacity: endDrag.pressed ? 1 : 0
                            visible: opacity > 0

                            Label {
                                id: endBubbleText
                                anchors.centerIn: parent
                                text: qsTr("End") + "  " + dialog.formatTime(dialog.endMs)
                                color: appTheme.textPrimary
                                font.pixelSize: 10
                                font.weight: Font.Medium
                            }
                            Rectangle {
                                anchors.horizontalCenter: parent.horizontalCenter
                                anchors.bottom: parent.bottom
                                anchors.bottomMargin: -4
                                width: 8
                                height: 8
                                rotation: 45
                                color: parent.color
                                border.width: 1
                                border.color: parent.border.color
                            }
                            Behavior on opacity { NumberAnimation { duration: 90 } }
                        }

                        Label {
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            text: qsTr("Selected") + "  "
                                  + dialog.formatTime(Math.max(0, dialog.endMs - dialog.startMs))
                            color: appTheme.textMuted
                            font.pixelSize: 10
                            font.weight: Font.Medium
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: appTheme.border_
            opacity: 0.65
        }

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 64

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 8

                SoftButton {
                    pointerOwner: dialog
                    text: qsTr("Reset")
                    variant: "ghost"
                    enabled: dialog.sourceReady
                    onClicked: dialog.resetRange()
                }

                Item { Layout.fillWidth: true }

                SoftButton {
                    pointerOwner: dialog
                    text: qsTr("Cancel")
                    variant: "secondary"
                    onClicked: dialog.reject()
                }

                SoftButton {
                    pointerOwner: dialog
                    text: qsTr("Save")
                    variant: "primary"
                    enabled: dialog.rangeValid && dialog.rangeDirty
                             && dialog.controller !== null
                    onClicked: {
                        dialog.controller.setMediaTrim(
                            dialog.itemId, dialog.startMs, dialog.endMs, dialog.durationMs)
                        dialog.accept()
                    }
                }
            }
        }
    }
}
