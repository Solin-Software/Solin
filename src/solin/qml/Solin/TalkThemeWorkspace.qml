pragma Translator: "TalkThemeEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    objectName: "talkThemeWorkspace"
    focus: true

    property var bridge: null
    property var theme: null
    property bool drawerOpen: false
    property bool adjustBackground: false
    property string mobilePage: "canvas"
    property string mobileSection: "styles"
    property string pendingDeletePresetId: ""
    property string pendingDeletePresetName: ""
    readonly property bool wide: width >= 1180
    readonly property bool narrow: width < 760
    readonly property bool compactHeader: width < 560
    readonly property color bgColor: theme ? theme.bg : "#0e1622"
    readonly property color panelColor: theme ? theme.surface : "#172131"
    readonly property color subtlePanel: theme ? theme.bg : "#111927"
    readonly property color hoverColor: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color borderStrong: theme ? theme.borderStrong : "#46566c"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#7367f0"
    readonly property color accentTint: theme ? theme.accentTint : "#29244c"
    readonly property color warningColor: theme ? theme.warning : "#e0ad55"

    onWideChanged: if (wide) drawerOpen = false
    onNarrowChanged: if (narrow) drawerOpen = false

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function openSaveAs() {
        commitPendingEdits()
        presetNameField.text = ""
        saveAsDialog.open()
        presetNameField.forceActiveFocus()
    }

    function requestRender(exportImage) {
        if (!bridge)
            return
        commitPendingEdits()
        if (narrow)
            mobilePage = "canvas"
        if (exportImage)
            bridge.requestExport()
        else
            bridge.requestProjection()
    }

    function commitPendingEdits() {
        return canvas.commitInlineEdit()
    }

    function toggleBackgroundAdjustment() {
        if (!bridge)
            return
        var enabling = !adjustBackground
        if (enabling && !bridge.background.url)
            return
        if (enabling) {
            commitPendingEdits()
            bridge.selectLayer("")
            if (narrow)
                mobilePage = "canvas"
        }
        adjustBackground = enabling
        canvas.forceActiveFocus()
    }

    function requestPreset(presetId) {
        commitPendingEdits()
        if (bridge) {
            bridge.requestPreset(presetId)
            if (!wide && !narrow)
                drawerOpen = false
        }
    }

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onChanged() {
            if (root.adjustBackground && !root.bridge.background.url)
                root.adjustBackground = false
        }
        function onSaveAsRequested() { root.openSaveAs() }
        function onUnsavedChangesRequested(presetId) { unsavedDialog.open() }
    }

    Rectangle { anchors.fill: parent; color: root.bgColor }

    Rectangle {
        id: header
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 62
        color: root.subtlePanel
        border.width: 1
        border.color: root.borderColor
        z: 50

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
                    height: 40
                    spacing: root.narrow ? 7 : 10

                    DrawerToggle {
                        visible: !root.wide && !root.narrow
                        open: root.drawerOpen
                        onClicked: root.drawerOpen = !root.drawerOpen
                    }

                    ColumnLayout {
                        Layout.preferredWidth: root.narrow ? 96 : 220
                        Layout.alignment: Qt.AlignVCenter
                        spacing: 0
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                Layout.fillWidth: true
                                text: qsTr("Talk theme")
                                color: root.textPrimary
                                font.pixelSize: root.narrow ? 15 : 17
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Rectangle {
                                visible: root.narrow && root.bridge && root.bridge.dirty
                                Layout.preferredWidth: 6
                                Layout.preferredHeight: 6
                                radius: 3
                                color: root.warningColor
                            }
                        }
                        RowLayout {
                            visible: !root.narrow
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: root.bridge ? root.bridge.renderTargetLabel : ""
                                color: root.textMuted
                                font.pixelSize: 9
                            }
                            Text {
                                visible: root.bridge && root.bridge.dirty
                                text: "· " + qsTr("Unsaved changes")
                                color: root.warningColor
                                font.pixelSize: 9
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: editActions
                Layout.preferredWidth: editActionsRow.implicitWidth + 8
                Layout.preferredHeight: 40
                Layout.alignment: Qt.AlignVCenter
                radius: 11
                color: root.bgColor
                border.width: 1
                border.color: root.borderColor
                RowLayout {
                    id: editActionsRow
                    anchors.fill: parent
                    anchors.margins: 3
                    spacing: 2
                    ToolButton {
                        visible: !root.compactHeader
                        iconName: "undo"
                        toolTipText: qsTr("Undo")
                        enabled: root.bridge && root.bridge.canUndo
                        onClicked: root.bridge.undo()
                    }
                    ToolButton {
                        visible: !root.compactHeader
                        iconName: "redo"
                        toolTipText: qsTr("Redo")
                        enabled: root.bridge && root.bridge.canRedo
                        onClicked: root.bridge.redo()
                    }
                    Rectangle {
                        visible: !root.narrow
                        Layout.preferredWidth: 1
                        Layout.preferredHeight: 22
                        color: root.borderColor
                    }
                    SoftButton {
                        id: saveButton
                        visible: !root.narrow
                        Layout.preferredHeight: 32
                        text: root.bridge && root.bridge.canSave
                            ? qsTr("Save") : qsTr("Save as preset")
                        enabled: root.bridge && root.bridge.dirty
                        onClicked: {
                            if (!root.bridge)
                                return
                            root.commitPendingEdits()
                            if (root.bridge.canSave)
                                root.bridge.saveCurrentPreset()
                            else
                                root.openSaveAs()
                        }
                    }
                    ToolButton {
                        iconName: "more"
                        toolTipText: qsTr("More save options")
                        onClicked: saveMenu.open()
                        Menu {
                            id: saveMenu
                            y: parent.height + 4
                            width: 210
                            MenuItem {
                                text: root.bridge && root.bridge.canSave
                                    ? qsTr("Save") : qsTr("Save as preset")
                                enabled: root.bridge && root.bridge.dirty
                                onTriggered: {
                                    if (!root.bridge)
                                        return
                                    root.commitPendingEdits()
                                    if (root.bridge.canSave)
                                        root.bridge.saveCurrentPreset()
                                    else
                                        root.openSaveAs()
                                }
                            }
                            MenuItem {
                                text: qsTr("Save as new preset…")
                                onTriggered: root.openSaveAs()
                            }
                            background: Rectangle {
                                radius: 9
                                color: root.panelColor
                                border.width: 1
                                border.color: root.borderStrong
                            }
                        }
                    }
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                RowLayout {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    height: 40
                    spacing: root.narrow ? 7 : 10

                    Rectangle {
                        Layout.preferredWidth: outputActionsRow.implicitWidth + 8
                        Layout.preferredHeight: 40
                        radius: 11
                        color: root.bgColor
                        border.width: 1
                        border.color: root.borderColor
                        RowLayout {
                            id: outputActionsRow
                            anchors.fill: parent
                            anchors.margins: 3
                            spacing: 2
                            SoftButton {
                                primary: true
                                Layout.preferredHeight: 32
                                text: root.bridge && root.bridge.rendering
                                    ? qsTr("Preparing…") : qsTr("Project")
                                enabled: root.bridge && root.bridge.canRender
                                onClicked: root.requestRender(false)
                            }
                            ToolButton {
                                iconName: "export"
                                toolTipText: qsTr("Export image")
                                accessibleName: qsTr("Export image")
                                enabled: root.bridge && root.bridge.canRender
                                onClicked: root.requestRender(true)
                            }
                        }
                    }

                    SoftButton {
                        visible: root.narrow
                        Layout.alignment: Qt.AlignVCenter
                        text: root.mobilePage === "canvas" ? qsTr("Edit") : qsTr("Canvas")
                        onClicked: root.mobilePage = root.mobilePage === "canvas" ? "edit" : "canvas"
                    }
                }
            }
        }
    }

    Rectangle {
        id: mobileNavigation
        objectName: "talkThemeMobileNavigation"
        visible: root.narrow && root.mobilePage !== "canvas"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: header.bottom
        height: 46
        color: root.subtlePanel
        border.width: 1
        border.color: root.borderColor
        z: 20
        RowLayout {
            anchors.fill: parent
            anchors.margins: 6
            spacing: 4
            MobileTab { value: "styles"; label: qsTr("Styles") }
            MobileTab { value: "layers"; label: qsTr("Layers") }
            MobileTab { value: "text"; label: qsTr("Text") }
            MobileTab { value: "background"; label: qsTr("Background") }
        }
    }

    Item {
        id: body
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: root.narrow && root.mobilePage !== "canvas"
            ? mobileNavigation.bottom : header.bottom
        anchors.bottom: parent.bottom

        TalkThemeSidebar {
            id: desktopSidebar
            visible: root.wide
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 244
            bridge: root.bridge
            theme: root.theme
            onPresetRequested: function(presetId) { root.requestPreset(presetId) }
            onDeletePresetRequested: function(presetId, presetName) {
                root.pendingDeletePresetId = presetId
                root.pendingDeletePresetName = presetName
                deletePresetDialog.open()
            }
        }

        Rectangle {
            id: drawerScrim
            objectName: "talkThemeDrawerScrim"
            visible: !root.wide && !root.narrow
            enabled: root.drawerOpen
            anchors.fill: parent
            color: "#000000"
            opacity: root.drawerOpen ? 0.35 : 0
            z: 29
            Behavior on opacity {
                NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
            }
            MouseArea {
                anchors.fill: parent
                onClicked: root.drawerOpen = false
            }
        }

        TalkThemeSidebar {
            id: drawerSidebar
            objectName: "talkThemeDrawer"
            visible: !root.wide && !root.narrow
            enabled: root.drawerOpen
            x: root.drawerOpen ? 0 : -width - 8
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 260
            z: 30
            bridge: root.bridge
            theme: root.theme
            Behavior on x {
                NumberAnimation { duration: 190; easing.type: Easing.OutCubic }
            }
            onPresetRequested: function(presetId) { root.requestPreset(presetId) }
            onDeletePresetRequested: function(presetId, presetName) {
                root.pendingDeletePresetId = presetId
                root.pendingDeletePresetName = presetName
                deletePresetDialog.open()
            }
        }

        TalkThemeInspector {
            id: desktopInspector
            visible: !root.narrow
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 306
            bridge: root.bridge
            theme: root.theme
            adjustBackground: root.adjustBackground
            onAddTextRequested: if (root.bridge) root.bridge.addTextLayer()
            onToggleBackgroundAdjustment: root.toggleBackgroundAdjustment()
        }

        Item {
            id: canvasArea
            visible: !root.narrow || root.mobilePage === "canvas"
            anchors.left: root.wide ? desktopSidebar.right : parent.left
            anchors.right: root.narrow ? parent.right : desktopInspector.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom

            Rectangle {
                anchors.fill: parent
                anchors.margins: root.narrow ? 8 : 14
                radius: 16
                color: root.theme ? root.theme.surfaceChrome : "#111a28"
                border.width: 1
                border.color: root.borderColor

                Item {
                    id: stage
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: canvasToolbar.top
                    anchors.margins: root.narrow ? 10 : 18

                    readonly property real ratio: Math.max(
                        0.2, root.bridge ? root.bridge.renderAspectRatio : 16 / 9)
                    readonly property real maxWidth: Math.max(1, width)
                    readonly property real maxHeight: Math.max(1, height)

                    Item {
                        id: canvasViewport
                        anchors.centerIn: parent
                        width: Math.min(stage.maxWidth, stage.maxHeight * stage.ratio)
                        height: width / stage.ratio

                        Rectangle {
                            anchors.fill: parent
                            anchors.margins: -3
                            radius: 10
                            color: "transparent"
                            border.width: 1
                            border.color: root.borderStrong
                        }

                        TalkThemeCanvas {
                            id: canvas
                            anchors.fill: parent
                            bridge: root.bridge
                            adjustBackground: root.adjustBackground
                            interactive: true
                            onCanvasPressed: root.forceActiveFocus()
                        }
                    }
                }

                Item {
                    id: canvasToolbar
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: 45
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 16
                        anchors.rightMargin: 16
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: root.adjustBackground
                                ? qsTr("Drag the image to reposition it")
                                : qsTr("Drag text · double-click to edit")
                            color: root.adjustBackground ? root.accent : root.textMuted
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                        Rectangle {
                            id: aspectControl
                            objectName: "talkThemeOutputAspectControl"
                            visible: !root.narrow
                            Layout.preferredWidth: Math.max(104, aspectContent.implicitWidth + 20)
                            Layout.minimumWidth: Layout.preferredWidth
                            Layout.preferredHeight: 28
                            radius: 9
                            color: aspectMouse.containsMouse ? root.hoverColor : "transparent"
                            border.width: 1
                            border.color: root.borderColor
                            Accessible.role: Accessible.CheckBox
                            Accessible.name: qsTr("Auto format")
                            Accessible.checked: root.bridge ? root.bridge.followOutputAspect : true
                            Accessible.onToggleAction: aspectMouse.toggle()

                            Row {
                                id: aspectContent
                                objectName: "talkThemeOutputAspectContent"
                                anchors.centerIn: parent
                                spacing: 7
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: qsTr("Auto format")
                                    color: root.textMuted
                                    font.pixelSize: 10
                                }
                                Rectangle {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 28
                                    height: 16
                                    radius: 8
                                    color: root.bridge && root.bridge.followOutputAspect
                                        ? root.accent : root.borderStrong
                                    Rectangle {
                                        width: 12
                                        height: 12
                                        radius: 6
                                        y: 2
                                        x: root.bridge && root.bridge.followOutputAspect ? 14 : 2
                                        color: "white"
                                        Behavior on x { NumberAnimation { duration: 120 } }
                                    }
                                }
                            }
                            MouseArea {
                                id: aspectMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                function toggle() {
                                    if (root.bridge)
                                        root.bridge.setFollowOutputAspect(
                                            !root.bridge.followOutputAspect)
                                }
                                onClicked: toggle()
                            }
                            ToolTip.visible: aspectMouse.containsMouse
                            ToolTip.text: qsTr("Match the projection screen format")
                        }
                    }
                }
            }
        }

        TalkThemeSidebar {
            visible: root.narrow && root.mobilePage !== "canvas"
                     && (root.mobileSection === "styles" || root.mobileSection === "layers")
            anchors.fill: parent
            currentTab: root.mobileSection
            navigationVisible: false
            bridge: root.bridge
            theme: root.theme
            onPresetRequested: function(presetId) { root.requestPreset(presetId) }
            onDeletePresetRequested: function(presetId, presetName) {
                root.pendingDeletePresetId = presetId
                root.pendingDeletePresetName = presetName
                deletePresetDialog.open()
            }
        }

        TalkThemeInspector {
            visible: root.narrow && root.mobilePage !== "canvas"
                     && (root.mobileSection === "text" || root.mobileSection === "background")
            anchors.fill: parent
            currentTab: root.mobileSection
            navigationVisible: false
            bridge: root.bridge
            theme: root.theme
            adjustBackground: root.adjustBackground
            onAddTextRequested: if (root.bridge) root.bridge.addTextLayer()
            onToggleBackgroundAdjustment: root.toggleBackgroundAdjustment()
        }
    }

    Dialog {
        id: saveAsDialog
        anchors.centerIn: parent
        width: Math.min(380, root.width - 32)
        modal: true
        focus: true
        padding: 18
        title: qsTr("Save as preset")
        standardButtons: Dialog.NoButton
        onRejected: if (root.bridge) root.bridge.cancelPendingPreset()
        background: DialogSurface { }
        contentItem: ColumnLayout {
            width: saveAsDialog.availableWidth
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: qsTr("Save the complete composition so you can use it again later.")
                color: root.textSecondary
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            TextField {
                id: presetNameField
                Layout.fillWidth: true
                implicitHeight: 40
                placeholderText: qsTr("Preset name")
                color: root.textPrimary
                placeholderTextColor: root.textMuted
                leftPadding: 11
                rightPadding: 11
                background: Rectangle {
                    radius: 9
                    color: root.hoverColor
                    border.width: presetNameField.activeFocus ? 2 : 1
                    border.color: presetNameField.activeFocus ? root.accent : root.borderColor
                }
                Keys.onReturnPressed: savePresetAction.trigger()
            }
            Text {
                visible: root.bridge && root.bridge.statusMessage !== ""
                Layout.fillWidth: true
                text: root.bridge ? root.bridge.statusMessage : ""
                color: root.warningColor
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                Item { Layout.fillWidth: true }
                SoftButton { text: qsTr("Cancel"); onClicked: saveAsDialog.reject() }
                SoftButton {
                    id: savePresetAction
                    primary: true
                    text: qsTr("Save preset")
                    enabled: presetNameField.text.trim().length > 0
                    onClicked: {
                        if (root.bridge && root.bridge.saveAsPreset(presetNameField.text))
                            saveAsDialog.close()
                    }
                }
            }
        }
    }

    Shortcut {
        sequence: "Esc"
        enabled: root.drawerOpen
        onActivated: root.drawerOpen = false
    }

    Dialog {
        id: deletePresetDialog
        anchors.centerIn: parent
        width: Math.min(390, root.width - 32)
        modal: true
        padding: 18
        title: qsTr("Delete preset?")
        standardButtons: Dialog.NoButton
        background: DialogSurface { }
        onClosed: {
            root.pendingDeletePresetId = ""
            root.pendingDeletePresetName = ""
        }
        contentItem: ColumnLayout {
            width: deletePresetDialog.availableWidth
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: qsTr("“%1” will be permanently removed.")
                    .arg(root.pendingDeletePresetName)
                color: root.textSecondary
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                SoftButton { text: qsTr("Cancel"); onClicked: deletePresetDialog.close() }
                SoftButton {
                    text: qsTr("Delete")
                    danger: true
                    onClicked: {
                        if (root.bridge)
                            root.bridge.deletePreset(root.pendingDeletePresetId)
                        deletePresetDialog.close()
                    }
                }
            }
        }
    }

    Dialog {
        id: unsavedDialog
        anchors.centerIn: parent
        width: Math.min(430, root.width - 32)
        modal: true
        closePolicy: Popup.NoAutoClose
        padding: 18
        title: qsTr("Unsaved changes")
        standardButtons: Dialog.NoButton
        background: DialogSurface { }
        contentItem: ColumnLayout {
            width: unsavedDialog.availableWidth
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: qsTr("Would you like to save your changes before switching presets?")
                color: root.textSecondary
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                SoftButton {
                    text: qsTr("Cancel")
                    onClicked: {
                        if (root.bridge) root.bridge.resolveUnsavedPreset("cancel")
                        unsavedDialog.close()
                    }
                }
                Item { Layout.fillWidth: true }
                SoftButton {
                    text: qsTr("Discard")
                    danger: true
                    onClicked: {
                        if (root.bridge) root.bridge.resolveUnsavedPreset("discard")
                        unsavedDialog.close()
                    }
                }
                SoftButton {
                    text: qsTr("Save")
                    primary: true
                    onClicked: {
                        if (root.bridge) root.bridge.resolveUnsavedPreset("save")
                        unsavedDialog.close()
                    }
                }
            }
        }
    }

    component DialogSurface: Rectangle {
        radius: 13
        color: root.panelColor
        border.width: 1
        border.color: root.borderStrong
    }

    component SoftButton: Button {
        id: button
        property bool primary: false
        property bool danger: false
        opacity: enabled ? 1.0 : 0.45
        implicitHeight: 36
        leftPadding: 12
        rightPadding: 12
        contentItem: Text {
            text: button.text
            color: button.primary ? "#ffffff"
                 : button.danger && root.theme ? root.theme.danger : root.textPrimary
            font.pixelSize: 11
            font.weight: button.primary ? Font.DemiBold : Font.Medium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 9
            color: button.primary
                ? (button.hovered ? Qt.lighter(root.accent, 1.08) : root.accent)
                : button.danger && button.hovered && root.theme ? root.theme.dangerSubtle
                : button.hovered ? root.hoverColor : "transparent"
            border.width: button.primary ? 0 : 1
            border.color: button.danger && root.theme ? root.theme.danger : root.borderColor
        }
    }

    component ToolButton: Rectangle {
        id: tool
        property string iconName: ""
        property string text: ""
        property string toolTipText: ""
        property string accessibleName: toolTipText
        signal clicked()
        Layout.preferredWidth: 34
        Layout.preferredHeight: 34
        radius: 9
        color: toolMouse.containsMouse ? root.hoverColor : "transparent"
        border.width: toolMouse.containsMouse ? 1 : 0
        border.color: root.borderStrong
        opacity: enabled ? 1.0 : 0.38
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        Accessible.onPressAction: tool.clicked()
        Image {
            visible: tool.iconName !== ""
            anchors.centerIn: parent
            width: 15
            height: 15
            source: "image://talkthemeicons/" + tool.iconName + "/16/"
                + root.iconHex(root.textSecondary)
        }
        Text {
            visible: tool.iconName === ""
            anchors.centerIn: parent
            text: tool.text
            color: root.textSecondary
            font.pixelSize: 16
        }
        MouseArea {
            id: toolMouse
            anchors.fill: parent
            enabled: tool.enabled
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: tool.clicked()
        }
        ToolTip.visible: tool.enabled && toolMouse.containsMouse && tool.toolTipText !== ""
        ToolTip.text: tool.toolTipText
    }

    component DrawerToggle: Rectangle {
        id: toggle
        objectName: "talkThemeDrawerToggle"
        property bool open: false
        signal clicked()

        Layout.preferredWidth: 34
        Layout.preferredHeight: 34
        radius: 9
        color: toggle.open ? root.accentTint
                           : toggleMouse.containsMouse ? root.hoverColor : "transparent"
        border.width: toggle.open || toggleMouse.containsMouse || activeFocus ? 1 : 0
        border.color: toggle.open || activeFocus ? root.accent : root.borderStrong
        activeFocusOnTab: visible && enabled
        Accessible.role: Accessible.CheckBox
        Accessible.name: qsTr("Styles and layers")
        Accessible.checked: toggle.open
        Accessible.onPressAction: toggle.clicked()

        Behavior on color {
            ColorAnimation { duration: 120 }
        }
        Behavior on border.color {
            ColorAnimation { duration: 120 }
        }

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                    || event.key === Qt.Key_Space) {
                toggle.clicked()
                event.accepted = true
            }
        }

        Image {
            id: toggleIcon
            objectName: "talkThemeDrawerToggleIcon"
            anchors.centerIn: parent
            width: 16
            height: 16
            source: "image://talkthemeicons/panel-left/16/"
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
        ToolTip.visible: toggleMouse.containsMouse
        ToolTip.text: qsTr("Styles and layers")
    }

    component MobileTab: Rectangle {
        id: tab
        property string value: ""
        property string label: ""
        Layout.fillWidth: true
        Layout.fillHeight: true
        radius: 8
        color: root.mobileSection === value ? root.panelColor
              : mobileMouse.containsMouse ? root.hoverColor : "transparent"
        Text {
            anchors.centerIn: parent
            text: tab.label
            color: root.mobileSection === tab.value ? root.textPrimary : root.textMuted
            font.pixelSize: 10
            font.weight: root.mobileSection === tab.value ? Font.DemiBold : Font.Medium
        }
        MouseArea {
            id: mobileMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                if (tab.value !== "background" && root.adjustBackground)
                    root.toggleBackgroundAdjustment()
                root.mobileSection = tab.value
            }
        }
    }
}
