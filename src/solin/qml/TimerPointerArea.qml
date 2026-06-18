// TimerPointerArea.qml - MouseArea with Solin's native QQuickWidget cursor bridge.
import QtQuick 2.15

MouseArea {
    id: area

    property bool pointerCursor: true
    readonly property bool pointerActive: enabled && pointerCursor
    property bool _pointerEntered: false

    hoverEnabled: true
    cursorShape: pointerActive ? Qt.PointingHandCursor : Qt.ArrowCursor

    function _hasTimerBridge() {
        return typeof timer !== "undefined"
                && timer !== null
                && timer.pointerEnter !== undefined
                && timer.pointerExit !== undefined
    }

    function _beginPointer() {
        if (_pointerEntered || !pointerActive || !_hasTimerBridge())
            return
        _pointerEntered = true
        timer.pointerEnter()
    }

    function _endPointer() {
        if (!_pointerEntered)
            return
        _pointerEntered = false
        if (_hasTimerBridge())
            timer.pointerExit()
    }

    onEntered: _beginPointer()
    onExited: _endPointer()
    onPointerActiveChanged: {
        if (pointerActive && containsMouse)
            _beginPointer()
        else
            _endPointer()
    }
    Component.onDestruction: _endPointer()
}
