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
    property var selectedDeviceSnapshot: ({})
    property var selectedFormatSnapshot: ({})
    property bool advancedExpanded: false
    property bool customFrameRateInvalid: false
    property bool customResolutionPending: false
    property bool customResolutionDirty: false
    property string customResolutionError: ""
    property string customResolutionDeviceId: ""
    property int customResolutionWidth: 0
    property int customResolutionHeight: 0
    readonly property var devices: cameraDevices()
    readonly property var cameraDevice: deviceRecord()
    readonly property var formats: cameraFormats()
    readonly property var selectedFormat: formatRecord()
    readonly property var resolutions: modeChoices("resolutionId", "resolutionLabel", false)
    readonly property var frameRates: modeChoices("fpsId", "fpsLabel", true)
    readonly property var inputFormats: matchingFormats()
    readonly property bool customFrameRateAvailable: (cameraDevice.frameRateRanges || []).some(function(range) {
        return range.resolutionId === selectedFormat.resolutionId
            && range.pixelFormat === selectedFormat.pixelFormat
            && range.mediaType === selectedFormat.mediaType
    })
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
        return -1
    }

    function cameraDevices() {
        var items = bridge ? bridge.localCameraDevices.slice() : []
        if (deviceId.length && findIndex(items, "id", deviceId) < 0) {
            items.push({ id: deviceId,
                name: (selectedDeviceSnapshot.name || draft.deviceName || deviceId)
                    + " · " + qsTr("Unavailable device"),
                available: false, formats: [] })
        }
        return items
    }

    function deviceRecord() {
        var index = findIndex(devices, "id", deviceId)
        return index >= 0 ? devices[index] : ({ available: false, formats: [] })
    }

    function cameraFormats() {
        var items = cameraDevice.formats ? cameraDevice.formats.slice() : []
        if (!items.length)
            items.push({ id: "", label: qsTr("Automatic") })
        if (formatId.length && findIndex(items, "id", formatId) < 0
                && selectedFormatSnapshot.id === formatId) {
            var live = bridge ? bridge.localCameraFormat(deviceId, formatId) : ({})
            var saved = Object.assign({}, live.id ? live : selectedFormatSnapshot)
            saved.available = !!live.id
            items.push(saved)
        }
        return items
    }

    function formatRecord() {
        var index = findIndex(formats, "id", formatId)
        return index >= 0 ? formats[index] : ({})
    }

    function modeChoices(key, label, forFrameRate) {
        var items = forFrameRate && formatId.length
            ? [] : [{ id: "", label: qsTr("Automatic"), available: true }]
        for (var index = 0; index < formats.length; ++index) {
            var mode = formats[index]
            if (!mode.id || (forFrameRate && mode.resolutionId !== selectedFormat.resolutionId))
                continue
            var found = findIndex(items, "id", mode[key])
            if (found < 0)
                items.push({ id: mode[key], label: mode[label], available: mode.available !== false })
            else if (mode.available !== false)
                items[found].available = true
        }
        for (var item = 0; item < items.length; ++item) {
            if (items[item].available === false)
                items[item].label += " · " + qsTr("Unavailable")
        }
        return items
    }

    function matchingFormats() {
        if (!formatId.length)
            return [{ id: "", label: qsTr("Automatic"), available: true }]
        return formats.filter(function(mode) {
            return mode.id && mode.resolutionId === selectedFormat.resolutionId
                && mode.fpsId === selectedFormat.fpsId
        }).map(function(mode) {
            return { id: mode.id, label: mode.formatLabel,
                available: mode.available !== false }
        })
    }

    function selectFormat(mode) {
        resetCustomResolution()
        customFrameRateInvalid = false
        customFrameRateField.text = ""
        selectedFormatSnapshot = mode
        formatId = mode.id
        formatSelectionChanged = true
    }

    function resetCustomResolution() {
        customResolutionPending = false
        customResolutionDirty = false
        customResolutionError = ""
        customResolutionDeviceId = ""
    }

    function requestCustomResolution() {
        customResolutionDirty = false
        customResolutionError = ""
        var widthText = customWidthField.text.trim()
        var heightText = customHeightField.text.trim()
        if (!/^[0-9]{1,5}$/.test(widthText) || !/^[0-9]{1,5}$/.test(heightText)) {
            customResolutionError = qsTr("This resolution is outside the camera's supported limits or steps.")
            return
        }
        customResolutionDeviceId = deviceId
        customResolutionWidth = Number(widthText)
        customResolutionHeight = Number(heightText)
        customResolutionPending = true
        if (!bridge || !bridge.queryLocalCameraModes(deviceId, customResolutionWidth, customResolutionHeight)) {
            customResolutionPending = false
            customResolutionError = qsTr("This resolution is outside the camera's supported limits or steps.")
        }
    }

    Connections {
        target: root.bridge
        function onLocalCameraModesReady(deviceId, width, height, success) {
            if (!root.customResolutionPending || deviceId !== root.deviceId
                    || deviceId !== root.customResolutionDeviceId
                    || width !== root.customResolutionWidth || height !== root.customResolutionHeight)
                return
            root.customResolutionPending = false
            var candidates = root.formats.filter(function(mode) {
                return mode.width === width && mode.height === height && mode.available !== false
            })
            if (!success || !candidates.length) {
                root.customResolutionError = qsTr("Supported modes could not be retrieved for this resolution. Refresh the cameras or choose another resolution.")
                return
            }
            root.chooseMode(candidates)
        }
    }

    function applyCustomFrameRate(text) {
        if (!text.trim().length) {
            customFrameRateInvalid = false
            return
        }
        var mode = bridge ? bridge.localCameraCustomFrameRate(deviceId, formatId, text) : ({})
        customFrameRateInvalid = !mode.id
        if (mode.id) {
            selectedFormatSnapshot = mode
            formatId = mode.id
            formatSelectionChanged = true
        }
    }

    function selectResolution(resolutionId) {
        if (!resolutionId.length) {
            selectFormat({ id: "" })
            return
        }
        var candidates = formats.filter(function(mode) {
            return mode.resolutionId === resolutionId && mode.available !== false
        })
        chooseMode(candidates)
    }

    function selectFrameRate(fpsId) {
        var candidates = formats.filter(function(mode) {
            return mode.resolutionId === selectedFormat.resolutionId
                && mode.fpsId === fpsId && mode.available !== false
        })
        chooseMode(candidates)
    }

    function chooseMode(candidates) {
        if (!candidates.length)
            return
        var preferred = candidates.filter(function(mode) {
            return mode.pixelFormat === selectedFormat.pixelFormat
                && mode.mediaType === selectedFormat.mediaType
        })
        var pool = preferred.length ? preferred : candidates
        var sameRate = pool.filter(function(mode) { return mode.fpsId === selectedFormat.fpsId })
        pool.sort(function(left, right) {
            function distance(mode) {
                var rate = mode.fpsId.split("/")
                return Math.abs(Number(rate[0]) / Number(rate[1]) - 30)
            }
            return distance(left) - distance(right)
        })
        selectFormat(sameRate.length ? sameRate[0] : pool[0])
    }

    function capabilitiesMessage() {
        // Read the notified property even when capture health takes precedence.
        var discoveryStatus = bridge ? bridge.localCameraStatus : ""
        if (bridge && cameraId.length && !formatSelectionChanged) {
            var currentStatus = bridge.cameraCurrentStatus(cameraId)
            if (currentStatus.length)
                return currentStatus
        }
        if (discoveryStatus.length)
            return discoveryStatus
        if (cameraDevice.available === false)
            return qsTr("Unavailable device")
        if (formatId.length && selectedFormat.available === false)
            return qsTr("Saved camera mode is unavailable. Refresh the cameras or choose another mode.")
        return cameraDevice.capabilitiesStatus || ""
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
        selectedDeviceSnapshot = { name: draft.deviceName || deviceId }
        selectedFormatSnapshot = draft.format || ({})
        customFrameRateField.text = ""
        customFrameRateInvalid = false
        resetCustomResolution()
        customWidthField.text = ""
        customHeightField.text = ""
        advancedExpanded = false
        formatSelectionChanged = false
    }

    function openForCamera(kind) {
        addToScene = true
        loadDraft(bridge ? bridge.cameraDraft("") : ({}), kind)
        open()
        if (kind === "local_camera" && bridge)
            bridge.ensureLocalCameras()
    }

    function openForSelectedCamera() {
        addToScene = false
        loadDraft(bridge ? bridge.selectedCameraDraft() : ({}), "")
        open()
        if (cameraKind === "local_camera" && bridge)
            bridge.ensureLocalCameras()
    }

    function save() {
        if (customResolutionDirty) {
            requestCustomResolution()
            return
        }
        if (customResolutionPending || customResolutionError.length)
            return
        if (customFrameRateAvailable)
            applyCustomFrameRate(customFrameRateField.text)
        if (customFrameRateInvalid)
            return
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
        resetCustomResolution()
        passwordField.text = ""
        usernameField.text = ""
        if (bridge)
            bridge.stopAllPtz()
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
                            objectName: "cameraDeviceCombo"
                            model: root.devices
                            currentIndex: root.findIndex(model, "id", root.deviceId)
                            textRole: "name"
                            onActivated: {
                                root.selectedDeviceSnapshot = model[currentIndex]
                                root.deviceId = model[currentIndex].id
                                root.selectFormat({ id: "" })
                            }
                        }
                        SmallButton { iconName: "refresh"; toolTipText: qsTr("Refresh cameras"); onClicked: root.bridge.refreshLocalCameras() }
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera"; text: qsTr("Resolution") }
                    ScenesComboBox {
                        id: resolutionCombo
                        objectName: "cameraResolutionCombo"
                        theme: root.theme
                        visible: root.cameraKind === "local_camera"
                        Layout.fillWidth: true
                        model: root.resolutions
                        textRole: "label"
                        currentIndex: root.findIndex(model, "id", root.selectedFormat.resolutionId || "")
                        onActivated: root.selectResolution(model[currentIndex].id)
                    }

                    FieldLabel {
                        visible: root.cameraKind === "local_camera" && (root.cameraDevice.resolutionRanges || []).length > 0
                        text: qsTr("Custom resolution")
                    }
                    RowLayout {
                        visible: root.cameraKind === "local_camera" && (root.cameraDevice.resolutionRanges || []).length > 0
                        Layout.fillWidth: true
                        StyledField {
                            id: customWidthField
                            objectName: "cameraCustomWidthField"
                            Layout.fillWidth: true
                            maximumLength: 5
                            placeholderText: qsTr("Width")
                            inputMethodHints: Qt.ImhDigitsOnly
                            onTextEdited: { root.resetCustomResolution(); root.customResolutionDirty = true }
                        }
                        Text { text: "×"; color: root.textSecondary }
                        StyledField {
                            id: customHeightField
                            objectName: "cameraCustomHeightField"
                            Layout.fillWidth: true
                            maximumLength: 5
                            placeholderText: qsTr("Height")
                            inputMethodHints: Qt.ImhDigitsOnly
                            onTextEdited: { root.resetCustomResolution(); root.customResolutionDirty = true }
                        }
                        ScenesDialogButton {
                            theme: root.theme
                            text: qsTr("Check")
                            enabled: !root.customResolutionPending
                            onClicked: root.requestCustomResolution()
                        }
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera"; text: qsTr("Frame rate") }
                    ScenesComboBox {
                        id: frameRateCombo
                        objectName: "cameraFrameRateCombo"
                        theme: root.theme
                        visible: root.cameraKind === "local_camera"
                        enabled: root.formatId.length > 0 && root.cameraDevice.available !== false
                        Layout.fillWidth: true
                        model: root.frameRates
                        textRole: "label"
                        currentIndex: root.findIndex(model, "id", root.selectedFormat.fpsId || "")
                        onActivated: root.selectFrameRate(model[currentIndex].id)
                    }

                    FieldLabel {
                        visible: root.cameraKind === "local_camera" && root.customFrameRateAvailable
                        text: qsTr("Custom frame rate")
                    }
                    StyledField {
                        id: customFrameRateField
                        objectName: "cameraCustomFrameRateField"
                        visible: root.cameraKind === "local_camera" && root.customFrameRateAvailable
                        Layout.fillWidth: true
                        maximumLength: 64
                        placeholderText: qsTr("e.g. 29.97 or 30000/1001")
                        onEditingFinished: root.applyCustomFrameRate(text)
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
                    visible: root.cameraKind === "local_camera" && (root.customResolutionPending || root.customResolutionError.length > 0)
                    Layout.fillWidth: true
                    text: root.customResolutionPending ? qsTr("Checking supported camera modes…") : root.customResolutionError
                    color: root.customResolutionPending ? root.textSecondary : root.danger
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
                Text {
                    visible: root.cameraKind === "local_camera" && root.customFrameRateInvalid
                    Layout.fillWidth: true
                    text: qsTr("This frame rate is not supported at the selected resolution and format.")
                    color: root.danger
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    objectName: "cameraCapabilitiesMessage"
                    visible: root.cameraKind === "local_camera" && text.length > 0
                    text: root.capabilitiesMessage()
                    color: root.textSecondary
                    font.pixelSize: 11
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
                visible: root.cameraKind === "local_camera"
                titleText: qsTr("Advanced")
                ScenesCheckBox {
                    objectName: "cameraAdvancedToggle"
                    theme: root.theme
                    text: qsTr("Show advanced settings")
                    checked: root.advancedExpanded
                    onToggled: root.advancedExpanded = checked
                }
                FieldLabel { visible: root.advancedExpanded; text: qsTr("Format") }
                ScenesComboBox {
                    id: formatCombo
                    objectName: "cameraFormatCombo"
                    theme: root.theme
                    visible: root.advancedExpanded
                    enabled: root.formatId.length > 0 && root.cameraDevice.available !== false
                    Layout.fillWidth: true
                    model: root.inputFormats
                    textRole: "label"
                    currentIndex: root.findIndex(model, "id", root.formatId)
                    onActivated: {
                        var index = root.findIndex(root.formats, "id", model[currentIndex].id)
                        if (index >= 0)
                            root.selectFormat(root.formats[index])
                    }
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
            ScenesDialogButton { theme: root.theme; text: qsTr("Save"); primary: true; enabled: nameField.text.trim().length > 0 && !root.customResolutionPending && !root.customResolutionError.length; onClicked: root.save() }
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
