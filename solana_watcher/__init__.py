"""Solana smart-money watcher.

Polls Helius enhanced API for tracked wallets, detects new SPL token buys,
and sends Telegram alerts. Read-only: never signs or sends transactions.
"""
