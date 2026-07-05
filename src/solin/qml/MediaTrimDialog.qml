import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15
import QtMultimedia

Dialog {
    id: dialog

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
    readonly property real minimumRangeMs: 100
    readonly property bool sourceReady: durationMs > 0 && previewPlayer.seekable
    readonly property bool rangeValid: sourceReady
                                       && startMs >= 0
                                       && endMs <= durationMs
                                       && endMs - startMs >= minimumRangeMs

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(760, parent ? parent.width - 48 : 760)
    height: Math.min(650, parent ? parent.height - 48 : 650)
    modal: true
    closePolicy: Popup.NoAutoClose
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

    function parseTime(value) {
        var text = String(value).trim().replace(",", ".")
        var parts = text.split(":")
        if (parts.length < 2 || parts.length > 3)
            return NaN
        var seconds = Number(parts[parts.length - 1])
        var minutes = Number(parts[parts.length - 2])
        var hours = parts.length === 3 ? Number(parts[0]) : 0
        if (!isFinite(seconds) || !isFinite(minutes) || !isFinite(hours)
                || seconds < 0 || minutes < 0 || hours < 0)
            return NaN
        return (hours * 3600 + minutes * 60 + seconds) * 1000
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

    function previewFromStart() {
        if (!rangeValid)
            return
        previewPlayer.position = startMs
        previewPlayer.play()
    }

    onClosed: {
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
            if ((mediaStatus === MediaPlayer.LoadedMedia
                 || mediaStatus === MediaPlayer.BufferedMedia) && !seekable)
                dialog.preparationError = qsTr("This source does not support reliable seeking. Download it for offline use before setting custom times.")
        }
        onMediaStatusChanged: function(status) {
            if (status === MediaPlayer.InvalidMedia)
                dialog.preparationError = qsTr("The media could not be opened.")
        }
    }

    Timer {
        interval: 10000
        running: dialog.visible && !dialog.sourceReady
                 && dialog.preparationError === ""
        onTriggered: dialog.preparationError = qsTr("Timed out while checking whether this source supports seeking.")
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 22
            Layout.bottomMargin: 16
            spacing: 12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Label {
                    text: qsTr("Start and end times")
                    color: appTheme.textPrimary
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                }
                Label {
                    Layout.fillWidth: true
                    text: dialog.mediaTitle
                    color: appTheme.textSecondary
                    elide: Text.ElideRight
                    font.pixelSize: 13
                }
            }
            ToolButton {
                text: "×"
                font.pixelSize: 24
                onClicked: dialog.reject()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: dialog.mediaType === "video" ? 280 : 150
            Layout.leftMargin: 22
            Layout.rightMargin: 22
            radius: 10
            color: "#0b0d12"
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
                spacing: 10
                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "♫"
                    color: appTheme.accent
                    font.pixelSize: 42
                }
                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: qsTr("Audio preview")
                    color: appTheme.textSecondary
                }
            }

            BusyIndicator {
                anchors.centerIn: parent
                running: previewPlayer.mediaStatus === MediaPlayer.LoadingMedia
                         || previewPlayer.mediaStatus === MediaPlayer.BufferingMedia
                visible: running
            }

            Label {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 12
                visible: dialog.preparationError !== ""
                text: dialog.preparationError
                color: appTheme.warning
                wrapMode: Text.Wrap
                horizontalAlignment: Text.AlignHCenter
                font.pixelSize: 12
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 22
            Layout.rightMargin: 22
            Layout.topMargin: 18
            spacing: 10

            Item {
                id: timeline
                Layout.fillWidth: true
                Layout.preferredHeight: 42
                enabled: dialog.sourceReady

                Rectangle {
                    id: track
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    height: 8
                    radius: 4
                    color: appTheme.borderStrong
                }
                Rectangle {
                    x: track.x + (dialog.durationMs > 0 ? dialog.startMs / dialog.durationMs * track.width : 0)
                    width: dialog.durationMs > 0
                           ? Math.max(2, (dialog.endMs - dialog.startMs) / dialog.durationMs * track.width)
                           : 0
                    anchors.verticalCenter: track.verticalCenter
                    height: track.height
                    radius: 4
                    color: appTheme.accent
                }
                Rectangle {
                    id: startHandle
                    x: dialog.durationMs > 0
                       ? dialog.startMs / dialog.durationMs * (timeline.width - width) : 0
                    anchors.verticalCenter: track.verticalCenter
                    width: 18; height: 28; radius: 7
                    color: appTheme.accent
                    border.width: 2; border.color: appTheme.surface
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -8
                        onPositionChanged: function(mouse) {
                            if (!pressed)
                                return
                            var point = mapToItem(timeline, mouse.x, mouse.y)
                            dialog.startMs = dialog.clamp(
                                point.x / Math.max(1, timeline.width) * dialog.durationMs,
                                0, dialog.endMs - dialog.minimumRangeMs)
                            dialog.rangeDirty = true
                        }
                        onReleased: previewPlayer.position = dialog.startMs
                    }
                }
                Rectangle {
                    id: endHandle
                    x: dialog.durationMs > 0
                       ? dialog.endMs / dialog.durationMs * (timeline.width - width) : timeline.width - width
                    anchors.verticalCenter: track.verticalCenter
                    width: 18; height: 28; radius: 7
                    color: appTheme.accent
                    border.width: 2; border.color: appTheme.surface
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -8
                        onPositionChanged: function(mouse) {
                            if (!pressed)
                                return
                            var point = mapToItem(timeline, mouse.x, mouse.y)
                            dialog.endMs = dialog.clamp(
                                point.x / Math.max(1, timeline.width) * dialog.durationMs,
                                dialog.startMs + dialog.minimumRangeMs, dialog.durationMs)
                            dialog.rangeDirty = true
                        }
                        onReleased: previewPlayer.position = dialog.endMs
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                ColumnLayout {
                    Layout.fillWidth: true
                    Label { text: qsTr("Start"); color: appTheme.textSecondary; font.pixelSize: 12 }
                    TextField {
                        id: startField
                        Layout.fillWidth: true
                        enabled: dialog.sourceReady
                        selectByMouse: true
                        onEditingFinished: {
                            var parsed = dialog.parseTime(text)
                            if (isFinite(parsed)) {
                                dialog.startMs = dialog.clamp(parsed, 0, dialog.endMs - dialog.minimumRangeMs)
                                dialog.rangeDirty = true
                                previewPlayer.position = dialog.startMs
                            }
                        }
                        Binding {
                            target: startField
                            property: "text"
                            value: dialog.formatTime(dialog.startMs)
                            when: !startField.activeFocus
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Label { text: qsTr("End"); color: appTheme.textSecondary; font.pixelSize: 12 }
                    TextField {
                        id: endField
                        Layout.fillWidth: true
                        enabled: dialog.sourceReady
                        selectByMouse: true
                        onEditingFinished: {
                            var parsed = dialog.parseTime(text)
                            if (isFinite(parsed)) {
                                dialog.endMs = dialog.clamp(parsed, dialog.startMs + dialog.minimumRangeMs, dialog.durationMs)
                                dialog.rangeDirty = true
                            }
                        }
                        Binding {
                            target: endField
                            property: "text"
                            value: dialog.formatTime(dialog.endMs)
                            when: !endField.activeFocus
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Label { text: qsTr("Result"); color: appTheme.textSecondary; font.pixelSize: 12 }
                    Label {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 40
                        verticalAlignment: Text.AlignVCenter
                        text: dialog.formatTime(Math.max(0, dialog.endMs - dialog.startMs))
                        color: appTheme.textPrimary
                        font.pixelSize: 14
                        font.weight: Font.Medium
                    }
                }
            }
        }

        Item { Layout.fillHeight: true }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: appTheme.border_
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 18
            spacing: 10
            Button {
                text: qsTr("Reset")
                enabled: dialog.sourceReady
                onClicked: dialog.resetRange()
            }
            Button {
                text: previewPlayer.playbackState === MediaPlayer.PlayingState
                      ? qsTr("Pause preview") : qsTr("Preview")
                enabled: dialog.rangeValid
                onClicked: previewPlayer.playbackState === MediaPlayer.PlayingState
                           ? previewPlayer.pause() : dialog.previewFromStart()
            }
            Label {
                visible: !dialog.previewAudioEnabled
                text: qsTr("Preview audio is muted while another media item is active.")
                color: appTheme.textSecondary
                font.pixelSize: 11
            }
            Item { Layout.fillWidth: true }
            Button { text: qsTr("Cancel"); onClicked: dialog.reject() }
            Button {
                text: qsTr("Save")
                highlighted: true
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
