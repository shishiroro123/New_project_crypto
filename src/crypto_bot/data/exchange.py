"""Thin ccxt wrapper. Centralises retry / rate-limit handling.

Supports multiple exchanges via the EXCHANGE_NAME env var (default: binance).
Use 'binanceus' or 'kraken' when binance.com is geo-blocked. The `ca_bundle`
secret/env var lets us point ccxt's HTTP session at a system CA bundle in
environments that perform TLS interception.
"""

from __future__ import annotations

import ccxt
import requests
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


def _apply_ca_bundle(session: requests.Session, ca_bundle: str) -> None:
    """Force every request on this session to verify against the given bundle.

    ccxt explicitly passes ``verify=True`` to ``session.request``, which would
    otherwise shadow ``session.verify``. We monkey-patch ``session.request``
    on this single instance to always override ``verify`` with the bundle path.
    """
    original = session.request

    def patched(method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["verify"] = ca_bundle
        return original(method, url, **kwargs)

    session.request = patched  # type: ignore[assignment]
    session.verify = ca_bundle


def make_exchange(
    name: str | None = None,
    secrets: Secrets | None = None,
    testnet: bool | None = None,
) -> ccxt.Exchange:
    """Build a configured ccxt client. Read-only if no secrets provided.

    `name` falls back to secrets.exchange_name (default: 'binance').
    Currently supported: 'binance', 'binanceus', 'kraken'.
    """
    secrets = secrets or Secrets()
    name = (name or secrets.exchange_name or "binance").lower()

    common = {
        "enableRateLimit": True,
        "timeout": 20_000,
    }

    if name == "binance":
        use_testnet = secrets.binance_testnet if testnet is None else testnet
        client = ccxt.binance(
            {
                **common,
                "apiKey": secrets.binance_api_key or None,
                "secret": secrets.binance_api_secret or None,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
            }
        )
        if use_testnet:
            client.set_sandbox_mode(True)
            log.info("exchange.testnet_enabled", exchange="binance")
    elif name == "binanceus":
        client = ccxt.binanceus(
            {
                **common,
                "apiKey": secrets.binance_api_key or None,
                "secret": secrets.binance_api_secret or None,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
            }
        )
        log.info("exchange.binanceus_selected")
    elif name == "kraken":
        client = ccxt.kraken(
            {
                **common,
                "apiKey": secrets.kraken_api_key or None,
                "secret": secrets.kraken_api_secret or None,
            }
        )
        log.info("exchange.kraken_selected")
    else:
        raise ValueError(f"unsupported exchange: {name}")

    if secrets.ca_bundle:
        _apply_ca_bundle(client.session, secrets.ca_bundle)
        log.info("exchange.ca_bundle_applied", bundle=secrets.ca_bundle)

    return client


def make_binance(secrets: Secrets | None = None, testnet: bool | None = None) -> ccxt.binance:
    """Back-compat shim for tests and callers that explicitly want Binance."""
    return make_exchange("binance", secrets=secrets, testnet=testnet)  # type: ignore[return-value]


@retry(
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def fetch_ohlcv(
    client: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since_ms: int | None = None,
    limit: int = 1000,
) -> list[list[float]]:
    """Wrapped fetch_ohlcv with exponential backoff on transient errors."""
    return client.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
