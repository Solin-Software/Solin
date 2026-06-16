from __future__ import annotations

from solin.core.integrations.automation.zoom.poll_parser import (
    parse_zoom_poll_csv,
    parse_zoom_poll_text,
)


def test_zoom_poll_parser_deduplicates_divergences_family_and_skips_text():
    result = parse_zoom_poll_text(
        "\n".join(
            [
                "Overview",
                "Generated At,Topic,Meeting ID,Start Time",
                "2026-05-27 20:00:00,Midweek Meeting,123,2026-05-27 19:30:00",
                "Poll Summary",
                "Question,Responses",
                "How many people are watching?,7",
                "Question 1",
                "#,Name,Email,Date and Time,How many people are watching?",
                "1,Ana Silva,ana@example.com,2026-05-27 19:31:00,2",
                "2,Ana Silva,ana@example.com,2026-05-27 19:32:00,2",
                "3,Bruno Costa,bruno@example.com,2026-05-27 19:33:00,1",
                "4,Bruno Costa,bruno@example.com,2026-05-27 19:34:00,3",
                "5,Carlos Lima,carlos@example.com,2026-05-27 19:35:00,2",
                "6,Carla Lima,carla@example.com,2026-05-27 19:36:00,1",
                "7,Joao Souza,joao@example.com,2026-05-27 19:37:00,yes",
            ]
        )
    )

    assert result.parse_error == ""
    assert result.meeting.topic == "Midweek Meeting"
    assert result.meeting.meeting_id == "123"
    assert result.question_text == "How many people are watching?"
    assert result.raw_total_attendance == 11
    assert result.total_attendance == 8
    assert result.total_respondents == 4
    assert [alert.name for alert in result.duplicate_alerts] == ["Ana Silva"]
    assert [alert.kept_value for alert in result.divergence_alerts] == [3]
    assert [alert.last_name for alert in result.family_alerts] == ["Lima"]
    assert [response.name for response in result.skipped_responses] == ["Joao Souza"]


def test_zoom_poll_parser_reports_empty_and_missing_files(tmp_path):
    assert parse_zoom_poll_text("").parse_error == "Arquivo vazio."

    missing = parse_zoom_poll_csv(tmp_path / "missing.csv")
    assert missing.parse_error
