import QtQuick
import QtQuick.Layouts

RowLayout {
    id: root
    required property var spec
    required property var state
    required property int depth
    required property bool expanded
    required property bool collapsible
    required property real headingWidth
    spacing: 10

    Text {
        Layout.fillWidth: true
        text: root.spec.title
        color: appTheme.textPrimary
        font.pixelSize: root.depth > 0 ? 14 : 16
        font.weight: Font.DemiBold
        wrapMode: Text.Wrap
    }
    Text {
        visible: root.collapsible && root.spec.statusKey && text.length > 0
        text: root.state[root.spec.statusKey] || ""
        color: appTheme.textMuted
        font.pixelSize: 13
        elide: Text.ElideRight
        Layout.maximumWidth: Math.min(280, Math.max(100, root.headingWidth * 0.36))
    }
    SettingsIcon {
        visible: root.collapsible
        name: root.expanded ? "up" : "down"
        width: 16
        height: 16
    }
}
