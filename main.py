"""
ORB Trading Bot — entry point.

Startup sequence:
  1. Load config + logging
  2. Load universe
  3. Connect to IB Gateway
  4. Subscribe to market data
  5. Run event loop (ib.run())
  6. On each tick: evaluate strategy → risk check → Discord approval → order
  7. End of day: close positions, generate report
"""
import asyncio
import signal
import sys
import time as _time
from datetime import datetime

import schedule
from loguru import logger
from ib_insync import IB

from src.config import config
from src.utils import setup_logging, is_trading_day, is_trading_hours, is_opening_range_window
from src.connection import connect, disconnect, get_ib
from src.universe import load_universe
from src.data_feed import DataFeed
from src.strategy import StrategyEngine
from src.risk_manager import RiskManager
from src.execution import ExecutionManager
import src.reporting as reporting
import src.alerts as alerts


def main() -> None:
    setup_logging()
    logger.info("=" * 60)
    logger.info("ORB Trading Bot starting")
    paper = config.get("ibkr", "paper_trading", default=True)
    logger.info("Mode: {}", "PAPER TRADING" if paper else "*** LIVE TRADING ***")
    logger.info("=" * 60)

    # Load universe
    try:
        universe = load_universe()
    except FileNotFoundError as exc:
        logger.error("{}", exc)
        sys.exit(1)

    symbols = {s.symbol: s for s in universe}

    if not is_trading_day():
        logger.info("Today is not a trading day — exiting")
        sys.exit(0)

    # Connect to IBKR
    if not connect():
        logger.error("Cannot connect to IB Gateway — exiting")
        sys.exit(1)

    ib: IB = get_ib()

    # Get portfolio value for risk sizing
    account = ib.accountSummary()
    portfolio_value = 100_000.0  # fallback
    for item in account:
        if item.tag == "NetLiquidation":
            portfolio_value = float(item.value)
            break
    logger.info("Portfolio value: ${:,.2f}", portfolio_value)

    # Initialise modules
    feed = DataFeed(ib)
    strategy = StrategyEngine()
    risk = RiskManager(portfolio_value)
    execution = ExecutionManager(ib, risk)

    feed.subscribe(list(symbols.keys()))

    # Track signals fired today for report
    signals_today = 0

    # ------------------------------------------------------------------
    # Tick handler — runs every update_interval_seconds via schedule
    # ------------------------------------------------------------------
    def scan_tick() -> None:
        nonlocal signals_today

        if not is_trading_hours():
            return

        for sym, stock in symbols.items():
            if not feed.or_captured(sym):
                continue

            price = feed.get_price(sym)
            if price is None:
                continue

            or_high, or_low = feed.get_or(sym)
            signal = strategy.evaluate(sym, price, or_high, or_low, stock.stop_loss_pct)

            if signal:
                signals_today += 1
                asyncio.run(execution.handle_signal(signal, stock.max_position_usd))

    # ------------------------------------------------------------------
    # End-of-day handler
    # ------------------------------------------------------------------
    def end_of_day() -> None:
        logger.info("End of day — closing positions and generating report")
        execution.close_all_positions()
        report = reporting.generate_report(
            execution.get_filled_trades(),
            execution.get_open_positions(),
            signals_today,
        )
        asyncio.run(reporting.send_report(report))
        strategy.reset_day()
        risk.reset_day()
        execution.reset_day()

    # Schedule jobs
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

    # Main loop — drive ib_insync + schedule
    while True:
        ib.sleep(1)              # yields to ib_insync asyncio loop
        schedule.run_pending()


if __name__ == "__main__":
    main()
