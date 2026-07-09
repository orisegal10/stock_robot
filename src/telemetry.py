"""
ORB monitoring telemetry.

Records, for every scan tick, what the bot "sees" for each symbol so the
process can be debugged from the dashboard and Telegram:

  - Opening Range high/low and when it was captured
  - Price at each interval and whether it is above the OR high / below the OR low
  - Breakout + retest state coming from the strategy engine

Two SQLite tables in data/orb_log.db (mounted into the dashboard container):

  orb_levels     one row per (date, symbol) — the 15-min OR high/low
  orb_snapshots  one row per (date, symbol, tick) — the interval-by-interval log

Telegram messages are sent only when a symbol's phase changes (breakout,
retest, below-low, entry) so the chat is not spammed every 30 seconds.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict

from loguru import logger

from src.config import config
from src.utils import ET
from src.strategy import MonitorStatus
import src.alerts as alerts

DB_PATH = Path("data/orb_log.db")

# Last phase we notified per symbol, to detect transitions (reset each day)
_last_phase: Dict[str, str] = {}

# Phases worth a Telegram ping when first entered
_NOTIFY_PHASES = {"BREAKOUT", "RETEST", "BELOW_LOW", "ENTERED"}


def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _now_hms() -> str:
    return datetime.now(ET).strftime("%H:%M:%S")


def _init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orb_levels (
                date        TEXT,
                symbol      TEXT,
                or_high     REAL,
                or_low      REAL,
                captured_at TEXT,
                PRIMARY KEY (date, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orb_snapshots (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT,
                symbol   TEXT,
                ts       TEXT,
                price    REAL,
                or_high  REAL,
                or_low   REAL,
                position TEXT,
                retest   TEXT,
                phase    TEXT
            )
        """)


def record_or(symbol: str, or_high: float, or_low: float) -> None:
    """Store the captured 15-min Opening Range once per day per symbol."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orb_levels VALUES (?,?,?,?,?)",
            (_today(), symbol, or_high, or_low, _now_hms()),
        )


def record_snapshot(symbol: str, price: float, or_high: float,
                    or_low: float, status: MonitorStatus) -> None:
    """Append one interval reading for a symbol."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO orb_snapshots "
            "(date, symbol, ts, price, or_high, or_low, position, retest, phase) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_today(), symbol, _now_hms(), round(price, 4),
             or_high, or_low, status.position_text, status.retest_text, status.phase),
        )


def maybe_notify(symbol: str, price: float, status: MonitorStatus) -> None:
    """Send a Telegram message only when the symbol enters a new phase."""
    if not config.get("telemetry", "telegram_updates", default=True):
        return
    if _last_phase.get(symbol) == status.phase:
        return
    _last_phase[symbol] = status.phase

    if status.phase not in _NOTIFY_PHASES:
        return

    if status.phase == "BREAKOUT":
        msg = (f"🔼 *{symbol}* broke ABOVE OR high `${status.or_high:.2f}`\n"
               f"Now `${price:.2f}` — awaiting retest.")
    elif status.phase == "RETEST":
        msg = (f"🔄 *{symbol}* retesting OR high `${status.or_high:.2f}` "
               f"@ `${price:.2f}` ({status.retest_text}).")
    elif status.phase == "BELOW_LOW":
        msg = (f"🔽 *{symbol}* dropped BELOW OR low `${status.or_low:.2f}` "
               f"@ `${price:.2f}`.")
    elif status.phase == "ENTERED":
        msg = (f"🎯 *{symbol}* retest confirmed — entry signal @ `${price:.2f}`.")
    else:
        return

    alerts.notify(msg)


def reset_day() -> None:
    _last_phase.clear()
    logger.info("Telemetry phase tracking reset for new day")
