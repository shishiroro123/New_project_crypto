"""Telegram alerts. Synchronous httpx-based to keep the runner simple.

The bot is no-op if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are missing,
so it stays optional everywhere.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_bot.logging_setup import get_logger
from crypto_bot.state import ClosedTrade

log = get_logger(__name__)


class TelegramAlerter:
    def __init__(self, bot_token: str = "", chat_id: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            log.info("alerts.telegram_disabled")

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=False,
    )
    def _post(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        with httpx.Client(timeout=10.0) as client:
            client.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
            )

    def send(self, text: str) -> None:
        try:
            self._post(text)
        except Exception as exc:  # noqa: BLE001
            # Never let alerting fail the bot.
            log.warning("alerts.send_failed", error=str(exc))

    def notify_entry(self, symbol: str, qty: float, price: float, stop: float) -> None:
        msg = (
            f"*ENTRY* `{symbol}`\n"
            f"qty: `{qty:.6f}` @ `{price:.4f}`\n"
            f"stop: `{stop:.4f}` (risk: `{((price - stop) / price) * 100:.2f}%`)"
        )
        self.send(msg)

    def notify_exit(self, trade: ClosedTrade) -> None:
        emoji = "✅" if trade.pnl > 0 else "❌"
        msg = (
            f"{emoji} *EXIT* `{trade.symbol}` ({trade.reason})\n"
            f"entry `{trade.entry_price:.4f}` → exit `{trade.exit_price:.4f}`\n"
            f"pnl: `{trade.pnl:+.2f}` ( `{trade.pnl_pct * 100:+.2f}%` )"
        )
        self.send(msg)

    def notify_error(self, label: str, error: str) -> None:
        self.send(f"⚠️ *ERROR* {label}\n`{error[:500]}`")

    def daily_summary(
        self,
        ts: datetime,
        equity: float,
        n_trades: int,
        day_pnl: float,
        n_positions: int,
    ) -> None:
        msg = (
            f"📊 *Daily summary* {ts:%Y-%m-%d}\n"
            f"equity: `{equity:.2f}`\n"
            f"trades today: `{n_trades}`\n"
            f"day pnl: `{day_pnl:+.2f}`\n"
            f"open positions: `{n_positions}`"
        )
        self.send(msg)
