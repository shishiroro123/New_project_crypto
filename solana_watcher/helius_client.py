"""Thin Helius REST client.

We use Helius' enhanced transactions endpoint, which returns already-parsed
swap events for a given wallet — no need to decode raw Solana account state
ourselves.

Docs: https://docs.helius.dev/solana-apis/enhanced-transactions-api/parsed-transaction-history
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class HeliusClient:
    BASE = "https://api.helius.xyz/v0"

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def get_transactions(
        self,
        address: str,
        tx_type: str | None = "SWAP",
        limit: int = 25,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent enhanced transactions for `address`.

        Returns oldest-last (Helius default) — caller iterates accordingly.
        """
        url = f"{self.BASE}/addresses/{address}/transactions"
        params: dict[str, Any] = {"api-key": self.api_key, "limit": limit}
        if tx_type:
            params["type"] = tx_type
        if before:
            params["before"] = before

        # Light retry on 429 / 5xx — Helius free tier rate-limits us at
        # 10 req/sec so this rarely fires, but be safe.
        for attempt in range(3):
            try:
                r = self._client.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("helius http error (attempt %d): %s", attempt + 1, e)
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                wait = 2 ** attempt
                log.warning("helius %d, retrying in %ds", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                log.error("helius %d: %s", r.status_code, r.text[:200])
                return []
            try:
                return r.json() or []
            except ValueError:
                log.error("helius returned non-JSON: %s", r.text[:200])
                return []
        log.error("helius gave up after 3 attempts for %s", address)
        return []

    def close(self) -> None:
        self._client.close()
