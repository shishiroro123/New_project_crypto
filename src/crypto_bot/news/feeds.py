"""News feed pollers (read-only).

For v1 the news layer is informational: we surface incoming items to the
logs and (optionally) Telegram. Sentiment-driven sizing is on the v0.3 roadmap
once we have a clean dataset to evaluate.

Two feeds:
- CryptoPanicFeed: JSON API, free tier, requires no key for /v1/posts/?public=true
- BinanceAnnouncements: scrapes the public RSS-like JSON used by Binance's
  /bapi/composite/v1/public/cms/article/list/query endpoint (no auth needed).

Both expose `.poll(since: datetime | None) -> list[NewsItem]` returning items
strictly newer than `since` (or all known if None).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)

_TRANSIENT = (httpx.HTTPError, httpx.TimeoutException)


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published_at: datetime
    currencies: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@retry(
    retry=retry_if_exception_type(_TRANSIENT),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    reraise=True,
)
def _http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    ca_bundle: str = "",
) -> Any:
    verify: str | bool = ca_bundle if ca_bundle else True
    with httpx.Client(
        timeout=15.0,
        headers={"User-Agent": "crypto-bot/0.1"},
        verify=verify,
    ) as client:
        resp = client.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()


class CryptoPanicFeed:
    """Free public posts. Returns news items, optionally filtered by symbols."""

    API = "https://cryptopanic.com/api/v1/posts/"

    def __init__(
        self,
        auth_token: str = "",
        currencies: list[str] | None = None,
        ca_bundle: str = "",
    ) -> None:
        self.token = auth_token
        self.currencies = currencies or []
        self.ca_bundle = ca_bundle

    def poll(self, since: datetime | None = None) -> list[NewsItem]:
        params: dict[str, Any] = {"public": "true"}
        if self.token:
            params["auth_token"] = self.token
        if self.currencies:
            params["currencies"] = ",".join(self.currencies)

        try:
            data = _http_get_json(self.API, params, ca_bundle=self.ca_bundle)
        except Exception as exc:  # noqa: BLE001
            log.warning("news.cryptopanic_failed", error=str(exc))
            return []

        items: list[NewsItem] = []
        for r in data.get("results", []) or []:
            published = r.get("published_at")
            if not published:
                continue
            try:
                ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if since is not None and ts <= since:
                continue
            ccys = [c.get("code", "") for c in r.get("currencies") or [] if c.get("code")]
            items.append(
                NewsItem(
                    source="cryptopanic",
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    published_at=ts,
                    currencies=ccys,
                    raw=r,
                )
            )
        items.sort(key=lambda i: i.published_at)
        return items


class BinanceAnnouncements:
    """Polls Binance's public announcement list (listings, delistings, etc.).

    The endpoint is public but rate-limited. We use catalogId=48 = "New Cryptocurrency Listing".
    Adjust catalog_id to follow other categories (49 = Latest, 161 = Earn, ...).
    """

    API = (
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    )

    def __init__(self, catalog_id: int = 48, page_size: int = 20, ca_bundle: str = "") -> None:
        self.catalog_id = catalog_id
        self.page_size = page_size
        self.ca_bundle = ca_bundle

    def poll(self, since: datetime | None = None) -> list[NewsItem]:
        params = {"type": "1", "catalogId": str(self.catalog_id), "pageNo": "1",
                  "pageSize": str(self.page_size)}
        try:
            data = _http_get_json(self.API, params, ca_bundle=self.ca_bundle)
        except Exception as exc:  # noqa: BLE001
            log.warning("news.binance_announcements_failed", error=str(exc))
            return []

        items: list[NewsItem] = []
        for art in (data.get("data") or {}).get("articles") or []:
            release_ms = art.get("releaseDate")
            if not release_ms:
                continue
            ts = datetime.fromtimestamp(release_ms / 1000, tz=UTC)
            if since is not None and ts <= since:
                continue
            code = art.get("code", "")
            items.append(
                NewsItem(
                    source="binance_announcements",
                    title=art.get("title", ""),
                    url=f"https://www.binance.com/en/support/announcement/{code}" if code else "",
                    published_at=ts,
                    raw=art,
                )
            )
        items.sort(key=lambda i: i.published_at)
        return items
