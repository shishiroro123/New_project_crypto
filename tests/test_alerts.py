"""Tests for the Telegram alerter — focus on Markdown escaping correctness.

Telegram's MarkdownV2 mode silently rejects messages with unescaped reserved
chars (returns HTTP 400). These tests verify that user-supplied / dynamic
content (symbols with `/`, error messages with `_`, `*`, `[`...) gets escaped.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crypto_bot.alerts import TelegramAlerter, _md_code, _md_escape
from crypto_bot.state import ClosedTrade


def test_md_escape_handles_all_reserved_chars():
    payload = "_*[]()~`>#+-=|{}.!"
    escaped = _md_escape(payload)
    # Every reserved char must be preceded by a backslash.
    for ch in payload:
        assert f"\\{ch}" in escaped


def test_md_escape_passes_safe_chars_through():
    assert _md_escape("BTC/USDT 12.5 abc") == "BTC/USDT 12\\.5 abc"


def test_md_code_escapes_only_backtick_and_backslash():
    raw = "msg with `backtick` and \\ backslash plus _ * # ! safe"
    out = _md_code(raw)
    # _ * # ! must NOT be escaped inside a code span.
    assert "\\_" not in out
    assert "\\*" not in out
    assert "\\#" not in out
    assert "\\!" not in out
    # ` and \ must be escaped.
    assert out.count("\\`") == 2  # opening + closing backticks in raw
    assert "\\\\ backslash" in out


def test_alerter_disabled_when_no_credentials():
    al = TelegramAlerter()
    assert al.enabled is False
    # send / notify_* are no-ops, must not raise.
    al.send("hi")
    al.notify_error("label", "boom")
    al.daily_summary(datetime(2024, 1, 1, tzinfo=UTC), 500.0, 3, 12.34, 1)


def test_notify_entry_escapes_symbol_with_slash():
    al = TelegramAlerter()
    sent: list[str] = []
    al._post = lambda text: sent.append(text)  # type: ignore[method-assign]
    al.enabled = True
    al.notify_entry("BTC/USDT", qty=0.01, price=50_000, stop=49_000)
    msg = sent[0]
    # Symbol slash is inside backticks → no escape needed; verify it's there raw.
    assert "`BTC/USDT`" in msg
    # The risk pct contains '%' which is not reserved in V2 outside code span.
    # The escape of the parens IS needed; verify they're escaped.
    assert "\\(risk:" in msg


def test_notify_error_escapes_label_outside_backticks():
    al = TelegramAlerter()
    sent: list[str] = []
    al._post = lambda text: sent.append(text)  # type: ignore[method-assign]
    al.enabled = True
    al.notify_error("kill_switch", "DD -25.5% breached")
    msg = sent[0]
    # Underscore in the label MUST be escaped (it's outside any code span).
    assert "kill\\_switch" in msg
    # Hyphen in the error body inside ``` must be escaped (V2 reserved).
    # We escape only ` and \ inside code blocks per spec, so '-' is allowed.
    assert "DD -25.5% breached" in msg


def test_notify_exit_with_special_chars_in_reason():
    al = TelegramAlerter()
    sent: list[str] = []
    al._post = lambda text: sent.append(text)  # type: ignore[method-assign]
    al.enabled = True
    trade = ClosedTrade(
        symbol="ETH/USDT",
        entry_time=datetime(2024, 1, 1, tzinfo=UTC),
        exit_time=datetime(2024, 1, 2, tzinfo=UTC),
        entry_price=2000.0,
        exit_price=1900.0,
        qty=0.5,
        pnl=-50.0,
        pnl_pct=-0.05,
        fees=2.0,
        reason="stop_intracycle",
    )
    al.notify_exit(trade)
    # Reason contains '_' which is escaped because it's inside \( ... \) outside code spans.
    assert "stop\\_intracycle" in sent[0]
