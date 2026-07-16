"""
Streamlit Dashboard — ORB Bot monitor + personal portfolio viewer.
Accessible at http://<vps-ip>:8501
"""
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from ib_insync import IB

import sheets_alerts

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Dark card */
.card {
    background: #1e2130;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 16px;
}
.card-title {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #8b95a5;
    margin-bottom: 4px;
}
.card-value {
    font-size: 28px;
    font-weight: 700;
    color: #ffffff;
}
.card-sub {
    font-size: 13px;
    color: #8b95a5;
    margin-top: 2px;
}
.green  { color: #00d084; }
.red    { color: #ff4d4d; }
.badge-live  { background:#00d084; color:#000; padding:2px 10px; border-radius:20px; font-size:11px; font-weight:700; }
.badge-paper { background:#f0a500; color:#000; padding:2px 10px; border-radius:20px; font-size:11px; font-weight:700; }
.section-header {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
    margin: 24px 0 12px 0;
    display: flex;
    align-items: center;
    gap: 10px;
}
div[data-testid="stMetricValue"] { font-size: 24px !important; }
div[data-testid="stTab"] button { font-size: 14px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)


# ── Data loaders ──────────────────────────────────────────────────────────────

# IBKR symbol -> yfinance symbol for non-US listings (yfinance needs the suffix)
YF_OVERRIDE = {"CSPX": "CSPX.L"}


def _price_table(symbols: list) -> dict:
    """Batch price lookup -> {symbol: (price, day_pct)}."""
    yf_syms = [YF_OVERRIDE.get(s, s) for s in symbols]
    try:
        raw = yf.download(yf_syms, period="2d", interval="1d", auto_adjust=True, progress=False)
        closes = raw["Close"]
    except Exception:
        closes = pd.DataFrame()
    out = {}
    for s in symbols:
        ys = YF_OVERRIDE.get(s, s)
        try:
            col = closes[ys] if ys in getattr(closes, "columns", []) else closes.get(s)
            vals = col.dropna().values if col is not None else []
            price = float(vals[-1]) if len(vals) >= 1 else 0.0
            prev = float(vals[-2]) if len(vals) >= 2 else price
            pct = ((price - prev) / prev * 100) if prev else 0.0
        except Exception:
            price, pct = 0.0, 0.0
        out[s] = (price, pct)
    return out


def _holding_row(sym, qty, avg_cost, price, pct) -> dict:
    return {
        "Ticker": sym, "Qty": int(qty), "Avg Cost": round(avg_cost, 2),
        "Price": round(price, 2), "Day %": round(pct, 2),
        "Total": round(qty * price, 2),
        "Profit $": round((price - avg_cost) * qty, 2),
        "Profit %": round(((price - avg_cost) / avg_cost * 100) if avg_cost else 0, 2),
    }


def _live_from_yaml() -> pd.DataFrame:
    """Fallback: build the live table from the last snapshot in my_portfolio.yaml."""
    import yaml
    pf = Path("my_portfolio.yaml")
    if not pf.exists():
        return pd.DataFrame({"_error": ["my_portfolio.yaml not found"]})
    data = yaml.safe_load(pf.read_text()) or {}
    holdings = data.get("holdings", [])
    df = pd.DataFrame()
    if holdings:
        prices = _price_table([h["symbol"] for h in holdings])
        rows = [_holding_row(h["symbol"], h["shares"], h["avg_cost"], *prices[h["symbol"]])
                for h in holdings]
        df = pd.DataFrame(rows).sort_values("Day %", ascending=False).reset_index(drop=True)
    df.attrs["cash"] = float(data.get("cash", 0))
    df.attrs["initial"] = float(data.get("initial_investment", 0))
    df.attrs["source"] = "snapshot"
    return df


def _write_yaml_snapshot(rows: list, cash: float, initial: float) -> None:
    """Persist a fresh snapshot so the offline fallback stays current."""
    import yaml
    pf = Path("my_portfolio.yaml")
    yf_map = {}
    if pf.exists():
        try:
            for h in (yaml.safe_load(pf.read_text()) or {}).get("holdings", []):
                if h.get("yf_ticker"):
                    yf_map[h["symbol"]] = h["yf_ticker"]
        except Exception:
            pass
    out = {"cash": round(cash, 2), "initial_investment": initial, "holdings": []}
    for r in rows:
        row = {"symbol": r["Ticker"], "shares": int(r["Qty"]), "avg_cost": r["Avg Cost"]}
        if r["Ticker"] in yf_map:
            row["yf_ticker"] = yf_map[r["Ticker"]]
        out["holdings"].append(row)
    try:
        pf.write_text("# Snapshot cached from the last successful live read (Refresh Live).\n"
                      + yaml.safe_dump(out, sort_keys=False))
    except Exception:
        pass


@st.cache_data(ttl=60)
def load_live_portfolio() -> pd.DataFrame:
    """Read real holdings live from the IBKR gateway; fall back to the last
    snapshot (my_portfolio.yaml) if the gateway is logged out/offline."""
    import yaml
    pf = Path("my_portfolio.yaml")
    initial = 0.0
    if pf.exists():
        try:
            initial = float((yaml.safe_load(pf.read_text()) or {}).get("initial_investment", 0))
        except Exception:
            initial = 0.0

    ib = IB()
    try:
        ib.connect("ibgateway-live", 4003, clientId=13, timeout=8, readonly=True)
    except Exception:
        return _live_from_yaml()          # gateway offline -> last snapshot

    try:
        positions = [p for p in ib.positions() if p.contract.secType == "STK" and p.position != 0]
        cash = 0.0
        for v in ib.accountValues():
            if v.tag == "TotalCashValue" and v.currency == "USD":
                cash = float(v.value); break
    except Exception:
        return _live_from_yaml()
    finally:
        if ib.isConnected():
            ib.disconnect()

    if not positions:
        df = pd.DataFrame()
        df.attrs.update(cash=cash, initial=initial, source="live")
        return df

    prices = _price_table([p.contract.symbol for p in positions])
    rows = [_holding_row(p.contract.symbol, p.position, p.avgCost, *prices[p.contract.symbol])
            for p in positions]
    _write_yaml_snapshot(rows, cash, initial)   # keep the offline fallback fresh
    df = pd.DataFrame(rows).sort_values("Day %", ascending=False).reset_index(drop=True)
    df.attrs.update(cash=cash, initial=initial, source="live")
    return df


@st.cache_data(ttl=60)
def load_paper_portfolio() -> pd.DataFrame:
    ib = IB()
    try:
        ib.connect("ibgateway", 4004, clientId=11, timeout=10, readonly=True)
        positions = ib.positions()
        if not positions:
            return pd.DataFrame()

        rows = []
        symbols = [p.contract.symbol for p in positions]

        try:
            raw    = yf.download(symbols, period="2d", interval="1d", auto_adjust=True, progress=False)
            closes = raw["Close"]
        except Exception:
            closes = pd.DataFrame()

        for pos in positions:
            sym      = pos.contract.symbol
            qty      = pos.position
            avg_cost = pos.avgCost
            try:
                col   = closes[sym] if sym in closes.columns else None
                vals  = col.dropna().values if col is not None else []
                price = float(vals[-1]) if len(vals) >= 1 else 0.0
                prev  = float(vals[-2]) if len(vals) >= 2 else price
                pct   = ((price - prev) / prev * 100) if prev else 0.0
            except Exception:
                price, pct = 0.0, 0.0

            rows.append({
                "Ticker":   sym,
                "Qty":      int(qty),
                "Avg Cost": round(avg_cost, 2),
                "Price":    round(price, 2),
                "Day %":    round(pct, 2),
                "Total":    round(qty * price, 2),
                "Profit $": round((price - avg_cost) * qty, 2),
                "Profit %": round(((price - avg_cost) / avg_cost * 100) if avg_cost else 0, 2),
            })
        return pd.DataFrame(rows)
    except Exception as exc:
        return pd.DataFrame({"_error": [str(exc)]})
    finally:
        if ib.isConnected():
            ib.disconnect()


def load_trades(days: int = 30) -> pd.DataFrame:
    db = Path("data/trades.db")
    if not db.exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(db) as conn:
            return pd.read_sql_query(
                "SELECT * FROM trades ORDER BY date DESC, id DESC LIMIT 200", conn
            )
    except Exception:
        return pd.DataFrame()


def load_universe() -> pd.DataFrame:
    p = Path("universe/universe.csv")
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def read_todays_log(tail: int = 100) -> list[str]:
    log_file = Path("logs") / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    if not log_file.exists():
        return ["No log file yet for today."]
    return log_file.read_text(encoding="utf-8").splitlines()[-tail:]


def load_config_raw() -> str:
    p = Path("config.yaml")
    return p.read_text(encoding="utf-8") if p.exists() else "config.yaml not found"


@st.cache_data(ttl=30)
def load_alert_rules() -> pd.DataFrame:
    return sheets_alerts.load_alert_rules()


# ── Sector ETF loaders ────────────────────────────────────────────────────────

SECTOR_ETFS = [
    {"ticker": "SPY",  "name": "S&P 500"},
    {"ticker": "QQQ",  "name": "Nasdaq 100"},
    {"ticker": "XLK",  "name": "Technology Select Sector"},
    {"ticker": "SOXX", "name": "Semiconductor ETF"},
    {"ticker": "SMH",  "name": "VanEck Semiconductor"},
    {"ticker": "IGV",  "name": "Software ETF"},
    {"ticker": "XLF",  "name": "Financials Select Sector"},
    {"ticker": "XLV",  "name": "Healthcare Select Sector"},
    {"ticker": "XBI",  "name": "Biotech ETF"},
    {"ticker": "XLE",  "name": "Energy Select Sector"},
    {"ticker": "ICLN", "name": "Clean Energy ETF"},
    {"ticker": "XLI",  "name": "Industrials Select Sector"},
    {"ticker": "PAVE", "name": "Infrastructure ETF"},
    {"ticker": "XLY",  "name": "Consumer Discretionary"},
    {"ticker": "XLP",  "name": "Consumer Staples"},
    {"ticker": "XLC",  "name": "Communication Services"},
    {"ticker": "XLRE", "name": "Real Estate Select Sector"},
    {"ticker": "ARKX", "name": "Space Exploration ETF"},
    {"ticker": "CIBR", "name": "Cybersecurity ETF"},
    {"ticker": "FINX", "name": "FinTech ETF"},
]


@st.cache_data(ttl=300)
def load_sector_etfs() -> pd.DataFrame:
    symbols = [e["ticker"] for e in SECTOR_ETFS]

    # One batch download — a month of daily bars covers today, yesterday and a week ago
    try:
        raw = yf.download(symbols, period="1mo", interval="1d", auto_adjust=True, progress=False)
        closes = raw["Close"]
    except Exception:
        closes = pd.DataFrame()

    rows = []
    for e in SECTOR_ETFS:
        sym = e["ticker"]
        try:
            col  = closes[sym] if sym in closes.columns else None
            vals = col.dropna().values if col is not None else []
        except Exception:
            vals = []

        # % today = last close vs previous close
        today = ((vals[-1] - vals[-2]) / vals[-2] * 100) if len(vals) >= 2 and vals[-2] else 0.0
        # % yesterday = previous close vs the one before it
        yday  = ((vals[-2] - vals[-3]) / vals[-3] * 100) if len(vals) >= 3 and vals[-3] else 0.0
        # % last 7 days (a trading week ≈ 5 sessions back)
        week  = ((vals[-1] - vals[-6]) / vals[-6] * 100) if len(vals) >= 6 and vals[-6] else 0.0

        rows.append({
            "Ticker":          sym,
            "Name":            e["name"],
            "% Change Today":  round(today, 2),
            "% Change Yesterday": round(yday, 2),
            "% Change 7d":     round(week, 2),
        })

    df = pd.DataFrame(rows).sort_values("% Change Today", ascending=False).reset_index(drop=True)
    df.insert(0, "#", range(1, len(df) + 1))
    return df


# ── Trading Log (ORB telemetry) loaders ───────────────────────────────────────

ORB_DB = Path("data/orb_log.db")


@st.cache_data(ttl=30)
def load_orb_dates() -> list[str]:
    if not ORB_DB.exists():
        return []
    with sqlite3.connect(ORB_DB) as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM orb_snapshots ORDER BY date DESC"
        ).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=30)
def load_orb_symbols(date: str) -> list[str]:
    if not ORB_DB.exists():
        return []
    with sqlite3.connect(ORB_DB) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM orb_snapshots WHERE date=? ORDER BY symbol",
            (date,),
        ).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=30)
def load_orb_levels(date: str, symbol: str) -> dict:
    if not ORB_DB.exists():
        return {}
    with sqlite3.connect(ORB_DB) as conn:
        row = conn.execute(
            "SELECT or_high, or_low, captured_at FROM orb_levels WHERE date=? AND symbol=?",
            (date, symbol),
        ).fetchone()
    if not row:
        return {}
    return {"or_high": row[0], "or_low": row[1], "captured_at": row[2]}


@st.cache_data(ttl=30)
def load_orb_snapshots(date: str, symbol: str) -> pd.DataFrame:
    if not ORB_DB.exists():
        return pd.DataFrame()
    with sqlite3.connect(ORB_DB) as conn:
        return pd.read_sql_query(
            "SELECT ts AS Time, price AS Price, position AS Position, "
            "retest AS Retest, or_high, or_low "
            "FROM orb_snapshots WHERE date=? AND symbol=? ORDER BY id",
            conn, params=(date, symbol),
        )


# ── Shared portfolio renderer ─────────────────────────────────────────────────

def render_portfolio(df: pd.DataFrame, badge_html: str) -> None:
    if "_error" in df.columns:
        st.error(f"Could not connect: {df['_error'].iloc[0]}")
        return
    if df.empty:
        st.info("No open positions.")
        return

    cash    = df.attrs.get("cash", 0)
    initial = df.attrs.get("initial", 0)

    stocks_val   = df["Total"].sum()
    total_val    = stocks_val + cash
    total_profit = (total_val - initial) if initial else df["Profit $"].sum()
    profit_pct   = (total_profit / initial * 100) if initial else 0

    # Summary cards
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        sub = f"${stocks_val:,.2f} stocks" + (f" + ${cash:,.2f} cash" if cash else "")
        st.markdown(f"""
        <div class="card">
            <div class="card-title">Portfolio Value</div>
            <div class="card-value">${total_val:,.2f}</div>
            <div class="card-sub">{sub}</div>
        </div>""", unsafe_allow_html=True)
    with c2:
        color = "green" if total_profit >= 0 else "red"
        initial_str = f" vs ${initial:,.0f} invested" if initial else ""
        st.markdown(f"""
        <div class="card">
            <div class="card-title">Total P&L</div>
            <div class="card-value {color}">${total_profit:+,.2f}</div>
            <div class="card-sub {color}">{profit_pct:+.2f}%{initial_str}</div>
        </div>""", unsafe_allow_html=True)
    with c3:
        winners = (df["Profit $"] > 0).sum()
        st.markdown(f"""
        <div class="card">
            <div class="card-title">Winners / Losers</div>
            <div class="card-value">{winners} / {len(df) - winners}</div>
            <div class="card-sub">open positions</div>
        </div>""", unsafe_allow_html=True)
    with c4:
        best = df.loc[df["Profit %"].idxmax()]
        st.markdown(f"""
        <div class="card">
            <div class="card-title">Best Performer</div>
            <div class="card-value green">{best['Ticker']}</div>
            <div class="card-sub green">{best['Profit %']:+.2f}%</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Color-coded table
    def style_row(val, col):
        if col in ("Day %", "Profit $", "Profit %"):
            return "color: #00d084" if val > 0 else "color: #ff4d4d" if val < 0 else ""
        return ""

    styled = df.style\
        .apply(lambda col: [style_row(v, col.name) for v in col], axis=0)\
        .format({
            "Avg Cost": "${:.2f}",
            "Price":    "${:.2f}",
            "Total":    "${:,.2f}",
            "Profit $": "${:+,.2f}",
            "Profit %": "{:+.2f}%",
            "Day %":    "{:+.2f}%",
        })
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Mini sparkline bar chart
    fig = go.Figure(go.Bar(
        x=df["Ticker"],
        y=df["Profit $"],
        marker_color=["#00d084" if v >= 0 else "#ff4d4d" for v in df["Profit $"]],
        text=[f"${v:+,.0f}" for v in df["Profit $"]],
        textposition="outside",
    ))
    fig.update_layout(
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="#ffffff", height=220,
        margin=dict(t=10, b=10, l=10, r=10),
        xaxis=dict(showgrid=False), yaxis=dict(showgrid=False, zeroline=True, zerolinecolor="#333"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Header ────────────────────────────────────────────────────────────────────

now = datetime.now()
st.markdown(f"""
<div style="
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    border-radius: 16px;
    padding: 24px 32px;
    margin-bottom: 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 4px 24px rgba(0,0,0,0.4);
">
    <div style="display:flex; align-items:center; gap:16px;">
        <div style="
            background: linear-gradient(135deg, #f7971e, #ffd200);
            border-radius: 50%;
            width: 56px; height: 56px;
            display: flex; align-items: center; justify-content: center;
            font-size: 28px;
            box-shadow: 0 0 20px rgba(255,210,0,0.4);
        ">💰</div>
        <div>
            <div style="
                font-size: 26px;
                font-weight: 900;
                background: linear-gradient(90deg, #ffd200, #f7971e, #ffffff);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: -0.5px;
                line-height: 1.1;
            ">Ori the Money Maker</div>
            <div style="color:#8b95a5; font-size:12px; margin-top:2px; letter-spacing:1px;">
                TRADING COMMAND CENTER
            </div>
        </div>
    </div>
    <div style="text-align:right;">
        <div style="color:#ffffff; font-size:15px; font-weight:600;">
            {now.strftime('%A, %B %d %Y')}
        </div>
        <div style="color:#ffd200; font-size:20px; font-weight:700; font-family:monospace;">
            {now.strftime('%H:%M:%S')}
        </div>
        <div style="color:#8b95a5; font-size:11px; margin-top:2px;">auto-refresh every 60s</div>
    </div>
</div>
""", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_home, tab_sectors, tab_alerts, tab_tradelog, tab_trades, tab_log, tab_config = st.tabs(
    ["📊 Stocks", "🏭 Sectors ETF", "🔔 Alerts", "📡 Trading Log", "🔁 Bot Trades", "📋 Live Log", "⚙️ Config"]
)

# ── Stocks tab (Live + Paper side by side) ───────────────────────────────────
with tab_home:

    # ---- LIVE ----
    st.markdown("""
    <div class="section-header">
        <span class="badge-live">LIVE</span> My Portfolio
    </div>""", unsafe_allow_html=True)

    if st.button("🔄 Refresh Live", key="ref_live"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Reading live positions from IBKR..."):
        live_df = load_live_portfolio()

    _src = live_df.attrs.get("source", "")
    if _src == "live":
        st.caption("🟢 Live from IBKR account U25029941")
    elif _src == "snapshot":
        st.caption("⚠️ Live gateway offline — showing last snapshot. "
                   "Reconnect the gateway and approve the 2FA, then press Refresh Live.")
    render_portfolio(live_df, "live")

    st.markdown("<br>", unsafe_allow_html=True)

    # ---- PAPER ----
    st.markdown("""
    <div class="section-header">
        <span class="badge-paper">PAPER</span> Bot Positions
    </div>""", unsafe_allow_html=True)

    if st.button("🔄 Refresh Paper", key="ref_paper"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Loading paper positions..."):
        paper_df = load_paper_portfolio()
    render_portfolio(paper_df, "paper")


# ── Sectors ETF tab ───────────────────────────────────────────────────────────
with tab_sectors:
    st.markdown("""
    <div class="section-header">
        🏭 Sector Trend Monitor
    </div>""", unsafe_allow_html=True)
    st.caption(
        "Sector & thematic ETFs sorted by today's move. "
        "Top 3 gainers are highlighted green, bottom 3 red."
    )

    if st.button("🔄 Refresh Sectors", key="ref_sectors"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("Loading sector ETFs..."):
        sectors_df = load_sector_etfs()

    if sectors_df.empty:
        st.info("No sector data available.")
    else:
        n = len(sectors_df)
        pct_cols = ["% Change Today", "% Change Yesterday", "% Change 7d"]

        # Highlight the 3 best / 3 worst rows by today's move
        def highlight_rows(row):
            i = row.name
            if i < 3:
                bg = "background-color: rgba(0, 208, 132, 0.18)"
            elif i >= n - 3:
                bg = "background-color: rgba(255, 77, 77, 0.18)"
            else:
                bg = ""
            return [bg] * len(row)

        # Per-cell text colour for the percentage columns
        def color_pct(val):
            return "color: #00d084" if val > 0 else "color: #ff4d4d" if val < 0 else ""

        styled = sectors_df.style\
            .apply(highlight_rows, axis=1)\
            .map(color_pct, subset=pct_cols)\
            .format({c: "{:+.2f}%" for c in pct_cols})

        st.dataframe(styled, use_container_width=True, hide_index=True)


# ── Alerts tab (stocks_alert Google Sheet) ────────────────────────────────────
with tab_alerts:
    st.markdown("""
    <div class="section-header">
        🔔 Stock Alert Rules
    </div>""", unsafe_allow_html=True)
    st.caption(
        "These rules live in the Google Sheet read by the stocks_alert bot. "
        "The bot picks up changes on startup and every morning at 09:00 ET."
    )

    if not sheets_alerts.is_configured():
        st.warning(
            "Google Sheets is not configured. Set `SPREADSHEET_ID` in `.env` and "
            "mount `google_credentials.json` into the streamlit-ui container "
            "(see docker-compose.yml), then restart the dashboard."
        )
    else:
        # Tickers from both portfolios — shown only as a hint under the ticker box
        portfolio_tickers = sorted({
            *(live_df["Ticker"] if "Ticker" in live_df.columns else []),
            *(paper_df["Ticker"] if "Ticker" in paper_df.columns else []),
        })

        # ---- Quick add ----
        st.markdown("**➕ Add alert**")
        with st.form("add_alert", clear_on_submit=True):
            typed_ticker = st.text_input("Ticker", placeholder="e.g. QCOM, TSLA, any symbol")
            if portfolio_tickers:
                st.caption("In your portfolio: " + ", ".join(portfolio_tickers))
            formula  = st.text_input("Formula", placeholder="Price<213.5 AND Volume>1000000")
            message  = st.text_input("Message", placeholder="Buy QCOM as price is down to 210")
            c3, c4 = st.columns(2)
            interval = c3.number_input("Scan interval (minutes)", min_value=1, max_value=390, value=5)
            active   = c4.checkbox("Active", value=True)

            if st.form_submit_button("Add to Google Sheet"):
                ticker = typed_ticker.strip().upper()
                if not ticker or not formula.strip():
                    st.error("Ticker and Formula are required.")
                else:
                    try:
                        sheets_alerts.append_alert_rule(ticker, formula, message, int(interval), active)
                        load_alert_rules.clear()
                        st.success(f"Alert for {ticker} added to the Google Sheet.")
                    except Exception as exc:
                        st.error(f"Failed to update Google Sheet: {exc}")

        st.markdown("<br>", unsafe_allow_html=True)

        # ---- Existing rules (editable) ----
        st.markdown("**📄 Current rules** — edit cells or delete rows, then save")
        try:
            rules_df = load_alert_rules()
        except Exception as exc:
            st.error(f"Could not read Google Sheet: {exc}")
            rules_df = None

        if rules_df is not None:
            if rules_df.empty:
                st.info("No alert rules in the sheet yet.")
            edited_df = st.data_editor(
                rules_df,
                num_rows="dynamic",
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Active": st.column_config.SelectboxColumn("Active", options=["Yes", "No"]),
                },
                key="alert_rules_editor",
            )
            if st.button("💾 Save changes to Google Sheet"):
                try:
                    sheets_alerts.save_alert_rules(edited_df)
                    load_alert_rules.clear()
                    st.success("Google Sheet updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to update Google Sheet: {exc}")


# ── Trading Log tab (ORB per-ticker monitoring) ───────────────────────────────

def render_orb_symbol(date: str, symbol: str) -> None:
    levels = load_orb_levels(date, symbol)
    snaps  = load_orb_snapshots(date, symbol)

    or_high = levels.get("or_high")
    or_low  = levels.get("or_low")
    captured_at = levels.get("captured_at", "—")

    # First line: ticker, date, time, OR high/low of the first 15 minutes
    hi = f"${or_high:.2f}" if or_high is not None else "—"
    lo = f"${or_low:.2f}"  if or_low  is not None else "—"
    st.markdown(f"""
    <div class="card">
        <div class="card-title">{symbol} — {date}</div>
        <div class="card-value">OR High {hi} &nbsp;·&nbsp; OR Low {lo}</div>
        <div class="card-sub">15-min opening range captured at {captured_at} ET</div>
    </div>""", unsafe_allow_html=True)

    if snaps.empty:
        st.info("No interval readings recorded yet for this ticker.")
        return

    # Price-vs-OR chart — line of price with OR high/low reference lines
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=snaps["Time"], y=snaps["Price"], mode="lines+markers",
        name="Price", line=dict(color="#4da3ff", width=2), marker=dict(size=3),
    ))
    if or_high is not None:
        fig.add_hline(y=or_high, line=dict(color="#00d084", width=1.5, dash="dash"),
                      annotation_text=f"OR High {or_high:.2f}", annotation_position="top left")
    if or_low is not None:
        fig.add_hline(y=or_low, line=dict(color="#ff4d4d", width=1.5, dash="dash"),
                      annotation_text=f"OR Low {or_low:.2f}", annotation_position="bottom left")
    fig.update_layout(
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="#ffffff",
        height=340, margin=dict(t=20, b=10, l=10, r=10), showlegend=False,
        xaxis=dict(showgrid=False, title="Time (ET)"),
        yaxis=dict(showgrid=True, gridcolor="#222", title="Price"),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Interval table — newest first
    def color_position(val):
        if val == "Above high":
            return "color: #00d084"
        if val == "Below low":
            return "color: #ff4d4d"
        return "color: #8b95a5"

    table = snaps[["Time", "Price", "Position", "Retest"]].iloc[::-1].reset_index(drop=True)
    styled = table.style\
        .applymap(color_position, subset=["Position"])\
        .format({"Price": "${:.2f}"})
    st.dataframe(styled, use_container_width=True, hide_index=True, height=360)


with tab_tradelog:
    st.markdown("""
    <div class="section-header">
        📡 Trading Log — Opening Range Breakout monitor
    </div>""", unsafe_allow_html=True)
    st.caption(
        "Every scan the bot records what it sees for each ticker: price vs the "
        "15-min opening range, and whether a breakout / retest has happened. "
        "Use this to debug why an entry did or didn't fire."
    )

    if st.button("🔄 Refresh log", key="ref_orb"):
        st.cache_data.clear()
        st.rerun()

    dates = load_orb_dates()
    if not dates:
        st.info(
            "No monitoring data yet. The bot writes to `data/orb_log.db` once the "
            "opening range is captured (after 09:45 ET on a trading day)."
        )
    else:
        sel_date = st.selectbox("Date", dates, index=0)
        symbols  = load_orb_symbols(sel_date)
        if not symbols:
            st.info("No tickers logged for this date.")
        else:
            sym_tabs = st.tabs(symbols)
            for sym_tab, sym in zip(sym_tabs, symbols):
                with sym_tab:
                    render_orb_symbol(sel_date, sym)


# ── Bot Trades tab ────────────────────────────────────────────────────────────
with tab_trades:
    trades_df = load_trades(30)

    if trades_df.empty:
        st.info("No trades recorded yet.")
    else:
        today = now.strftime("%Y-%m-%d")
        today_df = trades_df[trades_df.get("date", pd.Series()) == today] if "date" in trades_df.columns else pd.DataFrame()
        net_pnl  = today_df["net_pnl"].sum() if not today_df.empty and "net_pnl" in today_df.columns else 0.0
        n_trades = len(today_df)
        wins     = (today_df["net_pnl"] > 0).sum() if not today_df.empty and "net_pnl" in today_df.columns else 0

        c1, c2, c3 = st.columns(3)
        color = "green" if net_pnl >= 0 else "red"
        c1.markdown(f"""<div class="card"><div class="card-title">Today P&L</div>
            <div class="card-value {color}">${net_pnl:+.2f}</div></div>""", unsafe_allow_html=True)
        c2.markdown(f"""<div class="card"><div class="card-title">Trades Today</div>
            <div class="card-value">{n_trades}</div></div>""", unsafe_allow_html=True)
        c3.markdown(f"""<div class="card"><div class="card-title">Win Rate</div>
            <div class="card-value">{wins}/{n_trades}</div></div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.dataframe(trades_df, use_container_width=True, hide_index=True)

        if "date" in trades_df.columns and "net_pnl" in trades_df.columns:
            daily = trades_df.groupby("date")["net_pnl"].sum().reset_index()
            fig = px.bar(daily, x="date", y="net_pnl",
                         color="net_pnl", color_continuous_scale=["#ff4d4d", "#00d084"],
                         title="Daily P&L")
            fig.update_layout(
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="#ffffff", showlegend=False,
                margin=dict(t=40, b=10), coloraxis_showscale=False,
            )
            st.plotly_chart(fig, use_container_width=True)


# ── Live Log tab ──────────────────────────────────────────────────────────────
with tab_log:
    n_lines = st.slider("Lines to show", 20, 300, 80, 20)
    lines   = read_todays_log(n_lines)

    # Color-code log lines
    colored = []
    for ln in lines:
        if "ERROR" in ln:
            colored.append(f'<span style="color:#ff4d4d">{ln}</span>')
        elif "WARNING" in ln:
            colored.append(f'<span style="color:#f0a500">{ln}</span>')
        elif "INFO" in ln:
            colored.append(f'<span style="color:#c8d0db">{ln}</span>')
        else:
            colored.append(f'<span style="color:#8b95a5">{ln}</span>')

    st.markdown(
        f'<div style="background:#0e1117; border-radius:8px; padding:16px; '
        f'font-family:monospace; font-size:12px; line-height:1.6; '
        f'max-height:600px; overflow-y:auto;">'
        + "<br>".join(colored) +
        "</div>",
        unsafe_allow_html=True,
    )


# ── Config tab ────────────────────────────────────────────────────────────────
with tab_config:
    st.code(load_config_raw(), language="yaml")
    st.caption("Edit config.yaml on the server and restart the bot to apply changes.")


# ── Auto-refresh ──────────────────────────────────────────────────────────────
st.markdown("""
<script>setTimeout(function(){ window.location.reload(); }, 60000);</script>
""", unsafe_allow_html=True)
