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
    readonly property bool localInventoryActive: visible && cameraKind === "local_camera"
    onLocalInventoryActiveChanged: {
        if (bridge)
            bridge.setLocalCameraInventoryActive(localInventoryActive)
    }
    property string cameraId: ""
    property string deviceId: ""
    property string formatId: ""
    property bool cameraAccessPending: false
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
    property bool streamCredentialsEdited: false
    property bool clearCredentials: false
    property bool addToScene: true
    property var editContext: ({})
    property bool layerNameCustom: false
    property string validationError: ""
    property bool saveAttempted: false
    property string nameError: ""
    property bool deviceConflict: false
    property bool connectionChoicePending: false
    readonly property string deviceConflictMessage: qsTr("This device has conflicting camera connections. Check the existing connections before continuing.")
    property alias layerName: layerNameField.text
    readonly property bool reusingCamera: addToScene && cameraId.length > 0
    readonly property bool editingSettings: !reusingCamera && !deviceConflict && !connectionChoicePending
    readonly property var networkChoices: [{ id: "__new__", name: qsTr("New connection…") }].concat(
        bridge ? bridge.configuredCameras.filter(function(camera) { return camera.kind === "rtsp_camera" })
            .map(function(camera) { return Object.assign({}, camera, { selectable: true }) }) : []).concat(
        bridge ? bridge.onvifCameras.map(function(camera) {
            return Object.assign({}, camera, { discovered: true, selectable: true, reason: discoveredReason(camera) })
        }) : [])
    // A camera picked from the network search, and what asking it returned.
    property var discoveredCamera: ({})
    property var onvifStreams: []
    property int streamIndex: 0
    property int streamQueryId: 0
    property bool streamQueryRunning: false
    property bool streamQueryFailed: false
    property string streamQueryMessage: ""
    // The ONVIF PTZ finder: which camera to ask, and the PTZ profiles it has.
    property bool ptzFinderOpen: false
    property var ptzProfiles: []
    property int ptzQueryId: 0
    property bool ptzQueryRunning: false
    property bool ptzQueryFailed: false
    property string ptzQueryMessage: ""
    // The stream login a PTZ query borrowed, kept for the PTZ fields on success.
    property var ptzQueryLogin: null
    readonly property var ptzFinderChoices: {
        var items = []
        var own = bridge && cameraKind === "rtsp_camera" ? bridge.onvifDeviceServiceForAddress(uriField.text) : ""
        if (own.length)
            items.push({ id: "__address__", name: qsTr("This camera's address"), reason: own, deviceService: own })
        return items.concat(bridge ? bridge.onvifCameras.map(function(camera) {
            return { id: camera.id, name: camera.name, deviceService: camera.deviceService,
                reason: camera.host + (camera.location ? " · " + camera.location : "") }
        }) : [])
    }

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

    function validateLayerName() {
        nameError = bridge && visible && addToScene ? bridge.layerNameError(layerNameField.text, "") : ""
        return !nameError.length
    }

    function cameraDevices() {
        var items = bridge ? bridge.localCameraDevices.slice() : []
        if (addToScene)
            items = items.filter(function(device) { return !!device.id })
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
        function onChanged() { root.validateLayerName() }
        function onLocalCameraAccessResolved(granted) {
            if (!root.cameraAccessPending || !root.visible)
                return
            root.cameraAccessPending = false
            if (granted)
                root.save()
            else
                root.validationError = root.bridge.localCameraAccessError()
        }
        function onOnvifCameraQueried(requestId, result) {
            if (requestId === root.streamQueryId && root.streamQueryRunning)
                root.applyStreamQuery(result)
            else if (requestId === root.ptzQueryId && root.ptzQueryRunning)
                root.applyPtzQuery(result)
        }
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
                root.customResolutionError = qsTr("Supported modes could not be retrieved for this resolution. Choose another resolution.")
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
            return qsTr("Saved camera mode is unavailable. Choose another mode.")
        return cameraDevice.capabilitiesStatus || ""
    }

    function discoveredReason(camera) {
        var place = camera.host + (camera.location ? " · " + camera.location : "")
        return camera.savedName
            ? qsTr("%1 · Already added as “%2”").arg(place).arg(camera.savedName)
            : qsTr("%1 · Found on the network").arg(place)
    }

    function resetDiscovery() {
        discoveredCamera = ({})
        onvifStreams = []
        streamIndex = 0
        streamQueryId = 0
        streamQueryRunning = false
        streamQueryFailed = false
        streamQueryMessage = ""
        ptzFinderOpen = false
        ptzProfiles = []
        ptzQueryId = 0
        ptzQueryRunning = false
        ptzQueryFailed = false
        ptzQueryMessage = ""
        ptzQueryLogin = null
    }

    function selectDiscovered(choice) {
        var previousName = layerNameField.text
        var custom = layerNameCustom
        loadDraft(bridge ? bridge.cameraDraft("") : ({}), "rtsp_camera")
        discoveredCamera = choice
        nameField.text = choice.name
        layerNameField.text = custom ? previousName : nameField.text
        layerNameCustom = custom
        queryDiscoveredStreams()
    }

    function queryDiscoveredStreams() {
        if (!bridge || !discoveredCamera.deviceService)
            return
        streamQueryFailed = false
        streamQueryMessage = ""
        streamQueryId = bridge.queryOnvifCamera(discoveredCamera.deviceService,
            streamUsernameField.text, streamPasswordField.text)
        streamQueryRunning = streamQueryId > 0
    }

    function applyStreamQuery(result) {
        streamQueryRunning = false
        if (!result.ok) {
            streamQueryFailed = true
            var anonymous = !streamUsernameField.text.trim().length || !streamPasswordField.text.length
            streamQueryMessage = result.errorCode === "onvif_authentication_failed" && anonymous
                ? qsTr("This camera needs its login. Enter the user name and password above, then select Connect.")
                : result.message
            return
        }
        onvifStreams = result.profiles.filter(function(profile) { return profile.uri.length > 0 })
        if (!onvifStreams.length) {
            streamQueryFailed = true
            streamQueryMessage = qsTr("The camera did not list any video streams.")
            return
        }
        var movable = result.profiles.filter(function(profile) { return profile.ptz })
        var hasPtz = result.ptzEndpoint.length > 0 && movable.length > 0
        // First, so a login the camera returned in the stream address is in the
        // stream fields before the PTZ fields borrow it.
        selectStream(0)
        if (hasPtz) {
            ptzCombo.currentIndex = 1
            ptzProtocol = "onvif"
            onvifEndpoint.text = result.ptzEndpoint
            ptzProfiles = movable
            onvifProfileToken.text = onvifStreams[0].ptz ? onvifStreams[0].token : movable[0].token
            // One login usually covers both; the PTZ one stays editable.
            if (!usernameField.text.trim().length && streamUsernameField.text.trim().length) {
                usernameField.text = streamUsernameField.text.trim()
                passwordField.text = streamPasswordField.text
            }
        }
        streamQueryMessage = hasPtz
            ? qsTr("Connected. This camera can pan, tilt, and zoom; PTZ control is set up under Advanced settings.")
            : qsTr("Connected.")
    }

    function selectStream(index) {
        if (index < 0 || index >= onvifStreams.length)
            return
        streamIndex = index
        uriField.text = onvifStreams[index].uri
        // Follow the profile's login until the operator takes ownership of it.
        if (!streamCredentialsEdited && !hasStreamCredentials && !clearStreamCredentials) {
            streamUsernameField.text = onvifStreams[index].username || ""
            streamPasswordField.text = onvifStreams[index].password || ""
        }
        if (ptzProtocol === "onvif" && onvifStreams[index].ptz)
            onvifProfileToken.text = onvifStreams[index].token
    }

    function openPtzFinder() {
        ptzFinderOpen = true
        ptzQueryFailed = false
        ptzQueryMessage = ""
        if (bridge && !bridge.onvifCameras.length && !bridge.onvifDiscoveryRunning)
            bridge.discoverOnvifCameras()
    }

    function queryPtz(choice) {
        if (!bridge || !choice.deviceService)
            return
        // The PTZ login when typed, else the stream login: usually the same account.
        var ownLogin = usernameField.text.trim().length > 0
        ptzQueryFailed = false
        ptzQueryMessage = ""
        ptzQueryLogin = ownLogin ? null
            : { username: streamUsernameField.text.trim(), password: streamPasswordField.text }
        ptzQueryId = bridge.queryOnvifCamera(choice.deviceService,
            ownLogin ? usernameField.text : streamUsernameField.text,
            ownLogin ? passwordField.text : streamPasswordField.text)
        ptzQueryRunning = ptzQueryId > 0
    }

    function applyPtzQuery(result) {
        ptzQueryRunning = false
        ptzQueryFailed = true
        if (!result.ok) {
            var anonymous = !usernameField.text.trim().length || !passwordField.text.length
            ptzQueryMessage = result.errorCode === "onvif_authentication_failed" && anonymous
                ? qsTr("This camera needs its login. Enter the user name and password above, then choose the camera again.")
                : result.message
            return
        }
        var movable = result.profiles.filter(function(profile) { return profile.ptz })
        if (!result.ptzEndpoint.length || !movable.length) {
            ptzQueryMessage = qsTr("This camera does not offer PTZ control over ONVIF.")
            return
        }
        ptzQueryFailed = false
        // Saving without the login the query just succeeded with would leave
        // every later PTZ command unauthenticated.
        if (ptzQueryLogin && ptzQueryLogin.username.length && !usernameField.text.trim().length) {
            usernameField.text = ptzQueryLogin.username
            passwordField.text = ptzQueryLogin.password
        }
        onvifEndpoint.text = result.ptzEndpoint
        ptzProfiles = movable
        onvifProfileToken.text = movable[0].token
        ptzFinderOpen = false
        ptzQueryMessage = qsTr("PTZ settings filled in from the camera.")
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
        streamCredentialsEdited = false
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
        validationError = ""
        saveAttempted = false
        deviceConflict = false
        connectionChoicePending = false
        resetDiscovery()
    }

    function selectDevice(device) {
        if (!addToScene) {
            selectedDeviceSnapshot = device
            deviceId = device.id
            selectFormat({ id: "" })
            return
        }
        var previousName = layerNameField.text
        var custom = layerNameCustom
        var registered = bridge ? bridge.configuredLocalCamera(device.id) : ({})
        loadDraft(registered.id ? registered : ({ deviceId: device.id, name: device.name }), "local_camera")
        deviceConflict = registered.conflict === true
        selectedDeviceSnapshot = device
        layerNameField.text = custom ? previousName : (registered.name || device.name || "")
        layerNameCustom = custom
    }

    function selectNetwork(choice) {
        // A found camera that is already saved reuses that connection: a second
        // connection would ask the camera for another stream, and cameras serve
        // only a few at once.
        if (choice.discovered && choice.savedId) {
            selectNetwork({ id: choice.savedId })
            return
        }
        if (choice.discovered) {
            selectDiscovered(choice)
            return
        }
        var previousName = layerNameField.text
        var custom = layerNameCustom
        loadDraft(bridge ? bridge.cameraDraft(choice.id === "__new__" ? "" : choice.id) : ({}), "rtsp_camera")
        layerNameField.text = custom ? previousName : nameField.text
        layerNameCustom = custom
    }

    function openForCamera(kind) {
        addToScene = true
        editContext = bridge ? bridge.sourceEditContext() : ({})
        layerNameCustom = false
        layerNameField.text = ""
        loadDraft(bridge ? bridge.cameraDraft("") : ({}), kind)
        networkSelector.reset()
        connectionChoicePending = kind === "rtsp_camera" && networkChoices.length > 1
        if (kind === "local_camera") {
            var availableDevices = devices.filter(function(device) { return device.available !== false })
            if (availableDevices.length)
                selectDevice(availableDevices[0])
        }
        open()
        if (kind === "local_camera" && bridge)
            bridge.ensureLocalCameras()
    }

    function openForSelectedCamera() {
        addToScene = false
        editContext = bridge ? bridge.sourceEditContext() : ({})
        loadDraft(bridge ? bridge.selectedCameraDraft() : ({}), "")
        open()
        if (cameraKind === "local_camera" && bridge)
            bridge.ensureLocalCameras()
    }

    function save() {
        saveAttempted = true
        validationError = ""
        if (connectionChoicePending)
            return
        if (deviceConflict) {
            validationError = deviceConflictMessage
            return
        }
        if (addToScene && cameraKind === "local_camera" && !deviceId.length) {
            validationError = qsTr("Unavailable device")
            return
        }
        if (addToScene && !layerNameField.text.trim().length) {
            validationError = qsTr("Enter a name for this source in the scene.")
            layerNameField.forceActiveFocus()
            return
        }
        if (!validateLayerName()) {
            layerNameField.forceActiveFocus()
            return
        }
        if (cameraKind === "local_camera" && bridge) {
            var access = bridge.requestLocalCameraAccess()
            if (access !== "granted") {
                cameraAccessPending = access === "pending"
                if (!cameraAccessPending)
                    validationError = root.bridge.localCameraAccessError()
                return
            }
        }
        if (reusingCamera) {
            if (bridge && bridge.addConfiguredCamera(cameraId, layerNameField.text.trim(), editContext).length)
                close()
            return
        }
        if (!nameField.text.trim().length) {
            validationError = qsTr("Enter a camera name.")
            nameField.forceActiveFocus()
            return
        }
        if (cameraKind === "rtsp_camera" && !uriField.text.trim().length) {
            validationError = qsTr("Enter an RTSP address.")
            uriField.forceActiveFocus()
            return
        }
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
            layerName: layerNameField.text.trim(),
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
            ptzCameraAddress: serialAddress.value
        }
        if (bridge && (addToScene ? bridge.addCamera(values, editContext).length > 0
                                : bridge.updateCamera(values, editContext))) {
            passwordField.text = ""
            usernameField.text = ""
            close()
        }
    }

    onOpened: {
        validateLayerName()
        if (addToScene) {
            layerNameField.forceActiveFocus()
            layerNameField.selectAll()
        } else if (cameraKind === "rtsp_camera") {
            nameField.forceActiveFocus()
            nameField.selectAll()
        } else {
            deviceCombo.forceActiveFocus()
        }
    }

    onClosed: {
        cameraAccessPending = false
        resetCustomResolution()
        passwordField.text = ""
        usernameField.text = ""
        streamPasswordField.text = ""
        streamUsernameField.text = ""
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
                text: !root.addToScene ? qsTr("Configure camera")
                    : root.cameraKind === "rtsp_camera" ? qsTr("Network camera") : qsTr("Camera")
                color: root.textPrimary
                font.pixelSize: 17
                font.weight: Font.DemiBold
            }
            Rectangle {
                Layout.preferredWidth: 34; Layout.preferredHeight: 34; radius: 9
                color: closeMouse.containsMouse ? root.hover : "transparent"
                activeFocusOnTab: true
                Accessible.role: Accessible.Button
                Accessible.name: qsTr("Close")
                Keys.onReturnPressed: root.close()
                Keys.onSpacePressed: root.close()
                Image { anchors.centerIn: parent; width: 14; height: 14; source: "image://sceneicons/close/16/" + String(root.textMuted).replace("#", "") }
                MouseArea { id: closeMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.close() }
            }
        }
    }

    contentItem: ScrollView {
        id: cameraScroll
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: cameraScroll.availableWidth
            spacing: 14

            Text {
                Layout.fillWidth: true
                visible: !root.addToScene
                text: qsTr("These settings affect all scenes and scene profiles that use this camera. To change only the name in this scene, use Rename….")
                color: root.textSecondary
                wrapMode: Text.Wrap
                font.pixelSize: 12
                padding: 12
            }
            FormSection {
                visible: root.addToScene
                titleText: qsTr("Name in this scene")
                StyledField {
                    id: layerNameField
                    objectName: "cameraLayerNameField"
                    Layout.fillWidth: true
                    maximumLength: 120
                    onTextChanged: {
                        root.validationError = ""
                        root.saveAttempted = false
                        root.validateLayerName()
                    }
                    onTextEdited: root.layerNameCustom = true
                }
                Text {
                    objectName: "cameraLayerNameError"
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: root.nameError
                    color: root.danger
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                    Accessible.role: Accessible.AlertMessage
                }
            }
            FormSection {
                visible: root.addToScene && root.cameraKind === "rtsp_camera"
                titleText: qsTr("Connection")
                ScenesSourceSelector {
                    id: networkSelector
                    objectName: "cameraNetworkSelector"
                    theme: root.theme
                    Layout.fillWidth: true
                    options: root.networkChoices
                    selectedId: root.connectionChoicePending ? "" : root.discoveredCamera.id || root.cameraId || "__new__"
                    searchPlaceholder: qsTr("Search connections…")
                    onChosen: function(choice) { root.selectNetwork(choice) }
                }
                NetworkSearchRow { objectName: "cameraFindNetworkRow" }
            }
            FormSection {
                visible: root.reusingCamera
                titleText: qsTr("Saved camera settings")
                Text {
                    Layout.fillWidth: true
                    objectName: "cameraReuseSummary"
                    text: root.draft.name + (root.draft.summary ? "\n" + root.draft.summary : "")
                    textFormat: Text.PlainText
                    color: root.textSecondary
                    wrapMode: Text.Wrap
                    font.pixelSize: 12
                }
                Text {
                    Layout.fillWidth: true
                    text: qsTr("This scene will reuse the camera's shared settings.")
                    color: root.textSecondary
                    wrapMode: Text.Wrap
                    font.pixelSize: 11
                }
            }
            FormSection {
                visible: root.cameraKind === "local_camera" || root.editingSettings
                titleText: root.cameraKind === "rtsp_camera" ? qsTr("Connection settings") : qsTr("Camera")
                GridLayout {
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12
                    rowSpacing: 9
                    FieldLabel { visible: root.editingSettings && root.cameraKind === "rtsp_camera"; text: qsTr("Connection name") }
                    StyledField {
                        id: nameField
                        objectName: "cameraConnectionNameField"
                        visible: root.editingSettings && root.cameraKind === "rtsp_camera"
                        Layout.fillWidth: true
                        maximumLength: 120
                        placeholderText: qsTr("Camera name")
                        onTextEdited: if (root.addToScene && !root.layerNameCustom) layerNameField.text = text
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera"; text: qsTr("Device") }
                    ScenesComboBox {
                        id: deviceCombo
                        theme: root.theme
                        visible: root.cameraKind === "local_camera"
                        Layout.fillWidth: true
                        objectName: "cameraDeviceCombo"
                        model: root.devices
                        currentIndex: root.findIndex(model, "id", root.deviceId)
                        textRole: "name"
                        delegate: ItemDelegate {
                            required property int index
                            required property var modelData
                            width: deviceCombo.width - 8
                            text: modelData.name + (modelData.available === false ? " · " + qsTr("Unavailable device") : "")
                            enabled: {
                                var registered = root.bridge ? root.bridge.configuredLocalCamera(modelData.id) : ({})
                                return modelData.available !== false || !!registered.id || registered.conflict === true
                            }
                            highlighted: deviceCombo.highlightedIndex === index
                            palette.text: root.textPrimary
                            palette.highlight: root.hover
                        }
                        onActivated: {
                            root.selectDevice(model[currentIndex])
                        }
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera" && root.editingSettings; text: qsTr("Resolution") }
                    ScenesComboBox {
                        id: resolutionCombo
                        objectName: "cameraResolutionCombo"
                        theme: root.theme
                        visible: root.cameraKind === "local_camera" && root.editingSettings
                        Layout.fillWidth: true
                        model: root.resolutions
                        textRole: "label"
                        currentIndex: root.findIndex(model, "id", root.selectedFormat.resolutionId || "")
                        onActivated: root.selectResolution(model[currentIndex].id)
                    }

                    FieldLabel { visible: root.cameraKind === "local_camera" && root.editingSettings; text: qsTr("Frame rate") }
                    ScenesComboBox {
                        id: frameRateCombo
                        objectName: "cameraFrameRateCombo"
                        theme: root.theme
                        visible: root.cameraKind === "local_camera" && root.editingSettings
                        enabled: root.formatId.length > 0 && root.cameraDevice.available !== false
                        Layout.fillWidth: true
                        model: root.frameRates
                        textRole: "label"
                        currentIndex: root.findIndex(model, "id", root.selectedFormat.fpsId || "")
                        onActivated: root.selectFrameRate(model[currentIndex].id)
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera" && root.editingSettings; text: qsTr("RTSP address") }
                    StyledField { id: uriField; objectName: "cameraUriField"; visible: root.cameraKind === "rtsp_camera" && root.editingSettings; Layout.fillWidth: true; placeholderText: "rtsp://camera.local/stream" }

                    FieldLabel { visible: streamCombo.visible; text: qsTr("Stream") }
                    ScenesComboBox {
                        id: streamCombo
                        objectName: "cameraStreamCombo"
                        theme: root.theme
                        visible: root.cameraKind === "rtsp_camera" && root.editingSettings && root.onvifStreams.length > 1
                        Layout.fillWidth: true
                        model: root.onvifStreams.map(function(stream) { return stream.name })
                        currentIndex: root.streamIndex
                        onActivated: root.selectStream(currentIndex)
                    }

                    // The stream login. Kept out of the address, which is written to
                    // disk and printed by the engine; stored in the system keyring
                    // and put back only when connecting. Separate from the PTZ login
                    // below: the two accounts are routinely different.
                    FieldLabel { visible: root.cameraKind === "rtsp_camera" && root.editingSettings; text: qsTr("Stream user name") }
                    StyledField {
                        id: streamUsernameField
                        objectName: "cameraStreamUsernameField"
                        onTextEdited: root.streamCredentialsEdited = true
                        visible: root.cameraKind === "rtsp_camera" && root.editingSettings
                        Layout.fillWidth: true
                        placeholderText: root.hasStreamCredentials
                            ? qsTr("Leave blank to keep saved credentials")
                            : qsTr("Only if the camera asks for one")
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera" && root.editingSettings; text: qsTr("Stream password") }
                    StyledField {
                        id: streamPasswordField
                        objectName: "cameraStreamPasswordField"
                        onTextEdited: root.streamCredentialsEdited = true
                        visible: root.cameraKind === "rtsp_camera" && root.editingSettings
                        Layout.fillWidth: true
                        echoMode: TextInput.Password
                        placeholderText: root.hasStreamCredentials
                            ? qsTr("Leave blank to keep saved credentials")
                            : ""
                    }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera" && root.editingSettings && root.hasStreamCredentials; text: "" }
                    ScenesCheckBox {
                        theme: root.theme
                        visible: root.cameraKind === "rtsp_camera" && root.editingSettings && root.hasStreamCredentials
                        text: qsTr("Remove saved stream credentials")
                        checked: root.clearStreamCredentials
                        onToggled: root.clearStreamCredentials = checked
                    }
                }
                Text {
                    objectName: "cameraDeviceConflictMessage"
                    Layout.fillWidth: true
                    visible: root.deviceConflict
                    text: root.deviceConflictMessage
                    color: root.danger
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                    Accessible.role: Accessible.AlertMessage
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
                RowLayout {
                    objectName: "cameraDiscoveredConnectRow"
                    Layout.fillWidth: true
                    visible: root.cameraKind === "rtsp_camera" && root.editingSettings && !!root.discoveredCamera.id
                    spacing: 10
                    Text {
                        objectName: "cameraDiscoveredStatus"
                        Layout.fillWidth: true
                        text: root.streamQueryRunning ? qsTr("Asking the camera for its streams…") : root.streamQueryMessage
                        color: root.streamQueryFailed ? root.danger : root.textSecondary
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        Accessible.role: root.streamQueryFailed ? Accessible.AlertMessage : Accessible.StaticText
                    }
                    ScenesDialogButton {
                        objectName: "cameraDiscoveredConnectButton"
                        theme: root.theme
                        text: qsTr("Connect")
                        enabled: !root.streamQueryRunning
                        onClicked: root.queryDiscoveredStreams()
                    }
                }
                Text {
                    Layout.fillWidth: true
                    visible: root.cameraKind === "rtsp_camera" && root.editingSettings
                    text: qsTr("Credentials are stored separately. Do not put a username, password, or token in the RTSP address.")
                    color: root.textMuted
                    font.pixelSize: 9
                    wrapMode: Text.Wrap
                }
            }

            FormSection {
                visible: root.editingSettings
                titleText: qsTr("Advanced")
                ScenesCheckBox {
                    objectName: "cameraAdvancedToggle"
                    theme: root.theme
                    text: qsTr("Show advanced settings")
                    checked: root.advancedExpanded
                    onToggled: root.advancedExpanded = checked
                }
                GridLayout {
                    visible: root.advancedExpanded
                    Layout.fillWidth: true
                    columns: width >= 470 ? 2 : 1
                    columnSpacing: 12
                    rowSpacing: 9
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

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Transport") }
                    ScenesComboBox { id: transportCombo; theme: root.theme; visible: root.cameraKind === "rtsp_camera"; Layout.fillWidth: true; model: ["TCP", "UDP"] }

                    FieldLabel { visible: root.cameraKind === "rtsp_camera"; text: qsTr("Network buffer") }
                    ScenesSpinBox { id: latencyField; theme: root.theme; visible: root.cameraKind === "rtsp_camera"; Layout.fillWidth: true; from: 0; to: 10000 }

                }
                FieldLabel { visible: root.advancedExpanded && root.cameraKind === "local_camera"; text: qsTr("Format") }
                ScenesComboBox {
                    id: formatCombo
                    objectName: "cameraFormatCombo"
                    theme: root.theme
                    visible: root.advancedExpanded && root.cameraKind === "local_camera"
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
                visible: root.editingSettings && root.advancedExpanded
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
                    FieldLabel { text: "" }
                    ScenesDialogButton {
                        objectName: "cameraPtzFindButton"
                        theme: root.theme
                        Layout.alignment: Qt.AlignLeft
                        text: qsTr("Find PTZ settings…")
                        enabled: !root.ptzQueryRunning
                        onClicked: root.openPtzFinder()
                    }
                    FieldLabel { text: qsTr("PTZ service") }
                    StyledField { id: onvifEndpoint; Layout.fillWidth: true; placeholderText: "https://camera.local/onvif/ptz_service" }
                    FieldLabel { visible: ptzProfileCombo.visible; text: qsTr("PTZ profile") }
                    ScenesComboBox {
                        id: ptzProfileCombo
                        objectName: "cameraPtzProfileCombo"
                        theme: root.theme
                        visible: root.ptzProfiles.length > 1
                        Layout.fillWidth: true
                        model: root.ptzProfiles.map(function(profile) { return profile.name })
                        currentIndex: Math.max(0, root.findIndex(root.ptzProfiles, "token", onvifProfileToken.text))
                        onActivated: onvifProfileToken.text = root.ptzProfiles[currentIndex].token
                    }
                    FieldLabel { text: qsTr("Profile token") }
                    StyledField { id: onvifProfileToken; Layout.fillWidth: true; placeholderText: qsTr("Media profile token") }
                    FieldLabel { text: qsTr("Username") }
                    StyledField { id: usernameField; objectName: "cameraPtzUsernameField"; Layout.fillWidth: true; placeholderText: root.hasCredentials ? qsTr("Leave blank to keep saved credentials") : "" }
                    FieldLabel { text: qsTr("Password") }
                    StyledField { id: passwordField; objectName: "cameraPtzPasswordField"; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: root.hasCredentials ? qsTr("Leave blank to keep saved credentials") : "" }
                    FieldLabel { visible: root.hasCredentials; text: "" }
                    ScenesCheckBox { theme: root.theme; visible: root.hasCredentials; text: qsTr("Remove saved credentials"); checked: root.clearCredentials; onToggled: root.clearCredentials = checked }
                }

                ColumnLayout {
                    objectName: "cameraPtzFinder"
                    visible: ptzCombo.currentIndex === 1 && (root.ptzFinderOpen || root.ptzQueryMessage.length > 0 || root.ptzQueryRunning)
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        visible: root.ptzFinderOpen
                        text: qsTr("Choose the camera to read its PTZ settings from.")
                        color: root.textSecondary
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    ScenesSourceSelector {
                        id: ptzFinderSelector
                        objectName: "cameraPtzFinderSelector"
                        theme: root.theme
                        Layout.fillWidth: true
                        visible: root.ptzFinderOpen
                        enabled: !root.ptzQueryRunning
                        options: root.ptzFinderChoices
                        searchPlaceholder: qsTr("Search cameras…")
                        onChosen: function(choice) { root.queryPtz(choice) }
                    }
                    NetworkSearchRow { visible: root.ptzFinderOpen }
                    Text {
                        objectName: "cameraPtzFinderStatus"
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: root.ptzQueryRunning ? qsTr("Asking the camera for its PTZ settings…") : root.ptzQueryMessage
                        color: root.ptzQueryFailed ? root.danger : root.textSecondary
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        Accessible.role: root.ptzQueryFailed ? Accessible.AlertMessage : Accessible.StaticText
                    }
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
        implicitHeight: Math.max(64, footerColumn.implicitHeight + 20)
        color: root.surface
        ColumnLayout {
            id: footerColumn
            anchors.fill: parent
            anchors.margins: 10
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            Text {
                objectName: "cameraSaveError"
                Layout.fillWidth: true
                visible: text.length > 0
                text: root.validationError || (root.saveAttempted && root.bridge ? root.bridge.sourceEditError : "")
                color: root.danger
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Accessible.role: Accessible.AlertMessage
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true }
                ScenesDialogButton { theme: root.theme; text: qsTr("Cancel"); onClicked: root.close() }
                ScenesDialogButton {
                    objectName: "cameraSaveButton"
                    theme: root.theme
                    text: root.addToScene ? qsTr("Add") : qsTr("Save")
                    primary: true
                    enabled: !root.cameraAccessPending && !root.deviceConflict && !root.connectionChoicePending && !root.customResolutionPending && !root.customResolutionError.length
                        && (!root.addToScene || root.cameraKind !== "local_camera" || root.deviceId.length > 0)
                        && (!root.addToScene || (!root.nameError.length && layerNameField.text.trim().length > 0))
                    onClicked: root.save()
                }
            }
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

    // Multicast reaches this network only; the address search probes hosts
    // directly, so it also finds cameras behind a router.
    component NetworkSearchRow: ColumnLayout {
        id: searchRow
        Layout.fillWidth: true
        spacing: 8
        function searchAddress() {
            if (root.bridge && addressField.text.trim().length && !root.bridge.onvifDiscoveryRunning)
                root.bridge.searchOnvifAddress(addressField.text)
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            ScenesDialogButton {
                objectName: "cameraFindNetworkButton"
                theme: root.theme
                text: qsTr("Find cameras…")
                enabled: !!root.bridge && !root.bridge.onvifDiscoveryRunning
                onClicked: root.bridge.discoverOnvifCameras()
            }
            Text {
                objectName: "cameraFindNetworkStatus"
                Layout.fillWidth: true
                text: root.bridge ? root.bridge.onvifDiscoveryStatus : ""
                color: root.textSecondary
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            ScenesTextField {
                id: addressField
                objectName: "cameraSearchAddressField"
                theme: root.theme
                Layout.fillWidth: true
                maximumLength: 64
                placeholderText: qsTr("Camera address or range, such as 192.168.0.1-254")
                Accessible.name: qsTr("Camera address or range")
                onAccepted: searchRow.searchAddress()
            }
            ScenesDialogButton {
                objectName: "cameraSearchAddressButton"
                theme: root.theme
                text: qsTr("Search")
                enabled: !!root.bridge && !root.bridge.onvifDiscoveryRunning && addressField.text.trim().length > 0
                onClicked: searchRow.searchAddress()
            }
        }
    }

    component FieldLabel: Text {
        color: root.textMuted
        font.pixelSize: 10
        Layout.alignment: Qt.AlignVCenter
    }

    component StyledField: ScenesTextField {
        theme: root.theme
        onAccepted: root.save()
    }

    component SmallButton: Rectangle {
        id: small
        property string iconName: ""
        property string toolTipText: ""
        signal clicked()
        Layout.preferredWidth: 36; Layout.preferredHeight: 34
        radius: 9; color: smallMouse.containsMouse ? root.hover : root.surface
        border.width: 1; border.color: activeFocus ? root.accent : root.borderColor
        activeFocusOnTab: visible && enabled
        Accessible.role: Accessible.Button
        Accessible.name: toolTipText
        Accessible.onPressAction: small.clicked()
        Keys.onReturnPressed: small.clicked()
        Keys.onSpacePressed: small.clicked()
        Image { anchors.centerIn: parent; width: 14; height: 14; source: "image://sceneicons/" + small.iconName + "/16/" + String(root.textMuted).replace("#", "") }
        MouseArea { id: smallMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: small.clicked() }
        ToolTip.visible: smallMouse.containsMouse; ToolTip.text: small.toolTipText
    }

}
