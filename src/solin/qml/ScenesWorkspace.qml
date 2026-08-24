pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    objectName: "scenesWorkspace"
    focus: true

    property var bridge: null
    property var theme: null
    property bool drawerOpen: false
    property bool ptzInspectorOpen: false
    property string mobilePage: "canvas"
    property string contextSceneId: ""
    property string contextSceneName: ""
    property bool contextSceneDefault: false
    property bool contextSceneMedia: false
    property bool contextSceneLive: false
    property bool contextSceneTransitionOverride: false
    property string contextSceneTransitionKind: ""
    property int contextSceneTransitionDurationMs: 0
    property int contextSceneCount: 0
    readonly property bool fullWorkspace: width >= 1180
    readonly property bool sceneRailVisible: width >= 1180
    readonly property bool sourceRailVisible: width >= 760
    readonly property bool narrow: width < 760
    readonly property bool compactHeader: width < 860
    readonly property color bgColor: theme ? theme.bg : "#0e1622"
    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color borderStrong: theme ? theme.borderStrong : "#46566c"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color accentTint: theme ? theme.accentTint : "#17335b"
    readonly property color success: theme ? theme.success : "#36c98f"
    readonly property color warning: theme ? theme.warning : "#e0ad55"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"

    onFullWorkspaceChanged: if (fullWorkspace) drawerOpen = false

    function toggleWorkspaceDrawer() {
        if (!fullWorkspace && sourceRailVisible)
            drawerTabs.currentIndex = 0
        drawerOpen = !drawerOpen
    }

    function openPtzInspector() {
        if (!root.bridge || root.bridge.selectedLayerPtzCameras.length === 0)
            return
        ptzInspectorOpen = true
        if (!sourceRailVisible) {
            drawerTabs.currentIndex = 1
            drawerOpen = true
        }
    }

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function popupBelow(menu, anchor) {
        if (menu && anchor)
            popupAt(menu, anchor, 0, anchor.height + 4)
    }

    function popupAt(menu, anchor, x, y) {
        if (menu && anchor)
            menu.popup(anchor, x, y)
    }

    function popupSceneMenu(
            sceneId, sceneName, isDefaultScene, isMediaScene,
            isLiveScene, hasTransitionOverride, transitionKind,
            transitionDurationMs, sceneCount, anchor, x, y) {
        contextSceneId = sceneId
        contextSceneName = sceneName
        contextSceneDefault = isDefaultScene
        contextSceneMedia = isMediaScene
        contextSceneLive = isLiveScene
        contextSceneTransitionOverride = hasTransitionOverride
        contextSceneTransitionKind = transitionKind
        contextSceneTransitionDurationMs = transitionDurationMs
        contextSceneCount = sceneCount
        if (bridge)
            bridge.selectScene(sceneId)
        popupAt(sceneMenu, anchor, x, y)
    }

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onDocumentGenerationChanged() {
            scenePanel.cancelDrag()
            sourcePanel.cancelDrag()
            root.drawerOpen = false
            root.ptzInspectorOpen = false
        }
        function onChanged() {
            if (root.ptzInspectorOpen && root.bridge.selectedLayerPtzCameras.length === 0)
                root.ptzInspectorOpen = false
        }
    }

    Rectangle { anchors.fill: parent; color: root.bgColor }

    Rectangle {
        id: header
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 64
        color: root.surfaceSoft
        border.width: 1
        border.color: root.borderColor
        z: 100

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: root.narrow ? 12 : 18
            anchors.rightMargin: root.narrow ? 10 : 18
            spacing: root.narrow ? 7 : 12

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                RowLayout {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    height: 42
                    spacing: 9

                    DrawerToggle {
                        visible: !root.fullWorkspace
                        open: root.drawerOpen
                        toolTipText: root.sourceRailVisible
                            ? qsTr("Open scenes") : qsTr("Open scenes and sources")
                        onClicked: root.toggleWorkspaceDrawer()
                    }

                    ColumnLayout {
                        spacing: 0
                        RowLayout {
                            spacing: 7
                            Text {
                                text: qsTr("Scenes")
                                color: root.textPrimary
                                font.pixelSize: root.narrow ? 16 : 18
                                font.weight: Font.DemiBold
                            }
                            FeatureStageBadge {
                                objectName: "scenesBetaBadge"
                                Layout.alignment: Qt.AlignVCenter
                                label: "Beta"
                                theme: root.theme
                            }
                        }
                        Text {
                            visible: !root.compactHeader
                            text: qsTr("Compose cameras and projected content")
                            color: root.textMuted
                            font.pixelSize: 9
                        }
                    }

                    ProfileButton {
                        id: profileButton
                        objectName: "scenesHeaderProfileButton"
                        visible: !root.compactHeader
                        text: root.bridge ? root.bridge.activeProfileName : ""
                        enabled: root.bridge !== null
                        onClicked: root.popupBelow(profileMenu, profileButton)
                    }
                }
            }

            HeaderIconButton {
                iconName: "undo"
                enabled: root.bridge && root.bridge.canUndo
                toolTipText: qsTr("Undo")
                onClicked: root.bridge.undo()
            }
            HeaderIconButton {
                iconName: "redo"
                enabled: root.bridge && root.bridge.canRedo
                toolTipText: qsTr("Redo")
                onClicked: root.bridge.redo()
            }

            TransitionButton {
                id: transitionButton
                objectName: "scenesProgramTransitionButton"
                label: root.bridge ? root.bridge.programTransitionLabel : qsTr("Cut")
                onClicked: transitionPopover.openFor(transitionButton)
            }

            OutputButton {
                id: outputButton
                stateLabel: root.bridge ? root.bridge.outputStateLabel : qsTr("Off")
                active: root.bridge && root.bridge.virtualCameraEnabled
                onClicked: root.popupBelow(outputMenu, outputButton)
            }
        }
    }

    Item {
        id: workspace
        clip: true
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: header.bottom
        anchors.bottom: mobileNav.visible ? mobileNav.top : parent.bottom
        anchors.margins: root.narrow ? 8 : 14

        RowLayout {
            anchors.fill: parent
            spacing: 10

            ScenesScenePanel {
                id: scenePanel
                Layout.preferredWidth: root.sceneRailVisible
                    ? Math.max(218, Math.min(270, root.width * 0.21)) : 0
                Layout.fillHeight: true
                visible: root.sceneRailVisible
                bridge: root.bridge
                theme: root.theme
                onRequestProfileMenu: function(anchor) { root.popupBelow(profileMenu, anchor) }
                onRequestCreateScene: createSceneDialog.openForCreate()
                onRequestSceneMenu: function(
                        sceneId, sceneName, isDefaultScene, isMediaScene,
                        isLiveScene, hasTransitionOverride, transitionKind,
                        transitionDurationMs, sceneCount, anchor, x, y) {
                    root.popupSceneMenu(
                        sceneId, sceneName, isDefaultScene, isMediaScene,
                        isLiveScene, hasTransitionOverride, transitionKind,
                        transitionDurationMs, sceneCount, anchor, x, y)
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 14
                color: root.surface
                border.width: 1
                border.color: root.borderColor

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0

                    Item {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 58

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 16
                            anchors.rightMargin: 12
                            spacing: 10
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                Text {
                                    Layout.fillWidth: true
                                    text: root.bridge ? root.bridge.selectedSceneName : ""
                                    color: root.textPrimary
                                    font.pixelSize: 16
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    visible: root.bridge && root.bridge.selectedSceneLive
                                    text: qsTr("Live now")
                                    color: root.success
                                    font.pixelSize: 9
                                }
                            }
                            Rectangle {
                                Layout.preferredWidth: 104
                                Layout.preferredHeight: 36
                                radius: 10
                                color: takeMouse.containsMouse && takeMouse.enabled
                                    ? Qt.lighter(root.accent, 1.08) : root.accent
                                opacity: root.bridge && root.bridge.canTakeLive ? 1 : 0.38
                                Text {
                                    anchors.centerIn: parent
                                    text: root.bridge && root.bridge.selectedSceneLive
                                        ? qsTr("On air") : qsTr("Take live")
                                    color: "white"
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                                MouseArea {
                                    id: takeMouse
                                    anchors.fill: parent
                                    enabled: root.bridge && root.bridge.canTakeLive
                                    hoverEnabled: true
                                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                    onClicked: root.bridge.takeLive()
                                }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }

                    ScenesCanvas {
                        id: canvas
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.margins: root.narrow ? 8 : 14
                        bridge: root.bridge
                        theme: root.theme
                        onRequestSourceMenu: function(anchor, x, y) {
                            root.popupAt(sourceMenu, anchor, x, y)
                        }
                        onRequestLayerMenu: function(layerId, anchor, x, y) {
                            root.bridge.selectLayer(layerId)
                            root.popupAt(transformMenu, anchor, x, y)
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 42
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 14
                            spacing: 8
                            Rectangle {
                                Layout.preferredWidth: formatText.implicitWidth + 18
                                Layout.preferredHeight: 26
                                radius: 8
                                color: root.surfaceSoft
                                border.width: 1
                                border.color: root.borderColor
                                Text {
                                    id: formatText
                                    anchors.centerIn: parent
                                    text: root.bridge ? root.bridge.outputFormatLabel : ""
                                    color: root.textMuted
                                    font.pixelSize: 9
                                }
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                visible: root.bridge && root.bridge.engineStatus.length > 0
                                text: root.bridge ? root.bridge.engineStatus : ""
                                color: root.warning
                                font.pixelSize: 9
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }

            StackLayout {
                readonly property real railWidth: root.sourceRailVisible
                    ? Math.max(245, Math.min(300, root.width * 0.23)) : 0
                Layout.preferredWidth: railWidth
                Layout.minimumWidth: railWidth
                Layout.maximumWidth: railWidth
                Layout.fillWidth: false
                Layout.fillHeight: true
                visible: root.sourceRailVisible
                currentIndex: root.ptzInspectorOpen ? 1 : 0

                ScenesSourcePanel {
                    id: sourcePanel
                    bridge: root.bridge
                    theme: root.theme
                    onRequestAddSource: function(anchor) {
                        root.popupBelow(sourceMenu, anchor)
                    }
                    onRequestLayerMenu: function(layerId, anchor, x, y) {
                        root.bridge.selectLayer(layerId)
                        root.popupAt(transformMenu, anchor, x, y)
                    }
                    onRequestPtz: ptzPopup.open()
                }
                ScenesPtzInspector {
                    bridge: root.bridge
                    theme: root.theme
                    onRequestClose: root.ptzInspectorOpen = false
                }
            }
        }

        Rectangle {
            id: drawerScrim
            visible: !root.fullWorkspace && root.drawerOpen
            anchors.fill: parent
            color: "#88000000"
            z: 190
            opacity: visible ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 160 } }
            MouseArea { anchors.fill: parent; onClicked: root.drawerOpen = false }
        }

        Rectangle {
            id: drawer
            visible: !root.fullWorkspace
                && (root.drawerOpen || x > -width - 11)
            enabled: root.drawerOpen
            width: Math.min(350, workspace.width - 18)
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            x: root.drawerOpen ? 0 : -width - 12
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderStrong
            z: 200
            Behavior on x { NumberAnimation { duration: 190; easing.type: Easing.OutCubic } }

            // Keep unhandled clicks inside the drawer from reaching the workspace
            // behind it. Interactive children remain above this input shield.
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.AllButtons
                onPressed: function(mouse) { mouse.accepted = true }
            }

            ColumnLayout {
                z: 1
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8

                TabBar {
                    id: drawerTabs
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? 40 : 0
                    visible: !root.sourceRailVisible
                    spacing: 4
                    padding: 4
                    background: Rectangle {
                        color: root.surfaceSoft
                        radius: 11
                        border.width: 1
                        border.color: root.borderColor
                    }
                    TabButton {
                        text: qsTr("Scenes")
                        contentItem: Text {
                            text: parent.text
                            color: parent.checked ? root.textPrimary : root.textMuted
                            font.pixelSize: 10
                            font.weight: parent.checked ? Font.DemiBold : Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 8
                            color: parent.checked ? root.accentTint
                                : parent.hovered ? root.hover : "transparent"
                            border.width: parent.checked ? 1 : 0
                            border.color: root.accent
                        }
                    }
                    TabButton {
                        text: qsTr("Sources")
                        contentItem: Text {
                            text: parent.text
                            color: parent.checked ? root.textPrimary : root.textMuted
                            font.pixelSize: 10
                            font.weight: parent.checked ? Font.DemiBold : Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 8
                            color: parent.checked ? root.accentTint
                                : parent.hovered ? root.hover : "transparent"
                            border.width: parent.checked ? 1 : 0
                            border.color: root.accent
                        }
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.sourceRailVisible ? 0 : drawerTabs.currentIndex
                    ScenesScenePanel {
                        bridge: root.bridge
                        theme: root.theme
                        embedded: true
                        showHeader: root.sourceRailVisible
                        onRequestProfileMenu: function(anchor) { root.popupBelow(profileMenu, anchor) }
                        onRequestCreateScene: createSceneDialog.openForCreate()
                        onRequestSceneMenu: function(
                                sceneId, sceneName, isDefaultScene, isMediaScene,
                                isLiveScene, hasTransitionOverride, transitionKind,
                                transitionDurationMs, sceneCount, anchor, x, y) {
                            root.popupSceneMenu(
                                sceneId, sceneName, isDefaultScene, isMediaScene,
                                isLiveScene, hasTransitionOverride, transitionKind,
                                transitionDurationMs, sceneCount, anchor, x, y)
                        }
                    }
                    StackLayout {
                        currentIndex: root.ptzInspectorOpen ? 1 : 0
                        ScenesSourcePanel {
                            bridge: root.bridge
                            theme: root.theme
                            embedded: true
                            showHeader: false
                            onRequestAddSource: function(anchor) {
                                root.popupBelow(sourceMenu, anchor)
                            }
                            onRequestLayerMenu: function(layerId, anchor, x, y) {
                                root.bridge.selectLayer(layerId)
                                root.popupAt(transformMenu, anchor, x, y)
                            }
                            onRequestPtz: ptzPopup.open()
                        }
                        ScenesPtzInspector {
                            bridge: root.bridge
                            theme: root.theme
                            embedded: true
                            onRequestClose: root.ptzInspectorOpen = false
                        }
                    }
                }
            }
        }
    }

    Rectangle {
        id: mobileNav
        visible: root.narrow
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 48
        color: root.surfaceSoft
        border.width: 1
        border.color: root.borderColor
        z: 120

        RowLayout {
            anchors.fill: parent
            anchors.margins: 5
            Repeater {
                model: [
                    { key: "scenes", label: qsTr("Scenes"), icon: "video" },
                    { key: "canvas", label: qsTr("Canvas"), icon: "screen" },
                    { key: "sources", label: qsTr("Sources"), icon: "plus" }
                ]
                Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 9
                    color: mobileMouse.containsMouse ? root.hover : "transparent"
                    opacity: mobileMouse.pressed ? 0.75 : 1
                    Row {
                        anchors.centerIn: parent
                        spacing: 6
                        Image {
                            anchors.verticalCenter: parent.verticalCenter
                            width: 14; height: 14
                            source: "image://sceneicons/" + modelData.icon + "/16/"
                                + root.iconHex(root.textSecondary)
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.label
                            color: root.textSecondary
                            font.pixelSize: 10
                        }
                    }
                    MouseArea {
                        id: mobileMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: {
                            if (modelData.key === "canvas") {
                                root.drawerOpen = false
                            } else {
                                drawerTabs.currentIndex = modelData.key === "scenes" ? 0 : 1
                                root.drawerOpen = true
                            }
                        }
                    }
                }
            }
        }
    }

    Popup {
        id: transitionPopover
        objectName: "scenesProgramTransitionPopover"
        parent: root
        width: 326
        padding: 16
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        z: 500
        property string draftKind: "cut"
        property int draftDurationMs: 350

        function openFor(anchor) {
            if (!root.bridge || !anchor)
                return
            draftKind = root.bridge.programTransitionKind
            draftDurationMs = root.bridge.programTransitionKind === "cut"
                ? 350 : root.bridge.programTransitionDurationMs
            var point = anchor.mapToItem(root, 0, anchor.height + 6)
            x = Math.max(8, Math.min(root.width - width - 8, point.x))
            y = Math.max(header.height + 4,
                Math.min(root.height - implicitHeight - 8, point.y))
            open()
        }

        onOpened: {
            if (draftKind === "cut")
                transitionCutChoice.forceActiveFocus()
            else if (draftKind === "dissolve")
                transitionDissolveChoice.forceActiveFocus()
            else
                transitionFadeChoice.forceActiveFocus()
        }

        enter: Transition {
            ParallelAnimation {
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 130 }
                NumberAnimation { property: "scale"; from: 0.98; to: 1; duration: 150; easing.type: Easing.OutCubic }
            }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 90 }
        }
        background: Rectangle {
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderStrong
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true
                text: qsTr("Program transition")
                color: root.textPrimary
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Used when scenes go live. Editor preview stays instant.")
                color: root.textMuted
                font.pixelSize: 9
                wrapMode: Text.Wrap
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                TransitionChoice {
                    id: transitionCutChoice
                    Layout.fillWidth: true
                    title: qsTr("Cut")
                    detail: qsTr("Switch immediately")
                    selected: transitionPopover.draftKind === "cut"
                    onClicked: transitionPopover.draftKind = "cut"
                }
                TransitionChoice {
                    id: transitionDissolveChoice
                    Layout.fillWidth: true
                    title: qsTr("Dissolve")
                    detail: qsTr("Blend the two scenes")
                    selected: transitionPopover.draftKind === "dissolve"
                    onClicked: transitionPopover.draftKind = "dissolve"
                }
                TransitionChoice {
                    id: transitionFadeChoice
                    Layout.fillWidth: true
                    title: qsTr("Fade through black")
                    detail: qsTr("Fade out, then fade in")
                    selected: transitionPopover.draftKind === "fade_to_black"
                    onClicked: transitionPopover.draftKind = "fade_to_black"
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Text {
                    Layout.fillWidth: true
                    text: qsTr("Duration")
                    color: transitionPopover.draftKind === "cut"
                        ? root.textMuted : root.textSecondary
                    font.pixelSize: 10
                }
                ScenesSpinBox {
                    id: transitionDuration
                    objectName: "scenesProgramTransitionDuration"
                    theme: root.theme
                    Layout.preferredWidth: 116
                    from: 50
                    to: 10000
                    stepSize: 50
                    value: transitionPopover.draftDurationMs
                    enabled: transitionPopover.draftKind !== "cut"
                    unitText: qsTr("ms")
                    onValueModified: transitionPopover.draftDurationMs = value
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true }
                ScenesDialogButton {
                    theme: root.theme
                    text: qsTr("Cancel")
                    onClicked: transitionPopover.close()
                }
                ScenesDialogButton {
                    objectName: "scenesProgramTransitionApply"
                    theme: root.theme
                    text: qsTr("Apply")
                    primary: true
                    onClicked: {
                        var durationMs = transitionDuration.pendingValue()
                        transitionPopover.draftDurationMs = durationMs
                        root.bridge.setProgramTransition(
                            transitionPopover.draftKind,
                            transitionPopover.draftKind === "cut"
                                ? 0 : durationMs)
                        transitionPopover.close()
                    }
                }
            }
        }
    }

    Menu {
        id: sceneMenu
        objectName: "scenesContextMenu"
        width: 218
        padding: 6
        delegate: ScenesMenuItem { theme: root.theme }
        background: Rectangle {
            color: root.surface
            radius: 11
            border.width: 1
            border.color: root.borderColor
        }
        ScenesMenuItem {
            theme: root.theme
            text: root.contextSceneLive ? qsTr("On air") : qsTr("Take live")
            enabled: !root.contextSceneLive
            onTriggered: root.bridge.takeLive()
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Rename…")
            onTriggered: sceneNameDialog.openForRename(
                root.contextSceneId, root.contextSceneName)
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Duplicate")
            onTriggered: root.bridge.duplicateScene(root.contextSceneId)
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem {
            theme: root.theme
            text: root.contextSceneDefault
                ? qsTr("Remove default") : qsTr("Set as default")
            onTriggered: root.bridge.setDefaultScene(
                root.contextSceneId, !root.contextSceneDefault)
        }
        ScenesMenuItem {
            theme: root.theme
            text: root.contextSceneMedia
                ? qsTr("Remove media") : qsTr("Set as media")
            onTriggered: root.bridge.setMediaScene(
                root.contextSceneId, !root.contextSceneMedia)
        }
        Menu {
            title: qsTr("Transition override")
            width: 238
            padding: 6
            delegate: ScenesMenuItem { theme: root.theme }
            background: Rectangle {
                color: root.surface
                radius: 11
                border.width: 1
                border.color: root.borderColor
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Use profile transition")
                checkable: true
                checked: !root.contextSceneTransitionOverride
                onTriggered: root.bridge.clearSceneTransitionOverride(
                    root.contextSceneId)
            }
            ScenesMenuSeparator { theme: root.theme }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Cut")
                checkable: true
                checked: root.contextSceneTransitionOverride
                    && root.contextSceneTransitionKind === "cut"
                onTriggered: root.bridge.setSceneTransitionOverride(
                    root.contextSceneId, "cut", 0)
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Dissolve")
                checkable: true
                checked: root.contextSceneTransitionOverride
                    && root.contextSceneTransitionKind === "dissolve"
                onTriggered: root.bridge.setSceneTransitionOverride(
                    root.contextSceneId, "dissolve", -1)
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Fade through black")
                checkable: true
                checked: root.contextSceneTransitionOverride
                    && root.contextSceneTransitionKind === "fade_to_black"
                onTriggered: root.bridge.setSceneTransitionOverride(
                    root.contextSceneId, "fade_to_black", -1)
            }
            ScenesMenuSeparator { theme: root.theme }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Duration…")
                enabled: root.contextSceneTransitionOverride
                    && root.contextSceneTransitionKind !== "cut"
                onTriggered: transitionDurationDialog.openFor(
                    root.contextSceneId,
                    root.contextSceneTransitionKind,
                    root.contextSceneTransitionDurationMs)
            }
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Delete…")
            danger: true
            enabled: root.contextSceneCount > 1
            onTriggered: deleteSceneDialog.openForScene(
                root.contextSceneId, root.contextSceneName)
        }
    }

    Menu {
        id: profileMenu
        objectName: "scenesProfileMenu"
        width: 260
        padding: 6
        background: Rectangle {
            color: root.surface
            radius: 11
            border.width: 1
            border.color: root.borderColor
        }
        Repeater {
            model: root.bridge ? root.bridge.sceneProfiles : []
            ScenesMenuItem {
                required property var modelData
                theme: root.theme
                text: (modelData.active ? "✓  " : "    ") + modelData.name
                enabled: modelData.active || !(root.bridge && root.bridge.profileChangesBlocked)
                onTriggered: if (!modelData.active) root.bridge.activateSceneProfile(modelData.id)
            }
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("New Scene profile…")
            enabled: root.bridge && !root.bridge.profileChangesBlocked
            onTriggered: createProfileDialog.open()
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Rename profile…")
            onTriggered: renameProfileDialog.open()
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Delete profile…")
            danger: true
            enabled: root.bridge && root.bridge.sceneProfiles.length > 1
                && !root.bridge.profileChangesBlocked
            onTriggered: deleteProfileDialog.open()
        }
    }

    Menu {
        id: outputMenu
        width: 270
        padding: 6
        background: Rectangle {
            color: root.surface
            radius: 11
            border.width: 1
            border.color: root.borderColor
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Solin Virtual Camera")
            checkable: true
            checked: root.bridge && root.bridge.virtualCameraEnabled
            onTriggered: root.bridge.setVirtualCameraEnabled(checked)
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Auto-switch media")
            checkable: true
            checked: root.bridge && root.bridge.automaticEnabled
            enabled: root.bridge && root.bridge.automationConfigured
            onTriggered: root.bridge.setAutomaticEnabled(checked)
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Show in media windows")
            checkable: true
            checked: root.bridge && root.bridge.mediaMirrorEnabled
            onTriggered: root.bridge.setMediaMirrorEnabled(checked)
        }
    }

    Menu {
        id: sourceMenu
        width: 285
        padding: 6
        background: Rectangle {
            color: root.surface
            radius: 11
            border.width: 1
            border.color: root.borderColor
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Projected Solin content")
            onTriggered: root.bridge.addContentSource()
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem { theme: root.theme; text: qsTr("Cameras"); section: true; enabled: false }
        Repeater {
            model: root.bridge ? root.bridge.configuredCameras : []
            ScenesMenuItem {
                required property var modelData
                theme: root.theme
                text: modelData.name
                onTriggered: root.bridge.addConfiguredCamera(modelData.id)
            }
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("New local camera…")
            onTriggered: cameraDialog.openForCamera("local_camera")
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("New network camera…")
            onTriggered: cameraDialog.openForCamera("rtsp_camera")
        }
        ScenesMenuSeparator { theme: root.theme }
        ScenesMenuItem { theme: root.theme; text: qsTr("Existing scenes"); section: true; enabled: false }
        Repeater {
            model: root.bridge ? root.bridge.referencedScenes : []
            ScenesMenuItem {
                required property var modelData
                theme: root.theme
                text: modelData.name
                onTriggered: root.bridge.addSceneReference(modelData.id)
            }
        }
    }

    Menu {
        id: transformMenu
        width: 218
        padding: 6
        delegate: ScenesMenuItem { theme: root.theme }
        background: Rectangle {
            color: root.surface
            radius: 11
            border.width: 1
            border.color: root.borderColor
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Edit camera…")
            visible: root.bridge && (root.bridge.selectedLayer.kind === "local_camera"
                || root.bridge.selectedLayer.kind === "rtsp_camera")
            height: visible ? implicitHeight : 0
            onTriggered: cameraDialog.openForSelectedCamera()
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Keep camera active")
            visible: root.bridge && (root.bridge.selectedLayer.kind === "local_camera"
                || root.bridge.selectedLayer.kind === "rtsp_camera")
            height: visible ? implicitHeight : 0
            checkable: true
            checked: root.bridge && root.bridge.selectedLayer.camera_keep_active === true
            onTriggered: root.bridge.setCameraKeepActive(
                root.bridge.selectedLayer.source_id, checked)
        }
        ScenesMenuItem { theme: root.theme; text: qsTr("Precise transform…"); onTriggered: transformDialog.openForLayer() }
        ScenesMenuItem { theme: root.theme; text: qsTr("Reset transform"); onTriggered: root.bridge.resetLayerTransform(root.bridge.selectedLayerId) }
        ScenesMenuItem { theme: root.theme; text: qsTr("Center"); onTriggered: root.bridge.centerLayer(root.bridge.selectedLayerId) }
        ScenesMenuSeparator { theme: root.theme }
        Menu {
            title: qsTr("Fit")
            width: 190
            padding: 6
            delegate: ScenesMenuItem { theme: root.theme }
            background: Rectangle {
                color: root.surface
                radius: 11
                border.width: 1
                border.color: root.borderColor
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Contain")
                checkable: true
                checked: root.bridge && root.bridge.selectedLayer.fit_mode === "contain"
                onTriggered: root.bridge.setLayerFit(root.bridge.selectedLayerId, "contain")
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Cover")
                checkable: true
                checked: root.bridge && root.bridge.selectedLayer.fit_mode === "cover"
                onTriggered: root.bridge.setLayerFit(root.bridge.selectedLayerId, "cover")
            }
            ScenesMenuItem {
                theme: root.theme
                text: qsTr("Stretch")
                checkable: true
                checked: root.bridge && root.bridge.selectedLayer.fit_mode === "stretch"
                onTriggered: root.bridge.setLayerFit(root.bridge.selectedLayerId, "stretch")
            }
        }
        ScenesMenuSeparator {
            theme: root.theme
            visible: root.bridge && root.bridge.selectedLayer.ptz_available === true
            height: visible ? implicitHeight : 0
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("PTZ controls…")
            visible: root.bridge && root.bridge.selectedLayer.ptz_available === true
            height: visible ? implicitHeight : 0
            onTriggered: ptzPopup.open()
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("PTZ presets and scene actions…")
            visible: root.bridge && root.bridge.selectedLayer.ptz_available === true
            height: visible ? implicitHeight : 0
            onTriggered: root.openPtzInspector()
        }
        ScenesMenuItem {
            theme: root.theme
            text: qsTr("Remove source")
            danger: true
            onTriggered: root.bridge.removeLayer(root.bridge.selectedLayerId)
        }
    }

    ScenesCameraDialog { id: cameraDialog; bridge: root.bridge; theme: root.theme }
    ScenesTransformDialog { id: transformDialog; bridge: root.bridge; theme: root.theme }
    ScenesPtzPopup { id: ptzPopup; bridge: root.bridge; theme: root.theme }

    Dialog {
        id: transitionDurationDialog
        objectName: "scenesTransitionOverrideDurationDialog"
        modal: true
        anchors.centerIn: parent
        width: Math.min(360, root.width - 28)
        padding: 18
        standardButtons: Dialog.NoButton
        property string sceneId: ""
        property string transitionKind: ""
        function openFor(sceneIdValue, transitionKindValue, durationMs) {
            sceneId = sceneIdValue
            transitionKind = transitionKindValue
            overrideDuration.value = durationMs
            open()
        }
        onOpened: overrideDuration.forceActiveFocus()
        background: Rectangle {
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderColor
        }
        contentItem: ColumnLayout {
            spacing: 13
            Text {
                Layout.fillWidth: true
                text: qsTr("Transition duration")
                color: root.textPrimary
                font.pixelSize: 15
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Set how long this scene's transition takes.")
                color: root.textMuted
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            ScenesSpinBox {
                id: overrideDuration
                objectName: "scenesTransitionOverrideDuration"
                theme: root.theme
                Layout.fillWidth: true
                from: 50
                to: 10000
                stepSize: 50
                value: 350
                unitText: qsTr("ms")
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton {
                    theme: root.theme
                    text: qsTr("Cancel")
                    onClicked: transitionDurationDialog.close()
                }
                ScenesDialogButton {
                    objectName: "scenesTransitionOverrideDurationApply"
                    theme: root.theme
                    text: qsTr("Apply")
                    primary: true
                    onClicked: {
                        root.bridge.setSceneTransitionOverride(
                            transitionDurationDialog.sceneId,
                            transitionDurationDialog.transitionKind,
                            overrideDuration.pendingValue())
                        transitionDurationDialog.close()
                    }
                }
            }
        }
    }

    NameDialog {
        id: createSceneDialog
        titleText: qsTr("New scene")
        acceptText: qsTr("Create")
        function openForCreate() { value = ""; open() }
        onAcceptedValue: function(value) { root.bridge.createScene(value) }
    }
    NameDialog {
        id: sceneNameDialog
        property string sceneId: ""
        titleText: qsTr("Rename scene")
        acceptText: qsTr("Save")
        function openForRename(id, name) { sceneId = id; value = name; open() }
        onAcceptedValue: function(value) { root.bridge.renameScene(sceneId, value) }
    }
    NameDialog {
        id: createProfileDialog
        titleText: qsTr("New Scene profile")
        acceptText: qsTr("Create")
        onAcceptedValue: function(value) { root.bridge.createSceneProfile(value) }
    }
    NameDialog {
        id: renameProfileDialog
        titleText: qsTr("Rename Scene profile")
        acceptText: qsTr("Save")
        onAboutToShow: value = root.bridge ? root.bridge.activeProfileName : ""
        onAcceptedValue: function(value) { root.bridge.renameActiveProfile(value) }
    }

    Dialog {
        id: deleteProfileDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(390, root.width - 28)
        padding: 18
        standardButtons: Dialog.NoButton
        property string replacementId: ""
        onAboutToShow: {
            var profiles = root.bridge ? root.bridge.sceneProfiles : []
            for (var index = 0; index < profiles.length; ++index) {
                if (!profiles[index].active) { replacementId = profiles[index].id; break }
            }
        }
        background: Rectangle {
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderColor
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true
                text: qsTr("Delete Scene profile")
                color: root.textPrimary
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            Text { text: qsTr("Choose the Scene profile to show next."); color: root.textSecondary; font.pixelSize: 10; wrapMode: Text.Wrap; Layout.fillWidth: true }
            ScenesComboBox {
                theme: root.theme
                Layout.fillWidth: true
                model: root.bridge ? root.bridge.sceneProfiles.filter(function(item) { return !item.active }) : []
                textRole: "name"
                onActivated: deleteProfileDialog.replacementId = model[currentIndex].id
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: deleteProfileDialog.close() }
                ScenesDialogButton {
                    theme: root.theme
                    text: qsTr("Delete")
                    danger: true
                    enabled: deleteProfileDialog.replacementId.length > 0
                    onClicked: {
                        root.bridge.deleteActiveProfile(deleteProfileDialog.replacementId)
                        deleteProfileDialog.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: deleteSceneDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(410, root.width - 28)
        padding: 18
        standardButtons: Dialog.NoButton
        property string sceneId: ""
        property string sceneName: ""
        property string replacementId: ""
        property bool needsReplacement: false
        function openForScene(id, name) {
            sceneId = id
            sceneName = name
            needsReplacement = root.bridge && root.bridge.selectedSceneRequiresReplacement
                && root.bridge.selectedSceneId === id
            var options = root.bridge ? root.bridge.referencedScenes : []
            replacementId = options.length ? options[0].id : ""
            open()
        }
        background: Rectangle {
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderColor
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true
                text: qsTr("Delete scene")
                color: root.textPrimary
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: qsTr("Delete “%1” and remove every reference to it?").arg(deleteSceneDialog.sceneName)
                color: root.textSecondary
                wrapMode: Text.Wrap
            }
            Text { visible: deleteSceneDialog.needsReplacement; text: qsTr("Show this scene instead"); color: root.textMuted; font.pixelSize: 10 }
            ScenesComboBox {
                theme: root.theme
                visible: deleteSceneDialog.needsReplacement
                Layout.fillWidth: true
                model: root.bridge ? root.bridge.referencedScenes : []
                textRole: "name"
                onActivated: deleteSceneDialog.replacementId = model[currentIndex].id
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: deleteSceneDialog.close() }
                ScenesDialogButton {
                    theme: root.theme
                    text: qsTr("Delete")
                    danger: true
                    enabled: !deleteSceneDialog.needsReplacement
                        || deleteSceneDialog.replacementId.length > 0
                    onClicked: {
                        root.bridge.deleteScene(
                            deleteSceneDialog.sceneId,
                            deleteSceneDialog.needsReplacement
                                ? deleteSceneDialog.replacementId : "")
                        deleteSceneDialog.close()
                    }
                }
            }
        }
    }

    component HeaderIconButton: Rectangle {
        id: button
        property string iconName: ""
        property string toolTipText: ""
        signal clicked()
        Layout.preferredWidth: 36
        Layout.preferredHeight: 36
        radius: 10
        color: iconMouse.containsMouse && button.enabled ? root.hover : "transparent"
        border.width: 1
        border.color: root.borderColor
        opacity: button.enabled ? 1 : 0.35
        Image {
            anchors.centerIn: parent
            width: 15; height: 15
            source: "image://sceneicons/" + button.iconName + "/16/" + root.iconHex(root.textSecondary)
        }
        MouseArea {
            id: iconMouse
            anchors.fill: parent
            enabled: button.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: button.clicked()
        }
        ToolTip.visible: iconMouse.containsMouse && button.toolTipText.length > 0
        ToolTip.text: button.toolTipText
    }

    component DrawerToggle: Rectangle {
        id: toggle
        objectName: "scenesDrawerToggle"
        property bool open: false
        property string toolTipText: ""
        signal clicked()

        Layout.preferredWidth: 34
        Layout.preferredHeight: 34
        radius: 9
        color: toggle.open ? root.accentTint
                           : toggleMouse.containsMouse ? root.hover : "transparent"
        border.width: toggle.open || toggleMouse.containsMouse || activeFocus ? 1 : 0
        border.color: toggle.open || activeFocus ? root.accent : root.borderStrong
        activeFocusOnTab: visible && enabled
        Accessible.role: Accessible.CheckBox
        Accessible.name: toggle.toolTipText
        Accessible.checked: toggle.open
        Accessible.onPressAction: toggle.clicked()

        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                    || event.key === Qt.Key_Space) {
                toggle.clicked()
                event.accepted = true
            }
        }

        Image {
            id: toggleIcon
            objectName: "scenesDrawerToggleIcon"
            anchors.centerIn: parent
            width: 16
            height: 16
            source: "image://sceneicons/panel-left/16/"
                + root.iconHex(toggle.open ? root.accent : root.textSecondary)
            sourceSize.width: 16
            sourceSize.height: 16
            smooth: true
        }

        MouseArea {
            id: toggleMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: toggle.clicked()
        }
        ToolTip.visible: toggleMouse.containsMouse && toggle.toolTipText.length > 0
        ToolTip.text: toggle.toolTipText
    }

    component TransitionButton: Rectangle {
        id: button
        property string label: ""
        signal clicked()
        readonly property bool expanded: root.width >= 980
        Layout.preferredWidth: expanded ? Math.max(150, labelText.implicitWidth + 54) : 38
        Layout.preferredHeight: 38
        radius: 11
        color: transitionMouse.containsMouse ? root.hover : root.surface
        border.width: 1
        border.color: root.borderStrong
        Row {
            anchors.centerIn: parent
            spacing: 7
            Image {
                anchors.verticalCenter: parent.verticalCenter
                width: 14
                height: 14
                source: "image://sceneicons/transition/16/" + root.iconHex(root.textSecondary)
            }
            Text {
                id: labelText
                visible: button.expanded
                anchors.verticalCenter: parent.verticalCenter
                text: button.label
                color: root.textPrimary
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
            Image {
                visible: button.expanded
                anchors.verticalCenter: parent.verticalCenter
                width: 11
                height: 11
                source: "image://sceneicons/chevron-down/16/" + root.iconHex(root.textMuted)
            }
        }
        MouseArea {
            id: transitionMouse
            anchors.fill: parent
            hoverEnabled: true
            activeFocusOnTab: true
            cursorShape: Qt.PointingHandCursor
            Accessible.role: Accessible.Button
            Accessible.name: qsTr("Program transition: %1").arg(button.label)
            onClicked: button.clicked()
            Keys.onSpacePressed: button.clicked()
            Keys.onReturnPressed: button.clicked()
        }
        ToolTip.visible: transitionMouse.containsMouse && !button.expanded
        ToolTip.text: qsTr("Program transition: %1").arg(button.label)
    }

    component TransitionChoice: Rectangle {
        id: choice
        property string title: ""
        property string detail: ""
        property bool selected: false
        signal clicked()
        implicitHeight: 48
        radius: 10
        color: selected ? root.accentTint
            : choiceMouse.containsMouse ? root.hover : root.surfaceSoft
        border.width: selected ? 1 : 0
        border.color: root.accent
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 11
            anchors.rightMargin: 11
            spacing: 10
            Rectangle {
                Layout.preferredWidth: 14
                Layout.preferredHeight: 14
                radius: 7
                color: "transparent"
                border.width: 1
                border.color: choice.selected ? root.accent : root.borderStrong
                Rectangle {
                    visible: choice.selected
                    anchors.centerIn: parent
                    width: 6
                    height: 6
                    radius: 3
                    color: root.accent
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                    Layout.fillWidth: true
                    text: choice.title
                    color: root.textPrimary
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: choice.detail
                    color: root.textMuted
                    font.pixelSize: 8
                    elide: Text.ElideRight
                }
            }
        }
        MouseArea {
            id: choiceMouse
            anchors.fill: parent
            hoverEnabled: true
            activeFocusOnTab: true
            cursorShape: Qt.PointingHandCursor
            Accessible.role: Accessible.RadioButton
            Accessible.name: choice.title + ". " + choice.detail
            Accessible.checked: choice.selected
            onClicked: choice.clicked()
            Keys.onSpacePressed: choice.clicked()
            Keys.onReturnPressed: choice.clicked()
        }
    }

    component ProfileButton: Rectangle {
        id: profileButton
        property string text: ""
        signal clicked()
        Layout.preferredWidth: 160
        Layout.preferredHeight: 36
        radius: 10
        color: profileMouse.containsMouse ? root.hover : root.surface
        border.width: 1
        border.color: root.borderColor
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 9
            Text { Layout.fillWidth: true; text: profileButton.text; color: root.textSecondary; font.pixelSize: 11; elide: Text.ElideRight }
            Image { width: 12; height: 12; source: "image://sceneicons/chevron-down/16/" + root.iconHex(root.textMuted) }
        }
        MouseArea { id: profileMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: profileButton.clicked() }
    }

    component OutputButton: Rectangle {
        id: outputButton
        property string stateLabel: ""
        property bool active: false
        signal clicked()
        Layout.preferredWidth: root.compactHeader ? 48 : 130
        Layout.preferredHeight: 38
        radius: 11
        color: outputMouse.containsMouse ? root.hover : root.surface
        border.width: 1
        border.color: root.borderStrong
        Row {
            anchors.centerIn: parent
            spacing: 7
            Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 6; height: 6; radius: 3; color: outputButton.active ? root.success : root.textMuted }
            Text { visible: !root.compactHeader; anchors.verticalCenter: parent.verticalCenter; text: qsTr("Output · %1").arg(outputButton.stateLabel); color: root.textPrimary; font.pixelSize: 10; font.weight: Font.DemiBold }
            Image { visible: !root.compactHeader; anchors.verticalCenter: parent.verticalCenter; width: 11; height: 11; source: "image://sceneicons/chevron-down/16/" + root.iconHex(root.textMuted) }
        }
        MouseArea { id: outputMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: outputButton.clicked() }
    }

    component NameDialog: Dialog {
        id: nameDialog
        property string titleText: ""
        property string acceptText: ""
        property alias value: nameField.text
        signal acceptedValue(string value)
        modal: true
        anchors.centerIn: parent
        width: Math.min(390, root.width - 28)
        padding: 18
        standardButtons: Dialog.NoButton
        function commit() {
            var trimmed = nameField.text.trim()
            if (!trimmed.length)
                return
            acceptedValue(trimmed)
            close()
        }
        onOpened: { nameField.forceActiveFocus(); nameField.selectAll() }
        background: Rectangle {
            color: root.surface
            radius: 14
            border.width: 1
            border.color: root.borderColor
        }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: nameDialog.titleText
                color: root.textPrimary
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            ScenesTextField {
                id: nameField
                theme: root.theme
                Layout.fillWidth: true
                placeholderText: qsTr("Name")
                maximumLength: 120
                onAccepted: nameDialog.commit()
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: nameDialog.close() }
                ScenesDialogButton {
                    theme: root.theme
                    text: nameDialog.acceptText
                    primary: true
                    enabled: nameField.text.trim().length > 0
                    onClicked: nameDialog.commit()
                }
            }
        }
    }
}
