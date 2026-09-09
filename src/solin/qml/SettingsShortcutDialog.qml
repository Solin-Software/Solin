pragma Translator: "AutoKeyEditorDialog"
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    objectName: "settingsShortcutDialog"
    property var domain: settingsIntegrations
    readonly property var draftState: domain ? domain.state : ({})
    property bool recording: false
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(520, parent ? Math.max(0, parent.width - 24) : 520)
    height: Math.min(implicitHeight, parent ? Math.max(0, parent.height - 24) : implicitHeight)
    implicitHeight: body.implicitHeight + topPadding + bottomPadding
    padding: width < 380 ? 16 : 24
    modal: true
    focus: true
    visible: !!draftState.shortcutEditorOpen
    closePolicy: recording ? Popup.NoAutoClose : Popup.CloseOnEscape
    onOpened: {
        recording = false
        scroller.contentItem.contentY = 0
        Qt.callLater(function() {
            if (root.draftState.shortcutShareMode) capture.forceActiveFocus(Qt.TabFocusReason)
            else events.forceActiveFocus(Qt.TabFocusReason)
        })
    }
    onClosed: {
        recording = false
        if (draftState.shortcutEditorOpen) domain.invoke("cancelShortcut")
    }
    background: Rectangle {
        radius: 14
        color: appTheme.surface2
        border.color: appTheme.border
    }
    contentItem: ScrollView {
        id: scroller
        clip: true
        contentWidth: availableWidth
        contentHeight: body.implicitHeight
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AsNeeded
        ColumnLayout {
            id: body
            width: scroller.availableWidth
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: root.draftState.shortcutShareMode
                    ? qsTranslate("SettingsWidget", "Share Hotkey") : qsTr("Automatic Shortcut")
                font.pixelSize: 21
                font.weight: Font.DemiBold
                color: appTheme.textPrimary
                wrapMode: Text.Wrap
                Accessible.role: Accessible.Heading
                Accessible.name: text
            }
            Text {
                Layout.fillWidth: true
                text: root.draftState.shortcutShareMode
                    ? qsTranslate("SettingsWidget", "Press the Zoom shortcut that starts and stops screen sharing.")
                    : qsTr("Choose an app event and press the shortcut to send.")
                font.pixelSize: 13
                color: appTheme.textMuted
                wrapMode: Text.Wrap
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: !root.draftState.shortcutShareMode
                spacing: 6
                Text { text: qsTr("Event"); font.pixelSize: 15; color: appTheme.textPrimary }
                ComboBox {
                    id: events
                    objectName: "settingsShortcutEvent"
                    Layout.fillWidth: true
                    implicitHeight: 44
                    model: root.draftState.shortcutEvents || []
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: {
                        const choices = root.draftState.shortcutEvents || []
                        for (let i = 0; i < choices.length; ++i)
                            if (choices[i].value === root.draftState.shortcutEvent) return i
                        return -1
                    }
                    Accessible.name: qsTr("Event")
                    AppPointerCursor {}
                    onActivated: root.domain.setValue("shortcutEvent", currentValue)
                    background: Rectangle {
                        radius: 8
                        color: events.hovered ? appTheme.hover : appTheme.surfaceChrome
                        border.color: events.visualFocus ? appTheme.accent : appTheme.border
                        border.width: events.visualFocus ? 2 : 1
                    }
                    contentItem: Text {
                        leftPadding: 12
                        rightPadding: 34
                        text: events.displayText
                        color: appTheme.textPrimary
                        font.pixelSize: 15
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    indicator: Text {
                        x: events.width - width - 12
                        y: (events.height - height) / 2
                        text: "⌄"
                        font.pixelSize: 20
                        color: appTheme.textMuted
                    }
                    popup: Popup {
                        y: events.height + 4
                        width: events.width
                        padding: 4
                        implicitHeight: Math.min(196, eventList.contentHeight + 8)
                        background: Rectangle {
                            radius: 8
                            color: appTheme.surface2
                            border.color: appTheme.border
                        }
                        contentItem: ListView {
                            id: eventList
                            clip: true
                            model: events.popup.visible ? events.delegateModel : null
                            currentIndex: events.highlightedIndex
                            ScrollBar.vertical: ScrollBar {}
                        }
                    }
                    delegate: ItemDelegate {
                        required property var modelData
                        required property int index
                        width: events.width - 8
                        implicitHeight: Math.max(44, eventLabel.implicitHeight + 16)
                        highlighted: events.highlightedIndex === index
                        AppPointerCursor {}
                        contentItem: Text {
                            id: eventLabel
                            text: modelData.label
                            color: appTheme.textPrimary
                            font.pixelSize: 15
                            wrapMode: Text.Wrap
                        }
                        background: Rectangle {
                            radius: 6
                            color: parent.highlighted || parent.hovered ? appTheme.hover : "transparent"
                        }
                    }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                Text { text: qsTr("Shortcut"); font.pixelSize: 15; color: appTheme.textPrimary }
                SettingsButton {
                    id: capture
                    objectName: "settingsShortcutCapture"
                    Layout.fillWidth: true
                    text: root.recording ? qsTr("Listening... press one shortcut.")
                        : String(root.draftState.shortcutSequence || qsTranslate("_ShortcutSequenceEdit", "Click to record"))
                    Accessible.name: qsTr("Shortcut") + ": " + text
                    onClicked: {
                        root.recording = true
                        forceActiveFocus(Qt.MouseFocusReason)
                    }
                    onActiveFocusChanged: if (!activeFocus) root.recording = false
                    Keys.priority: Keys.BeforeItem
                    Keys.onShortcutOverride: function(event) {
                        if (root.recording) event.accepted = true
                    }
                    Keys.onPressed: function(event) {
                        if (!root.recording) return
                        event.accepted = true
                        if (event.isAutoRepeat) return
                        if (event.key === Qt.Key_Escape) {
                            root.recording = false
                            return
                        }
                        if (event.key === Qt.Key_Control || event.key === Qt.Key_Shift
                                || event.key === Qt.Key_Alt || event.key === Qt.Key_Meta
                                || event.key === Qt.Key_AltGr || event.key === Qt.Key_unknown) return
                        root.domain.captureShortcut(event.key, event.modifiers)
                        root.recording = false
                        save.forceActiveFocus(Qt.TabFocusReason)
                    }
                    background: Rectangle {
                        radius: 8
                        color: root.recording ? appTheme.accentTint : capture.hovered ? appTheme.hover : appTheme.surfaceChrome
                        border.color: root.recording || capture.visualFocus ? appTheme.accent : appTheme.border
                        border.width: root.recording || capture.visualFocus ? 2 : 1
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: root.recording ? qsTr("Listening... press one shortcut.")
                        : qsTr("Click the field, then press one shortcut.")
                    color: root.recording ? appTheme.accent : appTheme.textMuted
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                }
            }
            Switch {
                id: enabledToggle
                objectName: "settingsShortcutEnabled"
                Layout.fillWidth: true
                visible: !root.draftState.shortcutShareMode
                implicitHeight: 44
                text: qsTr("Enabled")
                Accessible.name: text
                AppPointerCursor {}
                onClicked: {
                    root.domain.setValue("shortcutEnabled", checked)
                    checked = !!root.domain.state.shortcutEnabled
                }
                Binding {
                    target: enabledToggle
                    property: "checked"
                    value: !!root.draftState.shortcutEnabled
                }
                contentItem: Text {
                    text: enabledToggle.text
                    rightPadding: 60
                    font.pixelSize: 15
                    color: appTheme.textPrimary
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.Wrap
                }
                indicator: Rectangle {
                    x: enabledToggle.width - width
                    y: (enabledToggle.height - height) / 2
                    width: 46; height: 26; radius: 13
                    color: enabledToggle.checked ? appTheme.accent : appTheme.secondaryControl
                    border.width: enabledToggle.visualFocus ? 2 : 0
                    border.color: appTheme.textPrimary
                    Rectangle {
                        x: enabledToggle.checked ? 23 : 3
                        y: 3
                        width: 20; height: 20; radius: 10
                        color: appTheme.white
                        Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
                    }
                }
            }
            Text {
                objectName: "settingsShortcutError"
                Layout.fillWidth: true
                visible: text.length > 0
                text: String(root.draftState.shortcutError || "")
                font.pixelSize: 13
                color: appTheme.dangerText
                wrapMode: Text.Wrap
                Accessible.name: text
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true; visible: root.width >= 380 }
                SettingsButton {
                    objectName: "settingsShortcutCancel"
                    Layout.fillWidth: root.width < 380
                    text: qsTr("Cancel")
                    onClicked: root.domain.invoke("cancelShortcut")
                }
                SettingsButton {
                    id: save
                    objectName: "settingsShortcutSave"
                    Layout.fillWidth: root.width < 380
                    text: qsTr("Save")
                    primary: true
                    enabled: !root.recording && !!root.draftState.shortcutSequence
                    onClicked: root.domain.invoke("saveShortcut")
                }
            }
        }
    }
}
