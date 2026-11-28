pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    objectName: "scenesPtzInspector"
    property var bridge: null
    property var theme: null
    property bool embedded: false
    property string cameraId: ""
    signal requestClose()

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color accentTint: theme ? theme.accentTint : "#17335b"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"

    color: embedded ? "transparent" : surface
    radius: embedded ? 0 : 14
    border.width: embedded ? 0 : 1
    border.color: borderColor
    clip: true

    function iconHex(colorValue) { return String(colorValue).replace("#", "") }

    function cameras() {
        return bridge ? bridge.selectedLayerPtzCameras : []
    }

    function ensureCamera() {
        var values = cameras()
        if (!values.length) {
            cameraId = ""
            return
        }
        var found = false
        for (var index = 0; index < values.length; ++index) {
            if (values[index].id === cameraId) {
                cameraCombo.currentIndex = index
                found = true
                break
            }
        }
        if (!found) {
            cameraId = values[0].id
            cameraCombo.currentIndex = 0
        }
    }

    function presetsForCamera() {
        var values = bridge ? bridge.selectedLayerPtzPresets : []
        return values.filter(function(item) { return item.cameraId === cameraId })
    }

    onVisibleChanged: if (visible) ensureCamera()

    Connections {
        target: root.bridge
        enabled: root.bridge !== null
        function onChanged() { root.ensureCamera() }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.embedded ? 4 : 12
        spacing: 9

        RowLayout {
            Layout.fillWidth: true
            spacing: 7
            IconButton {
                iconName: "panel-right"
                toolTipText: qsTr("Back to sources")
                onClicked: root.requestClose()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text { text: qsTr("PTZ automation"); color: root.textPrimary; font.pixelSize: 13; font.weight: Font.DemiBold }
                Text { text: root.bridge ? root.bridge.selectedSceneName : ""; color: root.textMuted; font.pixelSize: 9; elide: Text.ElideRight; Layout.fillWidth: true }
            }
        }

        ScenesComboBox {
            id: cameraCombo
            theme: root.theme
            Layout.fillWidth: true
            model: root.cameras()
            textRole: "name"
            visible: count > 1
            onActivated: root.cameraId = model[currentIndex].id
        }

        SectionHeader {
            title: qsTr("Camera presets")
            actionEnabled: root.cameraId.length > 0
            onAction: presetDialog.openForPreset("")
        }

        ListView {
            id: presetList
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 96
            clip: true
            spacing: 6
            model: root.presetsForCamera()
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            delegate: Rectangle {
                required property var modelData
                width: presetList.width
                height: 48
                radius: 9
                color: presetMouse.containsMouse ? root.hover : root.surfaceSoft
                border.width: 1
                border.color: root.borderColor

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 5
                    spacing: 4
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Text { Layout.fillWidth: true; text: modelData.name; color: root.textPrimary; font.pixelSize: 10; font.weight: Font.Medium; elide: Text.ElideRight }
                        Text { Layout.fillWidth: true; text: modelData.targetMode === "position" ? qsTr("Absolute position") : qsTr("Camera preset"); color: root.textMuted; font.pixelSize: 8; elide: Text.ElideRight }
                    }
                    IconButton { iconName: "crosshair"; toolTipText: qsTr("Recall now"); onClicked: root.bridge.recallPtzPreset(modelData.id) }
                    IconButton { iconName: "edit"; toolTipText: qsTr("Edit preset"); onClicked: presetDialog.openForPreset(modelData.id) }
                    IconButton { iconName: "trash"; danger: true; toolTipText: qsTr("Delete preset"); onClicked: deleteDialog.openFor("preset", modelData.id, modelData.name) }
                }
                MouseArea { id: presetMouse; anchors.fill: parent; acceptedButtons: Qt.NoButton; hoverEnabled: true }
            }

            footer: Text {
                visible: presetList.count === 0
                width: presetList.width
                height: 68
                text: qsTr("No presets for this camera")
                color: root.textMuted
                font.pixelSize: 9
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: root.borderColor }

        SectionHeader {
            title: qsTr("When this scene goes live")
            actionEnabled: root.bridge && root.bridge.selectedScenePtzPresets.length > 0
            onAction: actionDialog.openForAction("")
        }

        ListView {
            id: actionList
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 96
            clip: true
            spacing: 6
            model: root.bridge ? root.bridge.selectedSceneEntryActions : []
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            delegate: Rectangle {
                required property var modelData
                width: actionList.width
                height: 53
                radius: 9
                color: actionMouse.containsMouse ? root.hover : root.surfaceSoft
                border.width: 1
                border.color: root.borderColor
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    anchors.rightMargin: 5
                    spacing: 4
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Text { Layout.fillWidth: true; text: modelData.cameraName + " · " + modelData.presetName; color: root.textPrimary; font.pixelSize: 9; font.weight: Font.Medium; elide: Text.ElideRight }
                        Text { Layout.fillWidth: true; text: modelData.timeoutMs + " ms · " + (modelData.onTimeout === "take_anyway" ? qsTr("take anyway") : qsTr("keep current")); color: root.textMuted; font.pixelSize: 8; elide: Text.ElideRight }
                    }
                    IconButton { iconName: "edit"; toolTipText: qsTr("Edit action"); onClicked: actionDialog.openForAction(modelData.presetId) }
                    IconButton { iconName: "trash"; danger: true; toolTipText: qsTr("Delete action"); onClicked: deleteDialog.openFor("action", modelData.presetId, modelData.presetName) }
                }
                MouseArea { id: actionMouse; anchors.fill: parent; acceptedButtons: Qt.NoButton; hoverEnabled: true }
            }

            footer: Text {
                visible: actionList.count === 0
                width: actionList.width
                height: 68
                text: qsTr("No automatic camera moves")
                color: root.textMuted
                font.pixelSize: 9
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    Dialog {
        id: presetDialog
        property string presetId: ""
        property string selectedCameraId: ""
        property string targetMode: "token"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(440, (Overlay.overlay ? Overlay.overlay.width : 468) - 28)
        title: presetId ? qsTr("Edit PTZ preset") : qsTr("New PTZ preset")
        standardButtons: Dialog.NoButton

        function openForPreset(id) {
            var draft = root.bridge.ptzPresetDraft(id)
            presetId = draft.id
            selectedCameraId = draft.cameraId || root.cameraId
            nameField.text = draft.name
            targetMode = draft.targetMode
            tokenField.text = draft.token
            panField.value = Math.round(Number(draft.pan) * 1000)
            tiltField.value = Math.round(Number(draft.tilt) * 1000)
            zoomField.value = Math.round(Number(draft.zoom) * 1000)
            var values = root.cameras()
            for (var index = 0; index < values.length; ++index) {
                if (values[index].id === selectedCameraId) {
                    presetCamera.currentIndex = index
                    break
                }
            }
            targetCombo.currentIndex = targetMode === "position" ? 1 : 0
            open()
        }

        function selectedProtocol() {
            var values = root.cameras()
            for (var index = 0; index < values.length; ++index) {
                if (values[index].id === selectedCameraId)
                    return values[index].ptzProtocol
            }
            return ""
        }

        function save() {
            var values = {
                id: presetId,
                cameraId: selectedCameraId,
                name: nameField.text,
                targetMode: targetMode,
                token: tokenField.text,
                pan: panField.value / 1000,
                tilt: tiltField.value / 1000,
                zoom: zoomField.value / 1000
            }
            if (root.bridge.savePtzPreset(values))
                close()
        }

        background: Rectangle { color: root.surface; radius: 14; border.width: 1; border.color: root.borderColor }
        contentItem: ColumnLayout {
            spacing: 10
            Text { Layout.fillWidth: true; text: presetDialog.title; color: root.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold }
            Text { text: qsTr("Camera"); color: root.textMuted; font.pixelSize: 9 }
            ScenesComboBox {
                id: presetCamera
                theme: root.theme
                Layout.fillWidth: true
                model: root.cameras()
                textRole: "name"
                enabled: !presetDialog.presetId
                onActivated: presetDialog.selectedCameraId = model[currentIndex].id
            }
            Text { text: qsTr("Name"); color: root.textMuted; font.pixelSize: 9 }
            ScenesTextField { id: nameField; theme: root.theme; Layout.fillWidth: true; maximumLength: 120 }
            Text { text: qsTr("Target"); color: root.textMuted; font.pixelSize: 9 }
            ScenesComboBox {
                id: targetCombo
                theme: root.theme
                Layout.fillWidth: true
                model: presetDialog.selectedProtocol() === "onvif"
                    ? [qsTr("Camera preset token"), qsTr("Absolute position")]
                    : [qsTr("Camera preset token")]
                onActivated: presetDialog.targetMode = currentIndex === 1 ? "position" : "token"
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: presetDialog.targetMode === "token"
                spacing: 4
                Text { text: presetDialog.selectedProtocol().indexOf("visca") === 0 ? qsTr("Preset number (0–127)") : qsTr("Preset token"); color: root.textMuted; font.pixelSize: 9 }
                ScenesTextField { id: tokenField; theme: root.theme; Layout.fillWidth: true; maximumLength: 512 }
            }
            GridLayout {
                Layout.fillWidth: true
                visible: presetDialog.targetMode === "position"
                columns: 2
                Text { text: qsTr("Pan"); color: root.textMuted; font.pixelSize: 9 }
                ScenesSpinBox { id: panField; theme: root.theme; Layout.fillWidth: true; from: -1000; to: 1000; stepSize: 50 }
                Text { text: qsTr("Tilt"); color: root.textMuted; font.pixelSize: 9 }
                ScenesSpinBox { id: tiltField; theme: root.theme; Layout.fillWidth: true; from: -1000; to: 1000; stepSize: 50 }
                Text { text: qsTr("Zoom"); color: root.textMuted; font.pixelSize: 9 }
                ScenesSpinBox { id: zoomField; theme: root.theme; Layout.fillWidth: true; from: 0; to: 1000; stepSize: 50 }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: presetDialog.close() }
                ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; onClicked: presetDialog.save() }
            }
        }
    }

    Dialog {
        id: actionDialog
        property string originalPresetId: ""
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(440, (Overlay.overlay ? Overlay.overlay.width : 468) - 28)
        title: originalPresetId ? qsTr("Edit scene PTZ action") : qsTr("New scene PTZ action")
        standardButtons: Dialog.NoButton

        function openForAction(presetId) {
            var draft = root.bridge.sceneEntryActionDraft(presetId)
            originalPresetId = draft.presetId
            timeoutField.value = draft.timeoutMs
            policyCombo.currentIndex = draft.onTimeout === "take_anyway" ? 1 : 0
            var presets = root.bridge.selectedScenePtzPresets
            presetCombo.currentIndex = 0
            for (var index = 0; index < presets.length; ++index) {
                if (presets[index].id === draft.presetId) {
                    presetCombo.currentIndex = index
                    break
                }
            }
            open()
        }

        function save() {
            var presets = root.bridge.selectedScenePtzPresets
            if (!presets.length)
                return
            if (root.bridge.saveSceneEntryAction({
                    presetId: presets[presetCombo.currentIndex].id,
                    timeoutMs: timeoutField.value,
                    onTimeout: policyCombo.currentIndex === 1 ? "take_anyway" : "keep_current"
                }))
                close()
        }

        background: Rectangle { color: root.surface; radius: 14; border.width: 1; border.color: root.borderColor }
        contentItem: ColumnLayout {
            spacing: 10
            Text { Layout.fillWidth: true; text: actionDialog.title; color: root.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold }
            Text { text: qsTr("Preset"); color: root.textMuted; font.pixelSize: 9 }
            ScenesComboBox {
                id: presetCombo
                theme: root.theme
                Layout.fillWidth: true
                model: root.bridge ? root.bridge.selectedScenePtzPresets : []
                textRole: "name"
                enabled: !actionDialog.originalPresetId
                displayText: currentIndex >= 0 && model.length
                    ? model[currentIndex].cameraName + " · " + model[currentIndex].name : ""
            }
            Text { text: qsTr("Timeout"); color: root.textMuted; font.pixelSize: 9 }
            ScenesSpinBox { id: timeoutField; theme: root.theme; Layout.fillWidth: true; from: 500; to: 10000; stepSize: 250 }
            Text { text: qsTr("If the camera cannot be positioned"); color: root.textMuted; font.pixelSize: 9 }
            ScenesComboBox { id: policyCombo; theme: root.theme; Layout.fillWidth: true; model: [qsTr("Keep current scene"), qsTr("Take scene anyway")] }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: actionDialog.close() }
                ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; onClicked: actionDialog.save() }
            }
        }
    }

    Dialog {
        id: deleteDialog
        property string kind: ""
        property string targetId: ""
        property string targetName: ""
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(390, (Overlay.overlay ? Overlay.overlay.width : 418) - 28)
        standardButtons: Dialog.NoButton
        function openFor(valueKind, valueId, valueName) {
            kind = valueKind
            targetId = valueId
            targetName = valueName
            open()
        }
        background: Rectangle { color: root.surface; radius: 14; border.width: 1; border.color: root.borderColor }
        contentItem: ColumnLayout {
            spacing: 12
            Text { Layout.fillWidth: true; text: deleteDialog.kind === "preset" ? qsTr("Delete PTZ preset") : qsTr("Delete scene action"); color: root.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; text: deleteDialog.kind === "preset" ? qsTr("Delete “%1” and its scene actions?").arg(deleteDialog.targetName) : qsTr("Remove “%1” from this scene?").arg(deleteDialog.targetName); color: root.textSecondary; font.pixelSize: 10; wrapMode: Text.Wrap }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: deleteDialog.close() }
                ScenesDialogButton {
                    theme: root.theme
                    text: qsTr("Delete")
                    danger: true
                    onClicked: {
                        if (deleteDialog.kind === "preset")
                            root.bridge.deletePtzPreset(deleteDialog.targetId)
                        else
                            root.bridge.deleteSceneEntryAction(deleteDialog.targetId)
                        deleteDialog.close()
                    }
                }
            }
        }
    }

    component SectionHeader: RowLayout {
        id: section
        property string title: ""
        property bool actionEnabled: true
        signal action()
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: section.title; color: root.textSecondary; font.pixelSize: 10; font.weight: Font.DemiBold }
        IconButton { iconName: "plus"; enabled: section.actionEnabled; toolTipText: qsTr("Add"); onClicked: section.action() }
    }

    component IconButton: Item {
        id: button
        property string iconName: ""
        property string toolTipText: ""
        property bool danger: false
        signal clicked()
        Layout.preferredWidth: 28
        Layout.preferredHeight: 28
        opacity: enabled ? 1 : 0.38
        Rectangle {
            anchors.fill: parent
            radius: 7
            color: buttonMouse.containsMouse && button.enabled ? root.hover : "transparent"
            Image { anchors.centerIn: parent; width: 13; height: 13; source: "image://sceneicons/" + button.iconName + "/16/" + root.iconHex(button.danger ? root.danger : root.textMuted) }
            MouseArea { id: buttonMouse; anchors.fill: parent; enabled: button.enabled; hoverEnabled: true; cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: button.clicked() }
            ToolTip.visible: buttonMouse.containsMouse
            ToolTip.text: button.toolTipText
        }
    }

}
