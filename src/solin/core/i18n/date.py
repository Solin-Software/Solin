"""
date_i18n.py  ─  Solin
=======================
Centralized, i18n-aware date formatting.

This is the **single source of truth** for every user-visible date string.
No other module should hardcode month names or date format patterns.

Design rules
────────────
• QT_TRANSLATE_NOOP  — marks strings for lupdate extraction at module level,
                       without translating them at import time.
• QApplication.translate("_Date", src)  — resolves the active locale at
                       runtime, after QApplication is constructed.
• Named {placeholders}  — let translators reorder tokens and add connectors
                          freely without touching any Python code.
• Source strings are English  — never use Portuguese (or any other language)
                                as a source string; that belongs in the .ts.
• Never use f-strings for translatable strings — lupdate cannot analyse them.

Translator guide (pt-BR .ts)
─────────────────────────────
  <context><n>_Date</n>
    <message><source>January</source>    <translation>Janeiro</translation></message>
    <message><source>February</source>   <translation>Fevereiro</translation></message>
    <message><source>March</source>      <translation>Março</translation></message>
    <message><source>April</source>      <translation>Abril</translation></message>
    <message><source>May</source>        <translation>Maio</translation></message>
    <message><source>June</source>       <translation>Junho</translation></message>
    <message><source>July</source>       <translation>Julho</translation></message>
    <message><source>August</source>     <translation>Agosto</translation></message>
    <message><source>September</source>  <translation>Setembro</translation></message>
    <message><source>October</source>    <translation>Outubro</translation></message>
    <message><source>November</source>   <translation>Novembro</translation></message>
    <message><source>December</source>   <translation>Dezembro</translation></message>
    <message>
      <source>{month} {day}</source>
      <translation>{day} de {month}</translation>
    </message>
    <message>
      <source>{month} {day_start}-{day_end}</source>
      <translation>{day_start}-{day_end} de {month}</translation>
    </message>
    <message>
      <source>{month_start} {day_start}-{month_end} {day_end}</source>
      <translation>{day_start} de {month_start}-{day_end} de {month_end}</translation>
    </message>
  </context>

Translator guide (en .ts)
──────────────────────────
  Source strings are already English — no translation needed.
  lupdate will mark them as "unfinished"; confirm each one as-is.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from PySide6.QtCore import QT_TRANSLATE_NOOP
from PySide6.QtWidgets import QApplication

# ── Month names ───────────────────────────────────────────────────────────────
# English source strings; translators supply the locale form.
# Index 0 is intentionally empty — date.month is 1-based.

_MONTHS: list[str] = [
    "",
    QT_TRANSLATE_NOOP("_Date", "January"),
    QT_TRANSLATE_NOOP("_Date", "February"),
    QT_TRANSLATE_NOOP("_Date", "March"),
    QT_TRANSLATE_NOOP("_Date", "April"),
    QT_TRANSLATE_NOOP("_Date", "May"),
    QT_TRANSLATE_NOOP("_Date", "June"),
    QT_TRANSLATE_NOOP("_Date", "July"),
    QT_TRANSLATE_NOOP("_Date", "August"),
    QT_TRANSLATE_NOOP("_Date", "September"),
    QT_TRANSLATE_NOOP("_Date", "October"),
    QT_TRANSLATE_NOOP("_Date", "November"),
    QT_TRANSLATE_NOOP("_Date", "December"),
]

# ── Format templates ──────────────────────────────────────────────────────────
# English source — translators reorder {placeholders} and add connectors.
#
# en (source, no translation needed):
#   "{month} {day}"                                    → "April 13"
#   "{month} {day_start}-{day_end}"                    → "April 6-12"
#   "{month_start} {day_start}-{month_end} {day_end}"  → "April 27-May 3"
#
# pt-BR (translator provides):
#   "{day} de {month}"                                 → "13 de Abril"
#   "{day_start}-{day_end} de {month}"                 → "6-12 de Abril"
#   "{day_start} de {month_start}-{day_end} de {month_end}" → "27 de Abril-3 de Maio"

_FMT_SINGLE_DAY  = QT_TRANSLATE_NOOP("_Date", "{month} {day}")
_FMT_SAME_MONTH  = QT_TRANSLATE_NOOP("_Date", "{month} {day_start}-{day_end}")
_FMT_CROSS_MONTH = QT_TRANSLATE_NOOP("_Date", "{month_start} {day_start}-{month_end} {day_end}")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _tr(src: str) -> str:
    return QApplication.translate("_Date", src)


def _month_name(m: int) -> str:
    return _tr(_MONTHS[m])


def _format_time_java_pattern(epoch: float, pattern: str) -> str:
    lt = time.localtime(epoch)
    replacements = {
        "yyyy": f"{lt.tm_year:04d}",
        "yy": f"{lt.tm_year % 100:02d}",
        "MM": f"{lt.tm_mon:02d}",
        "M": str(lt.tm_mon),
        "dd": f"{lt.tm_mday:02d}",
        "d": str(lt.tm_mday),
        "HH": f"{lt.tm_hour:02d}",
        "H": str(lt.tm_hour),
        "hh": f"{((lt.tm_hour - 1) % 12) + 1:02d}",
        "h": str(((lt.tm_hour - 1) % 12) + 1),
        "mm": f"{lt.tm_min:02d}",
        "m": str(lt.tm_min),
        "ss": f"{lt.tm_sec:02d}",
        "s": str(lt.tm_sec),
        "a": time.strftime("%p", lt),
    }
    tokens = sorted(replacements, key=len, reverse=True)
    result: list[str] = []
    i = 0
    while i < len(pattern):
        for token in tokens:
            if pattern.startswith(token, i):
                result.append(replacements[token])
                i += len(token)
                break
        else:
            result.append(pattern[i])
            i += 1
    return "".join(result)


# ── Public API ────────────────────────────────────────────────────────────────

def format_single_date(d: date) -> str:
    """Single-day label, e.g. 'April 13' (en) or '13 de Abril' (pt-BR)."""
    return _tr(_FMT_SINGLE_DAY).format(day=d.day, month=_month_name(d.month))


def format_time_with_seconds(epoch: float, pattern: str | None = None) -> str:
    """Locale-configured clock time with seconds, falling back to HH:mm:ss."""
    fmt = (pattern or "HH:mm:ss").strip() or "HH:mm:ss"
    try:
        if "%" in fmt:
            return time.strftime(fmt, time.localtime(epoch))
        return _format_time_java_pattern(epoch, fmt)
    except ValueError:
        return time.strftime("%H:%M:%S", time.localtime(epoch))


def format_datetime(epoch: float, pattern: str | None = None) -> str:
    """Locale-configured date/time label, falling back to dd/MM/yyyy HH:mm."""
    fmt = (pattern or "dd/MM/yyyy HH:mm").strip() or "dd/MM/yyyy HH:mm"
    try:
        if "%" in fmt:
            return time.strftime(fmt, time.localtime(epoch))
        return _format_time_java_pattern(epoch, fmt)
    except ValueError:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def week_label(monday: date) -> str:
    """Week-range label used in the nav bar and detail headers.

    Same month : 'April 6-12'      (en) / '6-12 de Abril'          (pt-BR)
    Cross month: 'April 27-May 3'  (en) / '27 de Abril-3 de Maio'  (pt-BR)
    """
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return _tr(_FMT_SAME_MONTH).format(
            day_start=monday.day,
            day_end=sunday.day,
            month=_month_name(monday.month),
        )
    return _tr(_FMT_CROSS_MONTH).format(
        day_start=monday.day,
        month_start=_month_name(monday.month),
        day_end=sunday.day,
        month_end=_month_name(sunday.month),
    )


def week_label_short(monday: date) -> str:
    """Compact week label used in the week-picker dropdown.

    Delegates to :func:`week_label`. A distinct short form (abbreviated months)
    can be introduced here later without touching any callers.
    """
    return week_label(monday)
