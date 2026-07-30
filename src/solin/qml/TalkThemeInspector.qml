pragma Translator: "TalkThemeEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: root
    objectName: "talkThemeInspector"

    property var bridge: null
    property var theme: null
    property string currentTab: "text"
    property bool navigationVisible: true
    property bool fontSizeEditing: false
    property bool adjustBackground: false
    readonly property var selectedRecord: bridge ? bridge.selectedLayer : ({})
    readonly property bool hasSelection: bridge && bridge.selectedLayerId !== ""
    readonly property bool hasBackgroundImage:
        bridge && Boolean(bridge.background.has_image)
    readonly property bool backgroundImageAvailable:
        bridge && Boolean(bridge.background.url)
    readonly property int outputHeight: bridge ? Math.max(1, bridge.renderPixelHeight) : 1080
    readonly property int selectedFontPixels:
        Math.round(Number(selectedRecord.font_size || 0.06) * outputHeight)
    property color panelColor: theme ? theme.surface : "#172131"
    property color subtlePanel: theme ? theme.bg : "#111927"
    property color inputColor: theme ? theme.hoverStrong : "#1b2738"
    property color hoverColor: theme ? theme.hover : "#233043"
    property color borderColor: theme ? theme.border_ : "#2c394b"
    property color borderStrong: theme ? theme.borderStrong : "#46566c"
    property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    property color textMuted: theme ? theme.textMuted : "#768397"
    property color accent: theme ? theme.accent : "#7367f0"
    signal addTextRequested()
    signal toggleBackgroundAdjustment()

    function iconHex(colorValue) {
        return String(colorValue).replace("#", "")
    }

    function applyFontPixels(pixelValue) {
        if (!bridge || !hasSelection)
            return
        var minimum = Math.ceil(0.012 * outputHeight)
        var maximum = Math.floor(0.30 * outputHeight)
        var bounded = Math.max(minimum, Math.min(maximum, Math.round(Number(pixelValue))))
        bridge.setLayerProperty(bridge.selectedLayerId, "font_size", bounded / outputHeight)
    }

    function beginFontSizeEdit() {
        fontSizeField.text = String(selectedFontPixels)
        fontSizeEditing = true
        fontSizeField.forceActiveFocus()
        fontSizeField.selectAll()
    }

    onSelectedRecordChanged: fontSizeEditing = false

    color: root.panelColor
    border.width: 1
    border.color: root.borderColor

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            id: navigationHeader
            objectName: "talkThemeInspectorNavigation"
            visible: root.navigationVisible
            Layout.fillWidth: true
            Layout.preferredHeight: root.navigationVisible ? 58 : 0
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: root.borderColor
            }
            Rectangle {
                anchors.fill: parent
                anchors.margins: 10
                radius: 10
                color: root.subtlePanel
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 3
                    spacing: 3
                    InspectorTab { value: "text"; label: qsTr("Text") }
                    InspectorTab { value: "background"; label: qsTr("Background") }
                }
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                id: inspectorContent
                width: root.width
                spacing: 10

                Item { Layout.preferredHeight: 4 }

                ColumnLayout {
                    visible: root.currentTab === "text" && !root.hasSelection
                    Layout.fillWidth: true
                    Layout.leftMargin: 22
                    Layout.rightMargin: 22
                    Layout.topMargin: 34
                    spacing: 10

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.preferredWidth: 48
                        Layout.preferredHeight: 48
                        radius: 24
                        color: root.theme ? root.theme.accentTint : "#29244c"
                        Text {
                            anchors.centerIn: parent
                            text: "T"
                            color: root.accent
                            font.pixelSize: 20
                            font.family: "Georgia"
                            font.weight: Font.DemiBold
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Select a text")
                        color: root.textPrimary
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Click a text on the canvas or choose a layer to edit it.")
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
                    }
                    SoftButton {
                        Layout.alignment: Qt.AlignHCenter
                        text: qsTr("Add text")
                        primary: true
                        enabled: root.bridge && root.bridge.canAddLayer
                        onClicked: root.addTextRequested()
                    }
                }

                ColumnLayout {
                    visible: root.currentTab === "text" && root.hasSelection
                    Layout.fillWidth: true
                    spacing: 9

                    SectionTitle {
                        text: root.selectedRecord.name || qsTr("Text")
                        Layout.topMargin: 4
                    }

                    FieldLabel { text: qsTr("Content") }
                    TextArea {
                        id: contentField
                        objectName: "talkThemeContentField"
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.preferredHeight: 88
                        text: root.selectedRecord.text || ""
                        placeholderText: qsTr("Write something…")
                        color: root.textPrimary
                        placeholderTextColor: root.textMuted
                        selectionColor: root.accent
                        selectedTextColor: "#ffffff"
                        font.pixelSize: 12
                        wrapMode: TextEdit.Wrap
                        leftPadding: 11
                        rightPadding: 11
                        topPadding: 9
                        bottomPadding: 9
                        background: FieldBackground { focused: contentField.activeFocus }
                        onTextChanged: {
                            if (activeFocus && root.bridge && root.hasSelection)
                                root.bridge.setLayerText(root.bridge.selectedLayerId, text)
                        }
                    }

                    FieldLabel { text: qsTr("Font") }
                    EditorCombo {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        model: root.bridge ? root.bridge.fontFamilies : []
                        currentIndex: Math.max(0, model.indexOf(root.selectedRecord.font_family || "Arial"))
                        onActivated: if (root.bridge) root.bridge.setLayerProperty(
                            root.bridge.selectedLayerId, "font_family", currentText)
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        spacing: 8
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 5
                            Text { text: qsTr("Weight"); color: root.textSecondary; font.pixelSize: 10 }
                            EditorCombo {
                                Layout.fillWidth: true
                                model: [qsTr("Normal"), qsTr("Medium"), qsTr("Semibold"), qsTr("Bold")]
                                property var values: ["normal", "medium", "semibold", "bold"]
                                currentIndex: Math.max(0, values.indexOf(root.selectedRecord.font_weight || "normal"))
                                onActivated: if (root.bridge) root.bridge.setLayerProperty(
                                    root.bridge.selectedLayerId, "font_weight", values[currentIndex])
                            }
                        }
                        ColumnLayout {
                            Layout.preferredWidth: 112
                            spacing: 5
                            Text { text: qsTr("Size"); color: root.textSecondary; font.pixelSize: 10 }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                radius: 9
                                color: root.inputColor
                                border.width: 1
                                border.color: root.borderColor
                                RowLayout {
                                    anchors.fill: parent
                                    spacing: 0
                                    StepButton {
                                        text: "−"
                                        onClicked: root.applyFontPixels(root.selectedFontPixels - 1)
                                    }
                                    Item {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
                                        Text {
                                            anchors.fill: parent
                                            visible: !root.fontSizeEditing
                                            text: root.selectedFontPixels + " px"
                                            color: root.textPrimary
                                            font.pixelSize: 10
                                            horizontalAlignment: Text.AlignHCenter
                                            verticalAlignment: Text.AlignVCenter
                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.IBeamCursor
                                                onDoubleClicked: root.beginFontSizeEdit()
                                            }
                                        }
                                        TextField {
                                            id: fontSizeField
                                            anchors.fill: parent
                                            visible: root.fontSizeEditing
                                            color: root.textPrimary
                                            font.pixelSize: 10
                                            horizontalAlignment: TextInput.AlignHCenter
                                            verticalAlignment: TextInput.AlignVCenter
                                            selectByMouse: true
                                            inputMethodHints: Qt.ImhDigitsOnly
                                            validator: IntValidator {
                                                bottom: Math.ceil(0.012 * root.outputHeight)
                                                top: Math.floor(0.30 * root.outputHeight)
                                            }
                                            background: Rectangle {
                                                radius: 6
                                                color: root.hoverColor
                                                border.width: 1
                                                border.color: root.accent
                                            }
                                            function finish(commit) {
                                                if (!root.fontSizeEditing)
                                                    return
                                                var value = Number(text)
                                                root.fontSizeEditing = false
                                                if (commit && isFinite(value))
                                                    root.applyFontPixels(value)
                                            }
                                            onAccepted: finish(true)
                                            onActiveFocusChanged: if (!activeFocus) finish(true)
                                            Keys.onEscapePressed: function(event) {
                                                finish(false)
                                                event.accepted = true
                                            }
                                        }
                                    }
                                    StepButton {
                                        text: "+"
                                        onClicked: root.applyFontPixels(root.selectedFontPixels + 1)
                                    }
                                }
                            }
                        }
                    }

                    FieldLabel { text: qsTr("Alignment") }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.preferredHeight: 38
                        radius: 9
                        color: root.subtlePanel
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 3
                            spacing: 3
                            AlignmentButton { value: "left"; iconName: "align-left" }
                            AlignmentButton { value: "center"; iconName: "align-center" }
                            AlignmentButton { value: "right"; iconName: "align-right" }
                        }
                    }

                    FieldLabel { text: qsTr("Letter spacing") }
                    EditorSlider {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        from: -5
                        to: 20
                        value: Number(root.selectedRecord.letter_spacing || 0)
                        onMoved: if (root.bridge) root.bridge.setLayerProperty(
                            root.bridge.selectedLayerId, "letter_spacing", value)
                    }
                    FieldLabel { text: qsTr("Text color") }
                    ColorPickerButton {
                        objectName: "talkThemeTextColorPicker"
                        colorValue: String(root.selectedRecord.color || "#FFFFFFFF")
                        accessibleName: qsTr("Text color")
                        onClicked: if (root.bridge) root.bridge.chooseTextColor()
                    }
                    Item { Layout.preferredHeight: 10 }
                }

                ColumnLayout {
                    visible: root.currentTab === "background"
                    Layout.fillWidth: true
                    spacing: 10

                    SectionTitle { text: qsTr("Background") }
                    FieldLabel { text: qsTr("Image") }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.preferredHeight: 104
                        radius: 12
                        clip: true
                        color: root.bridge
                            ? root.bridge.background.base_color || "#11182a"
                            : "#11182a"
                        Image {
                            anchors.fill: parent
                            source: root.bridge ? root.bridge.background.url || "" : ""
                            fillMode: Image.PreserveAspectCrop
                            asynchronous: true
                        }
                        Rectangle {
                            anchors.fill: parent
                            visible: root.backgroundImageAvailable
                            color: root.bridge.background.overlay_color || "#000000"
                            opacity: Number(root.bridge.background.overlay_opacity || 0)
                        }
                        Rectangle {
                            anchors.centerIn: parent
                            visible: !root.hasBackgroundImage
                            width: noImageLabel.implicitWidth + 18
                            height: 28
                            radius: 8
                            color: root.subtlePanel
                            opacity: 0.88
                            Text {
                                id: noImageLabel
                                anchors.centerIn: parent
                                text: qsTr("No image")
                                color: root.textSecondary
                                font.pixelSize: 10
                                font.weight: Font.Medium
                            }
                        }
                    }

                    RowLayout {
                        objectName: "talkThemeImageActions"
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        spacing: 8
                        SoftButton {
                            Layout.fillWidth: true
                            primary: !root.hasBackgroundImage
                            text: root.bridge && root.bridge.importing
                                ? qsTr("Importing…")
                                : (root.hasBackgroundImage
                                    ? qsTr("Replace image") : qsTr("Choose image"))
                            enabled: root.bridge && !root.bridge.importing
                            onClicked: root.bridge.chooseBackground()
                        }
                        SoftButton {
                            objectName: "talkThemeRemoveBackgroundButton"
                            visible: root.hasBackgroundImage
                            Layout.fillWidth: true
                            text: qsTr("Remove image")
                            enabled: root.bridge && !root.bridge.importing
                            onClicked: {
                                if (root.adjustBackground)
                                    root.toggleBackgroundAdjustment()
                                root.bridge.removeBackgroundImage()
                            }
                        }
                    }

                    ColumnLayout {
                        objectName: "talkThemeImageControls"
                        visible: root.backgroundImageAvailable
                        Layout.fillWidth: true
                        spacing: 10

                        SoftButton {
                            Layout.fillWidth: true
                            Layout.leftMargin: 16
                            Layout.rightMargin: 16
                            primary: root.adjustBackground
                            text: root.adjustBackground
                                ? qsTr("Finish adjusting") : qsTr("Adjust image position")
                            onClicked: root.toggleBackgroundAdjustment()
                        }

                        FieldLabel { text: qsTr("Darken image") }
                        EditorSlider {
                            Layout.fillWidth: true
                            Layout.leftMargin: 16
                            Layout.rightMargin: 16
                            from: 0
                            to: root.bridge ? root.bridge.backgroundOverlayMaximum : 0.95
                            value: root.bridge
                                ? Number(root.bridge.background.overlay_opacity || 0) : 0
                            onMoved: if (root.bridge) root.bridge.setBackgroundProperty(
                                "overlay_opacity", value)
                        }

                        FieldLabel { text: qsTr("Blur image") }
                        EditorSlider {
                            objectName: "talkThemeBackgroundBlurSlider"
                            Layout.fillWidth: true
                            Layout.leftMargin: 16
                            Layout.rightMargin: 16
                            from: 0
                            to: root.bridge ? root.bridge.backgroundBlurMaximum : 1
                            stepSize: 0.01
                            value: root.bridge
                                ? Number(root.bridge.background.blur || 0) : 0
                            Accessible.name: qsTr("Blur image")
                            onMoved: if (root.bridge) root.bridge.setBackgroundProperty(
                                "blur", value)
                        }

                        FieldLabel { text: qsTr("Image fit") }
                        EditorCombo {
                            Layout.fillWidth: true
                            Layout.leftMargin: 16
                            Layout.rightMargin: 16
                            model: [qsTr("Fill"), qsTr("Fit")]
                            property var values: ["cover", "contain"]
                            currentIndex: root.bridge
                                ? Math.max(0, values.indexOf(
                                    root.bridge.background.fill_mode || "cover")) : 0
                            onActivated: if (root.bridge) root.bridge.setBackgroundProperty(
                                "fill_mode", values[currentIndex])
                        }

                        FieldLabel { text: qsTr("Image zoom") }
                        EditorSlider {
                            Layout.fillWidth: true
                            Layout.leftMargin: 16
                            Layout.rightMargin: 16
                            from: 1
                            to: 3
                            value: root.bridge ? Number(root.bridge.background.zoom || 1) : 1
                            onMoved: if (root.bridge) root.bridge.setBackgroundProperty("zoom", value)
                        }
                    }

                    Text {
                        visible: root.bridge && Boolean(root.bridge.background.missing)
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        text: qsTr("The image for this preset could not be found.")
                        color: root.theme ? root.theme.warning : "#e0ad55"
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                    }

                    SoftButton {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        text: qsTr("Restore preset background")
                        enabled: root.bridge !== null
                        onClicked: root.bridge.resetBackground()
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.topMargin: 2
                        Layout.preferredHeight: 1
                        color: root.borderColor
                    }

                    ColumnLayout {
                        objectName: "talkThemeBackgroundColorSection"
                        Layout.fillWidth: true
                        spacing: 7

                        FieldLabel { text: qsTr("Background color") }
                        ColorPickerButton {
                            objectName: "talkThemeBackgroundColorPicker"
                            colorValue: root.bridge
                                ? String(root.bridge.background.base_color || "#11182A")
                                : "#11182A"
                            accessibleName: qsTr("Background color")
                            onClicked: if (root.bridge) root.bridge.chooseBackgroundColor()
                        }
                    }
                    Item { Layout.preferredHeight: 10 }
                }
            }
        }
    }

    component InspectorTab: Rectangle {
        id: tab
        property string value: ""
        property string label: ""
        Layout.fillWidth: true
        Layout.fillHeight: true
        radius: 8
        color: root.currentTab === value ? root.panelColor
              : tabMouse.containsMouse ? root.hoverColor : "transparent"
        Text {
            anchors.centerIn: parent
            text: tab.label
            color: root.currentTab === tab.value ? root.textPrimary : root.textSecondary
            font.pixelSize: 11
            font.weight: root.currentTab === tab.value ? Font.DemiBold : Font.Medium
        }
        MouseArea {
            id: tabMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                if (tab.value !== "background" && root.adjustBackground)
                    root.toggleBackgroundAdjustment()
                root.currentTab = tab.value
            }
        }
    }

    component SectionTitle: Text {
        Layout.fillWidth: true
        Layout.leftMargin: 16
        Layout.rightMargin: 16
        text: ""
        color: root.textPrimary
        font.pixelSize: 13
        font.weight: Font.DemiBold
        elide: Text.ElideRight
    }

    component FieldLabel: Text {
        Layout.fillWidth: true
        Layout.leftMargin: 16
        Layout.rightMargin: 16
        text: ""
        color: root.textSecondary
        font.pixelSize: 10
        font.weight: Font.Medium
    }

    component FieldBackground: Rectangle {
        property bool focused: false
        radius: 9
        color: root.inputColor
        border.width: focused ? 2 : 1
        border.color: focused ? root.accent : root.borderColor
    }

    component ColorPickerButton: Rectangle {
        id: colorPicker
        property string colorValue: "#FFFFFFFF"
        property string accessibleName: ""
        readonly property string displayValue:
            String(colorValue || "#FFFFFFFF").toUpperCase()
        signal clicked()

        Layout.fillWidth: true
        Layout.leftMargin: 16
        Layout.rightMargin: 16
        Layout.preferredHeight: 42
        activeFocusOnTab: true
        radius: 9
        color: colorPickerMouse.containsMouse
            ? root.hoverColor : root.inputColor
        border.width: colorPicker.activeFocus ? 2 : 1
        border.color: colorPicker.activeFocus ? root.accent
            : colorPickerMouse.containsMouse ? root.borderStrong : root.borderColor
        Behavior on color { ColorAnimation { duration: 110 } }
        Behavior on border.color { ColorAnimation { duration: 110 } }
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        Accessible.onPressAction: colorPicker.clicked()

        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                    || event.key === Qt.Key_Space) {
                colorPicker.clicked()
                event.accepted = true
            }
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 8
            anchors.rightMargin: 11
            spacing: 9
            Rectangle {
                Layout.preferredWidth: 26
                Layout.preferredHeight: 26
                radius: 7
                color: colorPicker.displayValue
                border.width: 2
                border.color: root.borderStrong
            }
            Text {
                Layout.fillWidth: true
                text: colorPicker.displayValue
                color: root.textPrimary
                font.pixelSize: 11
                font.weight: Font.Medium
            }
            Text {
                text: qsTr("Change")
                color: root.accent
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
        }
        MouseArea {
            id: colorPickerMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onPressed: colorPicker.forceActiveFocus()
            onClicked: colorPicker.clicked()
        }
    }

    component SoftButton: Button {
        id: button
        property bool primary: false
        implicitHeight: 38
        leftPadding: 12
        rightPadding: 12
        contentItem: Text {
            text: button.text
            color: button.primary ? "#ffffff" : root.textPrimary
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
                : button.hovered ? root.hoverColor : "transparent"
            border.width: button.primary ? 0 : 1
            border.color: root.borderColor
        }
    }

    component EditorCombo: ComboBox {
        id: combo
        implicitHeight: 38
        leftPadding: 10
        rightPadding: 30
        font.pixelSize: 11
        contentItem: Text {
            leftPadding: combo.leftPadding
            rightPadding: combo.rightPadding
            text: combo.displayText
            color: root.textPrimary
            font: combo.font
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        indicator: Text {
            x: combo.width - width - 10
            anchors.verticalCenter: parent.verticalCenter
            text: "⌄"
            color: root.textMuted
            font.pixelSize: 13
        }
        background: FieldBackground { focused: combo.activeFocus }
        delegate: ItemDelegate {
            width: combo.width - 8
            height: 34
            contentItem: Text {
                text: modelData
                color: root.textPrimary
                font.pixelSize: 11
                verticalAlignment: Text.AlignVCenter
                leftPadding: 7
            }
            background: Rectangle {
                radius: 6
                color: parent.highlighted ? root.hoverColor : "transparent"
            }
        }
        popup: Popup {
            y: combo.height + 4
            width: combo.width
            implicitHeight: Math.min(contentItem.implicitHeight + 8, 230)
            padding: 4
            contentItem: ListView {
                clip: true
                implicitHeight: contentHeight
                model: combo.popup.visible ? combo.delegateModel : null
                currentIndex: combo.highlightedIndex
                ScrollIndicator.vertical: ScrollIndicator { }
            }
            background: Rectangle {
                radius: 9
                color: root.panelColor
                border.width: 1
                border.color: root.borderStrong
            }
        }
    }

    component EditorSlider: Slider {
        id: slider
        implicitHeight: 28
        background: Rectangle {
            x: slider.leftPadding
            y: slider.topPadding + slider.availableHeight / 2 - height / 2
            width: slider.availableWidth
            height: 4
            radius: 2
            color: root.borderColor
            Rectangle {
                width: slider.visualPosition * parent.width
                height: parent.height
                radius: 2
                color: root.accent
            }
        }
        handle: Rectangle {
            x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
            y: slider.topPadding + slider.availableHeight / 2 - height / 2
            width: 16
            height: 16
            radius: 8
            color: root.accent
            border.width: 2
            border.color: root.textPrimary
        }
    }

    component StepButton: Button {
        id: step
        Layout.preferredWidth: 30
        Layout.fillHeight: true
        font.pixelSize: 14
        contentItem: Text {
            text: step.text
            color: root.textSecondary
            font: step.font
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 7
            color: step.hovered ? root.hoverColor : "transparent"
        }
    }

    component AlignmentButton: Rectangle {
        id: alignButton
        property string value: "left"
        property string iconName: "align-left"
        Layout.fillWidth: true
        Layout.fillHeight: true
        radius: 7
        color: root.selectedRecord.alignment === value ? root.panelColor
              : alignMouse.containsMouse ? root.hoverColor : "transparent"
        Image {
            anchors.centerIn: parent
            width: 18
            height: 18
            source: "image://talkthemeicons/" + alignButton.iconName + "/18/"
                + root.iconHex(root.selectedRecord.alignment === alignButton.value
                               ? root.accent : root.textMuted)
        }
        MouseArea {
            id: alignMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: if (root.bridge) root.bridge.setLayerProperty(
                root.bridge.selectedLayerId, "alignment", alignButton.value)
        }
    }
}
