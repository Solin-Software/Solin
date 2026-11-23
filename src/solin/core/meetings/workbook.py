"""Read a weekly meeting workbook without depending on the timer or UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import logging
import re
from typing import Literal
from urllib.parse import urlsplit

from solin.core.jw.wol_finder import finder_headers, finder_url
from solin.core.network.http import HttpError, get

log = logging.getLogger(__name__)

Section = Literal["opening_comments", "treasures", "ministry", "living", "concluding_comments"]
Kind = Literal["standard", "cbs"]
_TIMEOUT = (3.0, 8.0)
_MAX_RESPONSE_BYTES = 512 * 1024
_SECTIONS: dict[str, Section] = {
    "dc-icon--gem": "treasures",
    "dc-icon--wheat": "ministry",
    "dc-icon--sheep": "living",
}
_NUMBERED = re.compile(r"^\s*(\d+)\s*[.．-]\s*(\S.*?)\s*$")
_DURATION = re.compile(r"[\(（]\s*(\d+)\s*([^\d()（）]{1,30})[\)）]")
_BIDI_MARKS = str.maketrans(
    "", "", "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_DOC_PATH = re.compile(r"^/[^/]+/wol/d/[^/]+/[^/]+/(\d+)$")
_WEEK_PATH = re.compile(r"^/[^/]+/wol/meetings/[^/]+/[^/]+/(\d{4})/(\d{1,2})/?$")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


@dataclass(frozen=True, slots=True)
class WorkbookPart:
    section: Section
    kind: Kind
    number: int | None
    title: str
    duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class Workbook:
    parts: tuple[WorkbookPart, ...]


class _WeeklyLinks(HTMLParser):
    def __init__(self, api_code: str) -> None:
        super().__init__(convert_charrefs=True)
        self.api_code = api_code
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        path = attributes.get("href") or ""
        if (
            tag == "a"
            and {"docClass-106", f"ml-{self.api_code}"} <= classes
            and any(re.fullmatch(r"pub-mwb(?:\d{2})?", item) for item in classes)
            and _DOC_PATH.fullmatch(path)
        ):
            self.paths.add(path)


def _duration(text: str) -> tuple[int, str] | None:
    match = _DURATION.match(text.strip())
    if not match:
        return None
    minutes = int(match.group(1))
    unit = match.group(2).strip().casefold().rstrip(". ")
    return (minutes * 60, unit) if 0 < minutes <= 180 and unit else None


def _same_unit(left: str, right: str) -> bool:
    return left == right or left == right + "s" or right == left + "s"


class _WorkbookParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section: Section | None = None
        self.sections: list[Section] = []
        self.parts: list[WorkbookPart] = []
        self.durations: dict[int, tuple[int, str]] = {}
        self.framing_units: set[str] = set()
        self.capture: str | None = None
        self.depth = 0
        self.text: list[str] = []
        self.heading_classes: set[str] = set()
        self.pending_duration = False
        self.invalid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.capture:
            if tag not in _VOID_TAGS:
                self.depth += 1
            return
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "div":
            found: list[Section] = [
                section for icon, section in _SECTIONS.items() if icon in classes
            ]
            if found:
                if len(found) != 1:
                    self.invalid = True
                self.section = found[0]
                self.sections.append(found[0])
                self.pending_duration = False
        elif tag == "h3":
            self.capture, self.depth, self.text = "h3", 0, []
            self.heading_classes = classes
            self.pending_duration = False
        elif tag == "p" and self.pending_duration:
            self.capture, self.depth, self.text = "p", 0, []
            self.pending_duration = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.capture:
            return
        if self.depth:
            self.depth -= 1
            return
        if tag != self.capture:
            return
        text = " ".join("".join(self.text).translate(_BIDI_MARKS).split())
        capture = self.capture
        self.capture = None
        if capture == "p":
            duration = _duration(text)
            if duration:
                self.durations[len(self.parts) - 1] = duration
            return
        numbered = _NUMBERED.match(text)
        if numbered and self.section:
            self.parts.append(
                WorkbookPart(
                    self.section, "standard", int(numbered.group(1)), numbered.group(2), None
                )
            )
            self.pending_duration = True
        elif (
            self.section is None
            and not self.sections
            and "du-borderStyle-top--solid" in self.heading_classes
        ):
            title = text.rsplit("|", 1)[-1].strip()
            duration_match = _DURATION.search(title)
            duration = _duration(duration_match.group()) if duration_match else None
            title = _DURATION.sub("", title).strip()
            self.parts.append(
                WorkbookPart(
                    "opening_comments",
                    "standard",
                    None,
                    title,
                    duration[0] if duration else None,
                )
            )
            if duration:
                self.framing_units.add(duration[1])
        elif (
            self.section == "living"
            and self.parts
            and self.parts[-1].section == "living"
            and "du-borderStyle-top--solid" in self.heading_classes
        ):
            title = text.split("|", 1)[0].strip()
            duration_match = _DURATION.search(title)
            duration = _duration(duration_match.group()) if duration_match else None
            title = _DURATION.sub("", title).strip()
            self.parts.append(
                WorkbookPart(
                    "concluding_comments",
                    "standard",
                    None,
                    title,
                    duration[0] if duration else None,
                )
            )
            if duration:
                self.framing_units.add(duration[1])
            self.section = "concluding_comments"

    def result(self) -> Workbook | None:
        if self.invalid or self.sections != ["treasures", "ministry", "living"]:
            return None
        for index, (seconds, unit) in self.durations.items():
            if any(_same_unit(unit, framing_unit) for framing_unit in self.framing_units):
                part = self.parts[index]
                self.parts[index] = WorkbookPart(
                    part.section, part.kind, part.number, part.title, seconds
                )
        numbers = [p.number for p in self.parts if p.number is not None]
        if not numbers or numbers != list(range(1, len(numbers) + 1)):
            return None
        if (
            not self.parts
            or self.parts[0].section != "opening_comments"
            or self.parts[-1].section != "concluding_comments"
        ):
            return None
        if any(not part.title for part in self.parts):
            return None
        order = ["opening_comments", "treasures", "ministry", "living", "concluding_comments"]
        positions = [order.index(part.section) for part in self.parts]
        if positions != sorted(positions) or positions.count(0) != 1 or positions.count(4) != 1:
            return None
        living = [p for p in self.parts if p.section == "living"]
        if not any(p.section == "treasures" for p in self.parts) or not any(
            p.section == "ministry" for p in self.parts
        ):
            return None
        if (
            len(living) < 2
            or living[-1].duration_seconds != 1800
            or any(p.duration_seconds == 1800 for p in living[:-1])
        ):
            return None
        cbs = living[-1]
        index = self.parts.index(cbs)
        self.parts[index] = WorkbookPart(
            cbs.section, "cbs", cbs.number, cbs.title, cbs.duration_seconds
        )
        return Workbook(tuple(self.parts))


def parse_workbook(content: str) -> Workbook | None:
    """Return a workbook only when its section and study structure is unambiguous."""
    parser = _WorkbookParser()
    parser.feed(content)
    parser.close()
    return parser.result()


def fetch_workbook(
    week_monday: date,
    api_code: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> Workbook | None:
    """Fetch the workbook for the requested ISO week and WOL language."""
    if week_monday.weekday() != 0 or not api_code or not re.fullmatch(r"[A-Za-z0-9_-]+", api_code):
        return None
    cancelled = is_cancelled or (lambda: False)
    if cancelled():
        return None
    try:
        weekly = get(
            finder_url(alias="meetings", date=week_monday.isoformat(), wtlocale=api_code),
            headers=finder_headers(),
            timeout=_TIMEOUT,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
        parsed_url = urlsplit(weekly.url)
        week_match = _WEEK_PATH.fullmatch(parsed_url.path)
        iso_year, iso_week, _ = week_monday.isocalendar()
        if (
            weekly.status_code != 200
            or parsed_url.scheme != "https"
            or parsed_url.netloc != "wol.jw.org"
            or not week_match
            or (int(week_match.group(1)), int(week_match.group(2))) != (iso_year, iso_week)
        ):
            return None
        links = _WeeklyLinks(api_code)
        links.feed(weekly.text())
        if len(links.paths) != 1 or cancelled():
            return None
        path = next(iter(links.paths))
        match = _DOC_PATH.fullmatch(path)
        if not match:
            return None
        response = get(
            finder_url(docid=match.group(1), format="json", snip="yes", wtlocale=api_code),
            headers=finder_headers(),
            timeout=_TIMEOUT,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
        json_url = urlsplit(response.url)
        if (
            response.status_code != 200
            or json_url.scheme != "https"
            or json_url.netloc != "wol.jw.org"
            or cancelled()
        ):
            return None
        data = response.json()
        if not isinstance(data, dict) or data.get("exists") is not True or data.get("url") != path:
            return None
        content = data.get("content")
        return parse_workbook(content) if isinstance(content, str) and content else None
    except (HttpError, ValueError, RecursionError) as exc:
        log.debug("Workbook fetch failed for %s (%s): %s", week_monday, api_code, exc)
        return None
