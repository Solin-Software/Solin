from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from solin.core.remote.notification_policy import (
    Notification,
    resolve_remote_notifications,
)


def test_remote_notification_policy_resolves_localized_content_and_actions() -> None:
    result = resolve_remote_notifications(
        {
            "notifications": [
                {
                    "id": "notice-1",
                    "type": "warning",
                    "content": {
                        "E": {"title": "Fallback", "detail": "English detail"},
                        "T": {"title": "Titulo", "detail": "Detalhe"},
                    },
                    "action": {
                        "url": "https://solin.example/release-notes",
                        "label": "Read",
                    },
                }
            ]
        },
        api_code="T",
        seen_ids=set(),
    )

    assert result.mark_seen_ids == ("notice-1",)
    assert result.notifications == (
        Notification(
            id="notice-1",
            notif_type="warning",
            title="Titulo",
            detail="Detalhe",
            action_url="https://solin.example/release-notes",
            action_label="Read",
        ),
    )


def test_remote_notification_policy_uses_english_fallback_and_filters_seen_ids() -> None:
    result = resolve_remote_notifications(
        {
            "notifications": [
                {
                    "id": "seen",
                    "type": "info",
                    "content": {"E": {"title": "Seen", "detail": ""}},
                },
                {
                    "id": "new",
                    "type": "info",
                    "content": {"E": {"title": "Fallback", "detail": ""}},
                },
            ]
        },
        api_code="T",
        seen_ids={"seen"},
    )

    assert [notification.id for notification in result.notifications] == ["new"]
    assert result.mark_seen_ids == ("new",)


def test_remote_notification_policy_deduplicates_ids_inside_one_payload() -> None:
    result = resolve_remote_notifications(
        {
            "notifications": [
                {
                    "id": "same",
                    "type": "info",
                    "content": {"E": {"title": "First", "detail": ""}},
                },
                {
                    "id": "same",
                    "type": "warning",
                    "content": {"E": {"title": "Second", "detail": ""}},
                },
            ]
        },
        api_code="E",
        seen_ids=set(),
    )

    assert [notification.title for notification in result.notifications] == ["First"]
    assert result.mark_seen_ids == ("same",)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"notifications": "bad"},
        {"notifications": [None, {"id": "", "type": "info", "content": {}}]},
        {"notifications": [{"id": "x", "type": "info", "content": {"T": {}}}]},
    ],
)
def test_remote_notification_policy_rejects_invalid_payloads(payload: object) -> None:
    result = resolve_remote_notifications(payload, api_code="T", seen_ids=set())

    assert result.notifications == ()
    assert result.mark_seen_ids == ()


def test_remote_notification_policy_normalizes_type_and_rejects_unsafe_action_url() -> None:
    result = resolve_remote_notifications(
        {
            "notifications": [
                {
                    "id": "notice-1",
                    "type": "surprise",
                    "content": {"E": {"title": "Title", "detail": "Detail"}},
                    "action": {"url": "file:///tmp/unsafe.exe", "label": "Open"},
                }
            ]
        },
        api_code="E",
        seen_ids=set(),
    )

    assert len(result.notifications) == 1
    notification = result.notifications[0]
    assert notification.notif_type == "info"
    assert notification.action_url == ""
    assert notification.action_label == ""


def test_remote_notification_model_is_immutable() -> None:
    notification = Notification(
        id="notice-1",
        notif_type="info",
        title="Title",
        detail="Detail",
    )

    with pytest.raises(FrozenInstanceError):
        notification.title = "Changed"  # type: ignore[misc]
