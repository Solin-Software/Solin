"""The hall's meeting codes on the hub (Qt-free).

Every meeting is at ``https://<hub domain>/<code>``. The hub learns which hall serves a
code from the hall itself: Solin sends the full list of its codes whenever it changes
(``PUT /api/halls/<hall>/codes``, authenticated with the hall key). Codes the hall stops
using are retired on the hub, whose page then says the link is no longer valid.

Codes are drawn by Solin, so two congregations could draw the same one: the hub then
refuses the whole list (409) and names the codes another hall holds; Solin draws new
ones and sends the list again (:meth:`LinksState.redraw`).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .links import LinkMode, LinksState
from .settings import HubConnection

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
MAX_RETIRED = 2000   # the hub's limit; the newest are sent


@dataclass(frozen=True, slots=True)
class RegisterResult:
    ok: bool
    taken: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class CodesPayload:
    active: dict[str, str] = field(default_factory=dict)   # code -> ISO start time ("" if none)
    retired: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"codes": [{"code": code, "startsAt": starts} for code, starts in sorted(self.active.items())],
                "retired": list(self.retired)}


def codes_payload(state: LinksState, mode: LinkMode, now: datetime, *, running: str = "") -> CodesPayload:
    """The codes that lead to this hall's meetings, and those that no longer do.

    Same-link mode: the permanent code, with the next meeting's start for the hub's
    countdown. Per-meeting mode: every prepared meeting's code with its start. Codes
    of the other mode are retired (switching back re-activates them). ``running`` is
    the code of the meeting open now, always active.
    """

    active: dict[str, str] = {}
    if mode is LinkMode.SAME:
        if state.permanent is not None:
            following = next((m for m in state.upcoming(now) if not m.cancelled), None)
            active[state.permanent.code] = following.starts_at.isoformat() if following else ""
    else:
        for meeting in state.meetings:
            if meeting.code:
                active[meeting.code] = meeting.starts_at.isoformat() if meeting.scheduled else ""
    if running and running not in active:
        active[running] = ""
    inactive = {m.code for m in state.meetings if m.code}
    if state.permanent is not None:
        inactive.add(state.permanent.code)
    by_age = sorted(state.retired.items(), key=lambda item: item[1], reverse=True)
    retired = [code for code in sorted(inactive) if code not in active]
    retired += [code for code, _when in by_age if code not in active and code not in inactive]
    return CodesPayload(active=active, retired=tuple(retired[:MAX_RETIRED]))


def register_codes(hub: HubConnection, payload: CodesPayload, *, timeout: float = TIMEOUT_S) -> RegisterResult:
    """Send the hall's codes to the hub (blocking: call it off the UI thread)."""

    request = urllib.request.Request(
        f"{hub.public_origin}/api/halls/{hub.hall}/codes", method="PUT",
        data=json.dumps(payload.to_json()).encode("utf-8"),
        headers={"Authorization": f"Bearer {hub.hall_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        return RegisterResult(ok=True)
    except urllib.error.HTTPError as error:
        with error:
            try:
                body = json.loads(error.read() or b"{}")
            except ValueError:
                body = {}
        if error.code == 409 and isinstance(body, dict) and isinstance(body.get("taken"), list):
            return RegisterResult(ok=False, taken=tuple(str(code) for code in body["taken"]))
        if error.code == 401:
            return RegisterResult(ok=False, error="the hub does not accept this hall's key")
        return RegisterResult(ok=False, error=f"the hub answered {error.code}")
    except (OSError, ValueError) as error:
        log.info("hub code registration failed: %s", error)
        return RegisterResult(ok=False, error=f"the hub cannot be reached ({getattr(error, 'reason', error)})")
