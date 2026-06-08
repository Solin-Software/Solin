"""
zoom_poll_widget.py  —  Solin
─────────────────────────────
Janela standalone para análise de relatórios de enquete do Zoom.
Aberta EXCLUSIVAMENTE via argumento de linha de comando (.csv).
Não interfere com a instância principal nem com o servidor IPC.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QColor, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QWidget, QFrame, QSizePolicy, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from app.styles.theme import COLORS, STYLESHEET


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MeetingInfo:
    topic:        str = ""
    meeting_id:   str = ""
    start_time:   str = ""
    generated_at: str = ""


@dataclass
class PollResponse:
    index:        int
    name:         str
    email:        str
    submitted_at: str
    value:        int     # 0 se não-numérico
    raw_value:    str
    is_valid:     bool    # False = valor não-numérico

    @property
    def last_name(self) -> str:
        parts = self.name.strip().split()
        return parts[-1].lower() if len(parts) >= 2 else ""

    @property
    def dedup_key(self) -> str:
        email = self.email.strip().lower()
        if email:
            return f"email:{email}"
        name = re.sub(r"\s+", " ", self.name.strip().lower())
        if name:
            return f"name:{name}"
        return f"row:{self.index}"


@dataclass
class DuplicateAlert:
    email:      str
    name:       str
    count:      int
    value:      int
    kept_index: int


@dataclass
class DivergenceAlert:
    email:      str
    name:       str
    values:     list
    kept_value: int
    kept_index: int


@dataclass
class FamilyAlert:
    last_name:        str
    members:          list
    family_count:     int = 0   # nº de membros distintos da família no poll
    family_max_value: int = 0   # maior valor reportado entre os membros
    family_corrected: int = 0   # max(family_count, family_max_value)


@dataclass
class ParseResult:
    meeting:              MeetingInfo
    responses:            list
    raw_responses:        list
    skipped_responses:    list
    total_attendance:     int
    raw_total_attendance: int
    total_respondents:    int
    duplicate_alerts:     list
    divergence_alerts:    list
    family_alerts:        list
    question_text:        str = ""
    parse_error:          str = ""


# ─────────────────────────────────────────────────────────────────────────────
# CSV Parser
# ─────────────────────────────────────────────────────────────────────────────

def _try_parse_int(val):
    if not val:
        return None
    try:
        i = int(float(str(val).strip()))
        return i if i > 0 else None
    except (ValueError, TypeError):
        return None


def _find_col(header, keywords):
    for i, h in enumerate(header):
        hl = h.lower()
        for kw in keywords:
            if kw in hl:
                return i
    return None


def _is_section_title(row):
    return len([c for c in row if c.strip()]) == 1


def parse_zoom_poll_csv(filepath):
    result = ParseResult(
        meeting=MeetingInfo(),
        responses=[], raw_responses=[], skipped_responses=[],
        total_attendance=0, raw_total_attendance=0, total_respondents=0,
        duplicate_alerts=[], divergence_alerts=[], family_alerts=[],
    )

    try:
        with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
            raw = f.read()
    except Exception as e:
        result.parse_error = str(e)
        return result

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    all_rows = []
    for row in csv.reader(io.StringIO(raw)):
        trimmed = row
        while trimmed and not trimmed[-1].strip():
            trimmed = trimmed[:-1]
        if any(c.strip() for c in trimmed):
            all_rows.append(trimmed)

    if not all_rows:
        result.parse_error = "Arquivo vazio."
        return result

    sections = []
    cur_title, cur_rows = "", []
    for row in all_rows:
        if _is_section_title(row):
            if cur_rows:
                sections.append({"title": cur_title, "rows": cur_rows})
            cur_title, cur_rows = row[0].strip(), []
        else:
            cur_rows.append(row)
    if cur_rows:
        sections.append({"title": cur_title, "rows": cur_rows})

    if not sections:
        result.parse_error = "Nenhuma seção reconhecida no arquivo."
        return result

    # Visão Geral
    rows = sections[0]["rows"]
    data_rows = [r for r in rows if len(r) >= 4]
    data_row = data_rows[1] if len(data_rows) >= 2 else (data_rows[0] if data_rows else [])
    if len(data_row) >= 4:
        result.meeting.generated_at = data_row[0].strip()
        result.meeting.topic        = data_row[1].strip()
        result.meeting.meeting_id   = data_row[2].strip()
        result.meeting.start_time   = data_row[3].strip()

    # Respostas (seções 2+)
    all_raw = []
    question_text = ""

    for sec in sections[2:]:
        rows = sec["rows"]
        if len(rows) < 2:
            continue

        header    = rows[0]
        data_rows = rows[1:]

        name_col  = _find_col(header, ["nome", "name", "nombre", "usuário", "usuario"])
        email_col = _find_col(header, ["e-mail", "email", "correo"])
        date_col  = _find_col(header, ["data e hora", "date", "fecha", "envio", "hora"])

        if name_col  is None: name_col  = 1
        if email_col is None: email_col = 2
        if date_col  is None: date_col  = 3

        known      = {0, name_col, email_col, date_col}
        answer_col = 4
        for ci, h in enumerate(header):
            if ci not in known and h.strip():
                answer_col = ci
                if not question_text:
                    question_text = h.strip()
                break

        for row in data_rows:
            if not row:
                continue

            def _get(idx, _r=row):
                return _r[idx].strip() if idx < len(_r) else ""

            raw_val  = _get(answer_col)
            parsed   = _try_parse_int(raw_val)
            is_valid = parsed is not None

            all_raw.append(PollResponse(
                index        = len(all_raw) + 1,
                name         = _get(name_col),
                email        = _get(email_col).lower(),
                submitted_at = _get(date_col),
                value        = parsed if is_valid else 0,
                raw_value    = raw_val,
                is_valid     = is_valid,
            ))

    result.raw_responses   = all_raw
    result.question_text   = question_text

    valid_raw   = [r for r in all_raw if r.is_valid]
    skipped_raw = [r for r in all_raw if not r.is_valid]
    result.skipped_responses = skipped_raw
    result.raw_total_attendance = sum(r.value for r in valid_raw)

    by_key = defaultdict(list)
    for r in valid_raw:
        by_key[r.dedup_key].append(r)

    kept = []
    for _key, entries in by_key.items():
        if len(entries) == 1:
            kept.append(entries[0])
            continue
        values = [e.value for e in entries]
        if len(set(values)) == 1:
            best = entries[0]
            result.duplicate_alerts.append(DuplicateAlert(
                email=best.email, name=best.name,
                count=len(entries), value=best.value,
                kept_index=best.index,
            ))
        else:
            best = max(entries, key=lambda e: e.value)
            result.divergence_alerts.append(DivergenceAlert(
                email=best.email, name=best.name,
                values=sorted(values), kept_value=best.value,
                kept_index=best.index,
            ))
        kept.append(best)

    by_lastname = defaultdict(list)
    for r in kept:
        if r.last_name:
            by_lastname[r.last_name].append(r)

    for ln, members in by_lastname.items():
        if len(members) >= 2 and any(m.value > 1 for m in members):
            fcount     = len(members)
            fmax_val   = max(m.value for m in members)
            fcorrected = max(fcount, fmax_val)
            result.family_alerts.append(FamilyAlert(
                last_name=ln.title(), members=members,
                family_count=fcount,
                family_max_value=fmax_val,
                family_corrected=fcorrected,
            ))

    result.responses         = sorted(kept, key=lambda r: r.value, reverse=True)
    result.total_attendance  = sum(r.value for r in kept)
    result.total_respondents = len(kept)
    return result


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
    except Exception:
        return raw


def _identity_str(name, email):
    e = email.strip()
    return f"{name} ({e})" if e else name


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI primitives
# ─────────────────────────────────────────────────────────────────────────────

_TABLE_STYLE = """
    QTableWidget {{
        background: {bg1};
        alternate-background-color: {bg2};
        border: 1px solid {border};
        border-radius: 8px;
        gridline-color: transparent;
        selection-background-color: {accent_muted};
        color: {text_primary};
        outline: 0;
    }}
    QTableWidget::item {{
        padding: 7px 14px;
        border: none;
    }}
    QTableWidget::item:selected {{
        background: {accent_muted};
        color: {text_primary};
    }}
    QHeaderView::section {{
        background: {bg3};
        color: {text_secondary};
        font-size: 11px;
        font-weight: 600;
        padding: 7px 14px;
        border: none;
        border-bottom: 1px solid {border};
        letter-spacing: 0.4px;
    }}
    QHeaderView::section:hover {{
        background: {bg2};
        color: {text_primary};
    }}
    QHeaderView::section:pressed {{
        background: {accent_muted};
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
    t.setStyleSheet(_TABLE_STYLE.format(**COLORS))
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


def _section_lbl(text, size=12, color=COLORS["text_secondary"]):
    lbl = QLabel(text)
    f = QFont()
    f.setPointSize(size)
    f.setBold(True)
    lbl.setFont(f)
    lbl.setStyleSheet(
        f"color: {color}; background: transparent; border: none;"
    )
    return lbl


def _body_lbl(text, size=13, color=COLORS["text_primary"], bold=False):
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
        self.setStyleSheet(STYLESHEET % COLORS)
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
                background: {COLORS['bg1']};
                border-bottom: 1px solid {COLORS['border']};
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
            f"color: {COLORS['text_primary']}; background: transparent; border: none;"
        )
        bl.addWidget(tl)
        bl.addSpacing(14)

        sep = QLabel("·")
        sep.setStyleSheet(
            f"color: {COLORS['text_muted']}; background: transparent; border: none;"
        )
        bl.addWidget(sep)
        bl.addSpacing(14)

        fn = QLabel(os.path.basename(self._filepath))
        fn.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 12px; background: transparent; border: none;"
        )
        bl.addWidget(fn)
        bl.addStretch()
        root.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['bg0']};")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(28, 28, 28, 28)
        self._body.setSpacing(20)

        ph = _body_lbl(self.tr("Loading report…"), 13, COLORS["text_secondary"])
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
            self._body.addWidget(_body_lbl(f"⚠  {r.parse_error}", 13, COLORS["danger"]))
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
                background: {COLORS['bg2']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(5)

        topic = r.meeting.topic or self.tr("Untitled meeting")
        lay.addWidget(_body_lbl(topic, 15, COLORS["text_primary"], bold=True))

        parts = []
        if r.meeting.meeting_id:
            parts.append(f"ID {r.meeting.meeting_id}")
        if r.meeting.start_time:
            parts.append(_format_date(r.meeting.start_time, self._dfmt()))
        if parts:
            lay.addWidget(_body_lbl(
                "  ·  ".join(parts), 12, COLORS["text_secondary"]
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
            COLORS["text_secondary"],
            accent=False,
        ))
        h.addWidget(self._big_card(
            str(r.raw_total_attendance),
            self.tr("Attendance"),
            COLORS["accent2"],
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
        border_top = "border-top: 3px solid " + COLORS["accent2"] + ";" if accent else ""
        card.setStyleSheet(
            "QFrame { background: " + COLORS["bg2"] + "; border: 1px solid " + COLORS["border"] + "; " +
            border_top + " border-radius: 10px; }"
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
        lbl.setStyleSheet("color: " + COLORS["text_secondary"] + "; background: transparent; border: none;")
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
                border: 1px solid {COLORS['warning']}44;
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
            12, COLORS["warning"]
        ))

        # ── Subsection: Duplicados ─────────────────────────────────────────
        if r.duplicate_alerts:
            v.addWidget(_section_lbl(
                f'🔁  {self.tr("Duplicates")}',
                11, COLORS["danger"]
            ))
            for a in r.duplicate_alerts:
                v.addWidget(self._alert_row(
                    "🔁", COLORS["danger"],
                    a.name,
                    self.tr("Responded {count}× with the same value ({value}). Counted once.").replace("{count}", str(a.count)).replace("{value}", str(a.value)),
                ))

        # ── Subsection: Divergências ───────────────────────────────────────
        if r.divergence_alerts:
            v.addWidget(_section_lbl(
                f'🔀  {self.tr("Divergences")}',
                11, COLORS["warning"]
            ))
            for a in r.divergence_alerts:
                vals = ", ".join(str(x) for x in a.values)
                v.addWidget(self._alert_row(
                    "🔀", COLORS["warning"],
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
                11, COLORS["accent2"]
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
                    "👨‍👩‍👧", COLORS["accent2"],
                    self.tr("Possible family — last name \"{lastname}\"").replace("{lastname}", str(a.last_name)),
                    members_str,
                    extra_note=correction_note,
                ))

                if fam_delta > 0:
                    fam_details.append((
                        "👨‍👩‍👧",
                        f"{a.last_name}: −{fam_delta}",
                        COLORS["accent2"],
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
                COLORS["danger"],
            ))
        div_raw   = sum(sum(a.values) for a in r.divergence_alerts)
        div_kept  = sum(a.kept_value for a in r.divergence_alerts)
        div_delta = div_raw - div_kept
        if div_delta:
            details.append((
                "🔀",
                self.tr("{n} conflicting response(s) resolved  −{delta} person(s)").replace("{n}", str(len(r.divergence_alerts))).replace("{delta}", str(div_delta)),
                COLORS["warning"],
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
            "QFrame { background: " + COLORS["bg1"] +
            "; border: 1px solid " + COLORS["border_muted"] +
            "; border-radius: 8px; }"
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
            "color: " + COLORS["text_secondary"] +
            "; background: transparent; border: none;"
        )
        tl.addWidget(lbl_w)
        tl.addStretch()

        after_color = COLORS["text_secondary"] if muted else COLORS["accent2"]
        delta_color = COLORS["text_muted"]     if muted else COLORS["warning"]
        for txt, color, bold in [
            (str(before),      COLORS["text_muted"], True),
            ("→",              COLORS["text_muted"], False),
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
                "background: " + COLORS["border_muted"] +
                "; border: none; max-height: 1px;"
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
                background: {color}0e;
                border: 1px solid {color}33;
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
            f"color: {COLORS['text_primary']}; background: transparent; border: none;"
        )
        cl.addWidget(tl)

        bl = QLabel(body_text)
        bf = QFont(); bf.setPointSize(11)
        bl.setFont(bf)
        bl.setStyleSheet(
            f"color: {COLORS['text_secondary']}; background: transparent; border: none;"
        )
        bl.setWordWrap(True)
        cl.addWidget(bl)

        if extra_note:
            note_lbl = QLabel(self.tr("↳ Suggested value: {note}").replace("{note}", str(extra_note)))
            nf = QFont(); nf.setPointSize(11)
            note_lbl.setFont(nf)
            note_lbl.setStyleSheet(
                f"color: {COLORS['text_secondary']}; background: transparent; border: none;"
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
            12, COLORS["text_secondary"]
        ))
        hl.addStretch()
        v.addWidget(hrow)

        v.addWidget(_body_lbl(
            self.tr("These people did not enter a numbe."), 11, COLORS["text_muted"]
        ))

        table = _make_table(len(r.skipped_responses), [
            self.tr("Name"),
            self.tr("Time"),
            self.tr("Response"),
        ])

        dfmt = self._dfmt()
        for ri, resp in enumerate(r.skipped_responses):
            table.setItem(ri, 0, _cell(resp.name, color=COLORS["text_secondary"]))
            table.setItem(ri, 1, _cell(
                _format_date(resp.submitted_at, dfmt),
                color=COLORS["text_muted"]
            ))
            table.setItem(ri, 2, _cell(resp.raw_value, color=COLORS["text_muted"]))

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
            self.tr("Counted responses"), 12, COLORS["text_primary"]
        ))
        hl.addStretch()

        valid_count = len([x for x in r.raw_responses if x.is_valid])
        removed = valid_count - len(r.responses)
        if removed > 0:
            hl.addWidget(_body_lbl(
                self.tr("{n} removed as duplicate(s)").replace("{n}", str(removed)),
                11, COLORS["text_muted"]
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
                name_color  = COLORS["warning"]
                value_color = COLORS["warning"]
            elif is_family:
                name_color  = COLORS["accent2"]
                value_color = COLORS["accent2"]
            else:
                name_color  = COLORS["text_primary"]
                value_color = COLORS["accent2"] if resp.value > 1 else COLORS["text_secondary"]

            email_display = resp.email if resp.email else "—"
            email_color   = COLORS["text_secondary"] if resp.email else COLORS["text_muted"]

            table.setItem(ri, 0, _num_cell(
                ri + 1,
                Qt.AlignmentFlag.AlignRight,
                COLORS["text_muted"]
            ))
            table.setItem(ri, 1, _cell(
                resp.name, color=name_color,
                bold=(is_alert or is_family)
            ))
            table.setItem(ri, 2, _cell(email_display, color=email_color))
            table.setItem(ri, 3, _cell(
                _format_date(resp.submitted_at, dfmt),
                color=COLORS["text_secondary"]
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
