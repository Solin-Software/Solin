import QtQuick

Image {
    property string name: "settings"
    property color tint: appTheme.textSecondary
    source: name.length > 0
        ? "image://settingsicons/" + name + "/" + Math.ceil(width * Screen.devicePixelRatio) + "/" + String(tint).replace("#", "")
        : ""
    sourceSize.width: Math.ceil(width * Screen.devicePixelRatio)
    sourceSize.height: Math.ceil(height * Screen.devicePixelRatio)
    fillMode: Image.PreserveAspectFit
    width: 20
    height: 20
}
