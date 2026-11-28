// AnalogClockFace.qml — dispatches the idle analog clock to a named face style.
// Both faces render in a fixed design space and are scaled into this item, so
// display-size changes do not resize/repaint the Canvas layers.
import QtQuick 2.15

Item {
    id: root

    readonly property int logicalSize: 540
    readonly property real faceScale: Math.max(0.0, Math.min(width, height) / logicalSize)

    property string style: "signature"
    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0
    property var durationSector: ({})

    Loader {
        id: faceLoader
        width: root.logicalSize
        height: root.logicalSize
        anchors.centerIn: parent
        scale: root.faceScale
        transformOrigin: Item.Center
        sourceComponent: root.style === "classic" ? classicFace : signatureFace
    }

    Component {
        id: signatureFace
        AnalogClockSignature {
            width: root.logicalSize
            height: root.logicalSize
            hourAngle: root.hourAngle
            minuteAngle: root.minuteAngle
            secondAngle: root.secondAngle
            durationSector: root.durationSector
        }
    }

    Component {
        id: classicFace
        AnalogClockClassic {
            width: root.logicalSize
            height: root.logicalSize
            hourAngle: root.hourAngle
            minuteAngle: root.minuteAngle
            secondAngle: root.secondAngle
            durationSector: root.durationSector
        }
    }
}
