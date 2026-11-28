pragma Translator: "SettingsWidget"
pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    color: appTheme.bg
    readonly property bool compact: width < 840
    readonly property bool showingSearch: settingsNavigation.query.length > 0 && !settingsNavigation.detailsOpen
    readonly property bool showingPage: !showingSearch && (!compact || settingsNavigation.detailsOpen)
    readonly property int navigationWidth: 292
    property string loadedSection: ""
    property int direction: 1
    objectName: "settingsView"

    function selectSection(section, group, key) {
        direction = 1
        settingsNavigation.openSection(section, group || "", key || "")
    }

    function activeResultsPane() {
        if (compact)
            return navigationContent.item && navigationContent.item.objectName === "settingsResultsPane"
                ? navigationContent.item : null
        return resultLoader.item
    }

    function goBack() {
        direction = -1
        settingsNavigation.back()
        Qt.callLater(function() {
            if (root.showingSearch) {
                const pane = root.activeResultsPane()
                if (pane && pane.list)
                    pane.list.forceActiveFocus()
            } else if (navigationContent.item) {
                navigationContent.item.forceActiveFocus()
                navigationContent.item.positionViewAtIndex(
                    navigationContent.item.currentIndex, ListView.Contain
                )
            }
        })
    }

    function updatePage() {
        if (!showingPage)
            return
        const next = settingsNavigation.currentSection.id
        if (loadedSection !== next || !pageLoader.item) {
            loadedSection = next
            pageLoader.active = false
            pageLoader.active = true
        } else if (settingsNavigation.targetKey) {
            pageLoader.item.reveal(settingsNavigation.targetKey)
        }
    }

    onShowingPageChanged: updatePage()
    Component.onCompleted: updatePage()

    Connections {
        target: settingsNavigation
        function onDestinationChanged() {
            root.updatePage()
            transition.stop()
            stage.opacity = 0
            movement.x = root.compact ? 20 * root.direction : 0
            transition.start()
        }
    }

    Shortcut {
        sequences: [StandardKey.Find]
        onActivated: {
            search.forceActiveFocus()
            search.selectAll()
        }
    }
    Shortcut {
        sequences: [StandardKey.Back]
        enabled: root.compact && settingsNavigation.detailsOpen
        onActivated: root.goBack()
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: root.compact ? 0 : 16
        spacing: root.compact ? 0 : 12

        Rectangle {
            id: navigationPane
            objectName: "settingsNavigationPane"
            visible: !root.compact || !settingsNavigation.detailsOpen
            Layout.preferredWidth: root.compact ? root.width : root.navigationWidth
            Layout.fillWidth: root.compact
            Layout.fillHeight: true
            radius: root.compact ? 0 : 12
            color: root.compact ? appTheme.bg : appTheme.surfaceChrome
            border.width: root.compact ? 0 : 1
            border.color: appTheme.border_

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: root.compact ? 16 : 20
                anchors.rightMargin: root.compact ? 2 : 6
                anchors.topMargin: root.compact ? 16 : 20
                anchors.bottomMargin: root.compact ? 16 : 20
                spacing: 0

                Text {
                    Layout.fillWidth: true
                    Layout.rightMargin: 14
                    text: qsTr("Settings")
                    color: appTheme.textPrimary
                    font.pixelSize: 24
                    font.weight: Font.DemiBold
                    wrapMode: Text.Wrap
                }

                AppTextField {
                    id: search
                    objectName: "settingsSearch"
                    Layout.fillWidth: true
                    Layout.rightMargin: 14
                    Layout.topMargin: 14
                    searchIcon: true
                    clearActionVisible: settingsNavigation.query.length > 0
                    placeholderText: qsTr("Search settings")
                    text: settingsNavigation.query
                    Accessible.name: placeholderText
                    onTextEdited: settingsNavigation.search(text)
                    Keys.onDownPressed: {
                        const pane = root.activeResultsPane()
                        if (pane && pane.list) {
                            pane.list.forceActiveFocus()
                            pane.list.currentIndex = pane.list.count ? 0 : -1
                        }
                    }
                    Keys.onEscapePressed: {
                        settingsNavigation.search("")
                        root.updatePage()
                    }
                    onClearRequested: {
                        settingsNavigation.search("")
                        search.forceActiveFocus()
                    }
                }

                Loader {
                    id: navigationContent
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.topMargin: 14
                    sourceComponent: root.compact && root.showingSearch
                        ? resultsComponent : sectionsComponent
                }
            }
        }

        Item {
            id: stage
            objectName: "settingsContentPane"
            visible: !root.compact || settingsNavigation.detailsOpen
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            transform: Translate { id: movement }

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                Loader {
                    id: pageLoader
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: root.showingPage
                    active: false
                    sourceComponent: SettingsPage {
                        section: settingsNavigation.currentSection
                        compactNavigation: root.compact && settingsNavigation.detailsOpen
                        onBackRequested: root.goBack()
                        onChoose: (row, owner) => {
                            if (row.kind === "time")
                                timeSelection.present(row, owner)
                            else if (row.kind === "congregation")
                                congregationSelection.present(owner)
                            else
                                selection.present(row, owner)
                        }
                    }
                }

                Loader {
                    id: resultLoader
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    visible: !root.compact && root.showingSearch
                    active: visible
                    sourceComponent: resultsComponent
                }
            }
        }
    }

    Component {
        id: sectionsComponent
        AppSelectionList {
            id: sections
            objectName: "settingsSections"
            clip: true
            topMargin: 2
            bottomMargin: 8
            spacing: 4
            model: settingsNavigation.sections
            currentIndex: {
                const list = settingsNavigation.sections
                for (let i = 0; i < list.length; i++)
                    if (list[i].id === settingsNavigation.currentSection.id)
                        return i
                return 0
            }
            Keys.onReturnPressed: root.selectSection(model[currentIndex].id)
            Keys.onEnterPressed: root.selectSection(model[currentIndex].id)

            delegate: ItemDelegate {
                id: sectionItem
                required property var modelData
                required property int index
                objectName: "settingsSection_" + modelData.id
                width: sections.delegateWidth
                implicitHeight: Math.max(64, sectionContent.implicitHeight + 18)
                highlighted: !root.compact && settingsNavigation.currentSection.id === modelData.id
                Accessible.name: modelData.title
                Accessible.description: modelData.description
                AppPointerCursor {}
                onClicked: root.selectSection(modelData.id)

                contentItem: RowLayout {
                    id: sectionContent
                    spacing: 12
                    SettingsIcon {
                        name: sectionItem.modelData.icon
                        tint: sectionItem.highlighted
                            ? appTheme.accentText : appTheme.textSecondary
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 3
                        Text {
                            Layout.fillWidth: true
                            text: sectionItem.modelData.title
                            color: sectionItem.highlighted
                                ? appTheme.accentText : appTheme.textPrimary
                            font.pixelSize: 14
                            font.weight: Font.Medium
                            wrapMode: Text.Wrap
                        }
                        Text {
                            Layout.fillWidth: true
                            text: sectionItem.modelData.description
                            color: appTheme.textMuted
                            font.pixelSize: 12
                            wrapMode: Text.Wrap
                        }
                    }
                    SettingsIcon {
                        visible: root.compact
                        name: "next"
                        width: 16
                        height: 16
                    }
                }

                background: Rectangle {
                    radius: 9
                    color: sectionItem.highlighted
                        ? appTheme.accentTint
                        : sectionItem.hovered ? appTheme.hover : "transparent"
                    border.color: sectionItem.visualFocus ? appTheme.accent : "transparent"
                    border.width: sectionItem.visualFocus ? 2 : 0
                }
            }
        }
    }

    Component {
        id: resultsComponent
        Item {
            id: resultsPane
            objectName: "settingsResultsPane"
            property alias list: results

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: root.compact ? 0 : 28
                spacing: 14

                Text {
                    Layout.fillWidth: true
                    text: qsTr("Search results")
                    color: appTheme.textPrimary
                    font.pixelSize: root.compact ? 18 : 25
                    font.weight: Font.DemiBold
                    wrapMode: Text.Wrap
                }

                AppSelectionList {
                    id: results
                    objectName: "settingsResults"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 8
                    model: settingsNavigation.results
                    Keys.onReturnPressed: if (currentIndex >= 0 && currentIndex < count) {
                        const item = model[currentIndex]
                        root.selectSection(item.section, item.group, item.key)
                    }
                    Keys.onEnterPressed: if (currentIndex >= 0 && currentIndex < count) {
                        const item = model[currentIndex]
                        root.selectSection(item.section, item.group, item.key)
                    }

                    delegate: ItemDelegate {
                        id: resultItem
                        required property var modelData
                        required property int index
                        objectName: "settingsResult_" + modelData.key
                        width: results.delegateWidth
                        implicitHeight: Math.max(68, resultContent.implicitHeight + 22)
                        Accessible.name: modelData.title + ", " + modelData.description
                        AppPointerCursor {}
                        onClicked: root.selectSection(
                            modelData.section, modelData.group, modelData.key
                        )

                        background: Rectangle {
                            radius: 10
                            color: resultItem.hovered || resultItem.ListView.isCurrentItem
                                ? appTheme.hover : appTheme.surfaceChrome
                            border.color: resultItem.visualFocus
                                ? appTheme.accent : appTheme.border_
                            border.width: resultItem.visualFocus ? 2 : 1
                        }

                        contentItem: RowLayout {
                            id: resultContent
                            spacing: 12
                            SettingsIcon { name: resultItem.modelData.icon }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                Text {
                                    Layout.fillWidth: true
                                    text: resultItem.modelData.title
                                    color: appTheme.textPrimary
                                    font.pixelSize: 15
                                    font.weight: Font.Medium
                                    wrapMode: Text.Wrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: resultItem.modelData.description
                                    color: appTheme.textMuted
                                    font.pixelSize: 13
                                    wrapMode: Text.Wrap
                                }
                            }
                            SettingsIcon { name: "next"; width: 16; height: 16 }
                        }
                    }

                    Text {
                        anchors.centerIn: parent
                        width: parent.width - 32
                        visible: !results.count
                        text: qsTr("No settings found. Try another word.")
                        color: appTheme.textMuted
                        font.pixelSize: 15
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
                    }
                }
            }
        }
    }

    ParallelAnimation {
        id: transition
        NumberAnimation {
            target: stage
            property: "opacity"
            to: 1
            duration: root.compact ? 180 : 160
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: movement
            property: "x"
            to: 0
            duration: root.compact ? 180 : 160
            easing.type: Easing.OutCubic
        }
    }

    SettingsSelectionDialog { id: selection }
    SettingsTimeDialog { id: timeSelection }
    SettingsCongregationDialog { id: congregationSelection }
    SettingsShortcutDialog {}
    SettingsRemoteSetup {}
}
