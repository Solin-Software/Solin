// TimerSelect.qml — compact dropdown for timer option sets that may grow.
import QtQuick 2.15
import QtQuick.Controls 2.15

Rectangle {
    id: select

    property var options: []
    property var optionKeys: options
    property int current: 0
    property color accent: appTheme.accent
    property int controlHeight: 34
    property int labelPixelSize: 12
    property string accessibleName: ""
    signal picked(int index)

    readonly property int count: Math.max(0, options.length)
    readonly property int safeCurrent: count > 0 ? Math.min(count - 1, Math.max(0, current)) : 0
    readonly property string currentLabel: count > 0 ? options[safeCurrent] : ""

    function toggleMenu() {
        if (!select.enabled || select.count === 0)
            return
        if (menu.visible)
            menu.close()
        else
            menu.open()
    }

    function invalidateMenu() {
        if (menu && menu.acceptingSelection && (!select.enabled
                || JSON.stringify(select.options) !== menu.openLabels
                || JSON.stringify(select.optionKeys) !== menu.openKeys)) {
            menu.close()
        }
    }

    onOptionsChanged: invalidateMenu()
    onOptionKeysChanged: invalidateMenu()
    onEnabledChanged: invalidateMenu()

    implicitHeight: controlHeight
    implicitWidth: 206
    radius: 9
    activeFocusOnTab: enabled
    Accessible.role: Accessible.ComboBox
    Accessible.name: accessibleName !== "" ? accessibleName : currentLabel
    Accessible.onPressAction: select.toggleMenu()
    color: menu.opened ? appTheme.hover : (mouse.containsMouse ? appTheme.hover : appTheme.surfaceAlt)
    border.color: activeFocus || menu.opened ? select.accent : appTheme.border_
    border.width: activeFocus ? 2 : 1
    opacity: enabled ? 1.0 : 0.72

    Keys.onPressed: function(event) {
        if (!select.enabled || select.count === 0)
            return
        if (menu.visible)
            return
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            select.toggleMenu()
            event.accepted = true
        } else if (event.key === Qt.Key_Down && select.safeCurrent + 1 < select.count) {
            select.picked(select.safeCurrent + 1)
            event.accepted = true
        } else if (event.key === Qt.Key_Up && select.safeCurrent > 0) {
            select.picked(select.safeCurrent - 1)
            event.accepted = true
        }
    }

    Behavior on color { ColorAnimation { duration: 140 } }
    Behavior on border.color { ColorAnimation { duration: 140 } }
    Behavior on opacity { NumberAnimation { duration: 140 } }

    Text {
        id: label
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 34
        text: select.currentLabel
        textFormat: Text.PlainText
        color: select.enabled ? appTheme.textPrimary : appTheme.textMuted
        font.pixelSize: select.labelPixelSize
        font.weight: Font.DemiBold
        horizontalAlignment: Text.AlignLeft
        verticalAlignment: Text.AlignVCenter
        maximumLineCount: 1
        elide: Text.ElideRight
        clip: true
        Behavior on color { ColorAnimation { duration: 140 } }
    }

    TimerIcon {
        anchors.right: parent.right
        anchors.rightMargin: 11
        anchors.verticalCenter: parent.verticalCenter
        width: 14
        height: 14
        rotation: menu.opened ? -90 : 90
        opacity: select.enabled ? 0.95 : 0.55
        rasterSize: 28
        iconName: "chevron_right"
        tint: appTheme.textMuted
        Behavior on rotation { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
        Behavior on opacity { NumberAnimation { duration: 140 } }
    }

    TimerPointerArea {
        id: mouse
        anchors.fill: parent
        enabled: select.enabled && select.count > 0
        onClicked: select.toggleMenu()
    }

    ThemedToolTip {
        visible: label.truncated && mouse.containsMouse
        text: select.currentLabel
    }

    Popup {
        id: menu
        objectName: "timerSelectPopup"
        x: 0
        y: select.height + 6
        width: select.width
        readonly property Item viewport: Overlay.overlay
        readonly property int rowHeight: 34
        implicitHeight: Math.min(select.count * rowHeight + Math.max(0, select.count - 1) * optionsList.spacing + 8,
                                 viewport ? Math.max(0, viewport.height - 2 * margins) : 320)
        margins: 8
        padding: 4
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property int highlightedIndex: -1
        property bool acceptingSelection: false
        property string openLabels: ""
        property string openKeys: ""

        function pick(index) {
            select.invalidateMenu()
            if (!acceptingSelection || index < 0 || index >= select.count)
                return
            close()
            select.picked(index)
            select.forceActiveFocus()
        }

        onAboutToShow: {
            openLabels = JSON.stringify(select.options)
            openKeys = JSON.stringify(select.optionKeys)
            menu.highlightedIndex = select.safeCurrent
            acceptingSelection = true
        }
        onOpened: {
            if (acceptingSelection) {
                optionsList.positionViewAtIndex(highlightedIndex, ListView.Contain)
                optionsList.forceActiveFocus()
            }
        }
        onAboutToHide: {
            acceptingSelection = false
            highlightedIndex = -1
        }

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 120 }
            NumberAnimation { property: "scale"; from: 0.98; to: 1.0; duration: 140; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90 }
        }

        background: Rectangle {
            radius: 9
            color: appTheme.surface
            border.color: appTheme.borderStrong
            border.width: 1
        }

        contentItem: ListView {
            id: optionsList
            spacing: 2
            focus: true
            clip: true
            model: select.options
            currentIndex: menu.highlightedIndex
            keyNavigationEnabled: false
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: AppScrollBar { id: optionScrollBar }

            Keys.onUpPressed: function(event) {
                if (menu.acceptingSelection) {
                    menu.highlightedIndex = Math.max(0, menu.highlightedIndex - 1)
                    positionViewAtIndex(menu.highlightedIndex, ListView.Contain)
                    event.accepted = true
                }
            }
            Keys.onDownPressed: function(event) {
                if (menu.acceptingSelection) {
                    menu.highlightedIndex = Math.min(select.count - 1, menu.highlightedIndex + 1)
                    positionViewAtIndex(menu.highlightedIndex, ListView.Contain)
                    event.accepted = true
                }
            }
            Keys.onPressed: function(event) {
                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space)
                    && menu.acceptingSelection) {
                    menu.pick(menu.highlightedIndex)
                    event.accepted = true
                }
            }

            delegate: Rectangle {
                required property int index
                required property string modelData

                width: Math.max(0, optionsList.width - (optionsList.contentHeight > optionsList.height ? optionScrollBar.width : 0))
                height: menu.rowHeight
                radius: 7
                Accessible.role: Accessible.ListItem
                Accessible.name: modelData
                Accessible.onPressAction: menu.pick(index)
                color: (optionMouse.containsMouse || index === menu.highlightedIndex)
                       ? Qt.rgba(select.accent.r, select.accent.g, select.accent.b, 0.14)
                       : (index === select.safeCurrent
                          ? Qt.rgba(select.accent.r, select.accent.g, select.accent.b, 0.10)
                          : "transparent")

                Behavior on color { ColorAnimation { duration: 120 } }

                Rectangle {
                    anchors.left: parent.left
                    anchors.leftMargin: 6
                    anchors.verticalCenter: parent.verticalCenter
                    width: 3
                    height: 18
                    radius: 2
                    color: select.accent
                    visible: index === select.safeCurrent
                }

                Text {
                    anchors.fill: parent
                    anchors.leftMargin: 16
                    anchors.rightMargin: 12
                    text: modelData
                    textFormat: Text.PlainText
                    color: index === select.safeCurrent ? appTheme.textPrimary : appTheme.textSecondary
                    font.pixelSize: select.labelPixelSize
                    font.weight: index === select.safeCurrent ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignLeft
                    verticalAlignment: Text.AlignVCenter
                    maximumLineCount: 1
                    elide: Text.ElideRight
                }

                TimerPointerArea {
                    id: optionMouse
                    anchors.fill: parent
                    onClicked: menu.pick(index)
                }
            }
        }
    }

    component ThemedToolTip: ToolTip {
        id: tip
        delay: 450
        padding: 6
        contentItem: Text {
            text: tip.text
            textFormat: Text.PlainText
            color: appTheme.textPrimary
            font.pixelSize: 12
        }
        background: Rectangle {
            color: appTheme.surface
            radius: 6
            border.width: 1
            border.color: appTheme.border_
        }
    }
}
