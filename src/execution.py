"""
Order execution via ib_insync.

- BUY: Discord/Telegram approval → market order + stop loss
- SELL: market order immediately when strategy fires SELL signal (price touched target)
- Sends Telegram messages on bought and sold
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ib_insync import IB, Stock, MarketOrder, StopOrder, Trade
from loguru import logger

from src.config import config
from src.strategy import Signal, Action
from src.risk_manager import RiskManager
from src.utils import calc_commission
import src.alerts as alerts


@dataclass
class OpenPosition:
    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    trade: Optional[Trade] = None
    stop_trade: Optional[Trade] = None   # resting protective stop, so we can cancel it on exit


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
        self._paper = config.get("ibkr", "paper_trading", default=True)

    # ------------------------------------------------------------------
    # BUY flow
    # ------------------------------------------------------------------

    async def handle_signal(self, signal: Signal, max_position_usd: float) -> bool:
        if signal.action == Action.SELL:
            return self._handle_sell(signal)

        # BUY flow
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

        return self._place_buy(signal, shares)

    def _safety_check(self, symbol: str, shares: int, price: float) -> bool:
        """
        Absolute caps enforced immediately before any BUY order, independent of
        position-sizing math. Returns False (and blocks the order) if the order
        would exceed a cap. On a LIVE account, missing caps also block the order.
        """
        max_order_usd = config.get("risk", "max_order_value_usd", default=None)
        max_shares    = config.get("risk", "max_shares_per_order", default=None)
        order_value   = shares * price

        if not self._paper and (max_order_usd is None or max_shares is None):
            logger.error(
                "SAFETY: LIVE trading but risk.max_order_value_usd / max_shares_per_order "
                "not set — refusing BUY {} ({} sh @ ${:.2f})", symbol, shares, price)
            return False

        if max_shares is not None and shares > max_shares:
            logger.error("SAFETY BLOCK {}: {} shares exceeds max_shares_per_order={}",
                         symbol, shares, max_shares)
            return False

        if max_order_usd is not None and order_value > max_order_usd:
            logger.error("SAFETY BLOCK {}: order value ${:.2f} exceeds max_order_value_usd=${}",
                         symbol, order_value, max_order_usd)
            return False

        return True

    def _place_buy(self, signal: Signal, shares: int) -> bool:
        # ── HARD safety caps — absolute last line of defense before a real order ──
        if not self._safety_check(signal.symbol, shares, signal.entry_price):
            return False

        contract = Stock(signal.symbol, "SMART", "USD")
        try:
            self._ib.qualifyContracts(contract)
            entry_trade = self._ib.placeOrder(contract, MarketOrder("BUY", shares))

            # Wait for fill to get the actual fill price (up to 10s)
            for _ in range(10):
                self._ib.sleep(1)
                if entry_trade.orderStatus.filled >= shares:
                    break

            fill_price = entry_trade.orderStatus.avgFillPrice or signal.entry_price
            if not fill_price or fill_price != fill_price:  # nan check
                fill_price = signal.entry_price
            stop_loss_pct = signal.stop_price / signal.entry_price  # preserve ratio
            actual_stop = round(fill_price * stop_loss_pct, 2)

            logger.info("Buy filled @ ${:.2f} — placing stop at ${:.2f}", fill_price, actual_stop)
            stop_trade = self._ib.placeOrder(contract, StopOrder("SELL", shares, actual_stop))

            pos = OpenPosition(
                symbol=signal.symbol,
                shares=shares,
                entry_price=fill_price,
                stop_price=actual_stop,
                target_price=signal.target_price,
                trade=entry_trade,
                stop_trade=stop_trade,
            )
            self._positions.append(pos)
            self._risk.record_position_opened()

            mode = "PAPER" if self._paper else "LIVE"
            logger.info("[{}] BUY {} x {} @ ${:.2f} | Target ${:.2f} | Stop ${:.2f}",
                        mode, shares, signal.symbol,
                        fill_price, signal.target_price, actual_stop)

            alerts.send_bought(
                symbol=signal.symbol,
                shares=shares,
                price=fill_price,
                target=signal.target_price,
                gain_pct=signal.potential_gain_pct,
            )
            return True

        except Exception as exc:
            logger.error("BUY order failed for {}: {}", signal.symbol, exc)
            return False

    # ------------------------------------------------------------------
    # SELL flow (target touched)
    # ------------------------------------------------------------------

    def _cancel_stop(self, pos: OpenPosition) -> None:
        """Cancel a position's resting protective stop so a target/EOD exit can't
        leave a stray SELL order that would later open a short."""
        if pos.stop_trade is None:
            return
        try:
            self._ib.cancelOrder(pos.stop_trade.order)
            logger.info("Cancelled resting stop for {} before exit", pos.symbol)
        except Exception as exc:
            logger.error("Failed to cancel resting stop for {}: {}", pos.symbol, exc)
        finally:
            pos.stop_trade = None

    def _handle_sell(self, signal: Signal) -> bool:
        pos = next((p for p in self._positions if p.symbol == signal.symbol), None)
        if pos is None:
            logger.warning("SELL signal for {} but no open position found", signal.symbol)
            return False

        contract = Stock(signal.symbol, "SMART", "USD")
        try:
            self._ib.qualifyContracts(contract)
            # Cancel the protective stop FIRST — otherwise after this sell fills the
            # stale stop could trigger and sell shares we no longer own (going short).
            self._cancel_stop(pos)
            self._ib.placeOrder(contract, MarketOrder("SELL", pos.shares))

            exit_price = signal.entry_price  # signal.entry_price = current price at touch
            commission = calc_commission(pos.shares) * 2
            pnl = (exit_price - pos.entry_price) * pos.shares
            net_pnl = pnl - commission

            filled = FilledTrade(
                symbol=pos.symbol,
                shares=pos.shares,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                commission=commission,
                net_pnl=net_pnl,
            )
            self._filled.append(filled)
            self._positions.remove(pos)
            self._risk.record_position_closed(pnl=net_pnl)

            mode = "PAPER" if self._paper else "LIVE"
            logger.info("[{}] SELL {} x {} @ ${:.2f} | P&L ${:+.2f}",
                        mode, pos.shares, pos.symbol, exit_price, net_pnl)

            alerts.send_sold(
                symbol=pos.symbol,
                shares=pos.shares,
                entry=pos.entry_price,
                exit_price=exit_price,
            )
            return True

        except Exception as exc:
            logger.error("SELL order failed for {}: {}", signal.symbol, exc)
            return False

    # ------------------------------------------------------------------
    # End of day
    # ------------------------------------------------------------------

    def close_all_positions(self) -> None:
        for pos in list(self._positions):
            try:
                contract = Stock(pos.symbol, "SMART", "USD")
                self._ib.qualifyContracts(contract)
                # Cancel the protective stop first so it can't fire post-close and short us.
                self._cancel_stop(pos)
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
