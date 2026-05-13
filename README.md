# crypto-bot

Bot de trading crypto basé sur une stratégie **Donchian breakout** (time-series momentum) avec filtre de régime et sizing par ATR. Conçu pour BTC/USDT et ETH/USDT sur timeframe 4h, déployable sur un VPS via Docker.

> **Avertissement** — Ce projet est un MVP éducatif. Le trading algorithmique comporte un risque réel de perte en capital. À 500 € de capital, l'objectif est de **valider le système** (signaux, exécution, monitoring), pas de générer du rendement net positif les premiers mois. Les frais (~0.2% aller-retour Binance) compriment fortement la performance à petite échelle.

## Pourquoi cette stratégie ?

Time-series momentum est l'anomalie la mieux documentée sur crypto (Liu & Tsyvinski 2021, Han-Kang-Ryu 2023, Zarattini-Pagani-Barbon 2025). Sharpe brut typique 1.0–1.5, ~0.4–0.8 net de frais à notre échelle. Avantages pour un MVP :
- **1–2 paramètres** seulement → faible risque d'overfit
- **Peu de trades** (~10–30/an/actif sur 4h) → frais maîtrisables
- **Logique simple** → backtestable, débuggable

Stratégies écartées (et pourquoi) : voir le rapport d'analyse dans l'historique git.

## Architecture

```
src/crypto_bot/
├── config.py          # Pydantic config (YAML + .env)
├── logging_setup.py   # structlog (JSON ou console pretty)
├── state.py           # SQLite store (positions, trades, equity, meta)
├── alerts.py          # TelegramAlerter (no-op si tokens absents)
├── live_runner.py     # boucle de polling pour paper/testnet/live
├── walkforward.py     # walk-forward analysis (anchored, OOS)
├── data/
│   ├── exchange.py      # ccxt wrapper + retry exponentiel
│   └── market_data.py   # fetcher OHLCV + cache parquet incrémental
├── strategy/
│   ├── indicators.py    # Donchian, ATR (Wilder), SMA, EMA
│   └── donchian.py      # signaux Donchian breakout + filtre régime
├── risk/
│   └── sizing.py        # sizing par risque fixe ATR
├── backtest/
│   └── runner.py        # backtester vectorisé + métriques (Sharpe, DD, CAGR)
├── execution/
│   ├── base.py          # interface ExecutorBase (paper / live)
│   ├── paper.py         # simulateur d'ordres (cohérent avec le backtester)
│   └── live.py          # exécution réelle Binance via ccxt
├── news/
│   └── feeds.py         # CryptoPanic + Binance announcements
└── cli.py             # typer CLI : fetch / backtest / walkforward / run / news / status
```

## Règles de trading

**Entrée long** au bar `t` (exécutée à l'open de `t+1`) si :
- `high[t]` casse le plus haut sur les 20 bars précédentes (Donchian-high 20)
- ET filtre régime actif : `BTC > MA200 daily`
- ET ATR disponible (warmup terminé)

**Sortie long** au bar `t` (exécutée à l'open de `t+1`) si :
- `low[t]` casse le plus bas sur les 10 bars précédentes (Donchian-low 10), OU
- `low[t]` touche le stop fixé à `entry - 2 × ATR(14)`

**Sizing** : quantité = `(equity × 1%) / (entry − stop)`, plafonnée à 50% de l'equity en notionnel.

**Kill switch** : drawdown > 25% → flatten et halt (configurable).

## Quickstart local

```bash
# 1. venv + install
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. variables d'env (Binance testnet par défaut)
cp .env.example .env
# éditer .env si besoin (paper trading ne requiert PAS de clés)

# 3. télécharger les données historiques (5+ ans BTC/ETH 4h + BTC daily)
crypto-bot fetch --start 2020-01-01

# 4. backtester (single-symbol par défaut → tous les symboles configurés)
crypto-bot backtest

# 5. walk-forward analysis (anti-overfit) sur BTC/USDT, 5 folds
crypto-bot walkforward --symbol BTC/USDT --folds 5

# 6. lancer le bot en paper trading (mode défini dans config/default.yaml)
crypto-bot run --poll 300         # cycle toutes les 5 min
crypto-bot run --once             # un seul cycle (utile pour debug)

# 7. consulter l'état (positions ouvertes, trades, equity)
crypto-bot status

# 8. tester les flux news
crypto-bot news --currencies BTC,ETH

# 9. tests unitaires (22 tests)
pytest -q
```

## Déploiement VPS

Sur un VPS Linux (Hetzner CX11 / OVH VLE-2 / ~5 €/mois) :

```bash
# Pré-requis : Docker + docker compose plugin
curl -fsSL https://get.docker.com | sh

# Cloner et configurer
git clone <repo-url> crypto-bot && cd crypto-bot
cp .env.example .env
# Éditer .env (clés API Binance testnet, Telegram, etc.)

# Build + fetch historique
docker compose build
docker compose run --rm bot fetch --start 2020-01-01

# Backtest
docker compose run --rm bot backtest
```

### Sécurité des clés API Binance

Quand tu créeras des clés (testnet d'abord) :
- Active uniquement **Read** + **Spot Trading**
- **DÉSACTIVE** explicitement les retraits (`Enable Withdrawals = OFF`)
- Restreins par IP au VPS (whitelist)
- Stocke les clés UNIQUEMENT dans `.env` (jamais commit, gitignored par défaut)

## Configuration

Tout est centralisé dans `config/default.yaml`. Paramètres clés :

| Section | Clé | Défaut | Effet |
|---|---|---|---|
| strategy | `entry_lookback` | 20 | Donchian-high sur N bars (sensibilité aux breakouts) |
| strategy | `exit_lookback` | 10 | Donchian-low sur M bars (vitesse de sortie) |
| strategy | `atr_stop_multiplier` | 2.0 | Largeur du stop en multiples d'ATR |
| risk | `risk_per_trade` | 0.01 | Fraction de l'equity risquée par trade |
| risk | `max_position_pct` | 0.5 | Notionnel max d'une position (anti-leverage implicite) |
| risk | `max_drawdown_kill` | 0.25 | DD au-dessus duquel le bot s'arrête |
| execution | `mode` | paper | `paper` / `testnet` / `live` |
| execution | `fee_rate` | 0.001 | Frais Binance taker spot |
| execution | `slippage_bps` | 5 | Slippage assumé sur market orders |

## Roadmap

État actuel (v1.0) :
- [x] Fetcher OHLCV avec cache parquet incrémental
- [x] Indicateurs (Donchian, ATR, MA200)
- [x] Générateur de signaux Donchian + filtre régime BTC>MA200
- [x] Sizing par risque fixe ATR + kill switch drawdown
- [x] Backtester avec frais, slippage, métriques (Sharpe, DD, CAGR)
- [x] Walk-forward analysis (anchored OOS, grille de params minimale)
- [x] Persistance SQLite (positions, trades, equity snapshots, meta)
- [x] Exécution Paper + Live Binance via ccxt (testnet ou réel)
- [x] Live runner avec polling, kill switch, signal handling SIGTERM/SIGINT
- [x] Alertes Telegram (entry/exit/error/daily summary)
- [x] Module news (CryptoPanic + Binance announcements)
- [x] CLI complète : fetch / backtest / walkforward / run / news / status
- [x] Docker + 22 tests unitaires + tests d'intégration end-to-end

À venir (v1.1 et plus) :
- [ ] Multi-symbol portfolio runner (allocation dynamique entre signaux concurrents)
- [ ] Sentiment scoring NLP sur les news → modulation du sizing
- [ ] Dashboard Streamlit / Prometheus exporter pour Grafana
- [ ] WebSocket pour timeframes < 15min
- [ ] Optimisation bayésienne des params (Optuna) en remplacement de la grille
- [ ] Monitoring on-chain (whale alerts, flux exchange)

## Plan de validation avant passage en live

1. **Backtest in-sample** 2020–2023 sur BTC + ETH (`crypto-bot backtest`).
2. **Walk-forward OOS** 2024–aujourd'hui sans réoptimisation (`crypto-bot walkforward`). Si OOS Sharpe << IS Sharpe → overfit.
3. **Sensibilité paramètres** : Donchian 15–30, ATR 10–20. Si l'edge disparaît, c'est de l'overfit.
4. **Paper trading** 4–8 semaines (`crypto-bot run` avec `execution.mode: paper`).
5. **Testnet** 2–4 semaines (`execution.mode: testnet` + clés API testnet Binance).
6. **Go-live progressif** : 50–100 € pendant 4 semaines avant le reste du capital.

## Modes d'exécution

Trois modes pilotés par `config/default.yaml` → `execution.mode` :

| Mode | API keys requises | Données live | Ordres réels | Usage |
|---|---|---|---|---|
| `paper` | non | oui (publiques) | non (simulés) | dev & validation |
| `testnet` | testnet Binance | oui | fake money | dernière étape avant le réel |
| `live` | clés réelles | oui | oui | production |

Le live runner persiste tout dans `data/state.sqlite` : positions, trades, equity. À chaque redémarrage il reprend l'état précédent (idempotent). `crypto-bot status` affiche un instantané.

## Licence

MIT.
