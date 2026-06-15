"""
Streamlit Dashboard — live view of the ORB bot.
Accessible at http://<vps-ip>:8501 (desktop and mobile).

Reads from:
  - logs/YYYY-MM-DD.log    (live log stream)
  - data/trades.db         (trade history)
  - config.yaml            (current settings)
  - universe/universe.csv  (active stocks)
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(
    page_title="ORB Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def load_config_raw() -> str:
    p = Path("config.yaml")
    return p.read_text(encoding="utf-8") if p.exists() else "config.yaml not found"


def load_universe() -> pd.DataFrame:
    p = Path("universe/universe.csv")
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p)


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


def read_todays_log(tail: int = 100) -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = Path("logs") / f"{today}.log"
    if not log_file.exists():
        return ["No log file yet for today."]
    lines = log_file.read_text(encoding="utf-8").splitlines()
    return lines[-tail:]


# -----------------------------------------------------------------------
# Layout
# -----------------------------------------------------------------------

st.title("📈 ORB Trading Bot")
st.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}  •  Auto-refresh every 30s")

tab_status, tab_trades, tab_universe, tab_log, tab_config = st.tabs(
    ["Status", "Trades", "Universe", "Live Log", "Config"]
)

# ---- Status ----
with tab_status:
    col1, col2, col3, col4 = st.columns(4)
    trades_df = load_trades(1)

    if not trades_df.empty:
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = trades_df[trades_df["date"] == today]
        net_pnl = today_trades["net_pnl"].sum() if not today_trades.empty else 0.0
        n_trades = len(today_trades)
    else:
        net_pnl = 0.0
        n_trades = 0

    col1.metric("Today's Net P&L", f"${net_pnl:+.2f}")
    col2.metric("Trades Today", n_trades)
    col3.metric("Date", datetime.now().strftime("%Y-%m-%d"))
    col4.metric("Time", datetime.now().strftime("%H:%M:%S"))

    st.divider()
    st.subheader("Recent Log")
    log_lines = read_todays_log(20)
    st.code("\n".join(log_lines), language=None)

# ---- Trades ----
with tab_trades:
    df = load_trades(30)
    if df.empty:
        st.info("No trades recorded yet.")
    else:
        st.subheader("Trade History (last 30 days)")
        st.dataframe(df, use_container_width=True)

        daily = df.groupby("date")["net_pnl"].sum().reset_index()
        fig = px.bar(daily, x="date", y="net_pnl", title="Daily Net P&L",
                     color="net_pnl", color_continuous_scale=["red", "green"])
        st.plotly_chart(fig, use_container_width=True)

# ---- Universe ----
with tab_universe:
    uni = load_universe()
    if uni.empty:
        st.info("universe.csv not found.")
    else:
        st.subheader("Active Stock Universe")
        active = uni[uni["active"] == True] if "active" in uni.columns else uni
        st.dataframe(active, use_container_width=True)

# ---- Live Log ----
with tab_log:
    n_lines = st.slider("Lines to show", 20, 200, 100, 20)
    log_lines = read_todays_log(n_lines)
    st.code("\n".join(log_lines), language=None)

# ---- Config ----
with tab_config:
    st.subheader("Current config.yaml")
    st.code(load_config_raw(), language="yaml")
    st.caption("To change settings, edit config.yaml on the server and restart the bot.")

# Auto-refresh every 30 seconds
st.markdown(
    """
    <script>
    setTimeout(function(){ window.location.reload(1); }, 30000);
    </script>
    """,
    unsafe_allow_html=True,
)
