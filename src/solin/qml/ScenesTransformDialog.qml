pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root
    objectName: "scenesTransformDialog"
    property var bridge: null
    property var theme: null
    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"

    modal: true
    anchors.centerIn: parent
    width: Math.min(460, parent ? parent.width - 30 : 460)
    padding: 18
    standardButtons: Dialog.NoButton

    function loadPercent(field, value) { field.value = Math.round(Number(value || 0) * 10000) }
    function openForLayer() {
        var layer = bridge ? bridge.selectedLayer : ({})
        loadPercent(xField, layer.x)
        loadPercent(yField, layer.y)
        loadPercent(widthField, layer.width)
        loadPercent(heightField, layer.height)
        loadPercent(cropLeftField, layer.crop_left)
        loadPercent(cropTopField, layer.crop_top)
        loadPercent(cropRightField, layer.crop_right)
        loadPercent(cropBottomField, layer.crop_bottom)
        rotationField.value = Math.round(Number(layer.rotation || 0) * 100)
        open()
    }

    function save() {
        if (bridge)
            bridge.applyPreciseTransform(bridge.selectedLayerId, {
                x: xField.value / 10000,
                y: yField.value / 10000,
                width: widthField.value / 10000,
                height: heightField.value / 10000,
                cropLeft: cropLeftField.value / 10000,
                cropTop: cropTopField.value / 10000,
                cropRight: cropRightField.value / 10000,
                cropBottom: cropBottomField.value / 10000,
                rotation: rotationField.value / 100
            })
        close()
    }

    background: Rectangle { color: root.surface; radius: 14; border.width: 1; border.color: root.borderColor }

    contentItem: ColumnLayout {
        spacing: 14
        Text {
            Layout.fillWidth: true
            text: qsTr("Precise transform")
            color: root.textPrimary
            font.pixelSize: 16
            font.weight: Font.DemiBold
        }
        GridLayout {
            Layout.fillWidth: true
            columns: width >= 390 ? 4 : 2
            columnSpacing: 8
            rowSpacing: 8
            LabelText { text: "X" }
            PercentField { id: xField; from: 0; to: 10000 }
            LabelText { text: "Y" }
            PercentField { id: yField; from: 0; to: 10000 }
            LabelText { text: qsTr("Width") }
            PercentField { id: widthField; from: 200; to: 10000 }
            LabelText { text: qsTr("Height") }
            PercentField { id: heightField; from: 200; to: 10000 }
            LabelText { text: qsTr("Crop left") }
            PercentField { id: cropLeftField; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop top") }
            PercentField { id: cropTopField; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop right") }
            PercentField { id: cropRightField; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop bottom") }
            PercentField { id: cropBottomField; from: 0; to: 9900 }
            LabelText { text: qsTr("Rotation") }
            ScenesSpinBox { id: rotationField; theme: root.theme; Layout.fillWidth: true; from: -36000; to: 36000; textFromValue: function(value) { return (value / 100).toFixed(2) + "°" }; valueFromText: function(text) { return Math.round(parseFloat(text) * 100) } }
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
            ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; onClicked: root.save() }
        }
    }

    component LabelText: Text { color: root.textMuted; font.pixelSize: 10; Layout.alignment: Qt.AlignVCenter }
    component PercentField: ScenesSpinBox {
        theme: root.theme
        Layout.fillWidth: true
        editable: true
        stepSize: 50
        textFromValue: function(value) { return (value / 100).toFixed(2) + "%" }
        valueFromText: function(text) { return Math.round(parseFloat(text) * 100) }
    }
}
