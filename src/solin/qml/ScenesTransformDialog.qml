pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root
    objectName: "scenesTransformDialog"
    property var bridge: null
    property var theme: null
    readonly property var rectLimits: bridge ? bridge.normalizedRectLimits : null
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

    function openForLayer() {
        var layer = bridge ? bridge.selectedLayer : ({})
        xField.load(layer.x)
        yField.load(layer.y)
        widthField.load(layer.width)
        heightField.load(layer.height)
        cropLeftField.load(layer.crop_left)
        cropTopField.load(layer.crop_top)
        cropRightField.load(layer.crop_right)
        cropBottomField.load(layer.crop_bottom)
        rotationField.load(layer.rotation)
        open()
    }

    function save() {
        if (bridge)
            bridge.applyPreciseTransform(bridge.selectedLayerId, {
                x: xField.valueForSave(),
                y: yField.valueForSave(),
                width: widthField.valueForSave(),
                height: heightField.valueForSave(),
                cropLeft: cropLeftField.valueForSave(),
                cropTop: cropTopField.valueForSave(),
                cropRight: cropRightField.valueForSave(),
                cropBottom: cropBottomField.valueForSave(),
                rotation: rotationField.valueForSave()
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
            PercentField {
                id: xField
                objectName: "scenesTransformX"
                from: root.rectLimits ? Math.round(root.rectLimits.minimumPosition * valueScale) : 0
                to: root.rectLimits ? Math.round(root.rectLimits.maximumPosition * valueScale) : 0
            }
            LabelText { text: "Y" }
            PercentField { id: yField; objectName: "scenesTransformY"; from: xField.from; to: xField.to }
            LabelText { text: qsTr("Width") }
            PercentField {
                id: widthField
                objectName: "scenesTransformWidth"
                from: root.rectLimits ? Math.round(root.rectLimits.minimumSize * valueScale) : 0
                to: root.rectLimits ? Math.round(root.rectLimits.maximumSize * valueScale) : 0
            }
            LabelText { text: qsTr("Height") }
            PercentField { id: heightField; objectName: "scenesTransformHeight"; from: widthField.from; to: widthField.to }
            LabelText { text: qsTr("Crop left") }
            PercentField { id: cropLeftField; objectName: "scenesTransformCropLeft"; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop top") }
            PercentField { id: cropTopField; objectName: "scenesTransformCropTop"; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop right") }
            PercentField { id: cropRightField; objectName: "scenesTransformCropRight"; from: 0; to: 9900 }
            LabelText { text: qsTr("Crop bottom") }
            PercentField { id: cropBottomField; objectName: "scenesTransformCropBottom"; from: 0; to: 9900 }
            LabelText { text: qsTr("Rotation") }
            TransformField {
                id: rotationField
                objectName: "scenesTransformRotation"
                unitText: "°"
                from: -36000
                to: 36000
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
            ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; onClicked: root.save() }
        }
    }

    component LabelText: Text { color: root.textMuted; font.pixelSize: 10; Layout.alignment: Qt.AlignVCenter }
    component TransformField: ScenesSpinBox {
        id: field
        theme: root.theme
        Layout.fillWidth: true
        enabled: root.rectLimits !== null
        property real valueScale: 100
        property real originalValue: 0
        property int loadedValue: 0
        textFromValue: function(value) { return (value / 100).toFixed(2) }
        valueFromText: function(text) { return Math.round(parseFloat(text) * 100) }
        validator: DoubleValidator {
            bottom: field.from / 100
            top: field.to / 100
            decimals: 2
            notation: DoubleValidator.StandardNotation
            locale: "C"
        }

        function load(normalizedValue) {
            originalValue = Number(normalizedValue || 0)
            value = Math.round(originalValue * valueScale)
            loadedValue = value
            contentItem.text = Qt.binding(function() {
                return field.textFromValue(field.value, field.locale)
            })
        }

        function valueForSave() {
            var pending = pendingValue()
            // Display precision must not change an untouched model value.
            return pending === loadedValue ? originalValue : pending / valueScale
        }
    }
    component PercentField: TransformField {
        valueScale: 10000
        unitText: "%"
        stepSize: 50
    }
}
