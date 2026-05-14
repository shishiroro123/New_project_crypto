# solana_watcher

Read-only Solana smart-money watcher: polls Helius for tracked wallets, parses
their swap transactions, and sends Telegram alerts when they buy a non-quote
SPL token. Never signs or sends any transaction.

## Why this exists

Phase 1 of the "copy smart money" plan: **observe first**. Before risking
capital on an auto-trader, accumulate 2–3 weeks of data and verify that the
wallets we picked are actually profitable to follow. Cheaper to learn the hard
truths from a SQLite dump than from a blown-up wallet.

## Setup

### Required env vars

```env
HELIUS_API_KEY=...        # from https://www.helius.dev (free tier OK)
TELEGRAM_BOT_TOKEN=...    # from @BotFather
TELEGRAM_CHAT_ID=...      # from @userinfobot
```

### Curate the wallet list

Edit `wallets.yaml`. See its inline comments for tips on where to find good
candidates (Cielo, Birdeye, Lookonchain). Start with 5–20.

### Run

```bash
docker compose up -d solana_watcher
docker compose logs -f solana_watcher
```

You should immediately receive a Telegram message listing the wallets being
tracked. From then on, every detected buy fires an alert.

## What gets stored

`data/solana_watcher.sqlite`:

- `seen_signatures`: dedup table (one row per tx signature already processed)
- `detected_buys`: one row per detected buy with wallet, token mint, amounts,
  quote asset (SOL/USDC/USDT), and ISO timestamps

Phase 2 will analyse this table to compute, for each detected buy:

- price evolution at +1h / +24h / +7d
- which buys turned out to be winners vs losers
- which wallets are actually worth copying

## What it does NOT do (yet)

- No price/USD estimation on the input amount (TODO: fetch quote price)
- No on-chain enrichment (holder count, liquidity, dev wallet status — phase 2)
- No actual trading (phase 3, gated on the empirical edge being real)
