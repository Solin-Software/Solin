import QtQuick 2.15

Item {
    id: root
    objectName: "talkThemeEditorView"

    property bool lifecycleActive: true
    readonly property var editorBridge:
        root.lifecycleActive && typeof talkTheme !== "undefined" ? talkTheme : null

    function beginShutdown() {
        root.lifecycleActive = false
    }

    function commitPendingEdits() {
        return workspaceLoader.item ? workspaceLoader.item.commitPendingEdits() : false
    }

    Loader {
        id: workspaceLoader
        anchors.fill: parent
        active: root.lifecycleActive && root.editorBridge !== null
        sourceComponent: TalkThemeWorkspace {
            bridge: root.editorBridge
            theme: appTheme
        }
    }
}
