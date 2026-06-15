"""
Order execution via ib_insync.

- Checks Discord approval before placing any order.
- Places a bracket order: market entry + stop loss.
- Tracks open positions and filled orders for the day.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ib_insync import IB, Stock, MarketOrder, StopOrder, Trade
from loguru import logger

from src.config import config
from src.strategy import Signal
from src.risk_manager import RiskManager
from src.utils import calc_commission
import src.alerts as alerts


@dataclass
class OpenPosition:
    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    trade: Optional[Trade] = None


@dataclass
class FilledTrade:
    symbol: str
    shares: int
    entry_price: float
    exit_price: float
    pnl: float
    commission: float
    net_pnl: float


class ExecutionManager:
    def __init__(self, ib: IB, risk: RiskManager):
        self._ib = ib
        self._risk = risk
        self._positions: List[OpenPosition] = []
        self._filled: List[FilledTrade] = []
        self._dry_run = config.get("ibkr", "paper_trading", default=True)

    async def handle_signal(self, signal: Signal, max_position_usd: float) -> bool:
        """Full pipeline: risk check → approval → order."""
        allowed, reason, shares = self._risk.check_trade_allowed(
            signal.entry_price, signal.stop_price, max_position_usd
        )
        if not allowed:
            logger.info("Trade {} blocked by risk: {}", signal.symbol, reason)
            return False

        commission = calc_commission(shares) * 2
        approved = await alerts.request_approval(signal, shares, commission)
        if not approved:
            return False

        return self._place_order(signal, shares)

    def _place_order(self, signal: Signal, shares: int) -> bool:
        contract = Stock(signal.symbol, "SMART", "USD")
        try:
            self._ib.qualifyContracts(contract)
            entry_order = MarketOrder("BUY", shares)
            stop_order = StopOrder("SELL", shares, signal.stop_price)

            entry_trade = self._ib.placeOrder(contract, entry_order)
            self._ib.placeOrder(contract, stop_order)

            pos = OpenPosition(
                symbol=signal.symbol,
                shares=shares,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                trade=entry_trade,
            )
            self._positions.append(pos)
            self._risk.record_position_opened()

            mode = "PAPER" if self._dry_run else "LIVE"
            logger.info("[{}] Order placed: BUY {} x {} @ ~${:.2f} | Stop: ${:.2f}",
                        mode, shares, signal.symbol, signal.entry_price, signal.stop_price)
            alerts.notify(
                f"✅ [{mode}] BUY {shares}x {signal.symbol} @ ~${signal.entry_price:.2f} | "
                f"Stop ${signal.stop_price:.2f}"
            )
            return True
        except Exception as exc:
            logger.error("Order placement failed for {}: {}", signal.symbol, exc)
            return False

    def close_all_positions(self) -> None:
        """Market-close all open positions (called at end of day)."""
        for pos in list(self._positions):
            try:
                contract = Stock(pos.symbol, "SMART", "USD")
                self._ib.qualifyContracts(contract)
                self._ib.placeOrder(contract, MarketOrder("SELL", pos.shares))
                logger.info("EOD close: SELL {} x {}", pos.shares, pos.symbol)
            except Exception as exc:
                logger.error("Failed to close {}: {}", pos.symbol, exc)
        self._positions.clear()

    def get_open_positions(self) -> List[OpenPosition]:
        return list(self._positions)

    def get_filled_trades(self) -> List[FilledTrade]:
        return list(self._filled)

    def reset_day(self) -> None:
        self._positions.clear()
        self._filled.clear()
