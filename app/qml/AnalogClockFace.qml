// AnalogClockFace.qml — dispatches the idle analog clock to a named face style.
import QtQuick 2.15

Item {
    id: root

    property string style: "signature"
    property real hourAngle: 0
    property real minuteAngle: 0
    property real secondAngle: 0
    property var durationSector: ({})

    Loader {
        id: faceLoader
        anchors.fill: parent
        sourceComponent: root.style === "classic" ? classicFace : signatureFace
    }

    AnalogClockSector {
        anchors.fill: parent
        sector: root.durationSector
    }

    Component {
        id: signatureFace
        AnalogClockSignature {
            hourAngle: root.hourAngle
            minuteAngle: root.minuteAngle
            secondAngle: root.secondAngle
        }
    }

    Component {
        id: classicFace
        AnalogClockClassic {
            hourAngle: root.hourAngle
            minuteAngle: root.minuteAngle
            secondAngle: root.secondAngle
        }
    }
}
