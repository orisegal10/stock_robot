"""
Daily P&L report — generated at market close, saved to logs/, sent to Discord.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

from loguru import logger

from src.execution import FilledTrade, OpenPosition
import src.alerts as alerts

DB_PATH = Path("data/trades.db")


def _init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT,
                symbol      TEXT,
                shares      INTEGER,
                entry_price REAL,
                exit_price  REAL,
                pnl         REAL,
                commission  REAL,
                net_pnl     REAL
            )
        """)


def save_trades(trades: List[FilledTrade]) -> None:
    _init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        for t in trades:
            conn.execute(
                "INSERT INTO trades VALUES (NULL,?,?,?,?,?,?,?,?)",
                (today, t.symbol, t.shares, t.entry_price, t.exit_price,
                 t.pnl, t.commission, t.net_pnl),
            )
    logger.info("Saved {} trades to database", len(trades))


def generate_report(
    filled: List[FilledTrade],
    open_positions: List[OpenPosition],
    signals_fired: int,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    total_net = sum(t.net_pnl for t in filled)
    total_gross = sum(t.pnl for t in filled)
    total_commission = sum(t.commission for t in filled)

    lines = [
        f"=== Daily Report {today} ===",
        f"Signals fired  : {signals_fired}",
        f"Trades executed: {len(filled)}",
        f"Open at close  : {len(open_positions)}",
        "",
        f"Gross P&L  : ${total_gross:+.2f}",
        f"Commission : -${total_commission:.2f}",
        f"Net P&L    : ${total_net:+.2f}",
        "",
    ]

    if filled:
        lines.append("--- Trades ---")
        for t in filled:
            lines.append(
                f"  {t.symbol:6s} {t.shares:4d}sh  "
                f"entry ${t.entry_price:.2f}  exit ${t.exit_price:.2f}  "
                f"net ${t.net_pnl:+.2f}"
            )

    if open_positions:
        lines.append("")
        lines.append("--- Still Open ---")
        for p in open_positions:
            lines.append(f"  {p.symbol} {p.shares}sh @ ${p.entry_price:.2f}")

    report = "\n".join(lines)
    log_file = Path("logs") / f"{today}-report.txt"
    log_file.parent.mkdir(exist_ok=True)
    log_file.write_text(report, encoding="utf-8")
    logger.info("Daily report saved to {}", log_file)
    return report


async def send_report(report: str) -> None:
    await alerts.send_daily_report(report)
