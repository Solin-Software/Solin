from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from solin.core.jw.yeartext_content import (
    YeartextFetchError,
    fetch_yeartext,
    parse_yeartext_html,
    yeartext_request_url,
)
from solin.core.network.http import HttpDecodeError, HttpError


def test_parse_yeartext_html_preserves_reference_paragraph():
    quote, reference = parse_yeartext_html(
        "<p>Happy are those conscious</p>"
        "<p>of their spiritual need.</p>"
        "<p>-- Matthew 5:3.</p>"
        '<p class="sb"></p>'
    )

    assert quote == "Happy are those conscious\nof their spiritual need."
    assert reference == "-- Matthew 5:3."


def test_yeartext_request_url_uses_wol_docid_and_locale():
    url = yeartext_request_url("T", 2026)
    params = parse_qs(urlsplit(url).query)

    assert url.startswith("https://wol.jw.org/wol/finder?")
    assert params["docid"] == ["1102026800"]
    assert params["format"] == ["json"]
    assert params["snip"] == ["yes"]
    assert params["wtlocale"] == ["T"]


def test_fetch_yeartext_parses_successful_response():
    requests = []

    def http_get_json(url, **kwargs):
        requests.append((url, kwargs))
        return {
            "exists": True,
            "content": "<p>A quote</p><p>Matthew 5:3</p>",
        }

    result = fetch_yeartext("E", 2026, http_get_json=http_get_json)

    assert result.api_code == "E"
    assert result.year == 2026
    assert result.quote == "A quote"
    assert result.reference == "Matthew 5:3"
    assert requests[0][1]["timeout"] == 12
    assert requests[0][1]["headers"]["Referer"] == "https://wol.jw.org/"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"exists": False}, "Texto 2026 nao encontrado para 'E'"),
        ({"exists": True, "content": ""}, "Conteudo vazio"),
    ],
)
def test_fetch_yeartext_normalizes_invalid_responses(response, message):
    with pytest.raises(YeartextFetchError, match=message):
        fetch_yeartext("E", 2026, http_get_json=lambda *_args, **_kwargs: response)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (HttpDecodeError("bad json"), "JSON invalido"),
        (HttpError("offline"), "offline"),
    ],
)
def test_fetch_yeartext_normalizes_http_errors(error, message):
    def fail(*_args, **_kwargs):
        raise error

    with pytest.raises(YeartextFetchError, match=message):
        fetch_yeartext("E", 2026, http_get_json=fail)
