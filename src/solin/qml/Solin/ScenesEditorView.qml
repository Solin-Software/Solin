import QtQuick 2.15

Item {
    id: root
    objectName: "scenesEditorView"

    property bool lifecycleActive: true
    readonly property var editorBridge:
        root.lifecycleActive && typeof scenes !== "undefined" ? scenes : null

    function beginShutdown() {
        root.lifecycleActive = false
    }

    Loader {
        anchors.fill: parent
        active: root.lifecycleActive && root.editorBridge !== null
        sourceComponent: ScenesWorkspace {
            bridge: root.editorBridge
            theme: appTheme
        }
    }
}
