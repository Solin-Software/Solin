"""Framework-free yearly text parsing and fetch policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
import re
import urllib.parse
from typing import Any

from solin.core.network.http import HttpDecodeError, HttpError, get_json

log = logging.getLogger(__name__)

WOL_YEAR_TEXT_API_URL = "https://wol.jw.org/wol/finder"
YEAR_TEXT_API_TIMEOUT = 12
YEAR_TEXT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Yeartext:
    api_code: str
    year: int
    quote: str
    reference: str


class YeartextFetchError(RuntimeError):
    """The yearly text could not be fetched or parsed."""


def strip_yeartext_html_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def parse_yeartext_html(html: str) -> tuple[str, str]:
    """Parse WOL yearly text HTML into ``(quote, reference)``.

    The final non-empty paragraph is treated as the localized Bible reference
    and is preserved exactly as WOL sends it.
    """
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL | re.IGNORECASE)
    texts = [strip_yeartext_html_tags(paragraph) for paragraph in paragraphs]
    texts = [text for text in texts if text]

    if not texts:
        return strip_yeartext_html_tags(html), ""
    if len(texts) == 1:
        return texts[0], ""

    return "\n".join(texts[:-1]), texts[-1]


def yeartext_request_url(api_code: str, year: int) -> str:
    params = {
        "docid": f"110{year}800",
        "format": "json",
        "snip": "yes",
        "wtlocale": api_code,
    }
    return f"{WOL_YEAR_TEXT_API_URL}?{urllib.parse.urlencode(params)}"


def yeartext_request_headers() -> dict[str, str]:
    return {
        "User-Agent": YEAR_TEXT_USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://wol.jw.org/",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_yeartext(
    api_code: str,
    year: int,
    *,
    http_get_json: Callable[..., Mapping[str, Any]] = get_json,
) -> Yeartext:
    url = yeartext_request_url(api_code, year)
    log.debug("[yeartext] GET %s", url)

    try:
        data = http_get_json(
            url,
            timeout=YEAR_TEXT_API_TIMEOUT,
            headers=yeartext_request_headers(),
        )
    except HttpDecodeError as exc:
        raise YeartextFetchError(f"JSON invalido: {exc}") from exc
    except HttpError as exc:
        log.warning("[yeartext] Request failed (%s/%d): %s", api_code, year, exc)
        raise YeartextFetchError(str(exc)) from exc

    log.debug("[yeartext] JSON response (%s/%d): %s", api_code, year, data)

    if not data.get("exists", False):
        raise YeartextFetchError(f"Texto {year} nao encontrado para '{api_code}'")

    content = str(data.get("content") or "")
    if not content:
        raise YeartextFetchError("Conteudo vazio na resposta da API")

    quote, reference = parse_yeartext_html(content)
    if not quote:
        raise YeartextFetchError("Nao foi possivel extrair o texto da resposta")

    log.info(
        "[yeartext] OK (%s/%d): %s... / ref: %s",
        api_code,
        year,
        quote[:60],
        reference,
    )
    return Yeartext(api_code=api_code, year=year, quote=quote, reference=reference)
