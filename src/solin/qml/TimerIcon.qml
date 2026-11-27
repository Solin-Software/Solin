import QtQuick 2.15

Image {
    property string iconName: ""
    property color tint: appTheme.textSecondary
    property int rasterSize: Math.round(width * 2)

    sourceSize.width: Math.max(0, rasterSize)
    sourceSize.height: Math.max(0, rasterSize)
    source: iconName && rasterSize > 0 ? "image://timericons/" + iconName + "/" + rasterSize + "/" + String(tint).slice(1) : ""
}
