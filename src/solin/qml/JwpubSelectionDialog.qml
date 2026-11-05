import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    color: appTheme.bg
    focus: true

    Keys.onEscapePressed: function(event) {
        if (selectionBridge.showPlacement)
            selectionBridge.cancelSelection()
        else
            selectionBridge.cancel()
        event.accepted = true
    }

    function icon(name, size) {
        return "image://publicationicons/" + name + "/" + size + "/"
                + String(appTheme.textSecondary).replace("#", "")
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            spacing: 14

            Rectangle {
                width: 44; height: 44; radius: 13
                color: appTheme.accentTint
                Image {
                    anchors.centerIn: parent
                    width: 23; height: 23
                    source: root.icon("publication", 23)
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Text {
                    Layout.fillWidth: true
                    text: selectionBridge.publicTalk
                          ? qsTranslate("JwpubSelectionBridge", "Public talk media…")
                          : qsTranslate("JwpubSelectionDialog", "Import publication media")
                    color: appTheme.textPrimary
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Text {
                    Layout.fillWidth: true
                    text: selectionBridge.hasPublication
                          ? selectionBridge.publicationTitle
                          : qsTranslate("JwpubSelectionDialog", "Choose a publication to get started.")
                    color: appTheme.textSecondary
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: sourceRow.implicitHeight + 24
            radius: 10
            color: appTheme.surface
            border.color: appTheme.border_

            RowLayout {
                id: sourceRow
                anchors.fill: parent
                anchors.margins: 12
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text {
                        Layout.fillWidth: true
                        text: selectionBridge.publicTalk ? "S-34mp.jwpub" : "JWPUB"
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        color: appTheme.textPrimary
                    }
                    Text {
                        Layout.fillWidth: true
                        text: selectionBridge.sourcePath ||
                              qsTranslate("JwpubSelectionDialog", "Select the file on this computer.")
                        color: appTheme.textSecondary
                        font.pixelSize: 11
                        elide: Text.ElideMiddle
                    }
                }
                ActionButton {
                    objectName: "jwpubBrowse"
                    text: selectionBridge.hasPublication
                          ? qsTranslate("JwpubSelectionDialog", "Change file")
                          : qsTranslate("JwpubSelectionDialog", "Browse…")
                    enabled: !selectionBridge.busy
                    onClicked: selectionBridge.browse()
                }
            }
        }

        Text {
            Layout.fillWidth: true
            visible: !selectionBridge.hasPublication && selectionBridge.publicTalk
            text: qsTranslate("JwpubSelectionDialog",
                              "Choose S-34mp once for this profile. Keep the original file in its location; you can change it here at any time.")
            color: appTheme.textSecondary
            font.pixelSize: 12
            wrapMode: Text.Wrap
            lineHeight: 1.2
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 10
            visible: selectionBridge.hasPublication

            TextField {
                id: search
                objectName: "jwpubSearch"
                Layout.fillWidth: true
                implicitHeight: 40
                placeholderText: qsTranslate("JwpubSelectionDialog", "Search contents…")
                placeholderTextColor: appTheme.textMuted
                color: appTheme.textPrimary
                font.pixelSize: 13
                leftPadding: 12
                selectByMouse: true
                enabled: !selectionBridge.busy
                onTextChanged: selectionBridge.setSearchQuery(text)
                background: Rectangle {
                    radius: 8
                    color: appTheme.surface
                    border.color: search.activeFocus ? appTheme.accent : appTheme.borderStrong
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                ActionButton {
                    text: qsTranslate("JwpubSelectionDialog", "Select all")
                    enabled: !selectionBridge.busy
                    onClicked: selectionBridge.selectAll()
                }
                ActionButton {
                    text: qsTranslate("JwpubSelectionDialog", "Clear selection")
                    enabled: !selectionBridge.busy
                    onClicked: selectionBridge.clearSelection()
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: qsTranslate("JwpubSelectionDialog", "Expand selected contents to refine media.")
                    color: appTheme.textMuted
                    font.pixelSize: 11
                    Layout.maximumWidth: 245
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignRight
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: 10
            color: appTheme.surface
            border.color: appTheme.border_
            clip: true

            ListView {
                id: mediaList
                objectName: "jwpubContents"
                anchors.fill: parent
                anchors.margins: 5
                model: selectionBridge.rows
                clip: true
                reuseItems: true
                cacheBuffer: 0
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                enabled: !selectionBridge.busy

                delegate: ItemDelegate {
                    id: row
                    required property var entry
                    required property int index
                    property string requestedPreview: ""
                    readonly property bool isDocument: entry.kind === "document"
                    readonly property bool inViewport: visible && y + height > mediaList.contentY
                                                        && y < mediaList.contentY + mediaList.height
                    width: mediaList.width
                    height: isDocument ? 68 : 78
                    leftPadding: isDocument ? 8 : 34
                    rightPadding: 8
                    activeFocusOnTab: true
                    Accessible.name: entry.title
                    Accessible.role: Accessible.CheckBox
                    Accessible.checkable: true
                    Accessible.checked: entry.checkState === Qt.Checked
                    onClicked: {
                        mediaList.currentIndex = index
                        if (isDocument)
                            selectionBridge.toggleDocument(entry.documentId)
                        else
                            selectionBridge.toggleMedia(entry.documentId, entry.mediaId)
                    }
                    Keys.onRightPressed: {
                        if (isDocument && entry.selected && !entry.expanded)
                            selectionBridge.toggleExpanded(entry.documentId)
                    }
                    Keys.onLeftPressed: {
                        if (isDocument && entry.expanded)
                            selectionBridge.toggleExpanded(entry.documentId)
                    }

                    function updatePreview() {
                        var key = !isDocument && inViewport && entry.hasPreview ? entry.key : ""
                        if (requestedPreview !== "" && requestedPreview !== key)
                            selectionBridge.releasePreview(requestedPreview)
                        requestedPreview = key
                        if (key !== "")
                            selectionBridge.requestPreview(key)
                    }
                    onInViewportChanged: updatePreview()
                    onEntryChanged: Qt.callLater(updatePreview)
                    Component.onCompleted: Qt.callLater(updatePreview)
                    Component.onDestruction: {
                        if (requestedPreview !== "")
                            selectionBridge.releasePreview(requestedPreview)
                    }
                    ListView.onPooled: {
                        if (requestedPreview !== "")
                            selectionBridge.releasePreview(requestedPreview)
                        requestedPreview = ""
                    }
                    ListView.onReused: Qt.callLater(updatePreview)

                    background: Rectangle {
                        radius: 7
                        color: row.hovered || row.activeFocus ? appTheme.hover
                               : row.isDocument && row.entry.selected
                                 ? appTheme.accentTint : "transparent"
                        border.color: row.activeFocus ? appTheme.accent : "transparent"
                    }
                    contentItem: RowLayout {
                        spacing: 10
                        CheckBox {
                            id: selectionCheck
                            tristate: row.isDocument
                            checkState: row.entry.checkState
                            activeFocusOnTab: false
                            onClicked: {
                                if (row.isDocument)
                                    selectionBridge.toggleDocument(row.entry.documentId)
                                else
                                    selectionBridge.toggleMedia(row.entry.documentId, row.entry.mediaId)
                            }
                            indicator: Rectangle {
                                implicitWidth: 18; implicitHeight: 18
                                x: selectionCheck.leftPadding
                                y: (selectionCheck.height - height) / 2
                                radius: 5
                                color: row.entry.checkState !== Qt.Unchecked
                                       ? appTheme.accent : "transparent"
                                border.color: row.entry.checkState !== Qt.Unchecked
                                              ? appTheme.accent : appTheme.borderStrong
                                Text {
                                    anchors.centerIn: parent
                                    text: row.entry.checkState === Qt.PartiallyChecked ? "−" : "✓"
                                    visible: row.entry.checkState !== Qt.Unchecked
                                    font.pixelSize: 13
                                    font.weight: Font.Bold
                                    color: appTheme.textOnAccent
                                }
                            }
                        }

                        Rectangle {
                            visible: !row.isDocument
                            Layout.preferredWidth: visible ? 92 : 0
                            Layout.preferredHeight: 58
                            color: appTheme.hoverStrong
                            radius: 6
                            Image {
                                anchors.centerIn: parent
                                width: 23; height: 23
                                visible: row.entry.thumbnail === ""
                                source: root.icon(row.entry.mediaType, 23)
                            }
                            Image {
                                anchors.fill: parent
                                anchors.margins: 2
                                source: row.entry.thumbnail
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 5
                            Text {
                                Layout.fillWidth: true
                                text: row.entry.title
                                color: appTheme.textPrimary
                                font.pixelSize: 13
                                font.weight: row.isDocument ? Font.DemiBold : Font.Normal
                                maximumLineCount: 2
                                wrapMode: Text.Wrap
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: row.entry.subtitle !== ""
                                text: row.entry.subtitle
                                color: appTheme.textSecondary
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }
                        ToolButton {
                            visible: row.isDocument
                            enabled: row.entry.selected
                            Accessible.name: qsTranslate("JwpubSelectionDialog", "Refine media")
                            Layout.preferredWidth: 32
                            Layout.preferredHeight: 32
                            onClicked: selectionBridge.toggleExpanded(row.entry.documentId)
                            contentItem: Image {
                                source: root.icon(row.entry.expanded ? "up" : "down", 16)
                                sourceSize: Qt.size(16, 16)
                                fillMode: Image.PreserveAspectFit
                                opacity: parent.enabled ? 1 : 0.35
                            }
                            background: Rectangle {
                                radius: 7
                                color: parent.hovered ? appTheme.hoverStrong : "transparent"
                            }
                        }
                    }
                }
            }

            ColumnLayout {
                anchors.centerIn: parent
                width: parent.width - 60
                spacing: 12
                visible: selectionBridge.busy || mediaList.count === 0
                BusyIndicator {
                    Layout.alignment: Qt.AlignHCenter
                    running: selectionBridge.busy
                    visible: running
                    palette.highlight: appTheme.accent
                }
                Text {
                    Layout.fillWidth: true
                    text: selectionBridge.busy ? selectionBridge.statusText
                          : selectionBridge.hasPublication
                            ? qsTranslate("JwpubSelectionDialog", "No matching contents.")
                            : qsTranslate("JwpubSelectionDialog", "Your publication contents will appear here.")
                    color: appTheme.textSecondary
                    font.pixelSize: 13
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }
        }

        Text {
            Layout.fillWidth: true
            visible: selectionBridge.errorText !== ""
            text: selectionBridge.errorText
            color: appTheme.textPrimary
            font.pixelSize: 12
            wrapMode: Text.Wrap
            maximumLineCount: 3
            elide: Text.ElideRight
            Accessible.role: Accessible.AlertMessage
        }

        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            ActionButton {
                objectName: "jwpubCancel"
                text: qsTranslate("JwpubSelectionDialog", "Cancel")
                enabled: selectionBridge.cancellable
                onClicked: selectionBridge.cancel()
            }
            ActionButton {
                objectName: "jwpubImport"
                primary: true
                text: selectionBridge.importLabel
                enabled: selectionBridge.canImport
                onClicked: selectionBridge.importSelected()
            }
        }
    }

    MediaPlacementOverlay { placementBridge: selectionBridge }

    component ActionButton: Button {
        id: control
        property bool primary: false
        implicitHeight: 36
        leftPadding: 14
        rightPadding: 14
        opacity: enabled ? 1 : 0.45
        Keys.onReturnPressed: clicked()
        Keys.onEnterPressed: clicked()
        contentItem: Text {
            text: control.text
            color: control.primary ? appTheme.textOnAccent : appTheme.textPrimary
            font.pixelSize: 12
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 8
            color: control.primary ? (control.hovered ? appTheme.accentHover : appTheme.accent)
                   : control.hovered ? appTheme.hoverStrong : appTheme.surface
            border.color: control.activeFocus ? appTheme.accent
                          : control.primary ? "transparent" : appTheme.borderStrong
        }
    }
}
