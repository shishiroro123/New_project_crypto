"""Parse Helius-enhanced Solana transactions to detect SPL token buys.

A "buy" from our watcher's perspective is: the tracked wallet *receives* an
SPL token AND *sends* SOL or a stablecoin (USDC/USDT) in the same tx.

Helius enhanced API normalises swap transactions for us so we don't have to
decode raw account state. We only need their `tokenTransfers` and `events.swap`
fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Wrapped SOL and the two major Solana stables, all considered "quote" assets:
# spending these to receive another token = a buy.
QUOTE_MINTS: set[str] = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Tether)
}

QUOTE_SYMBOLS: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}


@dataclass
class BuyEvent:
    signature: str
    wallet: str
    token_mint: str
    token_symbol: str | None
    quote_mint: str
    quote_symbol: str
    amount_in: float          # quantity of quote spent
    amount_out: float         # quantity of token received
    tx_timestamp: str         # ISO8601


def parse_buy(tx: dict[str, Any], wallet: str) -> BuyEvent | None:
    """Return a BuyEvent if `tx` represents `wallet` buying an SPL token.

    Logic: look through `tokenTransfers`. We want exactly one transfer where
    the wallet is the recipient (the SPL token we bought) and at least one
    transfer where the wallet is the sender of a quote asset. If both
    conditions hold, it's a buy.
    """
    transfers = tx.get("tokenTransfers") or []
    if not transfers:
        return None

    received_non_quote: list[dict] = []
    sent_quote: list[dict] = []

    for tt in transfers:
        mint = tt.get("mint")
        if not mint:
            continue
        to_user = tt.get("toUserAccount")
        from_user = tt.get("fromUserAccount")
        if to_user == wallet and mint not in QUOTE_MINTS:
            received_non_quote.append(tt)
        elif from_user == wallet and mint in QUOTE_MINTS:
            sent_quote.append(tt)

    if not received_non_quote or not sent_quote:
        return None

    # Pick the largest received SPL token transfer (in case there are dust
    # token-account rent refunds in the same tx) and the largest quote spent.
    received_non_quote.sort(key=lambda t: float(t.get("tokenAmount") or 0), reverse=True)
    sent_quote.sort(key=lambda t: float(t.get("tokenAmount") or 0), reverse=True)
    recv = received_non_quote[0]
    sent = sent_quote[0]

    timestamp = tx.get("timestamp")
    iso = _epoch_to_iso(timestamp) if timestamp else ""

    return BuyEvent(
        signature=tx.get("signature", ""),
        wallet=wallet,
        token_mint=recv["mint"],
        token_symbol=None,
        quote_mint=sent["mint"],
        quote_symbol=QUOTE_SYMBOLS.get(sent["mint"], "?"),
        amount_in=float(sent.get("tokenAmount") or 0),
        amount_out=float(recv.get("tokenAmount") or 0),
        tx_timestamp=iso,
    )


def _epoch_to_iso(epoch: int) -> str:
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).isoformat()
    except (TypeError, ValueError):
        return ""
