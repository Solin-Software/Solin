pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var spec
    required property var domain
    property int depth: 0
    property var state: domain ? domain.state : ({})
    property bool expanded: !spec.collapsible || settingsNavigation.expanded(spec.id)
    signal choose(var row, var owner)
    visible: !spec.visibleWhen || !!state[spec.visibleWhen]
    implicitHeight: contentColumn.implicitHeight
    objectName: "group_" + spec.id

    Connections {
        target: settingsNavigation
        function onExpansionChanged(group, isExpanded) {
            if (group === root.spec.id)
                root.expanded = !root.spec.collapsible || isExpanded
        }
    }

    function reveal(key) {
        for (let i = 0; i < rows.count; i++) {
            const row = rows.itemAt(i)
            if (row && row.spec.key === key) {
                if (spec.collapsible)
                    settingsNavigation.expand(spec.id, true)
                row.highlighted = true
                return row
            }
        }
        for (let i = 0; i < childGroups.count; i++) {
            const loader = childGroups.itemAt(i)
            const child = loader ? loader.item : null
            if (!child)
                continue
            const row = child.reveal(key)
            if (row) {
                if (spec.collapsible)
                    settingsNavigation.expand(spec.id, true)
                return row
            }
        }
        return null
    }

    Rectangle {
        id: unifiedSurface
        objectName: "groupSurface_" + root.spec.id
        anchors.fill: parent
        visible: root.spec.collapsible
        radius: root.depth > 0 ? 9 : 11
        color: root.depth > 0 ? appTheme.surfaceAlt : appTheme.surfaceChrome
        border.width: 1
        border.color: root.expanded ? appTheme.borderStrong : appTheme.border_
    }

    ColumnLayout {
        id: contentColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: 0

        Loader {
            id: headingLoader
            Layout.fillWidth: true
            Layout.preferredHeight: item ? item.implicitHeight : 0
            sourceComponent: root.spec.collapsible ? collapsibleHeading : staticHeading
        }

        Item {
            id: bodyClip
            objectName: "groupBody_" + root.spec.id
            property real targetHeight: root.expanded ? bodyColumn.implicitHeight : 0
            property real animatedHeight: targetHeight
            Layout.fillWidth: true
            Layout.preferredHeight: animatedHeight
            visible: animatedHeight > 0
            opacity: root.expanded ? 1 : 0
            clip: true

            Behavior on animatedHeight {
                enabled: root.spec.collapsible
                NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
            }
            Behavior on opacity {
                enabled: root.spec.collapsible
                NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
            }

            Rectangle {
                objectName: "groupBodySurface_" + root.spec.id
                anchors.fill: parent
                visible: !root.spec.collapsible
                radius: 11
                color: appTheme.surfaceChrome
                border.width: 1
                border.color: appTheme.border_
            }

            ColumnLayout {
                id: bodyColumn
                width: bodyClip.width
                spacing: 0

                Rectangle {
                    Layout.fillWidth: true
                    Layout.leftMargin: 12
                    Layout.rightMargin: 12
                    Layout.preferredHeight: root.spec.collapsible ? 1 : 0
                    visible: root.spec.collapsible
                    color: appTheme.border_
                    opacity: 0.75
                }

                Repeater {
                    id: rows
                    model: root.spec.rows
                    delegate: SettingsRow {
                        required property var modelData
                        Layout.fillWidth: true
                        spec: modelData
                        domain: root.domain
                        onChoose: (row, owner) => root.choose(row, owner)
                    }
                }

                Text {
                    Layout.fillWidth: true
                    Layout.leftMargin: 12
                    Layout.rightMargin: 12
                    Layout.topMargin: visible ? 8 : 0
                    Layout.bottomMargin: visible ? 8 : 0
                    visible: root.spec.errorKey && text.length > 0
                    text: root.spec.errorKey ? String(root.state[root.spec.errorKey] || "") : ""
                    color: appTheme.dangerText
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                    Accessible.role: Accessible.AlertMessage
                }

                Text {
                    Layout.fillWidth: true
                    Layout.leftMargin: 12
                    Layout.rightMargin: 12
                    Layout.topMargin: visible ? 8 : 0
                    Layout.bottomMargin: visible ? 8 : 0
                    visible: root.spec.feedbackKey && text.length > 0
                    text: root.spec.feedbackKey ? String(root.state[root.spec.feedbackKey] || "") : ""
                    color: appTheme.textSecondary
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                    Accessible.role: Accessible.AlertMessage
                }

                Repeater {
                    id: childGroups
                    model: root.spec.children || []
                    delegate: Loader {
                        id: childLoader
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: 12
                        Layout.rightMargin: 12
                        Layout.topMargin: 8
                        Layout.bottomMargin: 10
                        Layout.preferredHeight: item ? item.implicitHeight : 0
                        Component.onCompleted: setSource("SettingsGroup.qml", {
                            "spec": modelData,
                            "domain": root.domain,
                            "depth": root.depth + 1
                        })
                        Connections {
                            target: childLoader.item
                            ignoreUnknownSignals: true
                            function onChoose(row, owner) { root.choose(row, owner) }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: staticHeading
        Item {
            id: staticControl
            objectName: "groupHeading_" + root.spec.id
            implicitHeight: Math.max(34, staticContent.implicitHeight + 6)
            Accessible.name: root.spec.title
            Accessible.role: Accessible.Heading

            SettingsGroupHeadingContent {
                id: staticContent
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spec: root.spec
                state: root.state
                depth: root.depth
                expanded: root.expanded
                collapsible: false
                headingWidth: staticControl.width
            }
        }
    }

    Component {
        id: collapsibleHeading
        AbstractButton {
            id: heading
            objectName: "groupHeading_" + root.spec.id
            implicitHeight: Math.max(58, headingContent.implicitHeight + 20)
            hoverEnabled: true
            focusPolicy: Qt.StrongFocus
            leftPadding: 12
            rightPadding: 12
            topPadding: 10
            bottomPadding: 10
            Accessible.name: root.spec.title
            Accessible.description: root.spec.statusKey
                ? String(root.state[root.spec.statusKey] || "") : ""
            Accessible.role: Accessible.Button
            AppPointerCursor {}
            onClicked: settingsNavigation.expand(root.spec.id, !root.expanded)
            Keys.onLeftPressed: if (root.expanded)
                settingsNavigation.expand(root.spec.id, false)
            Keys.onRightPressed: if (!root.expanded)
                settingsNavigation.expand(root.spec.id, true)

            contentItem: SettingsGroupHeadingContent {
                id: headingContent
                spec: root.spec
                state: root.state
                depth: root.depth
                expanded: root.expanded
                collapsible: true
                headingWidth: heading.width - heading.leftPadding - heading.rightPadding
            }
            background: Rectangle {
                radius: root.depth > 0 ? 9 : 11
                color: heading.hovered ? appTheme.hover : "transparent"
                border.width: heading.visualFocus ? 2 : 0
                border.color: appTheme.accent
            }
        }
    }
}
