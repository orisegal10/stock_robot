"""
Manages real-time price streaming from IBKR and captures the Opening Range.

During the OR window every tick updates or_high / or_low.
After the window the rolling price buffer is used by the strategy.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, Optional

from loguru import logger
from ib_insync import IB, Stock, Ticker

from src.config import config


@dataclass
class SymbolData:
    symbol: str
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    or_captured: bool = False           # True once OR window has closed
    prices: deque = field(default_factory=lambda: deque(maxlen=500))
    latest_price: Optional[float] = None
    ticker: Optional[Ticker] = None     # ib_insync Ticker object


class DataFeed:
    def __init__(self, ib: IB):
        self._ib = ib
        self._symbols: Dict[str, SymbolData] = {}
        self._or_end: Optional[time] = None
        self._compute_or_end()

    def _compute_or_end(self) -> None:
        start_str = config.get("trading", "start_time", default="09:30")
        duration = config.get("opening_range", "duration_minutes", default=5)
        start_dt = datetime.combine(datetime.today(), time.fromisoformat(start_str))
        self._or_end = (start_dt + timedelta(minutes=duration)).time()

    def subscribe(self, symbols: list[str]) -> None:
        for sym in symbols:
            contract = Stock(sym, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract, "", False, False)
            data = SymbolData(symbol=sym, ticker=ticker)
            self._symbols[sym] = data
            logger.info("Subscribed to market data: {}", sym)

        self._ib.pendingTickersEvent += self._on_ticks

    def unsubscribe_all(self) -> None:
        self._ib.pendingTickersEvent -= self._on_ticks
        for data in self._symbols.values():
            if data.ticker:
                self._ib.cancelMktData(data.ticker.contract)
        logger.info("Unsubscribed from all market data")

    def _on_ticks(self, tickers) -> None:
        now = datetime.now().time()
        in_or_window = now <= self._or_end

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
            data.prices.append(price)

            if in_or_window:
                # Capture Opening Range high/low tick by tick
                if data.or_high is None or price > data.or_high:
                    data.or_high = price
                if data.or_low is None or price < data.or_low:
                    data.or_low = price
            elif not data.or_captured and data.or_high is not None:
                data.or_captured = True
                logger.info("{} OR captured — High: {:.2f}  Low: {:.2f}",
                            sym, data.or_high, data.or_low)

    def get_price(self, symbol: str) -> Optional[float]:
        return self._symbols[symbol].latest_price if symbol in self._symbols else None

    def get_history(self, symbol: str, n: int = 50) -> list[float]:
        if symbol not in self._symbols:
            return []
        return list(self._symbols[symbol].prices)[-n:]

    def get_or(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        """Returns (or_high, or_low) — both None if OR not yet captured."""
        if symbol not in self._symbols:
            return None, None
        d = self._symbols[symbol]
        return d.or_high, d.or_low

    def or_captured(self, symbol: str) -> bool:
        return self._symbols.get(symbol, SymbolData("")).or_captured

    def all_symbols(self) -> list[str]:
        return list(self._symbols.keys())
