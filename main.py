"""
ORB Trading Bot — entry point.

Flow:
  09:30  → Telegram: "Following these stocks: MU, HOOD..."
  09:45  → Telegram: "Opening Range captured: MU H:$x L:$y ..."
  During → 5min breakout detection + 1min retest entry
  On buy → Telegram: "Bought MU 10 shares @ $192.50 | Target $196.50 | +2.1%"
  On sell→ Telegram: "Sold MU 10 shares @ $196.52 | Gained $40 (+2.1%)"
  16:00  → Close all + day summary Telegram
"""
import asyncio
import signal
import sys
import time as time_module
from datetime import datetime, time
from typing import Optional

import schedule
from loguru import logger
from ib_insync import IB

from src.config import config
from src.utils import setup_logging, is_trading_day, is_trading_hours, ET
from src.connection import connect, disconnect, get_ib
from src.universe import load_universe
from src.data_feed import DataFeed
from src.strategy import StrategyEngine, Action
from src.risk_manager import RiskManager
from src.execution import ExecutionManager
import src.alerts as alerts
import src.reporting as reporting


def main() -> None:
    setup_logging()
    logger.info("=" * 60)
    logger.info("ORB Trading Bot starting")
    paper = config.get("ibkr", "paper_trading", default=True)
    logger.info("Mode: {}", "PAPER TRADING" if paper else "*** LIVE TRADING ***")
    logger.info("=" * 60)

    try:
        universe = load_universe()
    except FileNotFoundError as exc:
        logger.error("{}", exc)
        sys.exit(1)

    symbols = {s.symbol: s for s in universe}

    if not is_trading_day():
        logger.info("Today is not a trading day — exiting")
        sys.exit(0)

    # Retry connecting for up to 10 minutes (IB Gateway takes time to start)
    connected = False
    for attempt in range(1, 61):
        if connect():
            connected = True
            break
        logger.warning("IB Gateway not ready (attempt {}/60) — retrying in 10s...", attempt)
        time_module.sleep(10)

    if not connected:
        logger.error("Cannot connect to IB Gateway after 10 minutes — exiting")
        sys.exit(1)

    ib: IB = get_ib()

    # Get portfolio value
    portfolio_value = 100_000.0
    for item in ib.accountSummary():
        if item.tag == "NetLiquidation":
            portfolio_value = float(item.value)
            break
    logger.info("Portfolio value: ${:,.2f}", portfolio_value)

    # Initialise modules
    feed     = DataFeed(ib)
    strategy = StrategyEngine()
    risk     = RiskManager(portfolio_value)
    execution = ExecutionManager(ib, risk)

    feed.subscribe(list(symbols.keys()))

    # Send startup message
    alerts.send_startup(list(symbols.keys()))

    signals_today = 0
    or_reported   = False

    # ------------------------------------------------------------------
    # OR end time
    # ------------------------------------------------------------------
    or_duration = config.get("opening_range", "duration_minutes", default=15)
    start_str   = config.get("trading", "start_time", default="09:30")
    from datetime import timedelta
    start_dt    = datetime.combine(datetime.now(ET).date(), time.fromisoformat(start_str))
    or_end_time = (start_dt + timedelta(minutes=or_duration)).time()

    latency_skip_until: Optional[datetime] = None
    LATENCY_THRESHOLD_SECS = 60

    # ------------------------------------------------------------------
    # Tick scan — runs every update_interval_seconds
    # ------------------------------------------------------------------
    def scan_tick() -> None:
        nonlocal signals_today, or_reported, latency_skip_until

        if not is_trading_hours():
            return

        # --- Data latency check ---
        max_latency = feed.get_max_latency_seconds()
        if max_latency is not None and max_latency > LATENCY_THRESHOLD_SECS:
            # Don't re-alert if already in skip mode
            if latency_skip_until is None:
                cont = asyncio.run(alerts.request_data_continue(max_latency))
                if not cont:
                    from datetime import timedelta
                    latency_skip_until = datetime.now() + timedelta(minutes=5)
                    logger.warning("Skipping trades for 5 min due to data latency ({:.0f}s)", max_latency)
                    return
        elif latency_skip_until is not None and datetime.now() > latency_skip_until:
            latency_skip_until = None
            alerts.notify("✅ Data feed recovered — resuming normal trading scan.")

        if latency_skip_until is not None and datetime.now() < latency_skip_until:
            return

        now_time = datetime.now(ET).time()

        # Report OR once after the window closes
        if not or_reported and now_time > or_end_time:
            or_data = {}
            for sym in symbols:
                h, l = feed.get_or(sym)
                if h and l:
                    or_data[sym] = {"high": h, "low": l}
            if or_data:
                alerts.send_or_captured(or_data)
                or_reported = True

        # Evaluate strategy for each symbol
        for sym, stock in symbols.items():
            if not feed.or_captured(sym):
                continue

            price = feed.get_price(sym)
            if price is None:
                continue

            or_high, or_low = feed.get_or(sym)
            swing_high = feed.get_swing_high(sym)

            sig = strategy.evaluate(
                sym, price, or_high, or_low,
                swing_high, stock.stop_loss_pct
            )

            if sig:
                if sig.action == Action.LONG:
                    signals_today += 1
                asyncio.run(execution.handle_signal(sig, stock.max_position_usd))

    # ------------------------------------------------------------------
    # End of day
    # ------------------------------------------------------------------
    def end_of_day() -> None:
        logger.info("End of day — closing positions and generating report")
        execution.close_all_positions()

        filled = execution.get_filled_trades()
        net_pnl = sum(t.net_pnl for t in filled)
        wins    = sum(1 for t in filled if t.net_pnl > 0)

        alerts.send_day_summary(
            trades=len(filled),
            net_pnl=net_pnl,
            wins=wins,
        )

        report = reporting.generate_report(
            filled,
            execution.get_open_positions(),
            signals_today,
        )
        asyncio.run(reporting.send_report(report))

        strategy.reset_day()
        risk.reset_day()
        execution.reset_day()

    # Schedule
    interval = config.get("trading", "update_interval_seconds", default=30)
    end_time = config.get("trading", "end_time", default="16:00")
    schedule.every(interval).seconds.do(scan_tick)
    schedule.every().day.at(end_time).do(end_of_day)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(signum, frame) -> None:
        logger.info("Shutdown signal received")
        feed.unsubscribe_all()
        disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Bot running — scanning every {}s | press Ctrl+C to stop", interval)

    while True:
        ib.sleep(1)
        schedule.run_pending()


if __name__ == "__main__":
    main()
