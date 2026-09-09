import QtQuick

MouseArea {
    id: area
    anchors.fill: parent
    acceptedButtons: Qt.NoButton
    hoverEnabled: true
    cursorShape: parent && parent.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    property bool bridgeEntered: false
    readonly property bool pointerActive: parent && parent.enabled

    function hasBridge() {
        return typeof appPointerCursor !== "undefined"
            && appPointerCursor !== null
            && appPointerCursor.enter !== undefined
            && appPointerCursor.exit !== undefined
    }

    function beginPointer() {
        if (!bridgeEntered && pointerActive && hasBridge()) {
            bridgeEntered = true
            appPointerCursor.enter()
        }
    }

    function endPointer() {
        if (bridgeEntered) {
            bridgeEntered = false
            if (hasBridge())
                appPointerCursor.exit()
        }
    }

    onEntered: beginPointer()
    onExited: endPointer()
    onContainsMouseChanged: {
        if (containsMouse)
            beginPointer()
        else
            endPointer()
    }
    onPointerActiveChanged: {
        if (pointerActive && containsMouse)
            beginPointer()
        else
            endPointer()
    }
    Component.onDestruction: endPointer()
}
