// TimerSegment.qml — a segmented selector with an animated sliding highlight.
// Controlled: `current` comes from a binding, selection emits `picked(index)`.
// Set `stretch: true` (with Layout.fillWidth) to divide the available width
// evenly between segments for a spacious, aligned look.
import QtQuick 2.15
import QtQuick.Controls 2.15

Rectangle {
    id: seg
    property var options: []
    property int current: 0
    property color accent: appTheme.accent
    property int segWidth: 92
    property int segHeight: 32
    property int labelPadding: 10
    property int labelPixelSize: 12
    property int labelWeight: Font.DemiBold
    property bool stretch: false
    signal picked(int index)

    property real _minSegW: segWidth
    readonly property int _count: Math.max(1, options.length)
    readonly property real _availableSegW: Math.max(0, (width - 6) / _count)
    readonly property real _segW: stretch ? _availableSegW : Math.min(_minSegW, _availableSegW)
    readonly property int _safeCurrent: Math.min(_count - 1, Math.max(0, current))

    function _syncMeasuredWidth() {
        var maxWidth = 0
        for (var i = 0; i < measureRepeater.count; ++i) {
            var item = measureRepeater.itemAt(i)
            if (item)
                maxWidth = Math.max(maxWidth, item.implicitWidth)
        }
        _minSegW = Math.max(segWidth, Math.ceil(maxWidth + labelPadding * 2))
    }

    implicitHeight: segHeight
    implicitWidth: _count * _minSegW + 6
    radius: 9
    color: appTheme.surfaceAlt
    border.color: appTheme.border_
    border.width: 1
    opacity: enabled ? 1.0 : 0.72
    Behavior on opacity { NumberAnimation { duration: 140 } }

    onOptionsChanged: Qt.callLater(_syncMeasuredWidth)
    onSegWidthChanged: Qt.callLater(_syncMeasuredWidth)
    onLabelPaddingChanged: Qt.callLater(_syncMeasuredWidth)
    onLabelPixelSizeChanged: Qt.callLater(_syncMeasuredWidth)
    onLabelWeightChanged: Qt.callLater(_syncMeasuredWidth)
    Component.onCompleted: Qt.callLater(_syncMeasuredWidth)

    Rectangle {
        id: hi
        y: 3
        height: parent.height - 6
        width: seg._segW
        radius: 6
        x: 3 + seg._safeCurrent * seg._segW
        color: seg.accent
        Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: 160 } }
    }

    Item {
        width: 0
        height: 0
        visible: false

        Repeater {
            id: measureRepeater
            model: seg.options
            onItemAdded: Qt.callLater(seg._syncMeasuredWidth)
            onItemRemoved: Qt.callLater(seg._syncMeasuredWidth)

            delegate: Text {
                required property string modelData
                text: modelData
                font.pixelSize: seg.labelPixelSize
                font.weight: seg.labelWeight
                Component.onCompleted: Qt.callLater(seg._syncMeasuredWidth)
                onImplicitWidthChanged: Qt.callLater(seg._syncMeasuredWidth)
            }
        }
    }

    Row {
        anchors.fill: parent
        anchors.margins: 3

        Repeater {
            model: seg.options
            delegate: Item {
                required property int index
                required property string modelData
                width: seg._segW
                height: parent.height

                Text {
                    id: label
                    anchors.fill: parent
                    anchors.leftMargin: seg.labelPadding
                    anchors.rightMargin: seg.labelPadding
                    text: modelData
                    color: index === seg.current ? appTheme.white : appTheme.textMuted
                    font.pixelSize: seg.labelPixelSize
                    font.weight: seg.labelWeight
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    maximumLineCount: 1
                    elide: Text.ElideRight
                    clip: true
                    Behavior on color { ColorAnimation { duration: 140 } }
                }
                TimerPointerArea {
                    id: mouse
                    anchors.fill: parent
                    enabled: seg.enabled
                    onClicked: seg.picked(index)
                }

                ToolTip.visible: label.truncated && mouse.containsMouse
                ToolTip.text: modelData
                ToolTip.delay: 450
            }
        }
    }
}
