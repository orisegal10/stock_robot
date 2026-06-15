"""
ORB (Opening Range Breakout) + Retest strategy engine.

Logic:
  1. After OR window closes, watch for price breaking above or_high.
  2. Once broken, wait for a Retest — price returns to or_high level.
  3. On confirmed Retest, emit a LONG signal.
  4. One signal per symbol per trading day.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from loguru import logger

from src.config import config


class Action(str, Enum):
    LONG = "LONG"
    HOLD = "HOLD"


@dataclass
class Signal:
    symbol: str
    action: Action
    entry_price: float
    stop_price: float
    or_high: float
    or_low: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


class _SymbolState:
    def __init__(self):
        self.breakout_confirmed = False
        self.retest_candles = 0
        self.signal_fired_today = False


class StrategyEngine:
    def __init__(self):
        self._states: Dict[str, _SymbolState] = {}
        self._threshold = config.get("strategy", "breakout_threshold_points", default=0.05)
        self._retest_max_candles = config.get("strategy", "retest_max_candles", default=5)
        self._confirmation = config.get("strategy", "retest_confirmation_type", default="touch")

    def _state(self, symbol: str) -> _SymbolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]

    def reset_day(self) -> None:
        """Call once at market open to clear previous day state."""
        for state in self._states.values():
            state.breakout_confirmed = False
            state.retest_candles = 0
            state.signal_fired_today = False
        logger.info("Strategy state reset for new trading day")

    def evaluate(
        self,
        symbol: str,
        current_price: float,
        or_high: Optional[float],
        or_low: Optional[float],
        stop_loss_pct: float,
    ) -> Optional[Signal]:
        """
        Returns a Signal if entry conditions are met, otherwise None.
        stop_loss_pct comes from universe.csv (per-symbol).
        """
        if or_high is None or or_low is None:
            return None

        state = self._state(symbol)

        if state.signal_fired_today:
            return None

        # Step 1 — Detect Breakout above OR high
        if not state.breakout_confirmed:
            if current_price > or_high + self._threshold:
                state.breakout_confirmed = True
                state.retest_candles = 0
                logger.info("{} BREAKOUT above OR high {:.2f} at price {:.2f}",
                            symbol, or_high, current_price)
            return None

        # Step 2 — Watch for Retest
        retest_zone_high = or_high + self._threshold
        retest_zone_low = or_high - self._threshold * 3

        price_in_retest_zone = retest_zone_low <= current_price <= retest_zone_high

        if price_in_retest_zone:
            state.retest_candles += 1
        else:
            # Price moved away from retest zone — check if retest is confirmed
            if state.retest_candles > 0 and current_price > or_high:
                return self._emit_signal(symbol, current_price, or_high, or_low,
                                         stop_loss_pct, state)
            state.retest_candles = 0

        # Retest candles accumulated enough — fire
        if state.retest_candles >= self._retest_max_candles and current_price > or_high:
            return self._emit_signal(symbol, current_price, or_high, or_low,
                                     stop_loss_pct, state)

        return None

    def _emit_signal(
        self,
        symbol: str,
        price: float,
        or_high: float,
        or_low: float,
        stop_loss_pct: float,
        state: _SymbolState,
    ) -> Signal:
        stop = round(price * (1 - stop_loss_pct / 100), 2)
        state.signal_fired_today = True
        state.retest_candles = 0
        signal = Signal(
            symbol=symbol,
            action=Action.LONG,
            entry_price=round(price, 2),
            stop_price=stop,
            or_high=or_high,
            or_low=or_low,
            reason=f"ORB breakout + retest of {or_high:.2f}",
        )
        logger.info("SIGNAL {} | Entry {:.2f} | Stop {:.2f} | OR High {:.2f}",
                    symbol, price, stop, or_high)
        return signal
