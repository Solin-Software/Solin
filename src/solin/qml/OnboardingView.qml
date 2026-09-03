pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    property var draft: onboardingBridge.state
    property string renderedPage: ""
    property int renderedSession: -1
    property color backgroundColor: appTheme.bg
    property color surfaceColor: appTheme.surface
    property color surfaceChrome: appTheme.surfaceChrome
    property color borderColor: appTheme.borderChrome
    property color textPrimary: appTheme.textPrimary
    property color textSecondary: appTheme.textSecondary
    property color textMuted: appTheme.textMuted
    property color textDim: appTheme.textDim
    property color accent: appTheme.accent
    property color accentTint: appTheme.accentTint

    color: backgroundColor
    focus: true

    function iconSource(name, size, color) {
        return "image://onboardingicons/" + name + "/" + size + "/"
                + String(color).replace("#", "")
    }

    function componentForPage(page) {
        if (page === "profile") return profilePage
        if (page === "preferences") return preferencesPage
        if (page === "integrations") return integrationsPage
        if (page === "obs") return obsPage
        if (page === "zoom") return zoomPage
        return reviewPage
    }

    function resetStack() {
        pageStack.clear()
        renderedPage = draft.currentPage
        renderedSession = draft.sessionRevision
        pageStack.push(componentForPage(renderedPage), {}, StackView.Immediate)
    }

    function syncPage() {
        if (renderedSession !== draft.sessionRevision) {
            resetStack()
            return
        }
        if (renderedPage === draft.currentPage)
            return
        renderedPage = draft.currentPage
        if (draft.direction > 0)
            pageStack.push(componentForPage(renderedPage))
        else if (pageStack.depth > 1)
            pageStack.pop()
        else
            pageStack.replace(componentForPage(renderedPage))
    }

    Component.onCompleted: resetStack()

    Connections {
        target: onboardingBridge
        function onStateChanged() { root.syncPage() }
    }

    Item {
        id: header
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 76

        Text {
            anchors.left: parent.left
            anchors.leftMargin: 36
            anchors.verticalCenter: parent.verticalCenter
            text: "Solin"
            color: root.textPrimary
            font.pixelSize: 20
            font.weight: Font.DemiBold
        }

        Row {
            anchors.right: parent.right
            anchors.rightMargin: 36
            anchors.verticalCenter: parent.verticalCenter
            spacing: 18

            TextButton {
                visible: root.draft.allowCancel
                text: qsTr("Cancel")
                onClicked: onboardingBridge.cancel()
            }

            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("%1 of 4").arg(root.draft.progressStage + 1)
                color: root.textMuted
                font.pixelSize: 12
            }

            Row {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 9

                Repeater {
                    model: 4

                    Rectangle {
                        required property int index
                        width: index === root.draft.progressStage ? 18 : 7
                        height: 7
                        radius: 4
                        color: index === root.draft.progressStage
                               ? root.accent
                               : (index < root.draft.progressStage
                                  ? appTheme.accentMutedHover : appTheme.textDim)

                        Behavior on width {
                            NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
                        }
                        Behavior on color { ColorAnimation { duration: 160 } }
                    }
                }
            }
        }
    }

    StackView {
        id: pageStack
        anchors.top: header.bottom
        anchors.bottom: footer.top
        anchors.left: parent.left
        anchors.right: parent.right
        clip: true

        pushEnter: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "x"
                    from: pageStack.width * 0.055
                    to: 0
                    duration: 220
                    easing.type: Easing.OutCubic
                }
                NumberAnimation {
                    property: "opacity"
                    from: 0
                    to: 1
                    duration: 170
                    easing.type: Easing.OutCubic
                }
            }
        }
        pushExit: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "x"
                    from: 0
                    to: -pageStack.width * 0.025
                    duration: 190
                    easing.type: Easing.OutCubic
                }
                NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 130 }
            }
        }
        popEnter: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "x"
                    from: -pageStack.width * 0.045
                    to: 0
                    duration: 220
                    easing.type: Easing.OutCubic
                }
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 170 }
            }
        }
        popExit: Transition {
            ParallelAnimation {
                NumberAnimation {
                    property: "x"
                    from: 0
                    to: pageStack.width * 0.025
                    duration: 190
                    easing.type: Easing.OutCubic
                }
                NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 130 }
            }
        }
    }

    Item {
        id: footer
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 86

        Item {
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(parent.width - 72, 760)
            height: parent.height

            TextButton {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                visible: root.draft.currentPage !== "profile"
                text: qsTr("Back")
                iconName: "arrow_left"
                onClicked: onboardingBridge.back()
            }

            PrimaryButton {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                enabled: !root.draft.busy
                text: root.draft.currentPage === "review"
                      ? qsTr("Start using Solin") : qsTr("Continue")
                busy: root.draft.busy
                onClicked: onboardingBridge.advance()
            }
        }
    }

    Rectangle {
        id: errorBanner
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: footer.top
        anchors.bottomMargin: 4
        width: Math.min(errorText.implicitWidth + 38, root.width - 80)
        height: visible ? Math.max(42, errorText.implicitHeight + 18) : 0
        radius: 10
        color: appTheme.dangerSubtle
        border.width: 1
        border.color: appTheme.danger
        visible: root.draft.errorText !== ""
        opacity: visible ? 1 : 0

        Behavior on opacity { NumberAnimation { duration: 140 } }

        Text {
            id: errorText
            anchors.centerIn: parent
            width: Math.min(implicitWidth, root.width - 118)
            text: root.draft.errorText
            color: appTheme.dangerText
            font.pixelSize: 11
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }
    }

    LanguageSheet {
        id: languageSheet
        anchors.fill: parent
    }

    CongregationSheet {
        id: congregationSheet
        anchors.fill: parent
    }

    Component {
        id: profilePage

        FocusScope {
            PageColumn {
                width: Math.min(parent.width - 72, 620)
                anchors.centerIn: parent
                title: qsTr("Welcome to Solin")
                subtitle: qsTr("Set up a clean, reliable workspace for meetings and media.")

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 28
                    spacing: 9

                    FieldLabel { text: qsTr("Profile name") }

                    AppTextField {
                        id: profileName
                        Layout.fillWidth: true
                        text: root.draft.profileName
                        placeholderText: qsTr("Example: Main Hall")
                        focus: true
                        onTextEdited: onboardingBridge.updateField("profileName", text)
                        onAccepted: onboardingBridge.advance()
                    }

                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Each profile keeps its own settings and playlists.")
                        color: root.textMuted
                        font.pixelSize: 11
                    }
                }
            }
        }
    }

    Component {
        id: preferencesPage

        FocusScope {
            Flickable {
                anchors.fill: parent
                contentHeight: preferencesContent.implicitHeight + 52
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                PageColumn {
                    id: preferencesContent
                    width: Math.min(parent.width - 72, 700)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 20
                    title: qsTr("Prepare Solin for your setup")
                    subtitle: qsTr("Choose languages and decide how meeting media should be handled.")

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 22
                        spacing: 10

                        LanguageChoice {
                            Layout.fillWidth: true
                            iconName: "interface"
                            title: qsTr("Interface language")
                            value: root.draft.interfaceName
                            onClicked: languageSheet.openSheet("interface", qsTr("Interface language"))
                        }

                        LanguageChoice {
                            Layout.fillWidth: true
                            iconName: "media_language"
                            title: qsTr("Media language")
                            value: root.draft.mediaName
                            onClicked: languageSheet.openSheet("media", qsTr("Media language"))
                        }

                        FieldLabel {
                            Layout.topMargin: 18
                            text: qsTr("Meeting schedule")
                        }

                        LanguageChoice {
                            Layout.fillWidth: true
                            iconName: "calendar"
                            title: qsTr("Congregation")
                            value: root.draft.congregationScheduleText !== ""
                                   ? root.draft.congregationScheduleText
                                   : qsTr("Not configured")
                            onClicked: congregationSheet.openSheet()
                        }

                        Text {
                            Layout.fillWidth: true
                            text: root.draft.congregationName !== ""
                                  ? root.draft.congregationName
                                  : qsTr("Find your congregation to fill the meeting days and times from jw.org.")
                            color: root.textMuted
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }

                        FieldLabel {
                            Layout.topMargin: 18
                            text: qsTr("Meeting media")
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            CompactChoice {
                                Layout.fillWidth: true
                                selected: root.draft.downloadMeetingMedia
                                iconName: "auto_download"
                                title: qsTr("Download automatically")
                                subtitle: qsTr("Keep this week and next week available offline.")
                                onClicked: onboardingBridge.updateField("downloadMeetingMedia", true)
                            }

                            CompactChoice {
                                Layout.fillWidth: true
                                selected: !root.draft.downloadMeetingMedia
                                iconName: "manual_download"
                                title: qsTr("Manual download")
                                subtitle: qsTr("Only download media when you click the cloud button.")
                                onClicked: onboardingBridge.updateField("downloadMeetingMedia", false)
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: integrationsPage

        FocusScope {
            PageColumn {
                width: Math.min(parent.width - 72, 760)
                anchors.centerIn: parent
                title: qsTr("Choose how Solin should help")
                subtitle: qsTr("Choose one option, both, or set them up later.")

                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 28
                    spacing: 12

                    IntegrationChoice {
                        Layout.fillWidth: true
                        selected: root.draft.obsSelected
                        iconName: "obs"
                        title: "OBS Studio"
                        subtitle: qsTr("Control scenes and cameras from Solin.")
                        onClicked: onboardingBridge.updateField("obsSelected", !selected)
                    }

                    IntegrationChoice {
                        Layout.fillWidth: true
                        selected: root.draft.zoomSelected
                        enabled: root.draft.zoomAvailable
                        iconName: "zoom"
                        title: "Zoom Meetings"
                        subtitle: root.draft.zoomAvailable
                                  ? qsTr("Share your screen automatically.")
                                  : root.draft.zoomUnavailableReason
                        onClicked: onboardingBridge.updateField("zoomSelected", !selected)
                    }
                }

                TextButton {
                    Layout.alignment: Qt.AlignHCenter
                    Layout.topMargin: 18
                    text: qsTr("Set up later")
                    visible: root.draft.obsSelected || root.draft.zoomSelected
                    onClicked: {
                        onboardingBridge.updateField("obsSelected", false)
                        onboardingBridge.updateField("zoomSelected", false)
                    }
                }
            }
        }
    }

    Component {
        id: obsPage

        FocusScope {
            Flickable {
                anchors.fill: parent
                contentHeight: obsContent.implicitHeight + 52
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                PageColumn {
                    id: obsContent
                    width: Math.min(parent.width - 72, 700)
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 14
                    title: qsTr("Connect OBS Studio")
                    subtitle: qsTr("Use OBS scenes and cameras from Solin. Automatic switching is optional.")

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 20
                        spacing: 12

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 7
                                FieldLabel { text: qsTr("WebSocket port") }
                                AppTextField {
                                    Layout.fillWidth: true
                                    text: root.draft.obsPort
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    onTextEdited: onboardingBridge.updateField("obsPort", text)
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 7
                                FieldLabel { text: qsTr("Password (optional)") }
                                AppTextField {
                                    Layout.fillWidth: true
                                    text: root.draft.obsPassword
                                    echoMode: TextInput.Password
                                    onTextEdited: onboardingBridge.updateField("obsPassword", text)
                                }
                            }

                            SecondaryButton {
                                Layout.alignment: Qt.AlignBottom
                                text: root.draft.obsState === "connecting"
                                      ? qsTr("Connecting…") : qsTr("Connect")
                                enabled: root.draft.obsState !== "connecting"
                                onClicked: onboardingBridge.testObsConnection()
                            }
                        }

                        StatusLine {
                            Layout.fillWidth: true
                            stateKey: root.draft.obsState
                            text: root.draft.obsStatusText
                        }

                        TextButton {
                            Layout.alignment: Qt.AlignLeft
                            Layout.topMargin: -6
                            iconName: "book"
                            text: qsTr("Need help setting up OBS?")
                            onClicked: onboardingBridge.openObsSetupGuide()
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: autoContent.implicitHeight + 28
                            visible: root.draft.obsConnected
                            radius: 12
                            color: root.surfaceColor
                            border.width: 1
                            border.color: root.draft.obsAutomatic ? root.accent : appTheme.border

                            ColumnLayout {
                                id: autoContent
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 14
                                spacing: 10

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 12

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 3
                                        Text {
                                            text: qsTr("Automatic scene switching")
                                            color: root.textPrimary
                                            font.pixelSize: 13
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: root.draft.obsAutomatic
                                                  ? qsTr("Solin switches scenes while media is projected.")
                                                  : qsTr("Manual scene and camera controls remain available.")
                                            color: root.textMuted
                                            font.pixelSize: 11
                                            wrapMode: Text.Wrap
                                        }
                                    }

                                    AppToggle {
                                        checked: root.draft.obsAutomatic
                                        onToggled: checked => onboardingBridge.updateField("obsAutomatic", checked)
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 10
                                    visible: root.draft.obsAutomatic

                                    SceneChoice {
                                        Layout.fillWidth: true
                                        label: qsTr("Default scene")
                                        value: root.draft.obsDefaultScene
                                        model: root.draft.obsScenes
                                        onSelected: value => onboardingBridge.updateField("obsDefaultScene", value)
                                    }

                                    SceneChoice {
                                        Layout.fillWidth: true
                                        label: qsTr("Media scene")
                                        value: root.draft.obsMediaScene
                                        model: root.draft.obsScenes
                                        onSelected: value => onboardingBridge.updateField("obsMediaScene", value)
                                    }
                                }
                            }
                        }

                        TextButton {
                            Layout.alignment: Qt.AlignHCenter
                            Layout.topMargin: 4
                            text: qsTr("Set up OBS later")
                            onClicked: onboardingBridge.skipCurrentIntegration()
                        }
                    }
                }
            }
        }
    }

    Component {
        id: zoomPage

        FocusScope {
            PageColumn {
                width: Math.min(parent.width - 72, 700)
                anchors.centerIn: parent
                title: qsTr("Set up Zoom sharing")
                subtitle: qsTr("Record the share shortcut and choose the click target Solin should use.")

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 22
                    spacing: 10

                    SetupRow {
                        Layout.fillWidth: true
                        step: "1"
                        title: qsTr("Share shortcut")
                        subtitle: root.draft.zoomHotkey === ""
                                  ? qsTr("Click here, then press Zoom's start/stop sharing shortcut.")
                                  : root.draft.zoomHotkey
                        complete: root.draft.zoomHotkey !== ""

                        FocusScope {
                            id: hotkeyCapture

                            property bool recording: false

                            width: 148
                            height: 42
                            activeFocusOnTab: true
                            onActiveFocusChanged: {
                                if (!activeFocus)
                                    recording = false
                            }

                            Rectangle {
                                anchors.fill: parent
                                radius: 9
                                color: hotkeyCapture.recording ? root.accentTint : appTheme.surfaceAlt
                                border.width: 1
                                border.color: hotkeyCapture.recording ? root.accent : appTheme.border
                            }
                            Text {
                                anchors.centerIn: parent
                                text: hotkeyCapture.recording
                                      ? qsTr("Press shortcut")
                                      : (root.draft.zoomHotkey || qsTr("Record shortcut"))
                                color: hotkeyCapture.recording ? appTheme.accentText : root.textSecondary
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    hotkeyCapture.recording = true
                                    hotkeyCapture.forceActiveFocus()
                                }
                            }
                            Keys.onPressed: event => {
                                if (!hotkeyCapture.recording) {
                                    event.accepted = false
                                } else {
                                    if (onboardingBridge.captureHotkey(event.key, event.modifiers)) {
                                        hotkeyCapture.recording = false
                                        root.forceActiveFocus()
                                    }
                                    event.accepted = true
                                }
                            }
                        }
                    }

                    SetupRow {
                        Layout.fillWidth: true
                        step: "2"
                        title: qsTr("Share target")
                        subtitle: root.draft.zoomTargetConfigured
                                  ? qsTr("Target configured")
                                  : qsTr("Choose the click position on your primary monitor.")
                        complete: root.draft.zoomTargetConfigured

                        SecondaryButton {
                            width: implicitWidth
                            height: implicitHeight
                            text: root.draft.zoomTargetConfigured
                                  ? qsTr("Choose again") : qsTr("Choose target")
                            enabled: root.draft.zoomAvailable
                            onClicked: onboardingBridge.configureZoomTarget()
                        }
                    }

                    TextButton {
                        Layout.alignment: Qt.AlignLeft
                        Layout.topMargin: 2
                        iconName: "book"
                        text: qsTr("Need help setting up Zoom?")
                        onClicked: onboardingBridge.openZoomSetupGuide()
                    }

                    TextButton {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.topMargin: 10
                        text: qsTr("Set up Zoom later")
                        onClicked: onboardingBridge.skipCurrentIntegration()
                    }
                }
            }
        }
    }

    Component {
        id: reviewPage

        FocusScope {
            PageColumn {
                width: Math.min(parent.width - 72, 680)
                anchors.centerIn: parent
                title: qsTr("Everything looks ready")
                subtitle: qsTr("Review your choices. You can change them later in Settings.")

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 24
                    spacing: 0

                    ReviewLine {
                        iconName: "interface"
                        title: qsTr("Languages")
                        value: root.draft.interfaceName + "  ·  " + root.draft.mediaName
                    }
                    ReviewLine {
                        iconName: "auto_download"
                        title: qsTr("Meeting media")
                        value: root.draft.downloadMeetingMedia
                               ? qsTr("This week and next week") : qsTr("Manual download")
                    }
                    ReviewLine {
                        iconName: "calendar"
                        title: qsTr("Meeting schedule")
                        value: root.draft.congregationScheduleText !== ""
                               ? root.draft.congregationScheduleText
                               : qsTr("Not configured")
                    }
                    ReviewLine {
                        iconName: "obs"
                        title: "OBS Studio"
                        value: !root.draft.obsSelected ? qsTr("Not configured")
                               : (root.draft.obsAutomatic
                                  ? qsTr("Connected · automatic scene switching")
                                  : qsTr("Connected · manual controls"))
                    }
                    ReviewLine {
                        iconName: "zoom"
                        title: "Zoom Meetings"
                        value: root.draft.zoomSelected
                               ? qsTr("Automatic sharing ready") : qsTr("Not configured")
                        last: true
                    }
                }
            }
        }
    }

    component PageColumn: ColumnLayout {
        property string title: ""
        property string subtitle: ""
        spacing: 8

        Text {
            Layout.fillWidth: true
            text: parent.title
            color: root.textPrimary
            font.pixelSize: 28
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }

        Text {
            Layout.fillWidth: true
            text: parent.subtitle
            color: root.textMuted
            font.pixelSize: 13
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }
    }

    component FieldLabel: Text {
        color: root.textSecondary
        font.pixelSize: 11
        font.weight: Font.DemiBold
    }

    component AppTextField: TextField {
        id: field
        implicitHeight: 44
        color: root.textPrimary
        placeholderTextColor: root.textDim
        selectByMouse: true
        leftPadding: 13
        rightPadding: 13
        font.pixelSize: 13

        background: Rectangle {
            radius: 9
            color: field.activeFocus ? appTheme.surfaceInputFocus : root.surfaceColor
            border.width: 1
            border.color: field.activeFocus ? root.accent : appTheme.border
            Behavior on border.color { ColorAnimation { duration: 120 } }
        }
    }

    component TextButton: Item {
        id: textButton
        property string text: ""
        property string iconName: ""
        signal clicked()
        implicitWidth: textRow.implicitWidth + 12
        implicitHeight: 38
        opacity: enabled ? 1 : 0.4
        Accessible.name: text
        Accessible.role: Accessible.Button

        Row {
            id: textRow
            anchors.centerIn: parent
            spacing: 7

            Image {
                anchors.verticalCenter: parent.verticalCenter
                visible: textButton.iconName !== ""
                width: 14
                height: 14
                source: root.iconSource(textButton.iconName, 14, root.textMuted)
                sourceSize: Qt.size(14, 14)
            }
            Text {
                text: textButton.text
                color: textMouse.containsMouse ? root.textPrimary : root.textMuted
                font.pixelSize: 12
                font.weight: Font.Medium
                Behavior on color { ColorAnimation { duration: 110 } }
            }
        }
        MouseArea {
            id: textMouse
            anchors.fill: parent
            enabled: textButton.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: textButton.clicked()
        }
    }

    component PrimaryButton: Rectangle {
        id: primary
        property string text: ""
        property bool busy: false
        signal clicked()
        implicitWidth: Math.max(126, primaryText.implicitWidth + 34)
        implicitHeight: 44
        radius: 11
        color: !enabled ? appTheme.accentMuted
              : primaryMouse.pressed ? appTheme.accentPressed
              : primaryMouse.containsMouse ? appTheme.accentHover : root.accent
        opacity: enabled ? 1 : 0.65
        Accessible.name: text
        Accessible.role: Accessible.Button

        Behavior on color { ColorAnimation { duration: 110 } }

        Text {
            id: primaryText
            anchors.centerIn: parent
            text: primary.busy ? qsTr("Finishing…") : primary.text
            color: appTheme.textOnAccent
            font.pixelSize: 13
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: primaryMouse
            anchors.fill: parent
            enabled: primary.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: primary.clicked()
        }
    }

    component SecondaryButton: Rectangle {
        id: secondary
        property string text: ""
        signal clicked()
        implicitWidth: Math.max(116, secondaryText.implicitWidth + 26)
        implicitHeight: 42
        radius: 9
        color: secondaryMouse.containsMouse && enabled ? appTheme.hover : root.surfaceColor
        border.width: 1
        border.color: enabled ? appTheme.borderChrome : appTheme.border
        opacity: enabled ? 1 : 0.45
        Accessible.name: text
        Accessible.role: Accessible.Button

        Text {
            id: secondaryText
            anchors.centerIn: parent
            text: secondary.text
            color: root.textSecondary
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: secondaryMouse
            anchors.fill: parent
            enabled: secondary.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: secondary.clicked()
        }
    }

    component IntegrationChoice: Rectangle {
        id: integration
        property bool selected: false
        property string iconName: ""
        property string title: ""
        property string subtitle: ""
        signal clicked()
        Layout.preferredHeight: 126
        radius: 13
        color: selected ? root.accentTint
              : (integrationMouse.containsMouse ? appTheme.hover : root.surfaceColor)
        border.width: 1
        border.color: selected ? root.accent : appTheme.borderChrome
        opacity: enabled ? 1 : 0.48
        Accessible.name: title
        Accessible.description: subtitle
        Accessible.role: Accessible.CheckBox
        Accessible.checked: selected

        Behavior on color { ColorAnimation { duration: 150 } }
        Behavior on border.color { ColorAnimation { duration: 150 } }

        RowLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 14

            Rectangle {
                Layout.preferredWidth: 48
                Layout.preferredHeight: 48
                radius: 24
                color: integration.selected ? appTheme.accentMuted : appTheme.surfaceAlt

                Image {
                    anchors.centerIn: parent
                    width: 23
                    height: 23
                    source: root.iconSource(
                        integration.iconName,
                        23,
                        integration.selected ? appTheme.accentText : root.textSecondary
                    )
                    sourceSize: Qt.size(23, 23)
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                Text {
                    Layout.fillWidth: true
                    text: integration.title
                    color: root.textPrimary
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: integration.subtitle
                    color: root.textMuted
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }

            Rectangle {
                Layout.preferredWidth: 24
                Layout.preferredHeight: 24
                radius: 12
                color: integration.selected ? root.accent : "transparent"
                border.width: 1.5
                border.color: integration.selected ? root.accent : root.textDim

                Text {
                    anchors.centerIn: parent
                    visible: integration.selected
                    text: "✓"
                    color: appTheme.white
                    font.pixelSize: 14
                    font.weight: Font.Bold
                }
            }
        }

        MouseArea {
            id: integrationMouse
            anchors.fill: parent
            enabled: integration.enabled
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: integration.clicked()
        }
    }

    component LanguageChoice: Rectangle {
        id: languageChoice
        property string iconName: ""
        property string title: ""
        property string value: ""
        signal clicked()
        implicitHeight: 58
        radius: 11
        color: languageMouse.containsMouse ? appTheme.hover : root.surfaceColor
        border.width: 1
        border.color: languageMouse.containsMouse ? appTheme.borderChrome : appTheme.border

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14
            spacing: 12
            Image {
                Layout.preferredWidth: 18
                Layout.preferredHeight: 18
                source: root.iconSource(languageChoice.iconName, 18, root.textMuted)
                sourceSize: Qt.size(18, 18)
            }
            Text {
                Layout.fillWidth: true
                text: languageChoice.title
                color: root.textSecondary
                font.pixelSize: 12
            }
            Text {
                text: languageChoice.value
                color: root.textPrimary
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Text { text: "›"; color: root.textMuted; font.pixelSize: 20 }
        }
        MouseArea {
            id: languageMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: languageChoice.clicked()
        }
    }

    component CompactChoice: Rectangle {
        id: compact
        property bool selected: false
        property string iconName: ""
        property string title: ""
        property string subtitle: ""
        signal clicked()
        Layout.preferredHeight: 86
        radius: 11
        color: selected ? root.accentTint : root.surfaceColor
        border.width: 1
        border.color: selected ? root.accent : appTheme.border

        RowLayout {
            anchors.fill: parent
            anchors.margins: 13
            spacing: 11
            Image {
                Layout.preferredWidth: 19
                Layout.preferredHeight: 19
                source: root.iconSource(
                    compact.iconName, 19, compact.selected ? appTheme.accentText : root.textMuted
                )
                sourceSize: Qt.size(19, 19)
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: compact.title
                    color: root.textPrimary
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: compact.subtitle
                    color: root.textMuted
                    font.pixelSize: 10
                    wrapMode: Text.Wrap
                }
            }
            Rectangle {
                Layout.preferredWidth: 18
                Layout.preferredHeight: 18
                radius: 9
                color: compact.selected ? root.accent : "transparent"
                border.width: 1
                border.color: compact.selected ? root.accent : root.textDim
                Text {
                    anchors.centerIn: parent
                    visible: compact.selected
                    text: "✓"
                    color: appTheme.white
                    font.pixelSize: 11
                    font.weight: Font.Bold
                }
            }
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: compact.clicked()
        }
    }

    component StatusLine: Rectangle {
        property string stateKey: "idle"
        property alias text: statusText.text
        implicitHeight: 42
        radius: 9
        color: stateKey === "connected" ? appTheme.successSurface
             : stateKey === "error" ? appTheme.dangerSubtle : appTheme.surfaceAlt

        Row {
            anchors.left: parent.left
            anchors.leftMargin: 13
            anchors.verticalCenter: parent.verticalCenter
            spacing: 9
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: 7
                height: 7
                radius: 4
                color: stateKey === "connected" ? appTheme.success
                     : stateKey === "connecting" ? appTheme.warning
                     : stateKey === "error" ? appTheme.danger : root.textDim
            }
            Text {
                id: statusText
                anchors.verticalCenter: parent.verticalCenter
                color: stateKey === "connected" ? appTheme.success : root.textMuted
                font.pixelSize: 11
                font.weight: Font.Medium
            }
        }
    }

    component AppToggle: Rectangle {
        id: toggle
        property bool checked: false
        signal toggled(bool checked)
        implicitWidth: 42
        implicitHeight: 24
        radius: 12
        color: checked ? root.accent : appTheme.borderChrome

        Behavior on color { ColorAnimation { duration: 130 } }
        Rectangle {
            width: 18
            height: 18
            radius: 9
            y: 3
            x: toggle.checked ? toggle.width - width - 3 : 3
            color: appTheme.white
            Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: toggle.toggled(!toggle.checked)
        }
    }

    component SceneChoice: ColumnLayout {
        id: sceneChoice
        property string label: ""
        property string value: ""
        property var model: []
        readonly property int popupScrollbarGutter: 14
        signal selected(string value)
        spacing: 6

        FieldLabel { text: sceneChoice.label }
        ComboBox {
            id: sceneCombo
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            model: sceneChoice.model
            currentIndex: sceneChoice.model.indexOf(sceneChoice.value)
            displayText: sceneChoice.value || qsTr("Choose a scene")
            hoverEnabled: true
            onActivated: index => {
                if (index >= 0)
                    sceneChoice.selected(String(sceneChoice.model[index]))
            }
            contentItem: Text {
                leftPadding: 13
                rightPadding: 38
                text: sceneCombo.displayText
                color: root.textPrimary
                font.pixelSize: 11
                font.weight: Font.Medium
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            indicator: Image {
                x: sceneCombo.width - width - 13
                y: Math.round((sceneCombo.height - height) / 2)
                width: 15
                height: 15
                source: root.iconSource(
                    "chevron_down",
                    15,
                    sceneCombo.popup.visible ? root.accent : root.textMuted
                )
                rotation: sceneCombo.popup.visible ? 180 : 0
                opacity: sceneCombo.enabled ? 1.0 : 0.45

                Behavior on rotation { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }
            background: Rectangle {
                radius: 10
                color: sceneCombo.popup.visible || sceneCombo.hovered
                       ? appTheme.surfaceAlt : root.surfaceChrome
                border.width: 1
                border.color: sceneCombo.popup.visible || sceneCombo.activeFocus
                              ? root.accent : appTheme.border

                Behavior on color { ColorAnimation { duration: 140 } }
                Behavior on border.color { ColorAnimation { duration: 140 } }
            }
            delegate: ItemDelegate {
                id: sceneOption

                required property int index
                required property var modelData

                width: Math.max(
                    0,
                    sceneCombo.width - 12 - sceneChoice.popupScrollbarGutter
                )
                height: 38
                leftPadding: 10
                rightPadding: 10
                topPadding: 0
                bottomPadding: 0
                hoverEnabled: true
                highlighted: sceneCombo.highlightedIndex === index

                property bool selectedScene: sceneChoice.value === String(modelData)

                background: Rectangle {
                    radius: 9
                    color: sceneOption.selectedScene
                           ? root.accentTint
                           : (sceneOption.highlighted || sceneOption.hovered
                              ? appTheme.hover : "transparent")
                    border.width: sceneOption.selectedScene ? 1 : 0
                    border.color: sceneOption.selectedScene ? root.accent : "transparent"

                    Behavior on color { ColorAnimation { duration: 110 } }
                }
                contentItem: RowLayout {
                    spacing: 9
                    Rectangle {
                        Layout.preferredWidth: 6
                        Layout.preferredHeight: 6
                        radius: 3
                        color: sceneOption.selectedScene ? root.accent : "transparent"
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(sceneOption.modelData)
                        color: sceneOption.selectedScene ? root.textPrimary : root.textSecondary
                        font.pixelSize: 11
                        font.weight: sceneOption.selectedScene ? Font.DemiBold : Font.Medium
                        elide: Text.ElideRight
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
            popup: Popup {
                id: scenePopup

                y: sceneCombo.height + 8
                width: sceneCombo.width
                implicitHeight: Math.min(sceneList.contentHeight + 12, 224)
                padding: 6
                modal: false
                focus: true
                closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent

                enter: Transition {
                    NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 120 }
                    NumberAnimation { property: "scale"; from: 0.98; to: 1.0; duration: 140; easing.type: Easing.OutCubic }
                }
                exit: Transition {
                    NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90 }
                    NumberAnimation { property: "scale"; from: 1.0; to: 0.98; duration: 90; easing.type: Easing.InCubic }
                }
                background: Rectangle {
                    radius: 13
                    color: root.surfaceColor
                    border.width: 1
                    border.color: appTheme.borderChrome
                }
                contentItem: ListView {
                    id: sceneList

                    clip: true
                    implicitHeight: Math.min(contentHeight, 212)
                    model: scenePopup.visible ? sceneCombo.delegateModel : null
                    currentIndex: sceneCombo.highlightedIndex
                    boundsBehavior: Flickable.StopAtBounds
                    rightMargin: sceneChoice.popupScrollbarGutter
                    spacing: 4

                    ScrollBar.vertical: ScrollBar {
                        id: sceneScrollBar

                        policy: ScrollBar.AsNeeded
                        width: 8
                        contentItem: Rectangle {
                            implicitWidth: 4
                            radius: 2
                            color: (sceneScrollBar.active || sceneScrollBar.hovered)
                                   ? root.textMuted : root.borderColor
                            opacity: (sceneScrollBar.active
                                      || sceneScrollBar.hovered
                                      || sceneList.moving) ? 1.0 : 0.35
                            anchors.horizontalCenter: parent.horizontalCenter

                            Behavior on color { ColorAnimation { duration: 150 } }
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                        }
                    }
                }
            }
        }
    }

    component SetupRow: Rectangle {
        id: setup
        property string step: ""
        property string title: ""
        property string subtitle: ""
        property bool complete: false
        default property alias trailing: trailingItem.data
        implicitHeight: 82
        radius: 11
        color: root.surfaceColor
        border.width: 1
        border.color: complete ? appTheme.successBorder : appTheme.border

        RowLayout {
            anchors.fill: parent
            anchors.margins: 13
            spacing: 12
            Rectangle {
                Layout.preferredWidth: 30
                Layout.preferredHeight: 30
                radius: 15
                color: setup.complete ? appTheme.successSurface : appTheme.surfaceAlt
                border.width: 1
                border.color: setup.complete ? appTheme.successBorder : appTheme.borderChrome
                Text {
                    anchors.centerIn: parent
                    text: setup.complete ? "✓" : setup.step
                    color: setup.complete ? appTheme.success : root.textMuted
                    font.pixelSize: 11
                    font.weight: Font.Bold
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: setup.title
                    color: root.textPrimary
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: setup.subtitle
                    color: setup.complete ? appTheme.success : root.textMuted
                    font.pixelSize: 10
                    wrapMode: Text.Wrap
                }
            }
            Item {
                id: trailingItem
                Layout.preferredWidth: Math.max(0, childrenRect.width)
                Layout.preferredHeight: Math.max(42, childrenRect.height)
            }
        }
    }

    component ReviewLine: Rectangle {
        id: review
        property string iconName: ""
        property string title: ""
        property string value: ""
        property bool last: false
        Layout.fillWidth: true
        Layout.preferredHeight: 58
        color: "transparent"

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: appTheme.border
            visible: !review.last
        }
        RowLayout {
            anchors.fill: parent
            spacing: 12
            Image {
                Layout.preferredWidth: 18
                Layout.preferredHeight: 18
                source: root.iconSource(review.iconName, 18, root.textMuted)
                sourceSize: Qt.size(18, 18)
            }
            Text {
                Layout.preferredWidth: 150
                text: review.title
                color: root.textSecondary
                font.pixelSize: 11
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: review.value
                color: root.textPrimary
                font.pixelSize: 11
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideRight
            }
        }
    }

    component CongregationSheet: Rectangle {
        id: congSheet
        visible: false
        color: Qt.rgba(0, 0, 0, 0.58)
        z: 100

        function openSheet() {
            congSearch.text = root.draft.congregationQuery
            visible = true
            congSearch.forceActiveFocus()
        }

        Timer {
            id: congDebounce
            interval: 350
            onTriggered: onboardingBridge.searchCongregation(congSearch.text)
        }

        MouseArea { anchors.fill: parent; onClicked: congSheet.visible = false }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - 72, 520)
            // Hugs its content so a short result list leaves no dead space.
            height: Math.min(parent.height - 72, congregationColumn.implicitHeight + 36)
            radius: 14
            color: appTheme.surface2
            border.width: 1
            border.color: appTheme.borderChrome

            MouseArea { anchors.fill: parent }

            ColumnLayout {
                id: congregationColumn
                anchors.fill: parent
                anchors.margins: 18
                spacing: 12

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Congregation")
                        color: root.textPrimary
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }
                    TextButton {
                        text: qsTr("Close")
                        onClicked: congSheet.visible = false
                    }
                }

                AppTextField {
                    id: congSearch
                    Layout.fillWidth: true
                    placeholderText: qsTr("Search by congregation name")
                    onTextEdited: congDebounce.restart()
                    onAccepted: onboardingBridge.searchCongregation(text)
                }

                Text {
                    Layout.fillWidth: true
                    text: root.draft.congregationScheduleText !== ""
                          ? root.draft.congregationScheduleText
                          : root.draft.congregationStatusText
                    visible: text !== ""
                    color: root.textMuted
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }

                ListView {
                    id: congList
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(contentHeight, 372)
                    visible: count > 0
                    clip: true
                    spacing: 4
                    boundsBehavior: Flickable.StopAtBounds
                    flickableDirection: Flickable.VerticalFlick
                    model: root.draft.congregationSuggestions
                    rightMargin: 14
                    ScrollBar.vertical: ScrollBar {
                        id: congScrollBar
                        policy: ScrollBar.AsNeeded
                        width: 8
                        contentItem: Rectangle {
                            implicitWidth: 4
                            radius: 2
                            color: (congScrollBar.active || congScrollBar.hovered)
                                   ? root.textMuted : root.borderColor
                            opacity: (congScrollBar.active
                                      || congScrollBar.hovered
                                      || congList.moving) ? 1.0 : 0.35
                            anchors.horizontalCenter: parent.horizontalCenter

                            Behavior on color { ColorAnimation { duration: 150 } }
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                        }
                    }

                    delegate: Rectangle {
                        id: congItem

                        required property var modelData

                        width: Math.max(0, congList.width - congList.rightMargin)
                        height: 48
                        radius: 8
                        color: congHover.hovered ? appTheme.hover : "transparent"

                        Text {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 18
                            verticalAlignment: Text.AlignVCenter
                            text: congItem.modelData.formattedName || congItem.modelData.name
                            color: root.textPrimary
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }

                        HoverHandler {
                            id: congHover
                            cursorShape: Qt.PointingHandCursor
                        }

                        TapHandler {
                            gesturePolicy: TapHandler.ReleaseWithinBounds
                            onTapped: {
                                onboardingBridge.chooseCongregation(
                                    congItem.modelData.guid,
                                    congItem.modelData.name
                                )
                                congSheet.visible = false
                            }
                        }
                    }
                }

                TextButton {
                    Layout.alignment: Qt.AlignLeft
                    visible: root.draft.congregationName !== ""
                    text: qsTr("Remove congregation")
                    onClicked: {
                        onboardingBridge.clearCongregation()
                        congSearch.text = ""
                    }
                }
            }
        }
    }

    component LanguageSheet: Rectangle {
        id: sheet
        property string kind: "interface"
        property string title: ""
        property var allItems: kind === "interface"
                               ? onboardingBridge.interfaceLanguages
                               : onboardingBridge.mediaLanguages
        visible: false
        color: Qt.rgba(0, 0, 0, 0.58)
        z: 100

        function openSheet(targetKind, targetTitle) {
            kind = targetKind
            title = targetTitle
            search.text = ""
            visible = true
            search.forceActiveFocus()
        }

        function filteredItems() {
            var query = search.text.trim().toLowerCase()
            if (query === "") return allItems
            var result = []
            for (var i = 0; i < allItems.length; ++i) {
                var item = allItems[i]
                var haystack = String(item.name || "") + " "
                             + String(item.secondary || "") + " "
                             + String(item.code || "")
                if (haystack.toLowerCase().indexOf(query) !== -1)
                    result.push(item)
            }
            return result
        }

        MouseArea { anchors.fill: parent; onClicked: sheet.visible = false }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(parent.width - 72, 520)
            height: Math.min(parent.height - 72, 520)
            radius: 14
            color: appTheme.surface2
            border.width: 1
            border.color: appTheme.borderChrome

            MouseArea { anchors.fill: parent }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 18
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true
                        text: sheet.title
                        color: root.textPrimary
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }
                    TextButton {
                        text: qsTr("Close")
                        onClicked: sheet.visible = false
                    }
                }
                AppTextField {
                    id: search
                    Layout.fillWidth: true
                    placeholderText: qsTr("Search languages")
                }
                ListView {
                    id: languageList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 4
                    boundsBehavior: Flickable.StopAtBounds
                    flickableDirection: Flickable.VerticalFlick
                    model: sheet.filteredItems()
                    rightMargin: 14
                    ScrollBar.vertical: ScrollBar {
                        id: languageScrollBar
                        policy: ScrollBar.AsNeeded
                        width: 8
                        contentItem: Rectangle {
                            implicitWidth: 4
                            radius: 2
                            color: (languageScrollBar.active || languageScrollBar.hovered)
                                   ? root.textMuted : root.borderColor
                            opacity: (languageScrollBar.active
                                      || languageScrollBar.hovered
                                      || languageList.moving) ? 1.0 : 0.35
                            anchors.horizontalCenter: parent.horizontalCenter

                            Behavior on color { ColorAnimation { duration: 150 } }
                            Behavior on opacity { NumberAnimation { duration: 180 } }
                        }
                    }

                    delegate: Rectangle {
                        id: languageItem

                        required property var modelData

                        width: Math.max(0, languageList.width - languageList.rightMargin)
                        height: 58
                        radius: 8
                        color: languageHover.hovered ? appTheme.hover : "transparent"

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 18
                            anchors.topMargin: 8
                            anchors.bottomMargin: 8
                            spacing: 2
                            Text {
                                Layout.fillWidth: true
                                text: languageItem.modelData.name || languageItem.modelData.code
                                color: root.textPrimary
                                font.pixelSize: 12
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: languageItem.modelData.secondary || languageItem.modelData.code
                                color: root.textMuted
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                        }

                        HoverHandler {
                            id: languageHover
                            cursorShape: Qt.PointingHandCursor
                        }

                        TapHandler {
                            gesturePolicy: TapHandler.ReleaseWithinBounds
                            onTapped: {
                                onboardingBridge.chooseLanguage(
                                    sheet.kind,
                                    languageItem.modelData.code
                                )
                                sheet.visible = false
                            }
                        }
                    }
                }
            }
        }
    }
}
