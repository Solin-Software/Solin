pragma Translator: "SettingsWidget"
pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var domain
    readonly property var state: domain.state
    spacing: 12
    objectName: "settingsReleaseHistory"

    // Append to the model instead of replacing it: expanded delegates, text
    // selection, keyboard focus and the surrounding viewport remain intact.
    function synchronize() {
        const entries = state.historyEntries
        let reset = releases.count > entries.length
        for (let i = 0; i < Math.min(releases.count, entries.length); i++) {
            if (releases.get(i).version !== entries[i].version)
                reset = true
        }
        if (reset)
            releases.clear()
        for (let i = 0; i < entries.length; i++) {
            if (i >= releases.count) {
                releases.append(entries[i])
            } else {
                for (const key in entries[i]) {
                    if (releases.get(i)[key] !== entries[i][key])
                        releases.setProperty(i, key, entries[i][key])
                }
            }
        }
    }

    Component.onCompleted: {
        synchronize()
        domain.activate()
    }
    Connections {
        target: root.domain
        function onStateChanged() { root.synchronize() }
    }
    ListModel { id: releases }

    Repeater {
        model: releases
        delegate: Item {
            id: release
            required property string version
            required property bool isBeta
            required property bool isInstalled
            required property string html
            required property string language
            required property bool expanded
            Layout.fillWidth: true
            implicitHeight: contentColumn.implicitHeight
            objectName: "release_" + version

            Rectangle {
                objectName: "releaseSurface_" + release.version
                anchors.fill: parent
                radius: 11
                color: appTheme.surfaceChrome
                border.width: 1
                border.color: release.expanded ? appTheme.borderStrong : appTheme.border_
            }

            ColumnLayout {
                id: contentColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                spacing: 0

                AbstractButton {
                    id: heading
                    Layout.fillWidth: true
                    implicitHeight: Math.max(58, headingContent.implicitHeight + 20)
                    leftPadding: 12
                    rightPadding: 12
                    topPadding: 10
                    bottomPadding: 10
                    hoverEnabled: true
                    focusPolicy: Qt.StrongFocus
                    objectName: "releaseHeading_" + release.version
                    Accessible.name: release.version
                        + (release.isBeta ? " · " + qsTr("Beta") : "")
                        + (release.isInstalled ? " · " + qsTr("Installed") : "")
                    Accessible.description: release.expanded ? qsTr("Collapse") : qsTr("Expand")
                    onClicked: root.domain.setReleaseExpanded(release.version, !release.expanded)
                    Keys.onLeftPressed: root.domain.setReleaseExpanded(release.version, false)
                    Keys.onRightPressed: root.domain.setReleaseExpanded(release.version, true)
                    AppPointerCursor {}
                    contentItem: ColumnLayout {
                        id: headingContent
                        spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10
                            Text {
                                Layout.fillWidth: true
                                text: release.version
                                wrapMode: Text.Wrap
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                color: appTheme.textPrimary
                            }
                            SettingsIcon {
                                name: release.expanded ? "up" : "down"
                                tint: appTheme.textSecondary
                                width: 16
                                height: 16
                            }
                        }
                        Flow {
                            Layout.fillWidth: true
                            visible: release.isBeta || release.isInstalled
                            spacing: 6
                            Repeater {
                                model: [
                                    { label: qsTr("Beta"), shown: release.isBeta, kind: "beta" },
                                    { label: qsTr("Installed"), shown: release.isInstalled, kind: "installed" }
                                ]
                                delegate: Rectangle {
                                    required property var modelData
                                    visible: modelData.shown
                                    width: badgeLabel.implicitWidth + 14
                                    height: badgeLabel.implicitHeight + 8
                                    radius: 5
                                    color: appTheme.accentTint
                                    objectName: "releaseBadge_" + modelData.kind + "_" + release.version
                                    Text {
                                        id: badgeLabel
                                        anchors.centerIn: parent
                                        text: parent.modelData.label
                                        color: appTheme.textPrimary
                                        font.pixelSize: 12
                                        font.weight: Font.Medium
                                    }
                                }
                            }
                        }
                    }
                    background: Rectangle {
                        radius: 11
                        color: heading.hovered ? appTheme.hover : "transparent"
                        border.width: heading.visualFocus ? 2 : 0
                        border.color: appTheme.accent
                    }
                }

                SettingsDisclosureBody {
                    id: bodyClip
                    objectName: "releaseBody_" + release.version
                    Layout.fillWidth: true
                    Layout.preferredHeight: animatedHeight
                    expanded: release.expanded
                    contentHeight: notesLoader.status === Loader.Ready
                        && notesLoader.width > 0 && notesLoader.item
                        ? notesLoader.item.implicitHeight + 25 : 0

                    Rectangle {
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        height: 1
                        color: appTheme.border_
                        opacity: 0.75
                    }
                    Loader {
                        id: notesLoader
                        x: 12
                        y: 13
                        width: Math.max(0, release.width - 24)
                        // Keep the notes alive until the closing animation ends.
                        active: release.expanded || bodyClip.animatedHeight > 0
                        sourceComponent: TextEdit {
                            id: notes
                            objectName: "releaseNotes_" + release.version
                            readOnly: true
                            selectByMouse: true
                            activeFocusOnTab: release.expanded && visible
                            textFormat: TextEdit.RichText
                            text: "<style>a { color: " + appTheme.accent + "; }</style>" + release.html
                            wrapMode: TextEdit.Wrap
                            color: appTheme.textPrimary
                            selectionColor: appTheme.accent
                            selectedTextColor: appTheme.textOnAccent
                            font.pixelSize: 14
                            Accessible.name: qsTr("Release notes") + " · " + release.version
                            onLinkActivated: link => root.domain.openLink(link)
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: -4
                                z: -1
                                color: "transparent"
                                radius: 4
                                border.width: notes.activeFocus ? 2 : 0
                                border.color: appTheme.accent
                            }
                        }
                    }
                }
            }
        }
    }

    Text {
        objectName: "releaseHistoryLoading"
        Layout.fillWidth: true
        visible: root.state.historyBusy
        text: qsTr("Loading release notes…")
        color: appTheme.textMuted
        font.pixelSize: 13
        wrapMode: Text.Wrap
        Accessible.role: Accessible.AlertMessage
    }
    Text {
        objectName: "releaseHistoryEmpty"
        Layout.fillWidth: true
        visible: root.state.historyLoaded && !root.state.historyBusy
            && !root.state.historyError && releases.count === 0
        text: qsTr("No release notes available.")
        color: appTheme.textMuted
        font.pixelSize: 13
        wrapMode: Text.Wrap
    }
    Text {
        objectName: "releaseHistoryError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: root.state.historyError
        color: appTheme.dangerText
        font.pixelSize: 13
        wrapMode: Text.Wrap
        Accessible.role: Accessible.AlertMessage
    }
    SettingsButton {
        objectName: "releaseHistoryRetry"
        visible: !!root.state.historyError
        enabled: !root.state.historyBusy
        text: qsTr("Try again")
        onClicked: root.domain.retry()
    }
    SettingsButton {
        objectName: "releaseHistoryLoadMore"
        visible: root.state.historyHasMore && !root.state.historyError
        enabled: !root.state.historyBusy
        text: qsTr("Load 5 more")
        onClicked: root.domain.loadMore()
    }
}
