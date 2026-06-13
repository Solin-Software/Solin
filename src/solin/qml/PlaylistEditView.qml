// PlaylistEditView.qml - Solin playlist editor screen chrome.
// Hosts the header/toolbar and delegates the reusable tree surface to PlaylistTreeView.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Rectangle {
    id: root
    color: "#0d1117"

    readonly property int contentLeftInset: 16
    readonly property int contentRightInset: 12
    readonly property int scrollbarGutter: 12
    readonly property int scrollbarContentGap: 8
    readonly property color bg: "#0d1117"
    readonly property color surface: "#13161c"
    readonly property color hover: "#1a1f2a"
    readonly property color border_: "#1e2430"
    readonly property color borderStrong: "#2a3040"
    readonly property color textPrimary: "#e6edf3"
    readonly property color textSecondary: "#c9d1d9"
    readonly property color textMuted: "#8b949e"
    readonly property color textDim: "#484f58"
    readonly property color accent: "#388bfd"
    readonly property color accentTint: "#132a46"
    readonly property color previewBg: Qt.rgba(0.22, 0.55, 0.99, 0.12)
    readonly property color previewBorder: "#388bfd"
    readonly property color danger: "#f85149"
    readonly property color dangerSubtle: "#3d1214"

    readonly property bool hasController: typeof controller !== "undefined" && controller !== null
    readonly property string playlistName: hasController ? controller.playlistName : ""
    readonly property bool isTemp: hasController && controller.isTemp
    readonly property bool isWatched: hasController && controller.isWatched
    readonly property bool hasItems: hasController && controller.hasItems
    readonly property string itemCountText: hasController ? controller.itemCountText : ""
    readonly property var playlistNodes: hasController ? controller.playlistData : []

    property string externalDropListId: "root"
    property int externalDropIndex: -1
    property int dropIndicatorIndex: -1

    function picon(name, size, colorHex) {
        return "image://playlisticons/" + name + "/" + size + "/" + colorHex
    }

    function commonTr(context, source) {
        return qsTranslate(context, source)
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
        return index
    }

    function clearExternalDropPreview() {
        playlistTree.clearExternalDropPreview()
        externalDropListId = "root"
        externalDropIndex = -1
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
                    colorHex: "8b949e"
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
                    visible: !root.isWatched
                    iconName: "export"
                    iconSize: 14
                    colorHex: "8b949e"
                    toolTipText: qsTranslate("_PlaylistEditView", "Export .jwlplaylist")
                    onClicked: if (root.hasController) controller.exportClicked()
                }

                HeaderButton {
                    id: plusBtn
                    iconName: "plus"
                    iconSize: 14
                    colorHex: "79c0ff"
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
                            icon.source: root.picon("plus", 13, "8b949e")
                            onTriggered: if (root.hasController) controller.addClicked()
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Search JW.org...")
                            icon.source: root.picon("media_video", 13, "8b949e")
                            onTriggered: jwCatalogModal.open()
                            contentItem: MenuLabel { label: parent.text; iconSrc: parent.icon.source }
                            background: MenuBg { hovered: parent.hovered }
                        }

                        MenuItem {
                            text: qsTranslate("_PlaylistEditView", "Add Song...")
                            icon.source: root.picon("media_audio", 13, "8b949e")
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
            hasItems: root.hasItems
            playlistNodes: root.playlistNodes
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
        property string colorHex: "8b949e"
        property string toolTipText: ""
        property bool accentButton: false
        signal clicked()

        implicitWidth: 32
        implicitHeight: 32
        radius: 8
        color: enabled && hdrMa.containsMouse
               ? (accentButton ? "#1a3d6d" : root.hover)
               : (accentButton ? root.accentTint : "transparent")
        border.width: 1
        border.color: enabled && hdrMa.containsMouse
                      ? (accentButton ? "#2d5fa6" : root.borderStrong)
                      : (accentButton ? "#1f4470" : root.border_)
        opacity: enabled ? 1.0 : 0.38
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Image {
            anchors.centerIn: parent
            width: hdrBtn.iconSize
            height: hdrBtn.iconSize
            source: root.picon(hdrBtn.iconName, hdrBtn.iconSize,
                               hdrMa.containsMouse ? "c9d1d9" : hdrBtn.colorHex)
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

        ToolTip.visible: hdrBtn.enabled && hdrMa.containsMouse && hdrBtn.toolTipText !== ""
        ToolTip.text: hdrBtn.toolTipText
        ToolTip.delay: 400
    }

    component ActionButton: Rectangle {
        id: actionBtn
        property string label: ""
        property string toolTipText: ""
        signal clicked()
        implicitWidth: actionLabel.implicitWidth + 20
        implicitHeight: 30
        radius: 7
        color: actionMouse.containsMouse ? "#1a3d6d" : root.accentTint
        border.width: 1
        border.color: actionMouse.containsMouse ? "#2d5fa6" : "#1f4470"
        Text {
            id: actionLabel
            anchors.centerIn: parent
            text: actionBtn.label
            color: "#79c0ff"
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

        ToolTip.visible: actionBtn.enabled && actionMouse.containsMouse && actionBtn.toolTipText !== ""
        ToolTip.text: actionBtn.toolTipText
        ToolTip.delay: 400
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
                source: root.picon(stb.iconName, 13, stbMa.containsMouse ? "c9d1d9" : "8b949e")
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

        ToolTip.visible: stb.enabled && stbMa.containsMouse && stb.toolTipText !== ""
        ToolTip.text: stb.toolTipText
        ToolTip.delay: 400
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
        color: "#1c2128"
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
        color: hovered ? (danger ? root.dangerSubtle : "#262c36") : "transparent"
        radius: 6
    }
}
