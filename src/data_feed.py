"""
Multi-timeframe data feed using IBKR real-time ticks.

Timeframes:
  - 15 min: Opening Range capture (09:30 → 09:45)
  - 5 min:  Breakout detection + swing high recording
  - 1 min:  Retest entry confirmation + exit (sell on touch)

All three are derived from the same tick stream — no multiple subscriptions needed.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from loguru import logger
from ib_insync import IB, Stock, Ticker

from src.config import config
from src.utils import ET
from src.connection import set_competing_session_callback


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    timestamp: datetime


@dataclass
class SymbolData:
    symbol: str

    # Opening Range (15 min)
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    or_captured: bool = False

    # Swing high recorded after breakout (5 min timeframe)
    swing_high: Optional[float] = None

    # Latest price (from ticks)
    latest_price: Optional[float] = None

    # Latency tracking
    last_tick_time: Optional[datetime] = None

    # Raw tick prices for bar aggregation
    tick_prices: List[Tuple[datetime, float]] = field(default_factory=list)

    # Aggregated bars
    bars_1min: List[Bar] = field(default_factory=list)
    bars_5min: List[Bar] = field(default_factory=list)

    # Current incomplete bar accumulators
    _cur_1min_prices: List[float] = field(default_factory=list)
    _cur_1min_start: Optional[datetime] = None
    _cur_5min_prices: List[float] = field(default_factory=list)
    _cur_5min_start: Optional[datetime] = None

    ticker: Optional[Ticker] = None


class DataFeed:
    def __init__(self, ib: IB):
        self._ib = ib
        self._symbols: Dict[str, SymbolData] = {}
        self._or_end: Optional[time] = self._compute_or_end()
        self._competing_session: bool = False
        set_competing_session_callback(self._on_competing_session)

    def _compute_or_end(self) -> time:
        start_str = config.get("trading", "start_time", default="09:30")
        duration = config.get("opening_range", "duration_minutes", default=15)
        start_dt = datetime.combine(datetime.now(ET).date(), time.fromisoformat(start_str))
        return (start_dt + timedelta(minutes=duration)).time()

    def _on_competing_session(self, active: bool) -> None:
        """Called by connection.py when error 10197 fires or a farm reconnects."""
        import src.alerts as alerts
        if active and not self._competing_session:
            self._competing_session = True
            logger.warning("Competing session active — switching to delayed data (15-min delay)")
            alerts.notify(
                "⚠️ *You are logged into IBKR* — bot switched to 15-min delayed data.\n"
                "New entries paused. Log out to restore live data."
            )
            # Resubscribe all symbols with delayed data (regulatory snapshot = False, frozen = True)
            for sym, data in self._symbols.items():
                if data.ticker:
                    self._ib.cancelMktData(data.ticker.contract)
                contract = Stock(sym, "SMART", "USD")
                data.ticker = self._ib.reqMktData(contract, "", False, True)  # frozen=True
        elif not active and self._competing_session:
            self._competing_session = False
            logger.info("Competing session cleared — switching back to live data")
            alerts.notify("✅ *Live data restored* — bot resumed normal trading scan.")
            for sym, data in self._symbols.items():
                if data.ticker:
                    self._ib.cancelMktData(data.ticker.contract)
                contract = Stock(sym, "SMART", "USD")
                data.ticker = self._ib.reqMktData(contract, "", False, False)  # live

    def is_competing_session(self) -> bool:
        return self._competing_session

    def subscribe(self, symbols: List[str]) -> None:
        for sym in symbols:
            contract = Stock(sym, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._symbols[sym] = SymbolData(symbol=sym, ticker=ticker)
            logger.info("Subscribed to market data: {}", sym)
        self._ib.pendingTickersEvent += self._on_ticks

    def unsubscribe_all(self) -> None:
        self._ib.pendingTickersEvent -= self._on_ticks
        for data in self._symbols.values():
            if data.ticker:
                self._ib.cancelMktData(data.ticker.contract)
        logger.info("Unsubscribed from all market data")

    def _on_ticks(self, tickers) -> None:
        now = datetime.now(ET).replace(tzinfo=None)
        now_time = datetime.now(ET).time()

        for ticker in tickers:
            sym = ticker.contract.symbol
            if sym not in self._symbols:
                continue

            price = ticker.last or ticker.close
            if not price or price <= 0:
                mid = (ticker.bid + ticker.ask) / 2 if ticker.bid and ticker.ask else None
                price = mid
            if not price:
                continue

            data = self._symbols[sym]
            data.latest_price = price
            data.last_tick_time = now

            # --- Opening Range capture (15 min window) ---
            if now_time <= self._or_end:
                if data.or_high is None or price > data.or_high:
                    data.or_high = price
                if data.or_low is None or price < data.or_low:
                    data.or_low = price
            elif not data.or_captured and data.or_high is not None:
                data.or_captured = True
                logger.info("{} OR captured — High: {:.2f}  Low: {:.2f}",
                            sym, data.or_high, data.or_low)

            # --- Aggregate 1-min bars ---
            self._aggregate(data, now, price, "1min",
                            data._cur_1min_prices, data.bars_1min,
                            lambda dt: dt.replace(second=0, microsecond=0),
                            timedelta(minutes=1))

            # --- Aggregate 5-min bars ---
            self._aggregate(data, now, price, "5min",
                            data._cur_5min_prices, data.bars_5min,
                            lambda dt: dt.replace(
                                minute=(dt.minute // 5) * 5,
                                second=0, microsecond=0),
                            timedelta(minutes=5))

            # --- Track swing high on 5-min bars (after OR, before retest) ---
            if data.or_captured and data.or_high is not None:
                if price > data.or_high:
                    if data.swing_high is None or price > data.swing_high:
                        data.swing_high = price
                        logger.debug("{} swing high updated: {:.2f}", sym, price)

    def _aggregate(self, data: SymbolData, now: datetime, price: float,
                   label: str, prices: List[float], bars: List[Bar],
                   floor_fn, duration: timedelta) -> None:
        bar_start = floor_fn(now)

        if not prices:
            prices.append(price)
            if label == "1min":
                data._cur_1min_start = bar_start
            else:
                data._cur_5min_start = bar_start
            return

        cur_start = data._cur_1min_start if label == "1min" else data._cur_5min_start

        if bar_start > cur_start:
            # Close the completed bar
            bar = Bar(
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                timestamp=cur_start,
            )
            bars.append(bar)
            if len(bars) > 200:
                bars.pop(0)
            prices.clear()

            if label == "1min":
                data._cur_1min_start = bar_start
            else:
                data._cur_5min_start = bar_start

        prices.append(price)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_latency_seconds(self, symbol: str) -> Optional[float]:
        """Returns seconds since last tick, or None if no tick received yet."""
        d = self._symbols.get(symbol)
        if d is None or d.last_tick_time is None:
            return None
        return (datetime.now() - d.last_tick_time).total_seconds()

    def get_max_latency_seconds(self) -> Optional[float]:
        """Returns the worst latency across all symbols."""
        latencies = [
            (datetime.now() - d.last_tick_time).total_seconds()
            for d in self._symbols.values()
            if d.last_tick_time is not None
        ]
        return max(latencies) if latencies else None

    def get_price(self, symbol: str) -> Optional[float]:
        return self._symbols[symbol].latest_price if symbol in self._symbols else None

    def get_or(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        if symbol not in self._symbols:
            return None, None
        d = self._symbols[symbol]
        return d.or_high, d.or_low

    def get_swing_high(self, symbol: str) -> Optional[float]:
        return self._symbols[symbol].swing_high if symbol in self._symbols else None

    def get_latest_1min_bar(self, symbol: str) -> Optional[Bar]:
        bars = self._symbols[symbol].bars_1min if symbol in self._symbols else []
        return bars[-1] if bars else None

    def get_latest_5min_bar(self, symbol: str) -> Optional[Bar]:
        bars = self._symbols[symbol].bars_5min if symbol in self._symbols else []
        return bars[-1] if bars else None

    def or_captured(self, symbol: str) -> bool:
        return self._symbols.get(symbol, SymbolData("")).or_captured

    def all_symbols(self) -> List[str]:
        return list(self._symbols.keys())
