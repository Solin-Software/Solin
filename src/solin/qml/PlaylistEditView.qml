// PlaylistEditView.qml - Solin playlist editor screen chrome.
// Hosts the header/toolbar and delegates the reusable tree surface to PlaylistTreeView.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: appTheme.bg

    readonly property int contentLeftInset: 16
    readonly property int contentRightInset: 12
    readonly property int scrollbarGutter: 12
    readonly property int scrollbarContentGap: 8
    readonly property color bg: appTheme.bg
    readonly property color surface: appTheme.surface
    readonly property color hover: appTheme.hover
    readonly property color hoverStrong: appTheme.hoverStrong
    readonly property color border_: appTheme.border_
    readonly property color borderStrong: appTheme.borderStrong
    readonly property color textPrimary: appTheme.textPrimary
    readonly property color textSecondary: appTheme.textSecondary
    readonly property color textMuted: appTheme.textMuted
    readonly property color textDim: appTheme.textDim
    readonly property color accent: appTheme.accent
    readonly property color accentTint: appTheme.accentTint
    readonly property color previewBg: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
    readonly property color previewBorder: appTheme.accent
    readonly property color danger: appTheme.danger
    readonly property color dangerSubtle: appTheme.dangerSubtle

    readonly property bool hasController: typeof controller !== "undefined" && controller !== null
    readonly property string playlistName: hasController ? controller.playlistName : ""
    readonly property bool isTemp: hasController && controller.isTemp
    readonly property bool isWatched: hasController && controller.isWatched
    readonly property bool isLoading: hasController && controller.isLoading
    readonly property bool hasItems: hasController && controller.hasItems
    readonly property string itemCountText: hasController ? controller.itemCountText : ""

    property string externalDropListId: "root"
    property int externalDropIndex: -1
    property string externalDropTreeId: ""
    property int externalDropStructureRevision: -1
    property int dropIndicatorIndex: -1

    function picon(name, size, colorHex) {
        return "image://playlisticons/" + name + "/" + size + "/" + colorHex
    }

    function commonTr(context, source) {
        return qsTranslate(context, source)
    }

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function pointerEntered() {
        if (root.hasController)
            controller.pointerEnter()
    }

    function pointerExited() {
        if (root.hasController)
            controller.pointerExit()
    }

    function getIndexAt(rootY) {
        var local = playlistTree.mapFromItem(root, 0, rootY)
        var index = playlistTree.getIndexAt(local.y)
        externalDropListId = playlistTree.externalDropListId
        externalDropIndex = playlistTree.externalDropIndex
        externalDropTreeId = playlistTree.externalDropTreeId
        externalDropStructureRevision = playlistTree.externalDropStructureRevision
        return index
    }

    function clearExternalDropPreview() {
        playlistTree.clearExternalDropPreview()
        externalDropListId = "root"
        externalDropIndex = -1
        externalDropTreeId = ""
        externalDropStructureRevision = -1
        dropIndicatorIndex = -1
    }

    // ── Main layout ───────────────────────────────────────────────────────────
    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header bar ────────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 52

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: root.contentLeftInset
                anchors.rightMargin: root.contentRightInset
                spacing: 8

                HeaderButton {
                    iconName: "back"
                    iconSize: 15
                    colorHex: root.iconHex(root.textMuted)
                    toolTipText: qsTranslate("_PlaylistEditView", "Back")
                    onClicked: if (root.hasController) controller.backClicked()
                }

                Text {
                    Layout.fillWidth: true
                    text: root.playlistName
                    color: root.textPrimary
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                    Layout.leftMargin: 2
                }

                ActionButton {
                    visible: root.isTemp
                    label: qsTranslate("_PlaylistEditView", "Save playlist")
                    toolTipText: qsTranslate("_PlaylistEditView", "Save as permanent playlist")
                    onClicked: if (root.hasController) controller.saveTempClicked()
                }

                HeaderButton {
                    id: exportBtn
                    visible: !root.isWatched
                    iconName: "export"
                    iconSize: 14
                    colorHex: root.iconHex(root.textMuted)
                    toolTipText: qsTranslate("_PlaylistEditView", "Export playlist")
                    onClicked: exportMenu.open()

                    Menu {
                        id: exportMenu
                        y: parent.height + 4
                        width: 210
                        background: MenuPanel {}

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Solin Playlist...")
                            icon.source: root.picon("export", 13, root.iconHex(root.textMuted))
                            onTriggered: if (root.hasController) controller.exportClicked("solin")
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "JW Library Playlist...")
                            icon.source: root.picon("export", 13, root.iconHex(root.textMuted))
                            onTriggered: if (root.hasController) controller.exportClicked("jwl")
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }
                    }
                }

                HeaderButton {
                    id: plusBtn
                    enabled: !root.isLoading
                    iconName: "plus"
                    iconSize: 14
                    colorHex: root.iconHex(root.accent)
                    accentButton: true
                    toolTipText: qsTranslate("_PlaylistEditView", "Add Media")
                    onClicked: addMenu.open()

                    Menu {
                        id: addMenu
                        y: parent.height + 4
                        width: 182
                        background: MenuPanel {}

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Local File...")
                            icon.source: root.picon("plus", 13, root.iconHex(root.textMuted))
                            onTriggered: if (root.hasController) controller.addClicked()
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Search JW.org...")
                            icon.source: root.picon("media_video", 13, root.iconHex(root.textMuted))
                            onTriggered: jwCatalogModal.open()
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Add Song...")
                            icon.source: root.picon("media_audio", 13, root.iconHex(root.textMuted))
                            onTriggered: jwSongsModal.open()
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }
                    }
                }
            }
        }

        // ── Toolbar ───────────────────────────────────────────────────────────
        Item {
            enabled: !root.isLoading
            opacity: enabled ? 1.0 : 0.55
            Layout.fillWidth: true
            Layout.preferredHeight: 36
            Layout.leftMargin: root.contentLeftInset
            Layout.rightMargin: root.contentRightInset

            RowLayout {
                anchors.fill: parent
                spacing: 6

                SmallToolButton {
                    iconName: "play_all"
                    toolTipText: qsTranslate("_PlaylistEditView", "Play all (in order)")
                    onClicked: if (root.hasController) controller.playAllClicked()
                }
                SmallToolButton {
                    iconName: "shuffle"
                    toolTipText: qsTranslate("_PlaylistEditView", "Play in random order")
                    onClicked: if (root.hasController) controller.shuffleClicked()
                }
                SmallToolButton {
                    iconName: "section"
                    label: qsTranslate("_PlaylistEditView", "Section")
                    toolTipText: qsTranslate("_PlaylistEditView", "Add a new section")
                    onClicked: if (root.hasController) controller.newSectionClicked()
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: root.itemCountText
                    color: root.textDim
                    font.pixelSize: 11
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        // ── Divider ───────────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: root.contentLeftInset
            Layout.rightMargin: root.contentRightInset
            Layout.preferredHeight: 1
            color: root.border_
            opacity: 0.6
        }

        // ── Playlist tree ──────────────────────────────────────────────────────
        PlaylistTreeView {
            id: playlistTree
            Layout.fillWidth: true
            Layout.fillHeight: true
            playlistController: root.hasController ? controller : null
            treeModel: typeof playlistModel !== "undefined" ? playlistModel : null
            hasItems: root.hasItems
            loading: root.isLoading
            contentLeftInset: root.contentLeftInset
            contentRightInset: root.contentRightInset
            scrollbarGutter: root.scrollbarGutter
            scrollbarContentGap: root.scrollbarContentGap
            bg: root.bg
            surface: root.surface
            hover: root.hover
            border_: root.border_
            borderStrong: root.borderStrong
            textPrimary: root.textPrimary
            textSecondary: root.textSecondary
            textMuted: root.textMuted
            textDim: root.textDim
            accent: root.accent
            accentTint: root.accentTint
            previewBg: root.previewBg
            previewBorder: root.previewBorder
            danger: root.danger
            dangerSubtle: root.dangerSubtle
        }
    }

    // SHARED SUB-COMPONENTS
    // ═══════════════════════════════════════════════════════════════════════════

    component HeaderButton: Rectangle {
        id: hdrBtn
        property string iconName: ""
        property int iconSize: 14
        property string colorHex: root.iconHex(root.textMuted)
        property string toolTipText: ""
        property bool accentButton: false
        signal clicked()

        implicitWidth: 32
        implicitHeight: 32
        radius: 8
        color: enabled && hdrMa.containsMouse
               ? (accentButton ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.26) : root.hover)
               : (accentButton ? root.accentTint : "transparent")
        border.width: 1
        border.color: enabled && hdrMa.containsMouse
                      ? (accentButton ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.55) : root.borderStrong)
                      : (accentButton ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.35) : root.border_)
        opacity: enabled ? 1.0 : 0.38
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Image {
            anchors.centerIn: parent
            width: hdrBtn.iconSize
            height: hdrBtn.iconSize
            source: root.picon(hdrBtn.iconName, hdrBtn.iconSize,
                               hdrMa.containsMouse ? root.iconHex(root.textSecondary) : hdrBtn.colorHex)
        }

        MouseArea {
            id: hdrMa
            anchors.fill: parent
            enabled: hdrBtn.enabled
            hoverEnabled: true
            onEntered: root.pointerEntered()
            onExited: root.pointerExited()
            onClicked: hdrBtn.clicked()
        }

        ThemedToolTip {
            visible: hdrBtn.enabled && hdrMa.containsMouse && hdrBtn.toolTipText !== ""
            text: hdrBtn.toolTipText
        }
    }

    component ActionButton: Rectangle {
        id: actionBtn
        property string label: ""
        property string toolTipText: ""
        signal clicked()
        implicitWidth: actionLabel.implicitWidth + 20
        implicitHeight: 30
        radius: 7
        color: actionMouse.containsMouse
               ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.26)
               : root.accentTint
        border.width: 1
        border.color: actionMouse.containsMouse
                      ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.55)
                      : Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.35)
        Text {
            id: actionLabel
            anchors.centerIn: parent
            text: actionBtn.label
            color: appTheme.accentText
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: actionMouse
            anchors.fill: parent
            hoverEnabled: true
            onEntered: root.pointerEntered()
            onExited: root.pointerExited()
            onClicked: actionBtn.clicked()
        }

        ThemedToolTip {
            visible: actionBtn.enabled && actionMouse.containsMouse && actionBtn.toolTipText !== ""
            text: actionBtn.toolTipText
        }
    }

    component SmallToolButton: Rectangle {
        id: stb
        property string iconName: ""
        property string label: ""
        property string toolTipText: ""
        signal clicked()
        implicitWidth: stbRow.implicitWidth + 14
        implicitHeight: 28
        radius: 7
        color: stbMa.containsMouse ? root.hover : "transparent"
        border.width: 1
        border.color: stbMa.containsMouse ? root.borderStrong : root.border_
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Row {
            id: stbRow
            anchors.centerIn: parent
            spacing: 5
            Image {
                anchors.verticalCenter: parent.verticalCenter
                width: 13
                height: 13
                source: root.picon(
                    stb.iconName,
                    13,
                    root.iconHex(stbMa.containsMouse ? root.textSecondary : root.textMuted)
                )
            }
            Text {
                visible: stb.label !== ""
                anchors.verticalCenter: parent.verticalCenter
                text: stb.label
                color: stbMa.containsMouse ? root.textSecondary : root.textMuted
                font.pixelSize: 11
                font.weight: Font.Medium
            }
        }
        MouseArea {
            id: stbMa
            anchors.fill: parent
            hoverEnabled: true
            onEntered: root.pointerEntered()
            onExited: root.pointerExited()
            onClicked: stb.clicked()
        }

        ThemedToolTip {
            visible: stb.enabled && stbMa.containsMouse && stb.toolTipText !== ""
            text: stb.toolTipText
        }
    }

    component ThemedToolTip: ToolTip {
        id: tip
        delay: 400
        padding: 6
        contentItem: Text {
            text: tip.text
            color: root.textPrimary
            font.pixelSize: 12
        }
        background: Rectangle {
            color: root.surface
            radius: 6
            border.width: 1
            border.color: root.border_
        }
    }

    JWMediaCatalogModal {
        id: jwCatalogModal
        anchors.fill: parent
        z: 1000
    }

    JWSongsModal {
        id: jwSongsModal
        anchors.fill: parent
        z: 1001
    }

    component MenuPanel: Rectangle {
        color: root.hoverStrong
        radius: 8
        border.width: 1
        border.color: root.borderStrong
    }

    component MenuLabel: RowLayout {
        property string label: ""
        property string iconSrc: ""
        property bool danger: false
        spacing: 8
        anchors.leftMargin: 8
        Image {
            Layout.preferredWidth: 13
            Layout.preferredHeight: 13
            source: iconSrc
        }
        Text {
            Layout.fillWidth: true
            text: label
            color: danger ? root.danger : root.textSecondary
            font.pixelSize: 12
        }
    }

    component MenuBg: Rectangle {
        property bool hovered: false
        property bool danger: false
        color: hovered ? (danger ? root.dangerSubtle : root.hoverStrong) : "transparent"
        radius: 6
    }
}
