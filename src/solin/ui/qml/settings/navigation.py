"""Page-independent navigation and translated search for settings."""

from __future__ import annotations

import unicodedata

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from solin.core.foundation.constants import APP_VERSION
from .catalogue import translated_catalogue
from .domain import SettingsDomain


def normalize_search(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


class SettingsNavigation(QObject):
    changed = Signal()
    destinationChanged = Signal()
    expansionChanged = Signal(str, bool)

    def __init__(self, domains: dict[str, SettingsDomain], parent=None):
        super().__init__(parent)
        self._domains = domains
        self._sections = []
        self._query = ""
        self._section = "appearance"
        self._target = ""
        self._target_group = ""
        self._details = False
        self._scroll: dict[str, float] = {}
        self._expanded: set[str] = set()
        self._group_ancestors: dict[str, tuple[str, ...]] = {}
        self.refresh_language()

    @Property("QVariantList", notify=changed)
    def sections(self):
        return self._sections

    @Property("QVariantMap", notify=changed)
    def currentSection(self):  # noqa: N802
        return next(section for section in self._sections if section["id"] == self._section)

    @Property(str, notify=changed)
    def query(self):
        return self._query

    @Property(bool, notify=changed)
    def detailsOpen(self):  # noqa: N802
        return self._details

    @Property(str, notify=changed)
    def targetKey(self):  # noqa: N802
        return self._target

    @Property(str, constant=True)
    def version(self):
        return APP_VERSION

    @Property("QVariantList", notify=changed)
    def results(self):
        terms = normalize_search(self._query).split()
        if not terms:
            return []
        matches = []
        for section in self._sections:
            for group in self._walk_groups(section["groups"]):
                for row in group["rows"]:
                    # Conditional edit fields remain discoverable: navigating reveals
                    # their owning form, without indexing private field values.
                    if row["kind"] in {"screens", "shortcuts"}:
                        continue
                    label = " ".join((section["title"], section["description"],
                                      group["title"], row["label"], row["description"]))
                    if all(term in normalize_search(label) for term in terms):
                        matches.append({"section": section["id"], "group": group["id"],
                                        "key": row["key"], "title": row["label"],
                                        "description": section["title"] + " · " + group["title"],
                                        "icon": section["icon"]})
        return matches

    @Slot(str)
    def search(self, text):
        if text != self._query:
            self._query = text
            self._details = False
            self.changed.emit()

    @Slot(str, str, str)
    def openSection(self, section, group="", key=""):  # noqa: N802
        if not any(item["id"] == section for item in self._sections):
            return
        self._section, self._target_group, self._target = section, group, key
        self._details = True
        if group:
            for expanded_group in (*self._group_ancestors.get(group, ()), group):
                self.expand(expanded_group, True)
        self.changed.emit()
        self.destinationChanged.emit()

    @Slot()
    def back(self):
        self._details = False
        self._target = ""
        self.changed.emit()
        self.destinationChanged.emit()

    @Slot(str, bool)
    def expand(self, group, expanded):
        was_expanded = group in self._expanded
        if expanded:
            self._expanded.add(group)
        else:
            self._expanded.discard(group)
        if was_expanded != expanded:
            self.expansionChanged.emit(group, expanded)

    @Slot(str, result=bool)
    def expanded(self, group):
        return group in self._expanded

    @Slot(str, float)
    def rememberScroll(self, section, value):  # noqa: N802
        self._scroll[section] = value

    @Slot(str, result=float)
    def scrollPosition(self, section):  # noqa: N802
        return self._scroll.get(section, 0.0)

    @Slot(str)
    def openLink(self, destination):  # noqa: N802
        urls = {"website": "https://solinav.vercel.app/",
                "changelog": "https://solinav.vercel.app/changelog"}
        if destination in urls:
            QDesktopServices.openUrl(QUrl(urls[destination]))

    def refresh_language(self):
        self._sections = translated_catalogue({key: domain.state for key, domain in self._domains.items()})
        self._group_ancestors.clear()
        for section in self._sections:
            self._record_group_ancestors(section["groups"])
        self.changed.emit()

    @staticmethod
    def _walk_groups(groups):
        for group in groups:
            yield group
            yield from SettingsNavigation._walk_groups(group.get("children", []))

    def _record_group_ancestors(self, groups, ancestors=()):
        for group in groups:
            self._group_ancestors[group["id"]] = ancestors
            self._record_group_ancestors(
                group.get("children", []), (*ancestors, group["id"])
            )
