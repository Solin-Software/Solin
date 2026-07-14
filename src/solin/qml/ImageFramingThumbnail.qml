import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root
    objectName: "imageFramingThumbnail"
    clip: true

    property string imageSource: ""
    property string placeholderSource: ""
    property real sourceAspectRatio: 0.0
    property real projectionAspectRatio: 16 / 9
    property var framing: null
    property color backgroundColor: "#000000"
    property color resetBackgroundColor: "#cc11151b"
    property color resetForegroundColor: "#ffffff"
    property string interactionHint: ""
    property string zoomHint: interactionHint
    property string resetToolTip: ""
    property bool editable: true
    property bool clickActionEnabled: true

    signal clicked()
    signal framingEdited(real zoom, real normX, real normY,
                         real sourceWidth, real sourceHeight,
                         bool snapZoomToCover)
    signal framingReset()
    signal pointerEntered()
    signal pointerExited()

    property bool framingActive: false
    property real framingZoom: 1.0
    property real framingNormX: 0.0
    property real framingNormY: 0.0
    property real requestedZoom: 1.0
    property real requestedNormX: 0.0
    property real requestedNormY: 0.0
    property bool geometrySyncQueued: false
    property bool panning: false
    property bool pressedForPan: false
    property bool gestureMoved: false
    property point pressPosition: Qt.point(0, 0)
    property real pressNormX: 0.0
    property real pressNormY: 0.0

    readonly property real safeAspectRatio:
        isFinite(projectionAspectRatio) && projectionAspectRatio > 0
        ? projectionAspectRatio : 16 / 9
    readonly property real containerAspectRatio: width > 0 && height > 0 ? width / height : 16 / 9
    // The projection frame is virtual: it covers the fixed 16:9 thumbnail.
    // This keeps the media occupying its historical thumbnail viewport while
    // preserving transform coordinates for the actual projection aspect.
    readonly property real frameWidth:
        containerAspectRatio > safeAspectRatio ? width : height * safeAspectRatio
    readonly property real frameHeight:
        containerAspectRatio > safeAspectRatio ? width / safeAspectRatio : height
    readonly property real frameX: (width - frameWidth) / 2
    readonly property real frameY: (height - frameHeight) / 2
    readonly property bool imageReady:
        sourceImage.status === Image.Ready
        && sourceImage.implicitWidth > 0
        && sourceImage.implicitHeight > 0
    readonly property bool hasSourceAspectRatio:
        isFinite(sourceAspectRatio) && sourceAspectRatio > 0
    readonly property real sourceWidth: hasSourceAspectRatio
        ? sourceAspectRatio : Math.max(1, sourceImage.implicitWidth)
    readonly property real sourceHeight: hasSourceAspectRatio
        ? 1.0 : Math.max(1, sourceImage.implicitHeight)
    // Projection geometry remains authoritative for bounds and snap.
    readonly property real projectionBaseScale: imageReady
        ? Math.min(frameWidth / sourceWidth, frameHeight / sourceHeight) : 1
    readonly property real projectionDrawnWidth:
        sourceWidth * projectionBaseScale * framingZoom
    readonly property real projectionDrawnHeight:
        sourceHeight * projectionBaseScale * framingZoom

    // Display geometry keeps the complete source visible at identity inside
    // the fixed thumbnail viewport, independently of projection aspect.
    readonly property real displayBaseScale: imageReady
        ? Math.min(width / sourceWidth, height / sourceHeight) : 1
    readonly property real projectionToDisplayScale:
        projectionBaseScale > 0 ? displayBaseScale / projectionBaseScale : 1
    readonly property real displayPanWidth:
        frameWidth * projectionToDisplayScale
    readonly property real displayPanHeight:
        frameHeight * projectionToDisplayScale
    readonly property real drawnWidth: sourceWidth * displayBaseScale * framingZoom
    readonly property real drawnHeight: sourceHeight * displayBaseScale * framingZoom
    readonly property bool panAvailable:
        editable
        && imageReady
        && framingActive
        && Math.abs(framingZoom - 1.0) > 0.000001
        && (maxPanX() > 0.000001 || maxPanY() > 0.000001)

    function fittedSource(sourceValue) {
        if (sourceValue === "")
            return ""
        if (sourceValue.indexOf("image://playlistthumbs/") === 0)
            return sourceValue + "/fit"
        return sourceValue
    }

    function validNumber(value) {
        return typeof value === "number" && isFinite(value)
    }

    function applyFraming(record) {
        if (record
                && record.version === 1
                && validNumber(record.zoom)
                && validNumber(record.norm_x)
                && validNumber(record.norm_y)) {
            requestedZoom = Math.max(0.1, Math.min(10.0, record.zoom))
            requestedNormX = record.norm_x
            requestedNormY = record.norm_y
            syncFramingToGeometry()
            return
        }
        framingActive = false
        requestedZoom = 1.0
        requestedNormX = 0.0
        requestedNormY = 0.0
        syncFramingToGeometry()
    }

    function maxPanX() {
        if (!imageReady || frameWidth <= 0)
            return 0
        return Math.max(
            0,
            (projectionDrawnWidth - frameWidth) / (2 * frameWidth))
    }

    function maxPanY() {
        if (!imageReady || frameHeight <= 0)
            return 0
        return Math.max(
            0,
            (projectionDrawnHeight - frameHeight) / (2 * frameHeight))
    }

    function clampPan() {
        if (!imageReady || frameWidth <= 0 || frameHeight <= 0)
            return false
        var maxX = maxPanX()
        var maxY = maxPanY()
        framingNormX = Math.max(-maxX, Math.min(maxX, framingNormX))
        framingNormY = Math.max(-maxY, Math.min(maxY, framingNormY))
        return true
    }

    function syncFramingToGeometry() {
        framingZoom = requestedZoom
        framingNormX = requestedNormX
        framingNormY = requestedNormY
        clampPan()
        framingActive = !isDefaultTransform()
    }

    function scheduleGeometrySync() {
        if (geometrySyncQueued)
            return
        geometrySyncQueued = true
        Qt.callLater(function() {
            geometrySyncQueued = false
            syncFramingToGeometry()
        })
    }

    function isDefaultTransform() {
        return Math.abs(framingZoom - 1.0) <= 0.000001
                && Math.abs(framingNormX) <= 0.000001
                && Math.abs(framingNormY) <= 0.000001
    }

    function coverZoom() {
        if (!imageReady || projectionBaseScale <= 0)
            return 1.0
        var fittedWidth = sourceWidth * projectionBaseScale
        var fittedHeight = sourceHeight * projectionBaseScale
        return Math.max(frameWidth / fittedWidth, frameHeight / fittedHeight)
    }

    function publishFraming(snapZoomToCover) {
        if (!imageReady)
            return
        requestedZoom = framingZoom
        requestedNormX = framingNormX
        requestedNormY = framingNormY
        framingActive = !isDefaultTransform()
        framingEdited(
            framingZoom,
            framingNormX,
            framingNormY,
            sourceWidth,
            sourceHeight,
            snapZoomToCover)
    }

    function applyZoomFactor(factor) {
        if (!editable || !imageReady || !isFinite(factor) || factor <= 0)
            return
        var previous = framingZoom
        var requested = Math.max(0.1, Math.min(10.0, previous * factor))
        if (Math.abs(requested - 1.0) <= 0.000001)
            requested = 1.0
        var cover = coverZoom()
        if (requested > previous && previous < cover && cover < requested)
            requested = cover
        framingZoom = requested
        clampPan()
        publishFraming(true)
    }

    onFramingChanged: applyFraming(framing)
    onImageReadyChanged: {
        if (imageReady)
            scheduleGeometrySync()
    }
    onSourceAspectRatioChanged: scheduleGeometrySync()
    onProjectionAspectRatioChanged: scheduleGeometrySync()
    onWidthChanged: scheduleGeometrySync()
    onHeightChanged: scheduleGeometrySync()
    Component.onCompleted: applyFraming(framing)

    Rectangle {
        anchors.fill: parent
        color: root.backgroundColor
    }

    Item {
        id: projectionFrame
        x: root.frameX
        y: root.frameY
        width: root.frameWidth
        height: root.frameHeight
        clip: true

        Image {
            id: sourceImage
            source: root.fittedSource(root.imageSource)
            sourceSize.width: 200
            sourceSize.height: 113
            asynchronous: true
            cache: false
            fillMode: Image.Stretch
            width: root.drawnWidth
            height: root.drawnHeight
            x: (root.width - width) / 2 - root.frameX
               + root.framingNormX * root.displayPanWidth
            y: (root.height - height) / 2 - root.frameY
               + root.framingNormY * root.displayPanHeight
            visible: root.imageReady
            onStatusChanged: {
                if (status === Image.Ready)
                    root.scheduleGeometrySync()
            }
        }

        Image {
            anchors.centerIn: parent
            visible: !root.imageReady && root.placeholderSource !== ""
            width: 22
            height: 22
            opacity: 0.88
            source: root.placeholderSource
        }
    }

    MouseArea {
        id: interactionArea
        objectName: "imageFramingInteractionArea"
        anchors.fill: parent
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        preventStealing: root.panAvailable
        cursorShape: root.panning ? Qt.ClosedHandCursor
                     : root.panAvailable ? Qt.OpenHandCursor
                     : root.clickActionEnabled
                       ? Qt.PointingHandCursor
                       : Qt.ArrowCursor

        onEntered: root.pointerEntered()
        onExited: root.pointerExited()

        onWheel: function(wheel) {
            if (!root.editable
                    || !root.imageReady
                    || !(wheel.modifiers & Qt.ControlModifier)) {
                wheel.accepted = false
                return
            }
            var delta = wheel.angleDelta.y !== 0
                    ? wheel.angleDelta.y : wheel.pixelDelta.y
            if (delta !== 0)
                root.applyZoomFactor(delta > 0 ? 1.12 : 1 / 1.12)
            wheel.accepted = true
        }

        onPressed: function(mouse) {
            root.pressPosition = Qt.point(mouse.x, mouse.y)
            root.pressNormX = root.framingNormX
            root.pressNormY = root.framingNormY
            root.panning = false
            root.gestureMoved = false
            root.pressedForPan = root.panAvailable
                    && mouse.x >= root.frameX
                    && mouse.x <= root.frameX + root.frameWidth
                    && mouse.y >= root.frameY
                    && mouse.y <= root.frameY + root.frameHeight
            mouse.accepted = true
        }

        onPositionChanged: function(mouse) {
            if (!pressed)
                return
            var dx = mouse.x - root.pressPosition.x
            var dy = mouse.y - root.pressPosition.y
            if (!root.gestureMoved && dx * dx + dy * dy >= 36)
                root.gestureMoved = true
            if (!root.pressedForPan || !root.gestureMoved)
                return
            root.panning = true
            root.framingNormX = root.pressNormX
                    + dx / Math.max(1, root.displayPanWidth)
            root.framingNormY = root.pressNormY
                    + dy / Math.max(1, root.displayPanHeight)
            root.clampPan()
            root.publishFraming(false)
        }

        onReleased: function(mouse) {
            var wasPanning = root.panning
            var wasMoved = root.gestureMoved
            root.pressedForPan = false
            root.panning = false
            root.gestureMoved = false
            if (!wasPanning && !wasMoved && root.clickActionEnabled)
                root.clicked()
            mouse.accepted = true
        }

        onCanceled: {
            root.pressedForPan = false
            root.panning = false
            root.gestureMoved = false
        }

        ToolTip.visible: containsMouse
                         && root.editable
                         && root.imageReady
                         && (root.panAvailable
                             ? root.interactionHint !== ""
                             : root.zoomHint !== "")
        ToolTip.delay: 650
        ToolTip.text: root.panAvailable
                      ? root.interactionHint : root.zoomHint
    }

    Rectangle {
        id: resetButton
        visible: root.framingActive
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        anchors.margins: 4
        width: 22
        height: 22
        radius: 11
        color: resetMouse.containsMouse
               ? Qt.lighter(root.resetBackgroundColor, 1.25)
               : root.resetBackgroundColor

        Text {
            anchors.centerIn: parent
            text: "↺"
            color: root.resetForegroundColor
            font.pixelSize: 14
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: resetMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: root.pointerEntered()
            onExited: root.pointerExited()
            onClicked: {
                root.applyFraming(null)
                root.framingReset()
            }
            ToolTip.visible: containsMouse && root.resetToolTip !== ""
            ToolTip.delay: 350
            ToolTip.text: root.resetToolTip
        }
    }
}
