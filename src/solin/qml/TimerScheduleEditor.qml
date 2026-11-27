pragma Translator: "AdvancedTimerPage"

// TimerScheduleEditor.qml — immersive, draft-based structure organizer for
// custom schedules. Replaces the navigation chrome while active, edits an
// in-memory draft so Cancel genuinely discards changes and Save commits
// atomically, and uses fluid animated displacement drag-and-drop inspired by
// Solin's PlaylistTreeView (animated placeholder + smoothly shifting cards +
// elevated floating card).
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15

Item {
    id: editor
    objectName: "timerScheduleEditor"

    property var scheduleData: ({})
    property color pal_bg: appTheme.bg
    property color pal_surface: appTheme.surface
    property color pal_surfaceAlt: appTheme.surfaceAlt
    property color pal_hover: appTheme.hover
    property color pal_border: appTheme.border_
    property color pal_borderStrong: appTheme.borderStrong
    property color pal_textPrimary: appTheme.textPrimary
    property color pal_textSecondary: appTheme.textSecondary
    property color pal_textMuted: appTheme.textMuted
    property color pal_accent: appTheme.accent
    property color pal_accentText: appTheme.accentText
    property color pal_danger: appTheme.danger

    signal cancelRequested()
    signal saved()
    signal moveRequested(string itemType, string itemId, string itemTitle, string currentSectionId, var draftEntries)
    signal addPartRequested(string defaultSectionId, var draftEntries)
    signal addSectionRequested()
    signal sectionAppearanceRequested(string sectionId, string sectionTitle, string iconChoice, string customColor)

    readonly property string scheduleTitle: draftScheduleTitle ? draftScheduleTitle : (scheduleData && scheduleData.title ? scheduleData.title : "")
    readonly property bool narrowEditor: width < 560
    readonly property int floatingToolbarClearance: 84

    // ── Local draft state (Cancel discards cleanly; Save commits atomically) ──
    property int draftGeneration: 0
    property string draftScheduleId: ""
    property int draftRevision: 0
    property string draftScheduleTitle: ""
    property var draftEntries: []
    property bool hasUnsavedChanges: false
    property int _draftCounter: 1

    // Layout geometry metrics
    readonly property int rowGap: 8
    readonly property int partH: 46
    readonly property int childPartH: 42
    readonly property int childGap: 6
    readonly property int secHeaderH: 44
    readonly property int secPad: 10
    readonly property int emptySlotH: 40

    // ── Fluid drag-and-drop state (PlaylistTreeView-style animated displacement) ──
    property bool dragActive: false
    property string dragType: ""          // "part" | "section"
    property string dragId: ""
    property string dragTitle: ""
    property string dragMeta: ""
    property string dragSectionIconKey: ""
    property color dragSectionAccent: pal_accent
    property color dragSectionBadgeBg: pal_surfaceAlt
    property real dragCardHeight: 46
    property real dragOriginY: 0
    property real dragGhostX: 0
    property real dragGhostY: 0
    property real dragGrabOffsetX: 28
    property real dragGrabOffsetY: 20
    property real lastDragPointerX: 0
    property real lastDragPointerY: 0

    // Computed live drop slot in draft coordinate space:
    // dropMode: "none" | "root" | "section"
    property string dropMode: "none"
    property string dropSectionId: ""
    property int dropIndex: -1

    // Computed layout positions for every entry & child part + placeholder
    property bool layoutAnimationsReady: false
    property var layoutMap: ({})
    property real placeholderX: 0
    property real placeholderY: 0
    property real placeholderW: 200
    property real placeholderH: 44
    property bool placeholderVisible: false
    property real totalContentHeight: 120

    function cloneEntries(source) {
        if (!source || !source.length) return []
        return JSON.parse(JSON.stringify(source))
    }

    function loadDraftFromSchedule() {
        draftGeneration++
        layoutAnimationsReady = false
        clearDragState()
        draftScheduleId = scheduleData && scheduleData.id ? scheduleData.id : ""
        draftRevision = scheduleData && scheduleData.revision !== undefined ? scheduleData.revision : 0
        draftScheduleTitle = scheduleData && scheduleData.title ? scheduleData.title : ""
        draftEntries = cloneEntries(scheduleData && scheduleData.entries ? scheduleData.entries : [])
        hasUnsavedChanges = false
        recomputeLayout()
        Qt.callLater(function() { layoutAnimationsReady = true })
    }

    // Mutators invoked by dialogs when editing inside the draft
    function applyDraftAddPart(title, plannedSeconds, sectionId) {
        var next = cloneEntries(draftEntries)
        var newId = "draft-part-" + (_draftCounter++)
        var partObj = {
            type: "part",
            id: newId,
            title: title,
            plannedSeconds: plannedSeconds,
            plannedLabel: timer.formatPlannedDuration(plannedSeconds, false),
            state: "idle",
            durationEditable: true,
            sectionId: sectionId || ""
        }
        var destination = destinationEntries(next, sectionId)
        if (!destination) return
        destination.push(partObj)
        if (sectionId) updateSectionSummary(next.find(entry => entry.id === sectionId))
        draftEntries = next
        hasUnsavedChanges = true
        recomputeLayout()
    }

    function applyDraftAddSection(title, iconChoice, colorHex) {
        var next = cloneEntries(draftEntries)
        var newId = "draft-sec-" + (_draftCounter++)
        var resolvedIcon = iconChoice || "default"
        var style = timer.customSectionPalette(colorHex || "")
        next.push({
            type: "section",
            id: newId,
            title: title,
            icon: resolvedIcon,
            iconKey: resolvedIcon === "none" ? "" : (resolvedIcon === "default" ? timer.defaultSectionIconKey : resolvedIcon),
            color: colorHex || "",
            accentColor: style.accent,
            textColor: style.text,
            badgeBg: style.badge,
            borderColor: style.border,
            partCount: 0,
            totalLabel: timer.formatPlannedDuration(0, true),
            parts: []
        })
        draftEntries = next
        hasUnsavedChanges = true
        recomputeLayout()
    }

    function applyDraftSectionAppearance(sectionId, iconChoice, colorHex) {
        var next = cloneEntries(draftEntries)
        var resolvedIcon = iconChoice || "default"
        var style = timer.customSectionPalette(colorHex || "")
        var section = next.find(entry => entry.type === "section" && entry.id === sectionId)
        if (!section || (section.icon === resolvedIcon && section.color === (colorHex || ""))) return
        section.icon = resolvedIcon
        section.iconKey = resolvedIcon === "none" ? "" : (resolvedIcon === "default" ? timer.defaultSectionIconKey : resolvedIcon)
        section.color = colorHex || ""
        section.accentColor = style.accent
        section.textColor = style.text
        section.badgeBg = style.badge
        section.borderColor = style.border
        draftEntries = next
        hasUnsavedChanges = true
        recomputeLayout()
    }

    function destinationEntries(entries, sectionId) {
        if (!sectionId) return entries
        var section = entries.find(entry => entry.type === "section" && entry.id === sectionId)
        return section ? section.parts : null
    }

    function movedEntries(itemType, itemId, targetSectionId, beforeId) {
        if (beforeId === itemId || (itemType === "section" && targetSectionId)) return null
        var next = cloneEntries(draftEntries)
        var moved = null
        if (itemType === "section") {
            var index = next.findIndex(entry => entry.type === "section" && entry.id === itemId)
            if (index >= 0) moved = next.splice(index, 1)[0]
        } else if (itemType === "part") {
            moved = removePartFromEntries(next, itemId)
        }
        if (!moved) return null

        var destination = destinationEntries(next, targetSectionId)
        if (!destination) return null
        var insertAt = beforeId ? destination.findIndex(entry => entry.id === beforeId) : destination.length
        if (insertAt < 0) return null
        if (itemType === "part") moved.sectionId = targetSectionId || ""
        destination.splice(insertAt, 0, moved)
        if (targetSectionId) {
            var section = next.find(entry => entry.id === targetSectionId)
            updateSectionSummary(section)
        }
        return JSON.stringify(next) === JSON.stringify(draftEntries) ? null : next
    }

    function applyDraftMove(itemType, itemId, targetSectionId, beforeId) {
        var next = movedEntries(itemType, itemId, targetSectionId, beforeId)
        if (!next) return
        draftEntries = next
        hasUnsavedChanges = true
        recomputeLayout()
    }

    function deleteDraftPart(partId) {
        var next = cloneEntries(draftEntries)
        if (removePartFromEntries(next, partId)) {
            draftEntries = next
            hasUnsavedChanges = true
            recomputeLayout()
        }
    }

    function deleteDraftSection(sectionId) {
        var next = cloneEntries(draftEntries)
        for (var i = 0; i < next.length; i++) {
            if (next[i].type === "section" && next[i].id === sectionId && (!next[i].parts || next[i].parts.length === 0)) {
                next.splice(i, 1)
                draftEntries = next
                hasUnsavedChanges = true
                recomputeLayout()
                return
            }
        }
    }

    function entryIdAt(entries, index) {
        return index >= 0 && index < entries.length ? entries[index].id : ""
    }

    function focusMoveControl(item, itemId, direction) {
        if (item.moveFocusId === itemId && item.moveFocusDirection === direction && item.enabled && item.visible) {
            item.forceActiveFocus(Qt.TabFocusReason)
            var position = item.mapToItem(editorFlick.contentItem, 0, 0).y
            var target = editorFlick.contentY
            if (position < target)
                target = position
            else if (position + item.height > target + editorFlick.height)
                target = position + item.height - editorFlick.height
            editorFlick.contentY = Math.max(0, Math.min(target, editorFlick.contentHeight - editorFlick.height))
            return true
        }
        for (var i = 0; i < item.children.length; i++) {
            if (focusMoveControl(item.children[i], itemId, direction)) return true
        }
        return false
    }

    function restoreMoveFocus(itemId, direction) {
        Qt.callLater(function() {
            if (!focusMoveControl(treeCanvas, itemId, direction))
                focusMoveControl(treeCanvas, itemId, -direction)
        })
    }

    function movePartFromControl(itemId, direction, hadFocus) {
        movePartStep(itemId, direction)
        if (hadFocus) restoreMoveFocus(itemId, direction)
    }

    function moveSectionFromControl(index, direction, hadFocus) {
        var itemId = draftEntries[index].id
        moveSectionStep(index, direction)
        if (hadFocus) restoreMoveFocus(itemId, direction)
    }

    function moveSectionStep(entryIndex, delta) {
        var target = entryIndex + delta
        if (target < 0 || target >= draftEntries.length) return
        applyDraftMove("section", draftEntries[entryIndex].id, "",
                       entryIdAt(draftEntries, delta > 0 ? target + 1 : target))
    }

    function movePartStep(partId, delta) {
        for (var i = 0; i < draftEntries.length; i++) {
            var entry = draftEntries[i]
            if (entry.type === "part" && entry.id === partId) {
                var neighborIndex = i + delta
                if (neighborIndex < 0 || neighborIndex >= draftEntries.length) return
                var neighbor = draftEntries[neighborIndex]
                if (neighbor.type === "section") {
                    applyDraftMove("part", partId, neighbor.id,
                                   delta > 0 ? entryIdAt(neighbor.parts, 0) : "")
                } else {
                    applyDraftMove("part", partId, "",
                                   entryIdAt(draftEntries, delta > 0 ? neighborIndex + 1 : neighborIndex))
                }
                return
            }
            if (entry.type !== "section") continue
            var childIndex = entry.parts.findIndex(part => part.id === partId)
            if (childIndex < 0) continue
            var target = childIndex + delta
            if (target >= 0 && target < entry.parts.length) {
                applyDraftMove("part", partId, entry.id,
                               entryIdAt(entry.parts, delta > 0 ? target + 1 : target))
            } else {
                applyDraftMove("part", partId, "", entryIdAt(draftEntries, delta < 0 ? i : i + 1))
            }
            return
        }
    }

    function removePartFromEntries(list, partId) {
        for (var i = 0; i < list.length; i++) {
            if (list[i].type === "part" && list[i].id === partId) {
                return list.splice(i, 1)[0]
            }
            if (list[i].type === "section" && list[i].parts) {
                for (var j = 0; j < list[i].parts.length; j++) {
                    if (list[i].parts[j].id === partId) {
                        var removed = list[i].parts.splice(j, 1)[0]
                        updateSectionSummary(list[i])
                        return removed
                    }
                }
            }
        }
        return null
    }

    function updateSectionSummary(section) {
        section.partCount = section.parts.length
        var total = section.parts.reduce((seconds, part) => seconds + part.plannedSeconds, 0)
        section.totalLabel = timer.formatPlannedDuration(total, true)
    }

    function saveDraft() {
        if (!hasUnsavedChanges) {
            editor.saved()
            return
        }
        if (timer.saveCustomScheduleDraft(editor.draftScheduleId, editor.draftRevision, editor.draftEntries)) {
            hasUnsavedChanges = false
            editor.saved()
        }
    }

    // ── Deterministic animated layout calculator (drives smooth Y transitions!) ──
    function recomputeLayout() {
        var map = {}
        var listW = Math.max(200, treeCanvas.width)
        var y = 0
        var pVisible = false
        var pX = 0
        var pY = 0
        var pW = listW
        var pH = dragCardHeight

        // Visible top-level entries excluding dragged top-level item
        var visibleTop = []
        for (var i = 0; i < draftEntries.length; i++) {
            var e = draftEntries[i]
            if (dragActive && dragId === e.id) continue
            visibleTop.push(e)
        }

        var rootDropIdx = (dragActive && dropMode === "root")
                          ? Math.max(0, Math.min(dropIndex, visibleTop.length))
                          : -1

        for (var t = 0; t <= visibleTop.length; t++) {
            if (t === rootDropIdx) {
                pVisible = true
                pX = 0
                pY = y
                pW = listW
                pH = dragCardHeight
                y += pH + rowGap
            }
            if (t === visibleTop.length) break

            var entry = visibleTop[t]
            if (entry.type === "part") {
                map[entry.id] = { y: y, h: partH }
                y += partH + rowGap
            } else {
                var secTop = y
                var innerY = secPad + secHeaderH + 6
                var visibleParts = []
                var parts = entry.parts || []
                for (var k = 0; k < parts.length; k++) {
                    if (dragActive && dragType === "part" && dragId === parts[k].id) continue
                    visibleParts.push(parts[k])
                }

                var secHasDrop = dragActive && dragType === "part" && dropMode === "section" && dropSectionId === entry.id
                var childDropIdx = secHasDrop ? Math.max(0, Math.min(dropIndex, visibleParts.length)) : -1

                if (visibleParts.length === 0 && !secHasDrop) {
                    innerY += emptySlotH
                } else {
                    for (var c = 0; c <= visibleParts.length; c++) {
                        if (c === childDropIdx) {
                            pVisible = true
                            pX = secPad + 14
                            pY = secTop + innerY
                            pW = Math.max(120, listW - (secPad + 14) - secPad)
                            pH = childPartH
                            innerY += childPartH + childGap
                        }
                        if (c === visibleParts.length) break
                        map[visibleParts[c].id] = { y: innerY, h: childPartH }
                        innerY += childPartH + childGap
                    }
                    if (visibleParts.length > 0 || secHasDrop)
                        innerY -= childGap
                }

                var secHeight = innerY + secPad
                map[entry.id] = { y: secTop, h: secHeight }
                y += secHeight + rowGap
            }
        }

        layoutMap = map
        placeholderX = pX
        placeholderY = pY
        placeholderW = pW
        placeholderH = pH
        placeholderVisible = pVisible
        totalContentHeight = Math.max(96, y)
    }

    function resetDragState() {
        dragActive = false
        dragType = ""
        dragId = ""
        dragTitle = ""
        dragMeta = ""
        dragSectionIconKey = ""
        dropMode = "none"
        dropSectionId = ""
        dropIndex = -1
        placeholderVisible = false
        autoScrollTimer.stop()
    }

    function clearDragState() {
        resetDragState()
        recomputeLayout()
    }

    function beginDrag(type, itemObj, cardHeight, editorPoint, grabX, grabY, originY) {
        editor.forceActiveFocus()
        dragType = type
        dragId = itemObj.id
        dragTitle = itemObj.title || ""
        dragMeta = type === "part" ? (itemObj.plannedLabel || "") : (itemObj.totalLabel || "")
        dragSectionIconKey = type === "section" ? (itemObj.iconKey || "") : ""
        dragSectionAccent = (type === "section" && itemObj.accentColor) ? itemObj.accentColor : pal_accent
        dragSectionBadgeBg = (type === "section" && itemObj.badgeBg) ? itemObj.badgeBg : pal_surfaceAlt
        dragCardHeight = cardHeight
        dragOriginY = originY !== undefined ? originY : 0
        dragGrabOffsetX = Math.max(16, Math.min(180, grabX))
        dragGrabOffsetY = Math.max(12, Math.min(32, grabY))
        dragActive = true
        updateDrag(editorPoint)
    }

    function updateDrag(editorPoint) {
        lastDragPointerX = editorPoint.x
        lastDragPointerY = editorPoint.y
        dragGhostX = Math.max(4, Math.min(editor.width - ghostCard.width - 4, editorPoint.x - dragGrabOffsetX))
        dragGhostY = Math.max(4, Math.min(editor.height - ghostCard.height - 4, editorPoint.y - dragGrabOffsetY))

        // Gentle auto-scroll near edges
        var flickPt = editor.mapToItem(editorFlick, editorPoint.x, editorPoint.y)
        if (flickPt.y < 42 && editorFlick.contentY > 0) {
            autoScrollTimer.scrollDelta = -11
            autoScrollTimer.start()
        } else if (flickPt.y > editorFlick.height - 42 && editorFlick.contentY < Math.max(0, editorFlick.contentHeight - editorFlick.height)) {
            autoScrollTimer.scrollDelta = 11
            autoScrollTimer.start()
        } else {
            autoScrollTimer.stop()
        }

        // Convert pointer Y to canvas space and compute smooth slot
        var canvasY = flickPt.y + editorFlick.contentY
        computeLiveDropSlot(canvasY)
    }

    function computeLiveDropSlot(canvasY) {
        var visibleTop = []
        for (var i = 0; i < draftEntries.length; i++) {
            if (draftEntries[i].id !== dragId)
                visibleTop.push(draftEntries[i])
        }

        var nextMode = "root"
        var nextSecId = ""
        var nextIdx = visibleTop.length

        if (dragType === "section") {
            for (var s = 0; s < visibleTop.length; s++) {
                var box = layoutMap[visibleTop[s].id]
                if (!box) continue
                if (canvasY < box.y + box.h * 0.5) {
                    nextIdx = s
                    break
                }
            }
        } else {
            for (var t = 0; t < visibleTop.length; t++) {
                var entry = visibleTop[t]
                var eBox = layoutMap[entry.id]
                if (!eBox) continue
                if (entry.type === "part") {
                    if (canvasY < eBox.y + eBox.h * 0.5) {
                        nextMode = "root"
                        nextSecId = ""
                        nextIdx = t
                        break
                    }
                } else {
                    // Top 14px band of section -> insert at root BEFORE section
                    if (canvasY < eBox.y + 14) {
                        nextMode = "root"
                        nextSecId = ""
                        nextIdx = t
                        break
                    }
                    // Inside section bounds -> place cleanly among its children!
                    if (canvasY <= eBox.y + eBox.h - 10) {
                        nextMode = "section"
                        nextSecId = entry.id
                        var visibleParts = []
                        var parts = entry.parts || []
                        for (var k = 0; k < parts.length; k++) {
                            if (parts[k].id !== dragId) visibleParts.push(parts[k])
                        }
                        nextIdx = visibleParts.length
                        for (var c = 0; c < visibleParts.length; c++) {
                            var cBox = layoutMap[visibleParts[c].id]
                            if (!cBox) continue
                            var absMid = eBox.y + cBox.y + cBox.h * 0.5
                            if (canvasY < absMid) {
                                nextIdx = c
                                break
                            }
                        }
                        break
                    }
                }
            }
        }

        if (nextMode !== dropMode || nextSecId !== dropSectionId || nextIdx !== dropIndex) {
            dropMode = nextMode
            dropSectionId = nextSecId
            dropIndex = nextIdx
            recomputeLayout()
        }
    }

    function finishDrag() {
        if (!dragActive) return
        var dType = dragType
        var dId = dragId
        var mode = dropMode
        var secId = dropSectionId
        var idx = dropIndex

        if (mode === "none" || idx < 0) {
            clearDragState()
            return
        }

        // Resolve the drop slot against the same destination used by keyboard and menu moves.
        var destination = destinationEntries(draftEntries, mode === "section" ? secId : "")
        var next = null
        if (destination) {
            var remaining = destination.filter(entry => entry.id !== dId)
            next = movedEntries(dType, dId, mode === "section" ? secId : "",
                                entryIdAt(remaining, idx))
        }
        if (!next) {
            clearDragState()
            return
        }

        // Disable Y/height animations across the atomic handoff from ghost/placeholder
        // to the newly committed order — every surrounding card is already in its final spot.
        layoutAnimationsReady = false
        resetDragState()

        draftEntries = next
        hasUnsavedChanges = true
        recomputeLayout()
        Qt.callLater(function() { layoutAnimationsReady = true })
    }

    onVisibleChanged: {
        if (visible) {
            loadDraftFromSchedule()
            forceActiveFocus()
        }
        else {
            draftGeneration++
            clearDragState()
        }
    }
    onWidthChanged: recomputeLayout()

    Keys.onEscapePressed: {
        if (dragActive) clearDragState()
        else editor.cancelRequested()
    }

    Timer {
        id: autoScrollTimer
        interval: 22
        repeat: true
        property real scrollDelta: 0
        onTriggered: {
            var prevY = editorFlick.contentY
            var maxY = Math.max(0, editorFlick.contentHeight - editorFlick.height)
            var nextY = Math.max(0, Math.min(maxY, prevY + scrollDelta))
            if (Math.abs(nextY - prevY) > 0.01) {
                editorFlick.contentY = nextY
                if (editor.dragActive) {
                    var flickPt = editor.mapToItem(editorFlick, editor.lastDragPointerX, editor.lastDragPointerY)
                    editor.computeLiveDropSlot(flickPt.y + editorFlick.contentY)
                }
            } else {
                autoScrollTimer.stop()
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12

        // ═══════════════════════════════════════════════════════════════════════
        // IMMERSIVE EDIT MODE HEADER (clearly distinct from schedule items!)
        // ═══════════════════════════════════════════════════════════════════════
        Rectangle {
            objectName: "timerEditorHeaderBar"
            Layout.fillWidth: true
            implicitHeight: editHeaderFlow.implicitHeight + 22
            radius: 14
            color: editor.pal_surfaceAlt
            border.color: Qt.rgba(editor.pal_accent.r, editor.pal_accent.g, editor.pal_accent.b, 0.40)
            border.width: 1

            // Subtle top-left accent bar signaling Edit Mode
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.topMargin: 8
                anchors.bottomMargin: 8
                width: 3
                radius: 1.5
                color: editor.pal_accent
            }

            Flow {
                id: editHeaderFlow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.leftMargin: 16
                anchors.rightMargin: 12
                anchors.topMargin: 11
                anchors.bottomMargin: 11
                spacing: 8

                ColumnLayout {
                    width: editor.narrowEditor
                           ? editHeaderFlow.width
                           : Math.max(170, editHeaderFlow.width - editActionsRow.implicitWidth - 12)
                    spacing: 2

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 7
                        TimerIcon {
                            width: 15; height: 15
                            rasterSize: 30
                            iconName: "sliders"
                            tint: editor.pal_accentText
                        }
                        Text {
                            Layout.fillWidth: true
                            textFormat: Text.PlainText
                            text: editor.scheduleTitle
                                  ? qsTr("Editing: %1").arg(editor.scheduleTitle)
                                  : qsTr("Edit schedule")
                            color: editor.pal_textPrimary
                            font.pixelSize: 15
                            font.weight: Font.Bold
                            elide: Text.ElideRight
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Drag items to reorder, add parts or sections, then save your changes.")
                        color: editor.pal_textMuted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }
                }

                RowLayout {
                    id: editActionsRow
                    spacing: 6

                    TimerButton {
                        objectName: "timerEditorAddSectionButton"
                        text: editor.narrowEditor ? "" : qsTr("Add section")
                        iconName: timer.defaultSectionIconKey
                        iconSize: 13
                        tip: qsTr("Add section")
                        variant: "ghost"
                        Layout.preferredWidth: editor.narrowEditor ? 34 : -1
                        implicitHeight: 34
                        onClicked: editor.addSectionRequested()
                    }

                    TimerButton {
                        objectName: "timerEditorAddPartButton"
                        text: editor.narrowEditor ? "" : qsTr("Add part")
                        iconName: "plus"
                        iconSize: 13
                        tip: qsTr("Add part")
                        variant: "soft"
                        accent: editor.pal_accent
                        Layout.preferredWidth: editor.narrowEditor ? 34 : -1
                        implicitHeight: 34
                        onClicked: editor.addPartRequested("", editor.draftEntries)
                    }

                    Rectangle {
                        Layout.preferredWidth: 1
                        Layout.preferredHeight: 20
                        color: editor.pal_borderStrong
                        opacity: 0.7
                    }

                    TimerButton {
                        objectName: "timerEditorCancelButton"
                        text: qsTr("Cancel")
                        variant: "ghost"
                        implicitHeight: 34
                        onClicked: editor.cancelRequested()
                    }

                    TimerButton {
                        objectName: "timerEditorSaveButton"
                        text: qsTr("Save")
                        iconName: "check"
                        iconSize: 13
                        variant: "primary"
                        accent: editor.pal_accent
                        implicitHeight: 34
                        onClicked: editor.saveDraft()
                    }
                }
            }
        }

        // ═══════════════════════════════════════════════════════════════════════
        // FLUID ANIMATED TREE CANVAS
        // ═══════════════════════════════════════════════════════════════════════
        Flickable {
            id: editorFlick
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: Math.max(editor.totalContentHeight, 120) + editor.floatingToolbarClearance
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: function(ev) {
                    var dy = ev.pixelDelta.y !== 0 ? ev.pixelDelta.y : ev.angleDelta.y / 2.2
                    var maxY = Math.max(0, editorFlick.contentHeight - editorFlick.height)
                    var prevY = editorFlick.contentY
                    editorFlick.contentY = Math.max(0, Math.min(maxY, editorFlick.contentY - dy))
                    if (dragActive && editorFlick.contentY !== prevY)
                        updateDrag({ x: lastDragPointerX, y: lastDragPointerY })
                    ev.accepted = true
                }
            }

            Item {
                id: treeCanvas
                width: editorFlick.width - (editorScroll.visible ? editorScroll.width + 6 : 0)
                height: editor.totalContentHeight
                onWidthChanged: editor.recomputeLayout()

                // Empty schedule hint
                Rectangle {
                    visible: editor.draftEntries.length === 0
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 92
                    radius: 12
                    color: editor.pal_surface
                    border.color: editor.pal_border
                    border.width: 1

                    ColumnLayout {
                        anchors.centerIn: parent
                        spacing: 6
                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: qsTr("This schedule has no parts yet.")
                            color: editor.pal_textSecondary
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: qsTr("Use Add part or Add section above to build it.")
                            color: editor.pal_textMuted
                            font.pixelSize: 12
                        }
                    }
                }

                // ── Animated drop placeholder (glides smoothly like PlaylistTreeView!) ──
                Rectangle {
                    id: dropPlaceholder
                    objectName: "timerDragPlaceholder"
                    visible: editor.placeholderVisible
                    x: editor.placeholderX
                    y: editor.placeholderY
                    width: editor.placeholderW
                    height: editor.placeholderH
                    z: 2
                    radius: 10
                    color: Qt.rgba(editor.pal_accent.r, editor.pal_accent.g, editor.pal_accent.b, 0.12)
                    border.color: editor.pal_accent
                    border.width: 2

                    Behavior on y { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                    Behavior on x { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                    Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                    Behavior on height { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                }

                // ── Animated top-level entries (root parts & sections) ────────
                Repeater {
                    id: entryRepeater
                    model: editor.draftEntries

                    delegate: Item {
                        id: entryWrapper
                        required property var modelData
                        required property int index

                        readonly property bool isSection: modelData.type === "section"
                        readonly property bool isBeingDragged: editor.dragActive && editor.dragId === modelData.id
                        readonly property var box: editor.layoutMap[modelData.id] || ({ y: 0, h: isSection ? 96 : editor.partH })

                        x: 0
                        y: isBeingDragged ? editor.dragOriginY : box.y
                        width: treeCanvas.width
                        height: isBeingDragged ? editor.dragCardHeight : box.h
                        // Keep visible: true while dragging so child MouseArea retains its mouse grab!
                        opacity: isBeingDragged ? 0.0 : 1.0
                        z: isBeingDragged ? -1 : (isSection ? 1 : 3)

                        Behavior on y { enabled: editor.layoutAnimationsReady && !entryWrapper.isBeingDragged; NumberAnimation { duration: 210; easing.type: Easing.OutCubic } }
                        Behavior on height { enabled: editor.layoutAnimationsReady && !entryWrapper.isBeingDragged; NumberAnimation { duration: 190; easing.type: Easing.OutCubic } }

                        // ── Root part card ────────────────────────────────────
                        Rectangle {
                            id: rootPartCard
                            objectName: "timerEditorPartRow"
                            visible: !entryWrapper.isSection
                            anchors.fill: parent
                            radius: 10
                            color: rootHover.hovered ? editor.pal_hover : editor.pal_surface
                            border.color: rootHover.hovered ? editor.pal_borderStrong : editor.pal_border
                            border.width: 1

                            Behavior on color { ColorAnimation { duration: 110 } }
                            Behavior on border.color { ColorAnimation { duration: 110 } }
                            HoverHandler { id: rootHover }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 8
                                anchors.rightMargin: 8
                                spacing: 6

                                // Drag handle spanning grip + title + duration for effortless dragging
                                Item {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.fillHeight: true

                                    RowLayout {
                                        anchors.fill: parent
                                        spacing: 6

                                        Item {
                                            Layout.preferredWidth: 24
                                            Layout.preferredHeight: 32
                                            TimerIcon {
                                                anchors.centerIn: parent
                                                width: 14; height: 14
                                                rasterSize: 28
                                                iconName: "grip"
                                                tint: rootDragMouse.containsMouse ? editor.pal_textPrimary : editor.pal_textMuted
                                            }
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            textFormat: Text.PlainText
                                            text: entryWrapper.modelData.title || ""
                                            color: editor.pal_textPrimary
                                            font.pixelSize: 13
                                            font.weight: Font.Medium
                                            elide: Text.ElideRight
                                        }

                                        Text {
                                            text: entryWrapper.modelData.plannedLabel || ""
                                            color: editor.pal_textMuted
                                            font.pixelSize: 12
                                        }
                                    }

                                    MouseArea {
                                        id: rootDragMouse
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                                        preventStealing: true
                                        property real pressX: 0
                                        property real pressY: 0
                                        property bool started: false
                                        onPressed: function(m) { pressX = m.x; pressY = m.y; started = false }
                                        onPositionChanged: function(m) {
                                            if (!pressed) return
                                            if (!started) {
                                                if (Math.hypot(m.x - pressX, m.y - pressY) < 5) return
                                                started = true
                                                var pt = mapToItem(editor, m.x, m.y)
                                                editor.beginDrag("part", entryWrapper.modelData, editor.partH, pt, m.x + 8, m.y + 4, entryWrapper.y)
                                            } else {
                                                editor.updateDrag(mapToItem(editor, m.x, m.y))
                                            }
                                        }
                                        onReleased: { if (started) editor.finishDrag(); started = false }
                                        onCanceled: { if (started) editor.clearDragState(); started = false }
                                    }
                                }

                                TimerButton {
                                    objectName: "timerPartMoveUpButton"
                                    property string moveFocusId: entryWrapper.modelData.id
                                    property int moveFocusDirection: -1
                                    iconName: "chevron_up"
                                    iconSize: 12
                                    tip: qsTr("Move up")
                                    variant: "ghost"
                                    enabled: entryWrapper.index > 0
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.movePartFromControl(moveFocusId, -1, activeFocus)
                                }

                                TimerButton {
                                    objectName: "timerPartMoveDownButton"
                                    property string moveFocusId: entryWrapper.modelData.id
                                    property int moveFocusDirection: 1
                                    iconName: "chevron_down"
                                    iconSize: 12
                                    tip: qsTr("Move down")
                                    variant: "ghost"
                                    enabled: entryWrapper.index < editor.draftEntries.length - 1
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.movePartFromControl(moveFocusId, 1, activeFocus)
                                }

                                TimerButton {
                                    objectName: "timerMovePartButton"
                                    text: editor.narrowEditor ? "" : qsTr("Move to…")
                                    iconName: editor.narrowEditor ? "sliders" : ""
                                    iconSize: 12
                                    tip: qsTr("Move to…")
                                    variant: "ghost"
                                    Layout.preferredWidth: editor.narrowEditor ? 28 : -1
                                    implicitHeight: 28
                                    onClicked: editor.moveRequested("part", entryWrapper.modelData.id, entryWrapper.modelData.title || "", "", editor.draftEntries)
                                }

                                TimerButton {
                                    objectName: "timerDeletePartButton"
                                    iconName: "trash"
                                    iconColor: editor.pal_danger
                                    iconSize: 13
                                    tip: qsTr("Delete part")
                                    variant: "ghost"
                                    accent: editor.pal_danger
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.deleteDraftPart(entryWrapper.modelData.id)
                                }
                            }
                        }

                        // ── Section container card ────────────────────────────
                        Rectangle {
                            id: sectionCard
                            objectName: "timerEditorSectionCard"
                            visible: entryWrapper.isSection
                            anchors.fill: parent
                            radius: 12
                            color: editor.pal_surface
                            readonly property color secAccent: entryWrapper.modelData.accentColor || editor.pal_accent
                            readonly property color secText: entryWrapper.modelData.textColor || editor.pal_textPrimary
                            readonly property color secBadgeBg: entryWrapper.modelData.badgeBg || editor.pal_surfaceAlt
                            readonly property bool isDropTarget: editor.dragActive && editor.dropMode === "section" && editor.dropSectionId === entryWrapper.modelData.id

                            border.color: isDropTarget ? editor.pal_accent : editor.pal_borderStrong
                            border.width: isDropTarget ? 1.5 : 1

                            Behavior on border.color { ColorAnimation { duration: 130 } }

                            // Section header row
                            RowLayout {
                                x: editor.secPad
                                y: editor.secPad
                                width: parent.width - editor.secPad * 2
                                height: editor.secHeaderH
                                spacing: 6

                                // Section drag grip
                                Item {
                                    Layout.preferredWidth: 24
                                    Layout.preferredHeight: 30
                                    TimerIcon {
                                        anchors.centerIn: parent
                                        width: 14; height: 14
                                        rasterSize: 28
                                        iconName: "grip"
                                        tint: secDragMouse.containsMouse ? editor.pal_textPrimary : editor.pal_textMuted
                                    }
                                    MouseArea {
                                        id: secDragMouse
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                                        preventStealing: true
                                        property real pressX: 0
                                        property real pressY: 0
                                        property bool started: false
                                        onPressed: function(m) { pressX = m.x; pressY = m.y; started = false }
                                        onPositionChanged: function(m) {
                                            if (!pressed) return
                                            if (!started) {
                                                if (Math.hypot(m.x - pressX, m.y - pressY) < 5) return
                                                started = true
                                                var pt = mapToItem(editor, m.x, m.y)
                                                editor.beginDrag("section", entryWrapper.modelData, Math.min(entryWrapper.height, 64), pt, m.x + 14, m.y + 14, entryWrapper.y)
                                            } else {
                                                editor.updateDrag(mapToItem(editor, m.x, m.y))
                                            }
                                        }
                                        onReleased: { if (started) editor.finishDrag(); started = false }
                                        onCanceled: { if (started) editor.clearDragState(); started = false }
                                    }
                                }

                                // Styled section icon + color picker trigger
                                Rectangle {
                                    id: sectionAppearanceButton
                                    objectName: "timerSectionIconButton"
                                    activeFocusOnTab: true
                                    Accessible.role: Accessible.Button
                                    Accessible.name: qsTr("Change section icon and color")
                                    Accessible.onPressAction: sectionAppearanceButton.activate()
                                    function activate() {
                                        editor.sectionAppearanceRequested(entryWrapper.modelData.id, entryWrapper.modelData.title || "",
                                                                          entryWrapper.modelData.icon || "default", entryWrapper.modelData.color || "")
                                    }
                                    Keys.onPressed: function(event) {
                                        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                                            activate()
                                            event.accepted = true
                                        }
                                    }
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30
                                    radius: 8
                                    color: entryWrapper.modelData.iconKey ? sectionCard.secBadgeBg : editor.pal_surfaceAlt
                                    border.color: (secIconHover.hovered || activeFocus) ? sectionCard.secAccent : Qt.rgba(sectionCard.secAccent.r, sectionCard.secAccent.g, sectionCard.secAccent.b, 0.32)
                                    border.width: 1

                                    HoverHandler { id: secIconHover }

                                    TimerIcon {
                                        anchors.centerIn: parent
                                        visible: !!(entryWrapper.modelData.iconKey && entryWrapper.modelData.iconKey.length > 0)
                                        width: 15; height: 15
                                        rasterSize: 30
                                        iconName: entryWrapper.modelData.iconKey
                                        tint: sectionCard.secAccent
                                    }

                                    TimerIcon {
                                        anchors.centerIn: parent
                                        visible: !(entryWrapper.modelData.iconKey && entryWrapper.modelData.iconKey.length > 0)
                                        width: 13; height: 13
                                        rasterSize: 26
                                        iconName: "ban"
                                        tint: editor.pal_textMuted
                                    }

                                    TimerPointerArea {
                                        anchors.fill: parent
                                        onClicked: sectionAppearanceButton.activate()
                                    }

                                    AppToolTip {
                                        visible: secIconHover.hovered
                                        text: qsTr("Change section icon and color")
                                    }
                                }

                                Text {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    textFormat: Text.PlainText
                                    text: entryWrapper.modelData.title || ""
                                    color: sectionCard.secText
                                    font.pixelSize: 13
                                    font.weight: Font.Bold
                                    elide: Text.ElideRight
                                }

                                TimerButton {
                                    objectName: "timerAddPartToSectionButton"
                                    iconName: "plus"
                                    iconSize: 12
                                    tip: qsTr("Add part to section")
                                    variant: "soft"
                                    accent: sectionCard.secAccent
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.addPartRequested(entryWrapper.modelData.id, editor.draftEntries)
                                }

                                TimerButton {
                                    property string moveFocusId: entryWrapper.modelData.id
                                    property int moveFocusDirection: -1
                                    iconName: "chevron_up"
                                    iconSize: 12
                                    tip: qsTr("Move section up")
                                    variant: "ghost"
                                    enabled: entryWrapper.index > 0
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.moveSectionFromControl(entryWrapper.index, -1, activeFocus)
                                }

                                TimerButton {
                                    property string moveFocusId: entryWrapper.modelData.id
                                    property int moveFocusDirection: 1
                                    iconName: "chevron_down"
                                    iconSize: 12
                                    tip: qsTr("Move section down")
                                    variant: "ghost"
                                    enabled: entryWrapper.index < editor.draftEntries.length - 1
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.moveSectionFromControl(entryWrapper.index, 1, activeFocus)
                                }

                                TimerButton {
                                    objectName: "timerMoveSectionButton"
                                    text: editor.narrowEditor ? "" : qsTr("Move to…")
                                    iconName: editor.narrowEditor ? "sliders" : ""
                                    iconSize: 12
                                    tip: qsTr("Move section")
                                    variant: "ghost"
                                    Layout.preferredWidth: editor.narrowEditor ? 28 : -1
                                    implicitHeight: 28
                                    onClicked: editor.moveRequested("section", entryWrapper.modelData.id, entryWrapper.modelData.title || "", "", editor.draftEntries)
                                }

                                TimerButton {
                                    objectName: "timerDeleteSectionButton"
                                    iconName: "trash"
                                    iconColor: editor.pal_danger
                                    iconSize: 13
                                    tip: (entryWrapper.modelData.parts || []).length > 0
                                         ? qsTr("Empty the section before deleting it")
                                         : qsTr("Delete section")
                                    variant: "ghost"
                                    accent: editor.pal_danger
                                    enabled: (entryWrapper.modelData.parts || []).length === 0
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    onClicked: editor.deleteDraftSection(entryWrapper.modelData.id)
                                }
                            }

                            // Empty section drop zone (hidden when placeholder is occupying it)
                            Rectangle {
                                objectName: "timerEmptySectionPlaceholder"
                                visible: (entryWrapper.modelData.parts || []).length === 0 && !sectionCard.isDropTarget
                                x: editor.secPad + 14
                                y: editor.secPad + editor.secHeaderH + 6
                                width: Math.max(80, parent.width - (editor.secPad + 14) - editor.secPad)
                                height: editor.emptySlotH
                                radius: 8
                                color: editor.pal_bg
                                border.color: editor.pal_border
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    width: parent.width - 16
                                    horizontalAlignment: Text.AlignHCenter
                                    text: qsTr("Empty section — drag a part here or click +")
                                    color: editor.pal_textMuted
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }
                            }

                            // Child part cards (each animates Y smoothly!)
                            Repeater {
                                model: entryWrapper.modelData.parts || []

                                delegate: Rectangle {
                                    id: childPartCard
                                    objectName: "timerEditorPartRow"
                                    required property var modelData
                                    required property int index

                                    readonly property bool isChildDragged: editor.dragActive && editor.dragType === "part" && editor.dragId === modelData.id
                                    readonly property var cBox: editor.layoutMap[modelData.id] || ({ y: editor.secPad + editor.secHeaderH + 6, h: editor.childPartH })

                                    x: editor.secPad + 14
                                    y: isChildDragged ? editor.dragOriginY : cBox.y
                                    width: Math.max(100, sectionCard.width - (editor.secPad + 14) - editor.secPad)
                                    height: editor.childPartH
                                    // Keep visible: true while dragging so childDragMouse retains its grab!
                                    opacity: isChildDragged ? 0.0 : 1.0
                                    z: isChildDragged ? -1 : 1
                                    radius: 8
                                    color: childHover.hovered ? editor.pal_hover : editor.pal_bg
                                    border.color: childHover.hovered ? editor.pal_borderStrong : editor.pal_border
                                    border.width: 1

                                    Behavior on y { enabled: editor.layoutAnimationsReady && !childPartCard.isChildDragged; NumberAnimation { duration: 210; easing.type: Easing.OutCubic } }
                                    Behavior on color { ColorAnimation { duration: 110 } }
                                    HoverHandler { id: childHover }

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 8
                                        anchors.rightMargin: 8
                                        spacing: 6

                                        // Drag handle spanning grip + title + duration
                                        Item {
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            Layout.fillHeight: true

                                            RowLayout {
                                                anchors.fill: parent
                                                spacing: 6

                                                Item {
                                                    Layout.preferredWidth: 22
                                                    Layout.preferredHeight: 28
                                                    TimerIcon {
                                                        anchors.centerIn: parent
                                                        width: 13; height: 13
                                                        rasterSize: 26
                                                        iconName: "grip"
                                                        tint: childDragMouse.containsMouse ? editor.pal_textPrimary : editor.pal_textMuted
                                                    }
                                                }

                                                Text {
                                                    Layout.fillWidth: true
                                                    Layout.minimumWidth: 0
                                                    textFormat: Text.PlainText
                                                    text: childPartCard.modelData.title || ""
                                                    color: editor.pal_textPrimary
                                                    font.pixelSize: 13
                                                    elide: Text.ElideRight
                                                }

                                                Text {
                                                    text: childPartCard.modelData.plannedLabel || ""
                                                    color: editor.pal_textMuted
                                                    font.pixelSize: 12
                                                }
                                            }

                                            MouseArea {
                                                id: childDragMouse
                                                anchors.fill: parent
                                                hoverEnabled: true
                                                cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                                                preventStealing: true
                                                property real pressX: 0
                                                property real pressY: 0
                                                property bool started: false
                                                onPressed: function(m) { pressX = m.x; pressY = m.y; started = false }
                                                onPositionChanged: function(m) {
                                                    if (!pressed) return
                                                    if (!started) {
                                                        if (Math.hypot(m.x - pressX, m.y - pressY) < 5) return
                                                        started = true
                                                        var pt = mapToItem(editor, m.x, m.y)
                                                        editor.beginDrag("part", childPartCard.modelData, editor.childPartH, pt, m.x + 8, m.y + 4, childPartCard.y)
                                                    } else {
                                                        editor.updateDrag(mapToItem(editor, m.x, m.y))
                                                    }
                                                }
                                                onReleased: { if (started) editor.finishDrag(); started = false }
                                                onCanceled: { if (started) editor.clearDragState(); started = false }
                                            }
                                        }

                                        TimerButton {
                                            objectName: "timerPartMoveUpButton"
                                            property string moveFocusId: childPartCard.modelData.id
                                            property int moveFocusDirection: -1
                                            iconName: "chevron_up"
                                            iconSize: 11
                                            tip: qsTr("Move up")
                                            variant: "ghost"
                                            enabled: true
                                            Layout.preferredWidth: 26
                                            implicitHeight: 26
                                            onClicked: editor.movePartFromControl(moveFocusId, -1, activeFocus)
                                        }

                                        TimerButton {
                                            objectName: "timerPartMoveDownButton"
                                            property string moveFocusId: childPartCard.modelData.id
                                            property int moveFocusDirection: 1
                                            iconName: "chevron_down"
                                            iconSize: 11
                                            tip: qsTr("Move down")
                                            variant: "ghost"
                                            enabled: true
                                            Layout.preferredWidth: 26
                                            implicitHeight: 26
                                            onClicked: editor.movePartFromControl(moveFocusId, 1, activeFocus)
                                        }

                                        TimerButton {
                                            objectName: "timerMovePartButton"
                                            text: editor.narrowEditor ? "" : qsTr("Move to…")
                                            iconName: editor.narrowEditor ? "sliders" : ""
                                            iconSize: 11
                                            tip: qsTr("Move to…")
                                            variant: "ghost"
                                            Layout.preferredWidth: editor.narrowEditor ? 26 : -1
                                            implicitHeight: 26
                                            onClicked: editor.moveRequested("part", childPartCard.modelData.id, childPartCard.modelData.title || "", entryWrapper.modelData.id, editor.draftEntries)
                                        }

                                        TimerButton {
                                            objectName: "timerDeletePartButton"
                                            iconName: "trash"
                                            iconColor: editor.pal_danger
                                            iconSize: 12
                                            tip: qsTr("Delete part")
                                            variant: "ghost"
                                            accent: editor.pal_danger
                                            Layout.preferredWidth: 26
                                            implicitHeight: 26
                                            onClicked: editor.deleteDraftPart(childPartCard.modelData.id)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            ScrollBar.vertical: ScrollBar {
                id: editorScroll
                policy: ScrollBar.AsNeeded
                width: 10
                contentItem: Rectangle {
                    implicitWidth: 5
                    radius: 3
                    color: editorScroll.hovered ? editor.pal_textMuted : editor.pal_borderStrong
                    opacity: (editorScroll.active || editorScroll.hovered) ? 1.0 : 0.35
                    anchors.horizontalCenter: parent.horizontalCenter
                }
            }
        }
    }

    // ── Elevated floating drag card (matches PlaylistTreeView tactile feel) ──
    Item {
        id: dragOverlay
        anchors.fill: parent
        z: 100
        visible: editor.dragActive

        // Subtle elevation halo behind floating card
        Rectangle {
            x: ghostCard.x - 2
            y: ghostCard.y + 3
            width: ghostCard.width + 4
            height: ghostCard.height + 4
            radius: 12
            color: appTheme.black
            opacity: 0.28
        }

        Rectangle {
            id: ghostCard
            x: editor.dragGhostX
            y: editor.dragGhostY
            width: Math.min(treeCanvas.width, 460)
            height: Math.min(54, Math.max(42, editor.dragCardHeight))
            radius: 10
            scale: editor.dragActive ? 1.015 : 1.0
            color: editor.pal_surfaceAlt
            border.color: editor.pal_accent
            border.width: 1.5
            opacity: 0.96

            Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 12
                spacing: 8

                TimerIcon {
                    width: 14; height: 14
                    rasterSize: 28
                    iconName: "grip"
                    tint: editor.pal_accentText
                }

                Rectangle {
                    visible: editor.dragType === "section" && editor.dragSectionIconKey.length > 0
                    Layout.preferredWidth: 26; Layout.preferredHeight: 26
                    radius: 7
                    color: editor.dragSectionBadgeBg
                    TimerIcon {
                        anchors.centerIn: parent
                        width: 14; height: 14
                        rasterSize: 28
                        iconName: editor.dragSectionIconKey
                        tint: editor.dragSectionAccent
                    }
                }

                Text {
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    text: editor.dragTitle
                    color: editor.pal_textPrimary
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                Text {
                    visible: editor.dragMeta.length > 0
                    text: editor.dragMeta
                    color: editor.pal_textMuted
                    font.pixelSize: 12
                }
            }
        }
    }
}
