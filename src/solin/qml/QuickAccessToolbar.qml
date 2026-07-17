import QtQuick

// ─────────────────────────────────────────────────────────────────────────────
// QuickAccessToolbar.qml
//
// Floating toolbar with monitor, OBS, camera, zoom buttons and a minimize
// chevron.  Rendered entirely in QML for proper alpha compositing (no black-
// corner artefacts over browser / web-engine content).
//
// IMPORTANT: the hosting QQuickWidget has a FIXED size that never changes.
// The pill Rectangle sizes itself from its content (btnRow) and is centred
// within the root Item.  This avoids OpenGL surface rebuilds on Windows
// that would destroy the alpha buffer.
//
// All state comes from the `bridge` context property (QuickToolbarBridge).
// ─────────────────────────────────────────────────────────────────────────────

Item {
    id: root

    property Item tooltipItem: null
    property string tooltipText: ""
    readonly property color toolbarSurface: appTheme.surfaceChrome
    readonly property color toolbarBorder: appTheme.borderChrome
    readonly property color toolbarHover: appTheme.hover
    readonly property color toolbarPressed: appTheme.hoverStrong
    readonly property color accent: appTheme.accent
    readonly property color success: appTheme.success
    readonly property color warning: appTheme.warning
    readonly property color iconMuted: appTheme.textDim
    readonly property color iconSecondary: appTheme.textMuted

    function withAlpha(c, a) {
        return Qt.rgba(c.r, c.g, c.b, a)
    }

    Timer {
        id: tooltipTimer
        interval: 700
        repeat: false
        onTriggered: {
            if (!root.tooltipItem || root.tooltipText === "")
                return
            var pos = root.tooltipItem.mapToItem(root, 0, 0)
            bridge.showTooltip(
                root.tooltipText,
                pos.x,
                pos.y,
                root.tooltipItem.width,
                root.tooltipItem.height
            )
        }
    }

    function beginButtonHover(item, text) {
        bridge.onPointerEntered()
        tooltipTimer.stop()
        bridge.hideTooltip()
        root.tooltipItem = item
        root.tooltipText = text
        if (text !== "")
            tooltipTimer.start()
    }

    function endButtonHover() {
        tooltipTimer.stop()
        root.tooltipItem = null
        root.tooltipText = ""
        bridge.hideTooltip()
        bridge.onPointerExited()
    }

    // ── Expanded Pill ────────────────────────────────────────────────────
    Rectangle {
        id: pill
        visible: bridge.pillVisible

        // Auto-size from content, centred horizontally in the fixed-width root.
        // In solidMode (macOS) the host widget is already content-sized, so the
        // pill fills it edge-to-edge — leaving NO transparent pixels (which the
        // macOS compositor would render black). Rounded corners are clipped on
        // the native layer instead of via QML transparency.
        width: bridge.solidMode ? parent.width : (btnRow.implicitWidth + 12 + 4)
        height: parent.height
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.verticalCenter: parent.verticalCenter

        radius: (bridge.solidMode || bridge.browserRectMode) ? 0 : 20
        color: bridge.solidMode
               ? root.withAlpha(root.toolbarSurface, 1.0)
               : root.withAlpha(root.toolbarSurface, 0.94)
        // solidMode: border is drawn on the native CALayer so it follows the
        // rounded-corner clip; a QML rectangular border would be clipped flat.
        border.width: bridge.solidMode ? 0 : 1
        border.color: root.withAlpha(root.toolbarBorder, 0.7)

        Row {
            id: btnRow
            anchors.centerIn: parent
            spacing: 3

            // ── Monitor ──────────────────────────────────────────────
            Item {
                width: 30; height: 30

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: monitorMA.pressed
                           ? root.toolbarPressed
                           : monitorMA.containsMouse
                             ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/monitor/14/" + bridge.monitorIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                MouseArea {
                    id: monitorMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(monitorMA, bridge.monitorTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onMonitorClicked()
                }
            }

            // ── Background Song ─────────────────────────────────────
            Item {
                visible: bridge.backgroundSongVisible
                width: 30; height: 30

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: backgroundSongMA.pressed
                           ? root.toolbarPressed
                           : backgroundSongMA.containsMouse
                             ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/background_song/14/" + bridge.backgroundSongIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                MouseArea {
                    id: backgroundSongMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(backgroundSongMA, bridge.backgroundSongTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onBackgroundSongClicked()
                }
            }

            // ── Separator ────────────────────────────────────────────
            Rectangle {
                visible: bridge.separatorVisible
                width: 1; height: 18
                color: root.withAlpha(root.toolbarBorder, 0.6)
                anchors.verticalCenter: parent.verticalCenter
            }

            // ── OBS ──────────────────────────────────────────────────
            Item {
                visible: bridge.obsVisible
                width: 30; height: 30

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: obsMA.pressed
                           ? root.toolbarPressed
                           : obsMA.containsMouse
                             ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/obs/14/" + bridge.obsIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                // Green connected dot
                Rectangle {
                    visible: bridge.obsDotVisible
                    x: 21; y: 3; width: 7; height: 7; radius: 3.5
                    color: root.success
                    border.width: 1.5
                    border.color: root.withAlpha(root.toolbarSurface, 0.95)
                }
                MouseArea {
                    id: obsMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(obsMA, bridge.obsTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onObsClicked()
                }
            }

            // ── Camera ───────────────────────────────────────────────
            Item {
                visible: bridge.cameraVisible
                width: 30; height: 30

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: cameraMA.pressed
                           ? root.toolbarPressed
                           : cameraMA.containsMouse
                             ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/camera/14/" + bridge.cameraIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                MouseArea {
                    id: cameraMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(cameraMA, bridge.cameraTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onCameraClicked()
                }
            }

            // ── Zoom ─────────────────────────────────────────────────
            Item {
                visible: bridge.zoomVisible
                width: 30; height: 30

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: zoomMA.pressed
                           ? root.toolbarPressed
                           : zoomMA.containsMouse
                             ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/zoom/14/" + bridge.zoomIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                MouseArea {
                    id: zoomMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(zoomMA, bridge.zoomTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onZoomClicked()
                }
            }

            // ── Remote control ───────────────────────────────────────
            Item {
                id: remoteControlButton
                visible: bridge.remoteControlVisible
                width: 30; height: 30
                activeFocusOnTab: visible
                Accessible.role: Accessible.Button
                Accessible.name: bridge.remoteControlTooltip
                Keys.onReturnPressed: bridge.onRemoteControlClicked()
                Keys.onEnterPressed: bridge.onRemoteControlClicked()
                Keys.onSpacePressed: bridge.onRemoteControlClicked()

                Rectangle {
                    anchors.fill: parent; radius: 15
                    color: remoteControlMA.pressed
                           ? root.toolbarPressed
                           : remoteControlMA.containsMouse || remoteControlButton.activeFocus
                             ? root.toolbarHover : "transparent"
                    border.width: remoteControlButton.activeFocus ? 1 : 0
                    border.color: root.accent
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/remote_control/14/" + bridge.remoteControlIconColor
                    sourceSize: Qt.size(14, 14)
                    cache: false
                }
                Rectangle {
                    visible: bridge.remoteControlBadge !== ""
                    x: 17; y: 0
                    width: 13; height: 13; radius: 6.5
                    color: root.accent
                    border.width: 1
                    border.color: root.toolbarSurface
                    Text {
                        anchors.centerIn: parent
                        text: bridge.remoteControlBadge
                        color: appTheme.textOnAccent
                        font.pixelSize: 8
                        font.weight: Font.DemiBold
                    }
                }
                Rectangle {
                    visible: bridge.remoteControlBadge === "" && bridge.remoteControlWarning
                    x: 22; y: 2; width: 7; height: 7; radius: 3.5
                    color: root.warning
                    border.width: 1
                    border.color: root.toolbarSurface
                }
                MouseArea {
                    id: remoteControlMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(remoteControlMA, bridge.remoteControlTooltip)
                    onExited: root.endButtonHover()
                    onClicked: {
                        remoteControlButton.forceActiveFocus()
                        bridge.onRemoteControlClicked()
                    }
                }
            }

            // ── Minimize chevron ─────────────────────────────────────
            Item {
                width: 22; height: 22
                anchors.verticalCenter: parent.verticalCenter

                Rectangle {
                    anchors.fill: parent; radius: 11
                    color: minMA.containsMouse
                           ? root.toolbarHover : "transparent"
                }
                Image {
                    anchors.centerIn: parent
                    source: "image://icons/chevron_down/10/" + String(root.iconMuted).replace("#", "")
                    sourceSize: Qt.size(10, 10)
                    cache: true
                }
                MouseArea {
                    id: minMA; anchors.fill: parent
                    hoverEnabled: true
                    onEntered: root.beginButtonHover(minMA, bridge.minimizeTooltip)
                    onExited: root.endButtonHover()
                    onClicked: bridge.onMinimizeClicked()
                }
            }
        }
    }

    // ── Minimized Tab ────────────────────────────────────────────────────
    Rectangle {
        id: miniTab
        visible: bridge.miniVisible

        // Windows: width is 34 with the right 8px clipped outside the widget
        // boundary, giving a flat/square right edge. solidMode (macOS): the host
        // widget is sized to the visible tab, so the mini tab fills it opaquely
        // (no transparent pixels → no black); corners clipped on the native layer.
        x: bridge.solidMode ? 0 : (parent.width - width + 8)
        y: bridge.solidMode ? 0 : (parent.height - 30) / 2
        width: bridge.solidMode ? parent.width : 34
        height: bridge.solidMode ? parent.height : 30
        radius: (bridge.solidMode || bridge.browserRectMode) ? 0 : 8

        color: (bridge.solidMode || bridge.browserRectMode)
               ? (miniMA.containsMouse ? root.withAlpha(root.toolbarSurface, 1.0) : root.withAlpha(root.toolbarSurface, 0.94))
               : (miniMA.containsMouse ? root.withAlpha(root.toolbarSurface, 0.92) : root.withAlpha(root.toolbarSurface, 0.55))
        // solidMode: border drawn on the native CALayer to follow the rounding.
        border.width: bridge.solidMode ? 0 : 1
        border.color: (bridge.solidMode || bridge.browserRectMode)
                      ? root.withAlpha(root.toolbarBorder, 0.7)
                      : (miniMA.containsMouse
                         ? root.withAlpha(root.accent, 0.35)
                         : root.withAlpha(root.toolbarBorder, 0.35))

        Image {
            anchors.verticalCenter: parent.verticalCenter
            x: 8  // Center of the visible 26px width (26 - 10)/2 = 8
            source: "image://icons/chevron_left/10/" + String(root.iconSecondary).replace("#", "")
            sourceSize: Qt.size(10, 10)
            cache: true
        }

        MouseArea {
            id: miniMA
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 26  // Constrain hover/clicks to the visible part
            hoverEnabled: true
            onEntered: root.beginButtonHover(miniMA, bridge.expandTooltip)
            onExited: root.endButtonHover()
            onClicked: bridge.onExpandClicked()
        }
    }
}
