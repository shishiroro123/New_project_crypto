"""Thin ccxt wrapper. Centralises retry / rate-limit handling."""

from __future__ import annotations

import ccxt
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_bot.config import Secrets
from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)


_TRANSIENT_ERRORS = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
)


def make_binance(secrets: Secrets | None = None, testnet: bool | None = None) -> ccxt.binance:
    """Build a configured Binance client. Read-only if no secrets provided."""
    secrets = secrets or Secrets()
    use_testnet = secrets.binance_testnet if testnet is None else testnet

    client = ccxt.binance(
        {
            "apiKey": secrets.binance_api_key or None,
            "secret": secrets.binance_api_secret or None,
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
    )
    if use_testnet:
        client.set_sandbox_mode(True)
        log.info("binance.testnet_enabled")
    return client


@retry(
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def fetch_ohlcv(
    client: ccxt.binance,
    symbol: str,
    timeframe: str,
    since_ms: int | None = None,
    limit: int = 1000,
) -> list[list[float]]:
    """Wrapped fetch_ohlcv with exponential backoff on transient errors."""
    return client.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
