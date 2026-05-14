"""Streamlit dashboard for the crypto-bot.

Launch:
    crypto-bot dashboard
or directly:
    streamlit run dashboard/app.py -- --state data/state.sqlite

The dashboard is read-only. It never places orders or mutates the state DB.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Allow `streamlit run dashboard/app.py` from project root without install.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from crypto_bot.state import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# CLI arg parsing (Streamlit passes everything after `--`).
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/state.sqlite")
    return parser.parse_args(sys.argv[1:] if "--" not in sys.argv else
                             sys.argv[sys.argv.index("--") + 1 :])


ARGS = _parse_args()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="crypto-bot dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Sidebar: data source + refresh controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("crypto-bot")
    st.caption("Paper / testnet / live monitoring")

    state_path = st.text_input("State DB path", ARGS.state)
    refresh_every = st.slider("Auto-refresh (s)", 0, 120, 30, step=5,
                              help="0 = manual refresh only")
    n_trades = st.number_input("Trades shown", min_value=10, max_value=500, value=50, step=10)

    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(
        "Read-only view. The bot runs in a separate process; "
        "this dashboard only reads `state.sqlite`."
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=10)
def load_data(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"missing": True, "path": str(p)}
    store = StateStore(p)
    positions = store.all_positions()
    trades = store.recent_trades(1000)
    latest = store.latest_equity()
    heartbeat = store.get_meta("last_heartbeat")
    started_at = store.get_meta("started_at")
    halted_at = store.get_meta("halted_at")
    halt_reason = store.halt_reason()

    # Equity history through the StateStore (no raw SQL duplication).
    history = store.equity_history()
    if history:
        eq_df = pd.DataFrame(history, columns=["ts", "equity"])
        eq_df["ts"] = pd.to_datetime(eq_df["ts"], utc=True)
    else:
        eq_df = pd.DataFrame(columns=["ts", "equity"])

    return {
        "missing": False,
        "positions": positions,
        "trades": trades,
        "latest_equity": latest,
        "equity_curve": eq_df,
        "heartbeat": heartbeat,
        "started_at": started_at,
        "halted_at": halted_at,
        "halt_reason": halt_reason,
    }


data = load_data(state_path)

if data.get("missing"):
    st.warning(
        f"State DB not found at `{data['path']}`. "
        "Run `crypto-bot run --once` (or `crypto-bot seed-demo`) to create it."
    )
    st.stop()

trades = data["trades"]
positions = data["positions"]
eq_df = data["equity_curve"]
heartbeat = data["heartbeat"]
halted_at = data.get("halted_at")
halt_reason = data.get("halt_reason")

# A halted bot is critical info — surface it at the very top, before any KPI.
if halted_at:
    st.error(
        f"🛑 **BOT HALTED** since `{halted_at}`. Reason: `{halt_reason or 'unknown'}`. "
        "Run `crypto-bot unhalt` after investigating to resume."
    )


# ---------------------------------------------------------------------------
# Header KPIs
# ---------------------------------------------------------------------------

trades_df = pd.DataFrame(
    [
        {
            "symbol": t.symbol,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "qty": t.qty,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
            "fees": t.fees,
            "reason": t.reason,
        }
        for t in trades
    ]
)

current_equity = data["latest_equity"][1] if data["latest_equity"] else None
initial_equity = float(eq_df["equity"].iloc[0]) if not eq_df.empty else None
total_return = (
    (current_equity / initial_equity - 1) if (current_equity and initial_equity) else None
)

if not trades_df.empty:
    today_utc = datetime.now(UTC).date()
    todays_trades = trades_df[trades_df["exit_time"].dt.date == today_utc]
    day_pnl = float(todays_trades["pnl"].sum()) if not todays_trades.empty else 0.0
    total_pnl = float(trades_df["pnl"].sum())
else:
    day_pnl = 0.0
    total_pnl = 0.0

st.title("📈 crypto-bot")
hb_text = "—"
hb_color = "off"
if heartbeat:
    hb_ts = datetime.fromisoformat(heartbeat)
    age = (datetime.now(UTC) - hb_ts).total_seconds()
    if age < 600:
        hb_color = "normal"
    elif age < 3600:
        hb_color = "off"
    else:
        hb_color = "inverse"
    hb_text = f"{int(age // 60)} min ago" if age >= 60 else f"{int(age)} s ago"

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric(
        "Equity",
        f"{current_equity:,.2f} USDT" if current_equity else "—",
        delta=f"{total_return * 100:+.2f}%" if total_return is not None else None,
    )
with c2:
    st.metric("Today's P&L", f"{day_pnl:+,.2f}", delta=f"{day_pnl:+.2f} USDT")
with c3:
    st.metric("Realized P&L (all)", f"{total_pnl:+,.2f}")
with c4:
    st.metric("Open positions", len(positions))
with c5:
    st.metric("Last heartbeat", hb_text, delta_color=hb_color)


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

st.subheader("Equity curve")
if eq_df.empty:
    st.info("No equity snapshots yet. Once the bot starts running, this chart will populate.")
else:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=eq_df["ts"],
            y=eq_df["equity"],
            mode="lines",
            name="Equity",
            line=dict(color="#22c55e", width=2),
        )
    )
    # Drawdown shading
    running_max = eq_df["equity"].cummax()
    fig.add_trace(
        go.Scatter(
            x=eq_df["ts"],
            y=running_max,
            mode="lines",
            name="Peak",
            line=dict(color="#94a3b8", width=1, dash="dot"),
        )
    )
    fig.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=20, b=10),
        hovermode="x unified",
        yaxis_title="USDT",
        showlegend=True,
        template="plotly_dark",
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

st.subheader(f"Open positions ({len(positions)})")
if not positions:
    st.caption("No open positions.")
else:
    pos_rows = []
    for p in positions:
        notional = p.qty * p.entry_price
        risk_pct = (p.entry_price - p.stop_price) / p.entry_price * 100
        pos_rows.append(
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "entry": p.entry_price,
                "stop": p.stop_price,
                "risk %": risk_pct,
                "notional": notional,
                "entry time": p.entry_time,
            }
        )
    pdf = pd.DataFrame(pos_rows)
    st.dataframe(
        pdf,
        use_container_width=True,
        hide_index=True,
        column_config={
            "entry": st.column_config.NumberColumn("entry", format="%.4f"),
            "stop": st.column_config.NumberColumn("stop", format="%.4f"),
            "risk %": st.column_config.NumberColumn("risk %", format="%.2f%%"),
            "notional": st.column_config.NumberColumn("notional", format="%.2f"),
            "qty": st.column_config.NumberColumn("qty", format="%.6f"),
        },
    )


# ---------------------------------------------------------------------------
# Trade stats
# ---------------------------------------------------------------------------

st.subheader("Trade statistics")
if trades_df.empty:
    st.caption("No closed trades yet.")
else:
    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] <= 0]
    win_rate = len(wins) / len(trades_df) if len(trades_df) else 0.0
    avg_win = wins["pnl"].mean() if not wins.empty else 0.0
    avg_loss = losses["pnl"].mean() if not losses.empty else 0.0
    profit_factor = (
        abs(wins["pnl"].sum() / losses["pnl"].sum())
        if not losses.empty and losses["pnl"].sum() != 0
        else float("inf") if not wins.empty else 0.0
    )
    expectancy = trades_df["pnl"].mean()

    if not eq_df.empty:
        running_max = eq_df["equity"].cummax()
        dd = (eq_df["equity"] / running_max - 1).min()
    else:
        dd = 0.0

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Win rate", f"{win_rate * 100:.1f}%", f"{len(wins)}W / {len(losses)}L")
    s2.metric("Avg win", f"{avg_win:+.2f}")
    s3.metric("Avg loss", f"{avg_loss:+.2f}")
    s4.metric("Profit factor", f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞")
    s5.metric("Max drawdown", f"{dd * 100:.2f}%")

    # PnL distribution
    st.markdown("**P&L distribution**")
    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=trades_df["pnl"],
            nbinsx=30,
            marker_color="#3b82f6",
            name="Trades",
        )
    )
    fig.update_layout(
        height=240,
        margin=dict(l=10, r=10, t=10, b=10),
        template="plotly_dark",
        xaxis_title="P&L (USDT)",
        yaxis_title="count",
        bargap=0.05,
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Trades table
# ---------------------------------------------------------------------------

st.subheader(f"Recent trades")
if trades_df.empty:
    st.caption("No trades yet.")
else:
    show_df = trades_df.head(int(n_trades)).copy()
    show_df["pnl_pct"] = show_df["pnl_pct"] * 100

    def color_pnl(val: float) -> str:
        if val > 0:
            return "color: #22c55e; font-weight: 600"
        if val < 0:
            return "color: #ef4444; font-weight: 600"
        return ""

    styled = show_df.style.map(color_pnl, subset=["pnl", "pnl_pct"])
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "entry_price": st.column_config.NumberColumn("entry", format="%.4f"),
            "exit_price": st.column_config.NumberColumn("exit", format="%.4f"),
            "qty": st.column_config.NumberColumn("qty", format="%.6f"),
            "pnl": st.column_config.NumberColumn("pnl", format="%+.2f"),
            "pnl_pct": st.column_config.NumberColumn("pnl %", format="%+.2f%%"),
            "fees": st.column_config.NumberColumn("fees", format="%.4f"),
        },
    )


# ---------------------------------------------------------------------------
# Footer / auto-refresh
# ---------------------------------------------------------------------------

st.caption(f"Loaded at {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC · state: `{state_path}`")

if refresh_every > 0:
    import time
    time.sleep(refresh_every)
    st.rerun()
