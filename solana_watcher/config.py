"""Watcher configuration. Loaded from env vars + wallets.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class WalletEntry:
    address: str
    name: str = ""
    notes: str = ""


@dataclass
class WatcherConfig:
    helius_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    wallets: list[WalletEntry] = field(default_factory=list)
    data_dir: Path = Path("/app/data")
    # Filter: only alert on buys where the wallet *received* a non-quote SPL
    # token AND spent SOL/USDC/USDT. This eliminates noise from intra-wallet
    # transfers, NFT sales, etc.
    alert_on_buys_only: bool = True
    # Mute alerts for buys < this USD amount (rough estimate via input mint
    # quote-price lookup). Set to 0 to keep all.
    min_buy_usd: float = 100.0

    @classmethod
    def load(cls, wallets_path: Path) -> "WatcherConfig":
        if not wallets_path.exists():
            raise FileNotFoundError(f"wallets file not found: {wallets_path}")
        with wallets_path.open() as f:
            raw = yaml.safe_load(f) or {}
        wallets = [WalletEntry(**w) for w in raw.get("wallets", [])]
        return cls(
            helius_api_key=_required_env("HELIUS_API_KEY"),
            # Telegram is optional. If not set, alerts go to stdout (visible in
            # `docker compose logs`). Set both to enable Telegram delivery.
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            wallets=wallets,
            data_dir=Path(os.environ.get("DATA_DIR", "/app/data")),
            min_buy_usd=float(os.environ.get("MIN_BUY_USD", "100")),
        )


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing required env var: {name}")
    return v
