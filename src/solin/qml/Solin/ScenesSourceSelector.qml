pragma Translator: "ScenesEditorView"
pragma ComponentBehavior: Bound
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ColumnLayout {
    id: root

    property var theme: null
    property var options: []
    property string selectedId: ""
    property string searchPlaceholder: qsTr("Search…")
    signal chosen(var option)

    readonly property var filteredOptions: {
        const query = searchField.text.trim().toLocaleLowerCase()
        return (root.options || []).filter(function(option) {
            return !query || String(option.name).toLocaleLowerCase().includes(query)
                || String(option.reason || "").toLocaleLowerCase().includes(query)
        })
    }

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    spacing: 6
    implicitWidth: 280

    function reset() {
        searchField.clear()
        syncCurrentIndex()
    }

    function canChoose(option) {
        return !!option && (option.available !== false || option.selectable === true)
    }

    function syncCurrentIndex() {
        let first = -1
        for (let index = 0; index < root.filteredOptions.length; ++index) {
            const option = root.filteredOptions[index]
            if (!root.canChoose(option))
                continue
            if (String(option.id) === root.selectedId) {
                choices.currentIndex = index
                choices.positionViewAtIndex(index, ListView.Contain)
                return
            }
            if (first < 0)
                first = index
        }
        choices.currentIndex = first
        if (first >= 0)
            choices.positionViewAtIndex(first, ListView.Contain)
    }

    function moveCurrentIndex(direction) {
        let index = choices.currentIndex
        if (index < 0)
            index = direction > 0 ? -1 : root.filteredOptions.length
        for (index += direction; index >= 0 && index < root.filteredOptions.length;
                index += direction) {
            if (root.canChoose(root.filteredOptions[index])) {
                choices.currentIndex = index
                choices.positionViewAtIndex(index, ListView.Contain)
                return
            }
        }
    }

    function chooseCurrent() {
        const option = root.filteredOptions[choices.currentIndex]
        if (root.enabled && root.canChoose(option))
            root.chosen(option)
    }

    function handleKey(event) {
        if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
            root.moveCurrentIndex(event.key === Qt.Key_Down ? 1 : -1)
            event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.chooseCurrent()
            event.accepted = true
        }
    }

    onFilteredOptionsChanged: Qt.callLater(root.syncCurrentIndex)
    onSelectedIdChanged: Qt.callLater(root.syncCurrentIndex)
    Component.onCompleted: root.syncCurrentIndex()

    ScenesTextField {
        id: searchField
        objectName: root.objectName + "Search"
        Layout.fillWidth: true
        theme: root.theme
        placeholderText: root.searchPlaceholder
        Accessible.name: root.searchPlaceholder
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) { root.handleKey(event) }
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.maximumHeight: 180
        implicitHeight: choices.count ? Math.min(180, choices.contentHeight + 8)
            : emptyLabel.implicitHeight + 24
        radius: 9
        color: root.surface
        border.width: 1
        border.color: choices.activeFocus ? root.accent : root.borderColor

        ListView {
            id: choices
            objectName: root.objectName + "List"
            anchors.fill: parent
            anchors.margins: 4
            clip: true
            spacing: 2
            model: root.filteredOptions
            currentIndex: -1
            activeFocusOnTab: count > 0
            keyNavigationEnabled: false
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            ScrollBar.vertical: ScrollBar { id: scrollBar }
            Keys.onPressed: function(event) { root.handleKey(event) }

            delegate: ItemDelegate {
                id: row
                required property int index
                required property var modelData
                readonly property bool selected: String(modelData.id) === root.selectedId

                width: choices.width - (scrollBar.visible ? scrollBar.width + 4 : 0)
                implicitHeight: Math.max(36, contentItem.implicitHeight + topPadding + bottomPadding)
                leftPadding: 9
                rightPadding: 9
                topPadding: 8
                bottomPadding: 8
                enabled: root.canChoose(modelData)
                focusPolicy: Qt.NoFocus
                highlighted: choices.currentIndex === index
                    && (choices.activeFocus || searchField.activeFocus)
                Accessible.role: Accessible.ListItem
                Accessible.name: String(modelData.name)
                Accessible.description: String(modelData.reason || "")
                Accessible.selected: row.selected
                Accessible.onPressAction: {
                    if (row.enabled)
                        root.chosen(row.modelData)
                }

                onClicked: {
                    choices.forceActiveFocus(Qt.MouseFocusReason)
                    choices.currentIndex = index
                    root.chosen(modelData)
                }

                contentItem: RowLayout {
                    spacing: 8

                    Text {
                        Layout.preferredWidth: 12
                        text: row.selected ? "✓" : ""
                        color: root.accent
                        font.pixelSize: 12
                        Accessible.ignored: true
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3

                        Text {
                            Layout.fillWidth: true
                            text: String(row.modelData.name)
                            textFormat: Text.PlainText
                            color: row.enabled ? root.textPrimary : root.textSecondary
                            font.pixelSize: 11
                            font.weight: row.selected ? Font.DemiBold : Font.Normal
                            elide: Text.ElideRight
                        }

                        Text {
                            Layout.fillWidth: true
                            visible: text.length > 0
                            text: String(row.modelData.reason || "")
                            textFormat: Text.PlainText
                            color: root.textSecondary
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }
                    }
                }

                background: Rectangle {
                    radius: 7
                    color: row.selected || row.highlighted || row.hovered
                        ? root.hover : "transparent"
                    border.width: row.selected || row.highlighted ? 1 : 0
                    border.color: root.accent
                }
            }
        }

        Text {
            id: emptyLabel
            objectName: root.objectName + "Empty"
            anchors.centerIn: parent
            width: Math.max(0, parent.width - 24)
            visible: choices.count === 0
            text: qsTr("No results found.")
            textFormat: Text.PlainText
            color: root.textSecondary
            font.pixelSize: 11
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }
    }
}
