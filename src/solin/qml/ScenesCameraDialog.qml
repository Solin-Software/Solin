pragma Translator: "ScenesEditorView"
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Dialog {
    id: root
    objectName: "scenesCameraDialog"
    property var bridge: null
    property var theme: null
    property var draft: ({})
    property string cameraKind: "local_camera"
    property string cameraId: ""
    property string deviceId: ""
    property string formatId: ""
    property bool formatSelectionChanged: false
    property string ptzProtocol: ""
    property bool hasCredentials: false
    property bool hasStreamCredentials: false
    property bool clearStreamCredentials: false
    property bool clearCredentials: false
    property bool addToScene: true

    readonly property color surface: theme ? theme.surface : "#172131"
    readonly property color surfaceSoft: theme ? theme.bg : "#111927"
    readonly property color hover: theme ? theme.hover : "#233043"
    readonly property color borderColor: theme ? theme.border_ : "#2c394b"
    readonly property color textPrimary: theme ? theme.textPrimary : "#f3f5fa"
    readonly property color textSecondary: theme ? theme.textSecondary : "#aeb8c8"
    readonly property color textMuted: theme ? theme.textMuted : "#768397"
    readonly property color accent: theme ? theme.accent : "#3b82f6"
    readonly property color danger: theme ? theme.danger : "#ef6a6a"

    modal: true
    anchors.centerIn: parent
    width: Math.min(620, parent ? parent.width - 30 : 620)
    height: Math.min(720, parent ? parent.height - 30 : 720)
    padding: 0
    closePolicy: Popup.CloseOnEscape

    function findIndex(items, key, value) {
        for (var index = 0; index < items.length; ++index) {
            if (items[index][key] === value)
                return index
        }
        return 0
    }

    function deviceRecord() {
        var devices = bridge ? bridge.localCameraDevices : []
        for (var index = 0; index < devices.length; ++index) {
            if (devices[index].id === deviceId)
                return devices[index]
        }
        return devices.length ? devices[0] : ({ formats: [] })
    }

    function loadDraft(values, forcedKind) {
        draft = values || ({})
        cameraKind = forcedKind || draft.kind || "local_camera"
        cameraId = draft.id || ""
        nameField.text = draft.name || ""
        deviceId = draft.deviceId || ""
        formatId = draft.formatId || ""
        uriField.text = draft.uri || ""
        transportCombo.currentIndex = draft.transport === "udp" ? 1 : 0
        latencyField.value = draft.latencyMs !== undefined ? draft.latencyMs : 200
        ptzProtocol = draft.ptzProtocol || ""
        ptzCombo.currentIndex = ["", "onvif", "visca_ip", "visca_serial"].indexOf(ptzProtocol)
        if (ptzCombo.currentIndex < 0)
            ptzCombo.currentIndex = 0
        onvifEndpoint.text = draft.ptzEndpoint || ""
        onvifProfileToken.text = draft.ptzProfileToken || ""
        viscaHost.text = draft.ptzHost || ""
        viscaPort.value = draft.ptzPort || 52381
        viscaTransport.currentIndex = draft.ptzTransport === "tcp" ? 1 : 0
        serialDevice.text = draft.ptzSerialDevice || ""
        serialBaud.currentIndex = [9600, 19200, 38400, 115200].indexOf(draft.ptzBaudRate || 9600)
        serialAddress.value = draft.ptzCameraAddress || 1
        hasCredentials = draft.hasCredentials === true
        hasStreamCredentials = draft.hasStreamCredentials === true
        clearStreamCredentials = false
        streamUsernameField.text = ""
        streamPasswordField.text = ""
        clearCredentials = false
        usernameField.text = ""
        passwordField.text = ""
        deviceCombo.currentIndex = findIndex(bridge ? bridge.localCameraDevices : [], "id", deviceId)
        var formats = deviceRecord().formats || []
        formatCombo.currentIndex = findIndex(formats, "id", formatId)
        formatSelectionChanged = false
    }

    function openForCamera(kind) {
        addToScene = true
        loadDraft(bridge ? bridge.cameraDraft("") : ({}), kind)
        open()
        if (kind === "local_camera" && bridge)
            bridge.refreshLocalCameras()
    }

    function openForSelectedCamera() {
        addToScene = false
        loadDraft(bridge ? bridge.selectedCameraDraft() : ({}), "")
        open()
        if (cameraKind === "local_camera" && bridge)
            bridge.refreshLocalCameras()
    }

    function save() {
        var values = {
            id: cameraId,
            kind: cameraKind,
            name: nameField.text.trim(),
            deviceId: deviceId,
            formatId: formatId,
            formatSelectionChanged: formatSelectionChanged,
            uri: uriField.text.trim(),
            transport: transportCombo.currentIndex === 1 ? "udp" : "tcp",
            latencyMs: latencyField.value,
            ptzProtocol: ["", "onvif", "visca_ip", "visca_serial"][ptzCombo.currentIndex],
            ptzEndpoint: onvifEndpoint.text.trim(),
            ptzProfileToken: onvifProfileToken.text.trim(),
            streamUsername: streamUsernameField.text.trim(),
            streamPassword: streamPasswordField.text,
            clearStreamCredentials: root.clearStreamCredentials,
            ptzUsername: usernameField.text.trim(),
            ptzPassword: passwordField.text,
            clearCredentials: clearCredentials,
            ptzHost: viscaHost.text.trim(),
            ptzPort: viscaPort.value,
            ptzTransport: viscaTransport.currentIndex === 1 ? "tcp" : "udp",
            ptzSerialDevice: serialDevice.text.trim(),
            ptzBaudRate: [9600, 19200, 38400, 115200][serialBaud.currentIndex],
            ptzCameraAddress: serialAddress.value,
            keepActive: draft.keepActive === true
        }
        if (bridge && bridge.saveCamera(values, addToScene)) {
            passwordField.text = ""
            usernameField.text = ""
            close()
        }
    }

    onClosed: {
        passwordField.text = ""
        usernameField.text = ""
        if (bridge)
            bridge.stopAllPtz()
    }

    Timer {
        interval: 2000
        repeat: true
        running: root.opened && root.cameraKind === "local_camera"
        onTriggered: if (root.bridge) root.bridge.refreshLocalCameras()
    }

    background: Rectangle {
        color: root.surface
        radius: 16
        border.width: 1
        border.color: root.borderColor
    }

    header: Item {
        implicitHeight: 62
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 20
            anchors.rightMargin: 12
            Text {
                Layout.fillWidth: true
                text: root.cameraId.length ? qsTr("Edit camera") : qsTr("Add camera")
                color: root.textPrimary
                font.pixelSize: 17
                font.weight: Font.DemiBold
            }
            Rectangle {
                Layout.preferredWidth: 34; Layout.preferredHeight: 34; radius: 9
                color: closeMouse.containsMouse ? root.hover : "transparent"
                Image { anchors.centerIn: parent; width: 14; height: 14; source: "image://sceneicons/close/16/" + String(root.textMuted).replace("#", "") }
                MouseArea { id: closeMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.close() }
            }
        }
    }

    contentItem: ScrollView {
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: root.availableWidth
            spacing: 14

            FormSection {
                titleText: qsTr("Camera")
                GridLayout {
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12
                    rowSpacing: 9
                    FieldLabel { text: qsTr("Name") }
                    StyledField { id: nameField; Layout.fillWidth: true; maximumLength: 120; placeholderText: qsTr("Camera name") }

                    FieldLabel { visible: root.cameraKind === "local_camera"; text: qsTr("Device") }
                    RowLayout {
                        visible: root.cameraKind === "local_camera"
                        Layout.fillWidth: true
                        ScenesComboBox {
                            id: deviceCombo
                            theme: root.theme
                            Layout.fillWidth: true
                            model: root.bridge ? root.bridge.localCameraDevices : []
                            textRole: "name"
                            onActivated: {
                                root.deviceId = model[currentIndex].id
                                root.formatId = ""
                                root.formatSelectionChanged = true
                                formatCombo.currentIndex = 0
                            }
                        }
                        SmallButton { iconName: "refresh"; toolTipText: qsTr("Refresh cameras"); onClicked: root.bridge.refreshLocalCameras() }
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera"; text: qsTr("Format") }
                    ScenesComboBox {
                        id: formatCombo
                        theme: root.theme
                        visible: root.cameraKind === "local_camera"
                        Layout.fillWidth: true
                        model: root.deviceRecord().formats || []
                        textRole: "label"
                        onActivated: {
                            root.formatId = model[currentIndex].id
                            root.formatSelectionChanged = true
                        }
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("RTSP address") }
                    StyledField { id: uriField; visible: root.cameraKind === "rtsp_camera"; Layout.fillWidth: true; placeholderText: "rtsp://camera.local/stream" }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Transport") }
                    ScenesComboBox { id: transportCombo; theme: root.theme; visible: root.cameraKind === "rtsp_camera"; Layout.fillWidth: true; model: ["TCP", "UDP"] }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Network buffer") }
                    ScenesSpinBox { id: latencyField; theme: root.theme; visible: root.cameraKind === "rtsp_camera"; Layout.fillWidth: true; from: 0; to: 10000 }

                    // The stream login. Kept out of the address, which is written to
                    // disk and printed by the engine; stored in the system keyring
                    // and put back only when connecting. Separate from the PTZ login
                    // below: the two accounts are routinely different.
                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Stream user name") }
                    StyledField {
                        id: streamUsernameField
                        visible: root.cameraKind === "rtsp_camera"
                        Layout.fillWidth: true
                        placeholderText: root.hasStreamCredentials
                            ? qsTr("Leave blank to keep saved credentials")
                            : qsTr("Only if the camera asks for one")
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Stream password") }
                    StyledField {
                        id: streamPasswordField
                        visible: root.cameraKind === "rtsp_camera"
                        Layout.fillWidth: true
                        echoMode: TextInput.Password
                        placeholderText: root.hasStreamCredentials
                            ? qsTr("Leave blank to keep saved credentials")
                            : ""
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera" && root.hasStreamCredentials; text: "" }
                    ScenesCheckBox {
                        theme: root.theme
                        visible: root.cameraKind === "rtsp_camera" && root.hasStreamCredentials
                        text: qsTr("Remove saved stream credentials")
                        checked: root.clearStreamCredentials
                        onToggled: root.clearStreamCredentials = checked
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.cameraKind === "local_camera" && root.bridge && root.bridge.localCameraStatus.length > 0
                    text: root.bridge ? root.bridge.localCameraStatus : ""
                    color: root.textMuted
                    font.pixelSize: 9
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.cameraKind === "rtsp_camera"
                    text: qsTr("Credentials are stored separately. Do not put a username, password, or token in the RTSP address.")
                    color: root.textMuted
                    font.pixelSize: 9
                    wrapMode: Text.Wrap
                }
            }

            FormSection {
                titleText: qsTr("PTZ control")
                ScenesComboBox {
                    id: ptzCombo
                    theme: root.theme
                    Layout.fillWidth: true
                    model: [qsTr("No PTZ control"), "ONVIF", qsTr("VISCA over IP"), qsTr("VISCA serial")]
                    onActivated: root.ptzProtocol = ["", "onvif", "visca_ip", "visca_serial"][currentIndex]
                }

                GridLayout {
                    visible: ptzCombo.currentIndex === 1
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12; rowSpacing: 9
                    FieldLabel { text: qsTr("PTZ service") }
                    StyledField { id: onvifEndpoint; Layout.fillWidth: true; placeholderText: "https://camera.local/onvif/ptz_service" }
                    FieldLabel { text: qsTr("Profile token") }
                    StyledField { id: onvifProfileToken; Layout.fillWidth: true; placeholderText: qsTr("Media profile token") }
                    FieldLabel { text: qsTr("Username") }
                    StyledField { id: usernameField; Layout.fillWidth: true; placeholderText: root.hasCredentials ? qsTr("Leave blank to keep saved credentials") : "" }
                    FieldLabel { text: qsTr("Password") }
                    StyledField { id: passwordField; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: root.hasCredentials ? qsTr("Leave blank to keep saved credentials") : "" }
                    FieldLabel { visible: root.hasCredentials; text: "" }
                    ScenesCheckBox { theme: root.theme; visible: root.hasCredentials; text: qsTr("Remove saved credentials"); checked: root.clearCredentials; onToggled: root.clearCredentials = checked }
                }

                GridLayout {
                    visible: ptzCombo.currentIndex === 2
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12; rowSpacing: 9
                    FieldLabel { text: qsTr("Host") }
                    StyledField { id: viscaHost; Layout.fillWidth: true; placeholderText: "camera.local" }
                    FieldLabel { text: qsTr("Port") }
                    ScenesSpinBox { id: viscaPort; theme: root.theme; Layout.fillWidth: true; from: 1; to: 65535 }
                    FieldLabel { text: qsTr("Transport") }
                    ScenesComboBox { id: viscaTransport; theme: root.theme; Layout.fillWidth: true; model: ["UDP", "TCP"] }
                }

                GridLayout {
                    visible: ptzCombo.currentIndex === 3
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12; rowSpacing: 9
                    FieldLabel { text: qsTr("Serial port") }
                    StyledField { id: serialDevice; Layout.fillWidth: true; placeholderText: "COM3" }
                    FieldLabel { text: qsTr("Baud rate") }
                    ScenesComboBox { id: serialBaud; theme: root.theme; Layout.fillWidth: true; model: ["9600", "19200", "38400", "115200"] }
                    FieldLabel { text: qsTr("Camera address") }
                    ScenesSpinBox { id: serialAddress; theme: root.theme; Layout.fillWidth: true; from: 1; to: 7 }
                }
            }
            Item { Layout.preferredHeight: 8 }
        }
    }

    footer: Rectangle {
        implicitHeight: 64
        color: root.surface
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            spacing: 8
            Item { Layout.fillWidth: true }
            ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
            ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; enabled: nameField.text.trim().length > 0; onClicked: root.save() }
        }
    }

    component FormSection: Rectangle {
        id: section
        property string titleText: ""
        default property alias content: sectionColumn.data
        Layout.fillWidth: true
        implicitHeight: sectionColumn.implicitHeight + 28
        color: root.surfaceSoft
        radius: 12
        border.width: 1
        border.color: root.borderColor
        ColumnLayout {
            id: sectionColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 14
            spacing: 10
            Text { text: section.titleText; color: root.textPrimary; font.pixelSize: 12; font.weight: Font.DemiBold }
        }
    }

    component FieldLabel: Text {
        color: root.textMuted
        font.pixelSize: 10
        Layout.alignment: Qt.AlignVCenter
    }

    component StyledField: ScenesTextField {
        theme: root.theme
    }

    component SmallButton: Rectangle {
        id: small
        property string iconName: ""
        property string toolTipText: ""
        signal clicked()
        Layout.preferredWidth: 36; Layout.preferredHeight: 34
        radius: 9; color: smallMouse.containsMouse ? root.hover : root.surface
        border.width: 1; border.color: root.borderColor
        Image { anchors.centerIn: parent; width: 14; height: 14; source: "image://sceneicons/" + small.iconName + "/16/" + String(root.textMuted).replace("#", "") }
        MouseArea { id: smallMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: small.clicked() }
        ToolTip.visible: smallMouse.containsMouse; ToolTip.text: small.toolTipText
    }

}
