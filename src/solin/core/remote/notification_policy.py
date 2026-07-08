from __future__ import annotations

from dataclasses import dataclass

from .urls import is_safe_remote_url

FALLBACK_LANG: str = "E"

_REQUIRED_FIELDS = frozenset({"id", "type", "content"})
_VALID_TYPES = frozenset({"info", "warning", "error"})


@dataclass(frozen=True, slots=True)
class Notification:
    """Remote notification resolved for display."""

    id: str
    notif_type: str
    title: str
    detail: str
    action_url: str = ""
    action_label: str = ""


@dataclass(frozen=True, slots=True)
class NotificationProcessingResult:
    notifications: tuple[Notification, ...]
    mark_seen_ids: tuple[str, ...]


def resolve_remote_notifications(
    payload: object,
    *,
    api_code: str,
    seen_ids: set[str],
) -> NotificationProcessingResult:
    if not isinstance(payload, dict):
        return NotificationProcessingResult((), ())

    raw_list = payload.get("notifications")
    if not isinstance(raw_list, list):
        return NotificationProcessingResult((), ())

    blocked_ids = set(seen_ids)
    notifications: list[Notification] = []
    mark_seen_ids: list[str] = []

    for item in raw_list:
        notification = _resolve_notification(item, api_code=api_code, seen_ids=blocked_ids)
        if notification is None:
            continue
        notifications.append(notification)
        mark_seen_ids.append(notification.id)
        blocked_ids.add(notification.id)

    return NotificationProcessingResult(tuple(notifications), tuple(mark_seen_ids))


def _resolve_notification(
    item: object,
    *,
    api_code: str,
    seen_ids: set[str],
) -> Notification | None:
    if not isinstance(item, dict):
        return None
    if not _REQUIRED_FIELDS.issubset(item.keys()):
        return None

    notification_id = str(item["id"]).strip()
    if not notification_id or notification_id in seen_ids:
        return None

    content = item.get("content", {})
    if not isinstance(content, dict):
        return None

    localized = _localized_content(content, api_code)
    if localized is None:
        return None

    title = str(localized.get("title", "")).strip()
    detail = str(localized.get("detail", "")).strip()
    notification_type = str(item.get("type", "info")).lower()
    if notification_type not in _VALID_TYPES:
        notification_type = "info"

    action_url = ""
    action_label = ""
    action = item.get("action")
    if isinstance(action, dict):
        candidate_url = str(action.get("url", "")).strip()
        if candidate_url and is_safe_remote_url(candidate_url):
            action_url = candidate_url
            action_label = str(action.get("label", "")).strip()

    return Notification(
        id=notification_id,
        notif_type=notification_type,
        title=title,
        detail=detail,
        action_url=action_url,
        action_label=action_label,
    )


def _localized_content(content: dict, api_code: str) -> dict | None:
    preferred_codes = (api_code, FALLBACK_LANG)
    for code in preferred_codes:
        candidate = content.get(code)
        if _has_title(candidate):
            return candidate
    return next(
        (candidate for candidate in content.values() if _has_title(candidate)),
        None,
    )


def _has_title(candidate: object) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("title"), str)
        and bool(candidate["title"].strip())
    )
