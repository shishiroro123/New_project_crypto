"""Lightweight Telegram notifier for the watcher.

No-op if any of the credentials are missing — useful for local dry runs.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class TelegramNotifier:
    BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = httpx.Client(timeout=15.0)

    def send(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            log.debug("telegram disabled (no token/chat_id)")
            return False
        url = f"{self.BASE}/bot{self.bot_token}/sendMessage"
        try:
            r = self._client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as e:
            log.warning("telegram request failed: %s", e)
            return False
        if r.status_code != 200:
            log.warning("telegram error %s: %s", r.status_code, r.text[:200])
            return False
        return True

    def close(self) -> None:
        self._client.close()


# MarkdownV2 needs every reserved char escaped, even inside code blocks for the
# closing backtick. The full set per Telegram docs is below.
_MD_V2_RESERVED = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(text: str) -> str:
    out = []
    for ch in str(text):
        if ch in _MD_V2_RESERVED:
            out.append("\\")
        out.append(ch)
    return "".join(out)
