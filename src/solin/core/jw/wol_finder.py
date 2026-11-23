"""Shared WOL finder request protocol."""

from urllib.parse import urlencode

FINDER_URL = "https://wol.jw.org/wol/finder"


def finder_url(**params: str) -> str:
    return f"{FINDER_URL}?{urlencode(params)}"


def finder_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/158.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://wol.jw.org/",
        "X-Requested-With": "XMLHttpRequest",
    }
