// TimerDigits.qml — stable numeric readout for timers and digital clocks.
// Uses a resolved sans font with stable numeric widths so digit changes do not
// shift same-length values, while genuine shape changes such as "-00:01" can
// still resize. The OpenType tnum flag stays enabled for fonts that expose it.
import QtQuick 2.15

Text {
    readonly property string resolvedDigitFamily: (
        typeof timerDigitFontFamily === "string" && timerDigitFontFamily.length > 0
    ) ? timerDigitFontFamily : "Consolas"

    font.family: resolvedDigitFamily
    font.features: ({ "tnum": 1 })
    font.kerning: false
}
