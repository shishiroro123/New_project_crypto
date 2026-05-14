"""Telegram alerts. Synchronous httpx-based to keep the runner simple.

The bot is no-op if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are missing,
so it stays optional everywhere.

Why MarkdownV2 instead of Markdown:
- Telegram's classic Markdown silently breaks on unbalanced underscores or
  asterisks in user-supplied content (e.g. an exchange error containing "_").
- MarkdownV2 makes parsing strict, but that means EVERY special char must be
  escaped. The `_md_escape` helper below does this for variable interpolations
  while leaving our own formatting markers alone.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_bot.logging_setup import get_logger
from crypto_bot.state import ClosedTrade

log = get_logger(__name__)

# Per Telegram MarkdownV2 spec.
_MD2_RESERVED = r"_*[]()~`>#+-=|{}.!"
# Inside `code` / ``` blocks only ` and \ need escaping.
_MD2_CODE_RESERVED = r"`\\"


def _md_escape(s: str) -> str:
    """Escape every reserved char for MarkdownV2 — use OUTSIDE code spans."""
    out = []
    for ch in s:
        if ch in _MD2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _md_code(s: str) -> str:
    """Escape only the chars that break a `code` span. Use INSIDE backticks."""
    out = []
    for ch in s:
        if ch in _MD2_CODE_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)


class TelegramAlerter:
    def __init__(self, bot_token: str = "", chat_id: str = "", ca_bundle: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        # Optional path to a CA bundle for environments performing TLS interception.
        self._verify: str | bool = ca_bundle if ca_bundle else True
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
        with httpx.Client(timeout=10.0, verify=self._verify) as client:
            resp = client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                },
            )
            # Surface API-level rejections (bad token, malformed parse, etc.).
            resp.raise_for_status()

    def send(self, text: str) -> None:
        try:
            self._post(text)
        except Exception as exc:  # noqa: BLE001
            # Never let alerting fail the bot.
            log.warning("alerts.send_failed", error=str(exc))

    # _md_code() escapes content inside backticks; _md_escape() escapes content outside.

    def notify_entry(self, symbol: str, qty: float, price: float, stop: float) -> None:
        risk = ((price - stop) / price) * 100 if price else 0.0
        msg = (
            f"*ENTRY* `{_md_code(symbol)}`\n"
            f"qty: `{qty:.6f}` @ `{price:.4f}`\n"
            f"stop: `{stop:.4f}` "
            f"\\(risk: `{risk:.2f}%`\\)"
        )
        self.send(msg)

    def notify_exit(self, trade: ClosedTrade) -> None:
        emoji = "✅" if trade.pnl > 0 else "❌"
        msg = (
            f"{emoji} *EXIT* `{_md_code(trade.symbol)}` "
            f"\\({_md_escape(trade.reason)}\\)\n"
            f"entry `{trade.entry_price:.4f}` → exit `{trade.exit_price:.4f}`\n"
            f"pnl: `{trade.pnl:+.2f}` \\( `{trade.pnl_pct * 100:+.2f}%` \\)"
        )
        self.send(msg)

    def notify_error(self, label: str, error: str) -> None:
        # `pre` block: only \ and ` need escaping, regardless of what the
        # exchange / runtime threw at us.
        truncated = _md_code(error[:500])
        self.send(
            f"⚠️ *ERROR* {_md_escape(label)}\n"
            f"```\n{truncated}\n```"
        )

    def daily_summary(
        self,
        ts: datetime,
        equity: float,
        n_trades: int,
        day_pnl: float,
        n_positions: int,
    ) -> None:
        msg = (
            f"📊 *Daily summary* {_md_escape(ts.strftime('%Y-%m-%d'))}\n"
            f"equity: `{equity:.2f}`\n"
            f"trades today: `{n_trades}`\n"
            f"day pnl: `{day_pnl:+.2f}`\n"
            f"open positions: `{n_positions}`"
        )
        self.send(msg)
