"""
zoom_poll_widget.py  —  Solin
─────────────────────────────
Janela standalone para análise de relatórios de enquete do Zoom.
Aberta EXCLUSIVAMENTE via argumento de linha de comando (.csv).
Não interfere com a instância principal nem com o servidor IPC.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QColor, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QWidget, QFrame, QSizePolicy, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from solin.styles.theme import PALETTE, qss_rgba
from solin.core.integrations.automation.zoom.poll_parser import parse_zoom_poll_csv


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_date(raw, date_format):
    raw_clean = re.sub(
        r"\s*(da manhã|da tarde|AM|PM)\s*", "", raw.strip(), flags=re.IGNORECASE
    ).strip()
    patterns = [
        "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",    "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    ]
    dt = None
    for p in patterns:
        try:
            dt = datetime.strptime(raw_clean, p)
            break
        except ValueError:
            pass
    if dt is None:
        return raw
    fmt_map = {
        "yyyy": "%Y", "yy": "%y", "MM": "%m", "dd": "%d",
        "HH": "%H", "hh": "%I", "mm": "%M", "ss": "%S", " a": " %p",
    }
    py_fmt = date_format
    for token, py_token in sorted(fmt_map.items(), key=lambda x: -len(x[0])):
        py_fmt = py_fmt.replace(token, py_token)
    try:
        return dt.strftime(py_fmt)
    except (ValueError, OSError):
        return raw


def _identity_str(name, email):
    e = email.strip()
    return f"{name} ({e})" if e else name


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI primitives
# ─────────────────────────────────────────────────────────────────────────────

_TABLE_STYLE = f"""
    QTableWidget {{
        background: {PALETTE.surface};
        alternate-background-color: {PALETTE.bg2};
        border: 1px solid {PALETTE.border};
        border-radius: 8px;
        gridline-color: transparent;
        selection-background-color: {PALETTE.accent_muted};
        color: {PALETTE.text_primary};
        outline: 0;
    }}
    QTableWidget::item {{
        padding: 7px 14px;
        border: none;
    }}
    QTableWidget::item:selected {{
        background: {PALETTE.accent_muted};
        color: {PALETTE.text_primary};
    }}
    QHeaderView::section {{
        background: {PALETTE.bg3};
        color: {PALETTE.text_muted};
        font-size: 11px;
        font-weight: 600;
        padding: 7px 14px;
        border: none;
        border-bottom: 1px solid {PALETTE.border};
        letter-spacing: 0.4px;
    }}
    QHeaderView::section:hover {{
        background: {PALETTE.bg2};
        color: {PALETTE.text_primary};
    }}
    QHeaderView::section:pressed {{
        background: {PALETTE.accent_muted};
    }}
    QHeaderView::up-arrow {{
        width: 8px;
        height: 8px;
    }}
    QHeaderView::down-arrow {{
        width: 8px;
        height: 8px;
    }}
"""


def _make_table(rows, headers):
    t = QTableWidget(rows, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setAlternatingRowColors(True)
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.verticalHeader().setDefaultSectionSize(38)
    t.setStyleSheet(_TABLE_STYLE)
    return t


def _cell(text, align=Qt.AlignmentFlag.AlignLeft,
          color=None, bold=False):
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | align)
    if color:
        item.setForeground(QColor(color))
    if bold:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    return item


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem que ordena numericamente pelo texto."""
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except (ValueError, TypeError):
            return super().__lt__(other)


def _num_cell(value: int, align=Qt.AlignmentFlag.AlignRight,
              color=None, bold=False) -> _NumericItem:
    item = _NumericItem(str(value))
    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | align)
    if color:
        item.setForeground(QColor(color))
    if bold:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    return item


def _section_lbl(text, size=12, color=PALETTE.text_muted):
    lbl = QLabel(text)
    f = QFont()
    f.setPointSize(size)
    f.setBold(True)
    lbl.setFont(f)
    lbl.setStyleSheet(
        f"color: {color}; background: transparent; border: none;"
    )
    return lbl


def _body_lbl(text, size=13, color=PALETTE.text_primary, bold=False):
    lbl = QLabel(text)
    f = QFont()
    f.setPointSize(size)
    f.setBold(bold)
    lbl.setFont(f)
    lbl.setStyleSheet(
        f"color: {color}; background: transparent; border: none;"
    )
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setWordWrap(True)
    return lbl


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────

class ZoomPollWindow(QDialog):

    def __init__(self, filepath, lang_manager, parent=None):
        super().__init__(parent)
        self._lang     = lang_manager
        self._filepath = filepath
        self._result   = None

        self.setWindowTitle(self.tr("Attendance Report"))
        self.setMinimumSize(820, 620)
        self.resize(1000, 740)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint
        )
        self._build_chrome()
        self._load()

    # _t() removido — use self.tr() diretamente

    def _dfmt(self):
        return self._lang.meta.get("date_format", "dd/MM/yyyy HH:mm")

    # ── Chrome ────────────────────────────────────────────────────────────────

    def _build_chrome(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QFrame()
        bar.setFixedHeight(54)
        bar.setStyleSheet(f"""
            QFrame {{
                background: {PALETTE.surface};
                border-bottom: 1px solid {PALETTE.border};
                border-radius: 0;
            }}
        """)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(22, 0, 22, 0)
        bl.setSpacing(0)

        ic = QLabel("📊")
        ic.setStyleSheet("font-size: 20px; background: transparent; border: none;")
        bl.addWidget(ic)
        bl.addSpacing(10)

        tl = QLabel(self.tr("Attendance Report"))
        tf = QFont(); tf.setPointSize(13); tf.setBold(True)
        tl.setFont(tf)
        tl.setStyleSheet(
            f"color: {PALETTE.text_primary}; background: transparent; border: none;"
        )
        bl.addWidget(tl)
        bl.addSpacing(14)

        sep = QLabel("·")
        sep.setStyleSheet(
            f"color: {PALETTE.text_dim}; background: transparent; border: none;"
        )
        bl.addWidget(sep)
        bl.addSpacing(14)

        fn = QLabel(Path(self._filepath).name)
        fn.setStyleSheet(
            f"color: {PALETTE.text_muted}; font-size: 12px; background: transparent; border: none;"
        )
        bl.addWidget(fn)
        bl.addStretch()
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {PALETTE.bg0};")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(28, 28, 28, 28)
        self._body.setSpacing(20)

        ph = _body_lbl(self.tr("Loading report…"), 13, PALETTE.text_muted)
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.addWidget(ph)
        self._body.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    # ── Load & render ─────────────────────────────────────────────────────────

    def _load(self):
        self._result = parse_zoom_poll_csv(self._filepath)
        self._render()

    def _render(self):
        r = self._result

        while self._body.count():
            item = self._body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if r.parse_error:
            self._body.addWidget(_body_lbl(f"⚠  {r.parse_error}", 13, PALETTE.danger))
            self._body.addStretch()
            return

        self._body.addWidget(self._meeting_card(r))
        self._body.addWidget(self._stats_cards(r))

        if r.duplicate_alerts or r.divergence_alerts or r.family_alerts:
            self._body.addWidget(self._alerts_block(r))

        if r.skipped_responses:
            self._body.addWidget(self._skipped_section(r))

        self._body.addWidget(self._responses_table(r))
        self._body.addStretch()

    # ─────────────────────────────────────────────────────────────────────────
    # Meeting card
    # ─────────────────────────────────────────────────────────────────────────

    def _meeting_card(self, r):
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {PALETTE.bg2};
                border: 1px solid {PALETTE.border};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(5)

        topic = r.meeting.topic or self.tr("Untitled meeting")
        lay.addWidget(_body_lbl(topic, 15, PALETTE.text_primary, bold=True))

        parts = []
        if r.meeting.meeting_id:
            parts.append(f"ID {r.meeting.meeting_id}")
        if r.meeting.start_time:
            parts.append(_format_date(r.meeting.start_time, self._dfmt()))
        if parts:
            lay.addWidget(_body_lbl(
                "  ·  ".join(parts), 12, PALETTE.text_muted
            ))
        return card

    # ─────────────────────────────────────────────────────────────────────────────
    # Attendance summary
    # ─────────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Stats cards — raw totals, no corrections
    # ─────────────────────────────────────────────────────────────────────────

    def _stats_cards(self, r):
        outer = QWidget()
        outer.setStyleSheet("background: transparent;")
        h = QHBoxLayout(outer)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        h.addWidget(self._big_card(
            str(r.total_respondents),
            self.tr("Responses received"),
            PALETTE.text_muted,
            accent=False,
        ))
        h.addWidget(self._big_card(
            str(r.raw_total_attendance),
            self.tr("Attendance"),
            PALETTE.accent,
            accent=True,
            stretch=True,
        ))
        return outer

    def _big_card(self, number, label, number_color, accent=False, stretch=False):
        card = QFrame()
        if stretch:
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            card.setMinimumWidth(180)
            card.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        card.setFixedHeight(100)
        border_top = f"border-top: 3px solid {PALETTE.accent};" if accent else ""
        card.setStyleSheet(
            f"QFrame {{ background: {PALETTE.bg2}; border: 1px solid {PALETTE.border}; "
            f"{border_top} border-radius: 10px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 14, 22, 14)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        num_lbl = QLabel(number)
        nf = QFont(); nf.setPointSize(32 if accent else 26); nf.setBold(True)
        num_lbl.setFont(nf)
        num_lbl.setStyleSheet("color: " + number_color + "; background: transparent; border: none;")
        lbl = QLabel(label)
        lf = QFont(); lf.setPointSize(12)
        lbl.setFont(lf)
        lbl.setStyleSheet(
            f"color: {PALETTE.text_muted}; background: transparent; border: none;"
        )
        lay.addWidget(num_lbl)
        lay.addWidget(lbl)
        return card

    # ─────────────────────────────────────────────────────────────────────────
    # Dedup block — dup/div alerts + correction applied below
    # ─────────────────────────────────────────────────────────────────────────

    def _alerts_block(self, r):
        """Container pai 'Inconsistências' — engloba duplicados, divergências e família."""
        outer = QFrame()
        outer.setStyleSheet(f"""
            QFrame {{
                background: transparent;
                border: 1px solid {qss_rgba(PALETTE.warning, 0.27)};
                border-radius: 10px;
            }}
        """)
        v = QVBoxLayout(outer)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        # Cabeçalho pai
        texto_inconsistencias = self.tr("Inconsistencies found")
        v.addWidget(_section_lbl(
            f'⚠  {texto_inconsistencias}',
            12, PALETTE.warning
        ))

        # ── Subsection: Duplicados ─────────────────────────────────────────
        if r.duplicate_alerts:
            v.addWidget(_section_lbl(
                f'🔁  {self.tr("Duplicates")}',
                11, PALETTE.danger
            ))
            for a in r.duplicate_alerts:
                v.addWidget(self._alert_row(
                    "🔁", PALETTE.danger,
                    a.name,
                    self.tr("Responded {count}× with the same value ({value}). Counted once.").replace("{count}", str(a.count)).replace("{value}", str(a.value)),
                ))

        # ── Subsection: Divergências ───────────────────────────────────────
        if r.divergence_alerts:
            v.addWidget(_section_lbl(
                f'🔀  {self.tr("Divergences")}',
                11, PALETTE.warning
            ))
            for a in r.divergence_alerts:
                vals = ", ".join(str(x) for x in a.values)
                v.addWidget(self._alert_row(
                    "🔀", PALETTE.warning,
                    _identity_str(a.name, a.email),
                    self.tr("Submitted different values: [{values}]. Highest value used: {kept}.").replace("{values}", str(vals)).replace("{kept}", str(a.kept_value)),
                ))

        # Correção de dedup — abaixo dos alertas de dup/div
        raw       = r.raw_total_attendance
        corrected = r.total_attendance
        delta     = raw - corrected
        if delta:
            v.addWidget(self._result_row(
                label   = self.tr("Automatic corrections applied"),
                before  = raw,
                after   = corrected,
                delta   = delta,
                details = self._dedup_details(r),
            ))

        # ── Subsection: Família ────────────────────────────────────────────
        if r.family_alerts:
            v.addWidget(_section_lbl(
                '👨‍👩‍👧  ' + self.tr("Same family"),
                11, PALETTE.accent
            ))

            base            = r.total_attendance   # base fixa = total já corrigido pelo dedup
            total_fam_delta = 0
            fam_details     = []

            for a in r.family_alerts:
                fam_reported     = sum(m.value for m in a.members)
                fam_delta        = fam_reported - a.family_corrected
                total_fam_delta += fam_delta
                # cada card mostra sempre: base → base − delta_desta_família
                after_this       = base - fam_delta
                delta_str        = f"  (−{fam_delta})" if fam_delta > 0 else ""
                correction_note  = self.tr("max({count} members, max {max_val}) = {corrected}  ·  total: {running} → {after}{delta}").replace("{count}", str(a.family_count)).replace("{max_val}", str(a.family_max_value)).replace("{corrected}", str(a.family_corrected)).replace("{running}", str(base)).replace("{after}", str(after_this)).replace("{delta}", str(delta_str))

                members_str = "  ·  ".join(f"{m.name} ({m.value})" for m in a.members)
                v.addWidget(self._alert_row(
                    "👨‍👩‍👧", PALETTE.accent,
                    self.tr("Possible family — last name \"{lastname}\"").replace("{lastname}", str(a.last_name)),
                    members_str,
                    extra_note=correction_note,
                ))

                if fam_delta > 0:
                    fam_details.append((
                        "👨‍👩‍👧",
                        f"{a.last_name}: −{fam_delta}",
                        PALETTE.accent,
                    ))

            if total_fam_delta > 0:
                v.addWidget(self._result_row(
                    label   = self.tr("Estimated total"),
                    before  = r.total_attendance,
                    after   = r.total_attendance - total_fam_delta,
                    delta   = total_fam_delta,
                    details = fam_details if len(r.family_alerts) > 1 else [],
                    muted   = True,
                ))

        return outer

    def _dedup_details(self, r):
        """Returns list of (icon, text, color) for each dedup correction line."""
        details = []
        dup_delta = sum((a.count - 1) * a.value for a in r.duplicate_alerts)
        if dup_delta:
            details.append((
                "🔁",
                self.tr("{n} duplicate response(s) removed  −{delta} person(s)").replace("{n}", str(len(r.duplicate_alerts))).replace("{delta}", str(dup_delta)),
                PALETTE.danger,
            ))
        div_raw   = sum(sum(a.values) for a in r.divergence_alerts)
        div_kept  = sum(a.kept_value for a in r.divergence_alerts)
        div_delta = div_raw - div_kept
        if div_delta:
            details.append((
                "🔀",
                self.tr("{n} conflicting response(s) resolved  −{delta} person(s)").replace("{n}", str(len(r.divergence_alerts))).replace("{delta}", str(div_delta)),
                PALETTE.warning,
            ))
        return details

    # ─────────────────────────────────────────────────────────────────────────
    # Shared result row (correction / suggestion strip)
    # ─────────────────────────────────────────────────────────────────────────

    def _result_row(self, label, before, after, delta,
                    details=None, muted=False):
        """
        A compact strip:  Label  ·  before → after  (−delta)
        Optionally expands with detail rows below a separator.
        muted=True tones down the colors for informational (non-applied) corrections.
        """
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {PALETTE.surface}; border: 1px solid "
            f"{PALETTE.border_muted}; border-radius: 8px; }}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 10, 18, 10)
        v.setSpacing(5)

        # Main row
        top = QWidget(); top.setStyleSheet("background: transparent;")
        tl  = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)

        lbl_w = QLabel(label)
        lf = QFont(); lf.setPointSize(11); lf.setBold(True)
        lbl_w.setFont(lf)
        lbl_w.setStyleSheet(
            f"color: {PALETTE.text_muted}; background: transparent; border: none;"
        )
        tl.addWidget(lbl_w)
        tl.addStretch()

        after_color = PALETTE.text_muted if muted else PALETTE.accent
        delta_color = PALETTE.text_dim if muted else PALETTE.warning
        for txt, color, bold in [
            (str(before),      PALETTE.text_dim, True),
            ("→",              PALETTE.text_dim, False),
            (str(after),       after_color,          True),
            (f"(−{delta})",    delta_color,          False),
        ]:
            w = QLabel(txt)
            f = QFont(); f.setPointSize(13); f.setBold(bold)
            w.setFont(f)
            w.setStyleSheet(
                "color: " + color + "; background: transparent; border: none;"
            )
            tl.addWidget(w)

        v.addWidget(top)

        if details:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(
                f"background: {PALETTE.border_muted}; border: none; max-height: 1px;"
            )
            v.addWidget(sep)

            for icon, text, color in details:
                row = QWidget(); row.setStyleSheet("background: transparent;")
                rl  = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(8)
                il = QLabel(icon)
                il.setStyleSheet("font-size: 13px; background: transparent; border: none;")
                il.setFixedWidth(20)
                tl2 = QLabel(text)
                tf2 = QFont(); tf2.setPointSize(11)
                tl2.setFont(tf2)
                tl2.setStyleSheet(
                    "color: " + color + "; background: transparent; border: none;"
                )
                rl.addWidget(il)
                rl.addWidget(tl2)
                rl.addStretch()
                v.addWidget(row)

        return card

    def _alert_row(self, icon, color, title_text, body_text, extra_note=None):
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {qss_rgba(color, 0.05)};
                border: 1px solid {qss_rgba(color, 0.20)};
                border-left: 3px solid {color};
                border-radius: 8px;
            }}
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(12)

        ic = QLabel(icon)
        ic.setStyleSheet("font-size: 18px; background: transparent; border: none;")
        ic.setFixedWidth(28)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(ic)

        col = QWidget()
        col.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(3)

        tl = QLabel(title_text)
        tf = QFont(); tf.setPointSize(12); tf.setBold(True)
        tl.setFont(tf)
        tl.setStyleSheet(
            f"color: {PALETTE.text_primary}; background: transparent; border: none;"
        )
        cl.addWidget(tl)

        bl = QLabel(body_text)
        bf = QFont(); bf.setPointSize(11)
        bl.setFont(bf)
        bl.setStyleSheet(
            f"color: {PALETTE.text_muted}; background: transparent; border: none;"
        )
        bl.setWordWrap(True)
        cl.addWidget(bl)

        if extra_note:
            note_lbl = QLabel(self.tr("↳ Suggested value: {note}").replace("{note}", str(extra_note)))
            nf = QFont(); nf.setPointSize(11)
            note_lbl.setFont(nf)
            note_lbl.setStyleSheet(
                f"color: {PALETTE.text_muted}; background: transparent; border: none;"
            )
            cl.addWidget(note_lbl)

        lay.addWidget(col, 1)
        return card

    # ─────────────────────────────────────────────────────────────────────────
    # Skipped (non-numeric) responses
    # ─────────────────────────────────────────────────────────────────────────

    def _skipped_section(self, r):
        outer = QWidget()
        outer.setStyleSheet("background: transparent;")
        v = QVBoxLayout(outer)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        hrow = QWidget()
        hrow.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(hrow)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(_section_lbl(
            f'💬  {self.tr("Not counted ({n})").replace("{n}", str(len(r.skipped_responses)))}',
            12, PALETTE.text_muted
        ))
        hl.addStretch()
        v.addWidget(hrow)

        v.addWidget(_body_lbl(
            self.tr("These people did not enter a numbe."), 11, PALETTE.text_dim
        ))

        table = _make_table(len(r.skipped_responses), [
            self.tr("Name"),
            self.tr("Time"),
            self.tr("Response"),
        ])

        dfmt = self._dfmt()
        for ri, resp in enumerate(r.skipped_responses):
            table.setItem(ri, 0, _cell(resp.name, color=PALETTE.text_muted))
            table.setItem(ri, 1, _cell(
                _format_date(resp.submitted_at, dfmt),
                color=PALETTE.text_dim
            ))
            table.setItem(ri, 2, _cell(resp.raw_value, color=PALETTE.text_dim))

        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(min(len(r.skipped_responses) * 38 + 38, 180))

        v.addWidget(table)
        return outer

    # ─────────────────────────────────────────────────────────────────────────
    # Main responses table
    # ─────────────────────────────────────────────────────────────────────────

    def _responses_table(self, r):
        outer = QWidget()
        outer.setStyleSheet("background: transparent;")
        v = QVBoxLayout(outer)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        hrow = QWidget()
        hrow.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(hrow)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(_section_lbl(
            self.tr("Counted responses"), 12, PALETTE.text_primary
        ))
        hl.addStretch()

        valid_count = len([x for x in r.raw_responses if x.is_valid])
        removed = valid_count - len(r.responses)
        if removed > 0:
            hl.addWidget(_body_lbl(
                self.tr("{n} removed as duplicate(s)").replace("{n}", str(removed)),
                11, PALETTE.text_dim
            ))
        v.addWidget(hrow)

        # Build highlight sets using dedup_key
        def _alert_key(name, email):
            if email:
                return "email:" + email
            normalized = re.sub(r"\s+", " ", name.strip().lower())
            return "name:" + normalized

        alerted_keys = set()
        for a in r.duplicate_alerts:
            alerted_keys.add(_alert_key(a.name, a.email))
        for a in r.divergence_alerts:
            alerted_keys.add(_alert_key(a.name, a.email))

        family_keys = set()
        for fa in r.family_alerts:
            for m in fa.members:
                family_keys.add(m.dedup_key)

        table = _make_table(len(r.responses), [
            "#",
            self.tr("Name"),
            self.tr("E-mail"),
            self.tr("Time"),
            self.tr("People"),
        ])

        # Disable sorting during population to avoid mid-insert reordering
        table.setSortingEnabled(False)
        dfmt = self._dfmt()

        for ri, resp in enumerate(r.responses):
            dk = resp.dedup_key
            is_alert  = dk in alerted_keys
            is_family = dk in family_keys

            if is_alert:
                name_color  = PALETTE.warning
                value_color = PALETTE.warning
            elif is_family:
                name_color  = PALETTE.accent
                value_color = PALETTE.accent
            else:
                name_color  = PALETTE.text_primary
                value_color = PALETTE.accent if resp.value > 1 else PALETTE.text_muted

            email_display = resp.email if resp.email else "—"
            email_color   = PALETTE.text_muted if resp.email else PALETTE.text_dim

            table.setItem(ri, 0, _num_cell(
                ri + 1,
                Qt.AlignmentFlag.AlignRight,
                PALETTE.text_dim
            ))
            table.setItem(ri, 1, _cell(
                resp.name, color=name_color,
                bold=(is_alert or is_family)
            ))
            table.setItem(ri, 2, _cell(email_display, color=email_color))
            table.setItem(ri, 3, _cell(
                _format_date(resp.submitted_at, dfmt),
                color=PALETTE.text_muted
            ))
            table.setItem(ri, 4, _num_cell(
                resp.value,
                Qt.AlignmentFlag.AlignRight,
                value_color,
                bold=(resp.value > 1),
            ))

        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 88)
        table.setMinimumHeight(min(len(r.responses) * 38 + 38, 500))

        # Enable interactive sorting; default: alphabético por nome (col 1)
        hh.setSortIndicatorShown(True)
        hh.setHighlightSections(True)
        table.setSortingEnabled(True)
        table.sortByColumn(1, Qt.SortOrder.AscendingOrder)

        v.addWidget(table)
        return outer


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def launch_zoom_poll(filepath, lang_manager):
    win = ZoomPollWindow(filepath, lang_manager)
    win.show()
    return win
