"""Zoom poll CSV parsing and attendance normalization."""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MeetingInfo:
    topic: str = ""
    meeting_id: str = ""
    start_time: str = ""
    generated_at: str = ""


@dataclass(frozen=True)
class PollResponse:
    index: int
    name: str
    email: str
    submitted_at: str
    value: int
    raw_value: str
    is_valid: bool

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


@dataclass(frozen=True)
class DuplicateAlert:
    email: str
    name: str
    count: int
    value: int
    kept_index: int


@dataclass(frozen=True)
class DivergenceAlert:
    email: str
    name: str
    values: list[int]
    kept_value: int
    kept_index: int


@dataclass(frozen=True)
class FamilyAlert:
    last_name: str
    members: list[PollResponse]
    family_count: int = 0
    family_max_value: int = 0
    family_corrected: int = 0


@dataclass
class ParseResult:
    meeting: MeetingInfo = field(default_factory=MeetingInfo)
    responses: list[PollResponse] = field(default_factory=list)
    raw_responses: list[PollResponse] = field(default_factory=list)
    skipped_responses: list[PollResponse] = field(default_factory=list)
    total_attendance: int = 0
    raw_total_attendance: int = 0
    total_respondents: int = 0
    duplicate_alerts: list[DuplicateAlert] = field(default_factory=list)
    divergence_alerts: list[DivergenceAlert] = field(default_factory=list)
    family_alerts: list[FamilyAlert] = field(default_factory=list)
    question_text: str = ""
    parse_error: str = ""


def parse_zoom_poll_csv(filepath: str | Path) -> ParseResult:
    try:
        raw = Path(filepath).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return ParseResult(parse_error=str(exc))
    return parse_zoom_poll_text(raw)


def parse_zoom_poll_text(raw: str) -> ParseResult:
    result = ParseResult()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    all_rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(raw)):
        trimmed = row
        while trimmed and not trimmed[-1].strip():
            trimmed = trimmed[:-1]
        if any(cell.strip() for cell in trimmed):
            all_rows.append(trimmed)

    if not all_rows:
        result.parse_error = "Arquivo vazio."
        return result

    sections: list[dict[str, object]] = []
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

    rows = _section_rows(sections[0])
    data_rows = [row for row in rows if len(row) >= 4]
    data_row = data_rows[1] if len(data_rows) >= 2 else (data_rows[0] if data_rows else [])
    if len(data_row) >= 4:
        result.meeting = MeetingInfo(
            generated_at=data_row[0].strip(),
            topic=data_row[1].strip(),
            meeting_id=data_row[2].strip(),
            start_time=data_row[3].strip(),
        )

    all_raw: list[PollResponse] = []
    question_text = ""

    for section in sections[2:]:
        rows = _section_rows(section)
        if len(rows) < 2:
            continue

        header = rows[0]
        data_rows = rows[1:]

        name_col = _find_col(header, ["nome", "name", "nombre", "usuário", "usuario"])
        email_col = _find_col(header, ["e-mail", "email", "correo"])
        date_col = _find_col(header, ["data e hora", "date", "fecha", "envio", "hora"])

        if name_col is None:
            name_col = 1
        if email_col is None:
            email_col = 2
        if date_col is None:
            date_col = 3

        known = {0, name_col, email_col, date_col}
        answer_col = 4
        for column_index, heading in enumerate(header):
            if column_index not in known and heading.strip():
                answer_col = column_index
                if not question_text:
                    question_text = heading.strip()
                break

        for row in data_rows:
            if not row:
                continue

            raw_value = _cell(row, answer_col)
            parsed = _try_parse_int(raw_value)
            is_valid = parsed is not None

            all_raw.append(
                PollResponse(
                    index=len(all_raw) + 1,
                    name=_cell(row, name_col),
                    email=_cell(row, email_col).lower(),
                    submitted_at=_cell(row, date_col),
                    value=parsed if is_valid else 0,
                    raw_value=raw_value,
                    is_valid=is_valid,
                )
            )

    result.raw_responses = all_raw
    result.question_text = question_text

    valid_raw = [response for response in all_raw if response.is_valid]
    result.skipped_responses = [
        response for response in all_raw if not response.is_valid
    ]
    result.raw_total_attendance = sum(response.value for response in valid_raw)

    kept = _deduplicate(valid_raw, result)
    result.family_alerts = _family_alerts(kept)
    result.responses = sorted(kept, key=lambda response: response.value, reverse=True)
    result.total_attendance = sum(response.value for response in kept)
    result.total_respondents = len(kept)
    return result


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _try_parse_int(value: object) -> int | None:
    if not value:
        return None
    try:
        parsed = int(float(str(value).strip()))
        return parsed if parsed > 0 else None
    except (ValueError, TypeError):
        return None


def _find_col(header: list[str], keywords: list[str]) -> int | None:
    for index, heading in enumerate(header):
        normalized = heading.lower()
        if any(keyword in normalized for keyword in keywords):
            return index
    return None


def _is_section_title(row: list[str]) -> bool:
    return len([cell for cell in row if cell.strip()]) == 1


def _section_rows(section: dict[str, object]) -> list[list[str]]:
    rows = section.get("rows", [])
    return rows if isinstance(rows, list) else []


def _deduplicate(
    valid_raw: list[PollResponse],
    result: ParseResult,
) -> list[PollResponse]:
    by_key: dict[str, list[PollResponse]] = defaultdict(list)
    for response in valid_raw:
        by_key[response.dedup_key].append(response)

    kept: list[PollResponse] = []
    for entries in by_key.values():
        if len(entries) == 1:
            kept.append(entries[0])
            continue

        values = [entry.value for entry in entries]
        if len(set(values)) == 1:
            best = entries[0]
            result.duplicate_alerts.append(
                DuplicateAlert(
                    email=best.email,
                    name=best.name,
                    count=len(entries),
                    value=best.value,
                    kept_index=best.index,
                )
            )
        else:
            best = max(entries, key=lambda entry: entry.value)
            result.divergence_alerts.append(
                DivergenceAlert(
                    email=best.email,
                    name=best.name,
                    values=sorted(values),
                    kept_value=best.value,
                    kept_index=best.index,
                )
            )
        kept.append(best)
    return kept


def _family_alerts(responses: list[PollResponse]) -> list[FamilyAlert]:
    by_lastname: dict[str, list[PollResponse]] = defaultdict(list)
    for response in responses:
        if response.last_name:
            by_lastname[response.last_name].append(response)

    alerts: list[FamilyAlert] = []
    for last_name, members in by_lastname.items():
        if len(members) < 2 or not any(member.value > 1 for member in members):
            continue
        family_count = len(members)
        family_max_value = max(member.value for member in members)
        alerts.append(
            FamilyAlert(
                last_name=last_name.title(),
                members=members,
                family_count=family_count,
                family_max_value=family_max_value,
                family_corrected=max(family_count, family_max_value),
            )
        )
    return alerts
