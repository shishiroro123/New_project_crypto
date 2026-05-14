"""Solana smart-money watcher entry point.

Polls Helius enhanced API for each tracked wallet, parses swap transactions
into BuyEvents, dedups against a SQLite signature table, sends a Telegram
alert for each new buy. Read-only — does NOT trade.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path

from solana_watcher.config import WatcherConfig
from solana_watcher.helius_client import HeliusClient
from solana_watcher.storage import Storage
from solana_watcher.telegram_notifier import TelegramNotifier, escape_md
from solana_watcher.tx_parser import parse_buy

POLL_INTERVAL_SECONDS = 15
PRIME_LIMIT = 25  # how many historical tx to mark "seen" on startup per wallet


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("watcher")

_stop_requested = False


def _request_stop(*_: object) -> None:
    global _stop_requested
    log.info("stop requested; finishing current loop iteration")
    _stop_requested = True


def main() -> int:
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    wallets_path = Path("/app/config/wallets.yaml")
    cfg = WatcherConfig.load(wallets_path)

    if not cfg.wallets:
        log.error("no wallets configured in %s — exiting", wallets_path)
        return 1

    log.info("loaded %d wallets to track", len(cfg.wallets))

    helius = HeliusClient(cfg.helius_api_key)
    tg = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    store = Storage(cfg.data_dir / "solana_watcher.sqlite")

    telegram_enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
    if telegram_enabled:
        # Sanity ping. If credentials are set but the ping fails, abort: better
        # than running silently for hours on a broken token.
        if not tg.send(_startup_message(cfg.wallets)):
            log.error("Telegram sanity ping failed; check TELEGRAM_BOT_TOKEN / CHAT_ID")
            return 1
        log.info("Telegram alerts enabled")
    else:
        log.warning("Telegram not configured — alerts will be logged to stdout only")

    _prime_seen(helius, store, cfg.wallets)

    log.info("entering watch loop (poll every %ds)", POLL_INTERVAL_SECONDS)
    while not _stop_requested:
        for w in cfg.wallets:
            if _stop_requested:
                break
            _poll_wallet(helius, store, tg, w.address, w.name or w.address[:8])
        # Sleep in small slices so SIGTERM is reactive.
        for _ in range(POLL_INTERVAL_SECONDS):
            if _stop_requested:
                break
            time.sleep(1)

    log.info("shutting down; final stats: %s", store.stats())
    helius.close()
    tg.close()
    return 0


def _prime_seen(helius: HeliusClient, store: Storage, wallets: list) -> None:
    """Mark recent history as seen so we don't fire alerts for old buys on startup."""
    log.info("priming seen-signatures from last %d tx per wallet…", PRIME_LIMIT)
    for w in wallets:
        try:
            txs = helius.get_transactions(w.address, tx_type="SWAP", limit=PRIME_LIMIT)
        except Exception as e:  # noqa: BLE001
            log.warning("priming failed for %s: %s", w.address, e)
            continue
        for tx in txs:
            sig = tx.get("signature")
            if sig:
                store.mark_seen(sig, w.address)
        log.info("primed %d tx for %s", len(txs), w.name or w.address[:8])


def _poll_wallet(
    helius: HeliusClient,
    store: Storage,
    tg: TelegramNotifier,
    address: str,
    name: str,
) -> None:
    try:
        txs = helius.get_transactions(address, tx_type="SWAP", limit=10)
    except Exception as e:  # noqa: BLE001
        log.error("poll %s failed: %s", name, e)
        return

    # Helius returns newest-first; replay oldest-first so alerts are in order.
    for tx in reversed(txs):
        sig = tx.get("signature")
        if not sig or store.has_seen(sig):
            continue
        store.mark_seen(sig, address)

        buy = parse_buy(tx, address)
        if buy is None:
            continue

        store.record_buy(
            signature=buy.signature,
            wallet=buy.wallet,
            wallet_name=name,
            token_mint=buy.token_mint,
            token_symbol=buy.token_symbol,
            quote_mint=buy.quote_mint,
            amount_in=buy.amount_in,
            amount_out=buy.amount_out,
            amount_in_usd=None,  # phase-2 enrichment
            tx_timestamp=buy.tx_timestamp,
        )

        msg = _format_buy_alert(buy, name)
        sent = tg.send(msg) if (tg.bot_token and tg.chat_id) else False
        log.info(
            "BUY detected: wallet=%s token=%s amount_in=%.6f %s amount_out=%.6f tg=%s",
            name,
            buy.token_mint,
            buy.amount_in,
            buy.quote_symbol,
            buy.amount_out,
            "sent" if sent else "skipped",
        )


def _format_buy_alert(buy, name: str) -> str:
    e = escape_md
    return (
        "💰 *Smart money BUY*\n"
        f"Wallet: `{e(name)}`\n"
        f"Token: `{e(buy.token_mint)}`\n"
        f"Spent: {e(f'{buy.amount_in:.4f}')} {e(buy.quote_symbol)}\n"
        f"Received: {e(f'{buy.amount_out:.4f}')} tokens\n"
        f"Time: {e(buy.tx_timestamp)}\n"
        f"[Solscan](https://solscan.io/tx/{buy.signature}) \\| "
        f"[Birdeye](https://birdeye.so/token/{buy.token_mint}?chain=solana) \\| "
        f"[DexScreener](https://dexscreener.com/solana/{buy.token_mint})"
    )


def _startup_message(wallets: list) -> str:
    e = escape_md
    lines = [f"🤖 *Solana watcher started* \\(read\\-only\\)"]
    lines.append(f"Tracking {len(wallets)} wallets:")
    for w in wallets[:10]:
        label = w.name or w.address[:8]
        lines.append(f"• `{e(label)}` — `{e(w.address[:6])}…{e(w.address[-4:])}`")
    if len(wallets) > 10:
        lines.append(f"• …and {len(wallets) - 10} more")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
