"""Typed configuration loaded from YAML + environment variables."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class RegimeConfig(BaseModel):
    enabled: bool = True
    reference_symbol: str = "BTC/USDT"
    reference_timeframe: str = "1d"
    ma_period: int = 200


class StrategyConfig(BaseModel):
    name: str = "donchian_breakout"
    timeframe: str = "4h"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    entry_lookback: int = 20
    exit_lookback: int = 10
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    regime: RegimeConfig = Field(default_factory=RegimeConfig)

    @field_validator("entry_lookback", "exit_lookback", "atr_period")
    @classmethod
    def positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("lookback periods must be > 0")
        return v


class RiskConfig(BaseModel):
    initial_capital: float = 500.0
    risk_per_trade: float = 0.01
    max_concurrent_positions: int = 2
    max_position_pct: float = 0.5
    max_drawdown_kill: float = 0.25


class ExecutionConfig(BaseModel):
    mode: ExecutionMode = ExecutionMode.PAPER
    fee_rate: float = 0.001
    slippage_bps: float = 5.0


class BacktestConfig(BaseModel):
    start: str = "2020-01-01"
    end: str | None = None
    warmup_bars: int = 250


class BotConfig(BaseModel):
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> BotConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls.model_validate(raw)


class Secrets(BaseSettings):
    """Loaded from environment / .env. Never logged or committed."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    exchange_name: str = "binance"  # binance | binanceus | kraken
    # Path to a CA bundle used to verify TLS for the exchange API. Useful in
    # environments that perform TLS interception (corporate proxies, sandboxes).
    # When set, this overrides certifi's default bundle on the ccxt session.
    ca_bundle: str = ""

    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    kraken_api_key: str = ""
    kraken_api_secret: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    log_level: str = "INFO"
    data_dir: str = "./data"
