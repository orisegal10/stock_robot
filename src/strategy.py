"""
ORB (Opening Range Breakout) + Retest strategy — multi-timeframe.

Timeframes:
  - OR captured on 15 min bars
  - Breakout + swing high tracked on 5 min bars
  - Retest entry on 1 min bars
  - Exit: sell immediately when price touches swing high (1 min)

Entry filter: only trade if potential gain >= min_gain_percent (default 2%)
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from loguru import logger

from src.config import config


class Action(str, Enum):
    LONG  = "LONG"
    SELL  = "SELL"
    HOLD  = "HOLD"


@dataclass
class Signal:
    symbol: str
    action: Action
    entry_price: float
    stop_price: float
    target_price: float
    or_high: float
    or_low: float
    potential_gain_pct: float
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MonitorStatus:
    """Read-only snapshot of what the strategy currently sees for a symbol.

    Used for telemetry/debugging — does not change any state.
    """
    symbol: str
    price: float
    or_high: float
    or_low: float
    position_text: str   # "Above high" | "Below low" | "Inside range"
    retest_text: str     # human-readable breakout/retest description
    phase: str           # coarse phase: PRE | BREAKOUT | RETEST | ENTERED | BELOW_LOW


class _SymbolState:
    def __init__(self):
        self.breakout_confirmed = False
        self.retest_candles     = 0
        self.signal_fired_today = False
        self.position_open      = False
        self.entry_price: Optional[float] = None
        self.target_price: Optional[float] = None


class StrategyEngine:
    def __init__(self):
        self._states: Dict[str, _SymbolState] = {}
        self._threshold     = config.get("strategy", "breakout_threshold_points", default=0.05)
        self._retest_candles = config.get("strategy", "retest_max_candles", default=5)
        self._min_gain_pct  = config.get("risk", "min_gain_percent", default=2.0)

    def _state(self, symbol: str) -> _SymbolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]

    def reset_day(self) -> None:
        for state in self._states.values():
            state.breakout_confirmed = False
            state.retest_candles     = 0
            state.signal_fired_today = False
            state.position_open      = False
            state.entry_price        = None
            state.target_price       = None
        logger.info("Strategy state reset for new trading day")

    def evaluate(
        self,
        symbol: str,
        current_price: float,
        or_high: Optional[float],
        or_low: Optional[float],
        swing_high: Optional[float],
        stop_loss_pct: float,
    ) -> Optional[Signal]:
        if or_high is None or or_low is None:
            return None

        state = self._state(symbol)

        # --- Check exit first: sell when price touches target ---
        if state.position_open and state.target_price is not None:
            if current_price >= state.target_price:
                gained_pct = ((current_price - state.entry_price) / state.entry_price) * 100
                logger.info("{} SELL — price {:.2f} touched target {:.2f} (+{:.2f}%)",
                            symbol, current_price, state.target_price, gained_pct)
                state.position_open = False
                return Signal(
                    symbol=symbol,
                    action=Action.SELL,
                    entry_price=state.entry_price,
                    stop_price=round(state.entry_price * (1 - stop_loss_pct / 100), 2),
                    target_price=state.target_price,
                    or_high=or_high,
                    or_low=or_low,
                    potential_gain_pct=round(gained_pct, 2),
                    reason=f"Target {state.target_price:.2f} touched",
                )

        if state.signal_fired_today or state.position_open:
            return None

        # --- Step 1: Detect breakout above OR high (5 min timeframe) ---
        if not state.breakout_confirmed:
            if current_price > or_high + self._threshold:
                state.breakout_confirmed = True
                state.retest_candles = 0
                logger.info("{} BREAKOUT above OR high {:.2f} @ {:.2f}",
                            symbol, or_high, current_price)
            return None

        # --- Step 2: Validate swing high exists and gain >= min_gain_pct ---
        if swing_high is None:
            logger.debug("{} breakout confirmed but no swing high recorded yet — waiting", symbol)
            return None

        if or_high <= 0:
            return None

        # Entry will be approximately current price on retest
        potential_gain = ((swing_high - current_price) / current_price) * 100
        if potential_gain < self._min_gain_pct:
            logger.debug("{} no entry — potential gain {:.2f}% < min {:.2f}% "
                         "(swing {:.2f} vs price {:.2f})",
                         symbol, potential_gain, self._min_gain_pct, swing_high, current_price)
            return None   # not worth trading

        # --- Step 3: Retest detection (1 min timeframe) ---
        retest_zone_high = or_high + self._threshold
        retest_zone_low  = or_high - self._threshold * 3

        in_zone = retest_zone_low <= current_price <= retest_zone_high

        if in_zone:
            state.retest_candles += 1
        else:
            if state.retest_candles > 0 and current_price > or_high:
                return self._emit_buy(symbol, current_price, or_high, or_low,
                                      swing_high, stop_loss_pct, state)
            state.retest_candles = 0

        if state.retest_candles >= self._retest_candles and current_price > or_high:
            return self._emit_buy(symbol, current_price, or_high, or_low,
                                  swing_high, stop_loss_pct, state)

        return None

    def _emit_buy(
        self,
        symbol: str,
        price: float,
        or_high: float,
        or_low: float,
        swing_high: float,
        stop_loss_pct: float,
        state: _SymbolState,
    ) -> Signal:
        stop   = round(price * (1 - stop_loss_pct / 100), 2)
        target = round(swing_high, 2)
        gain   = round(((target - price) / price) * 100, 2)

        state.signal_fired_today = True
        state.retest_candles     = 0
        state.position_open      = True
        state.entry_price        = round(price, 2)
        state.target_price       = target

        signal = Signal(
            symbol=symbol,
            action=Action.LONG,
            entry_price=round(price, 2),
            stop_price=stop,
            target_price=target,
            or_high=or_high,
            or_low=or_low,
            potential_gain_pct=gain,
            reason=f"ORB retest of {or_high:.2f} | target swing high {target:.2f}",
        )
        logger.info("BUY SIGNAL {} | Entry {:.2f} | Target {:.2f} | Stop {:.2f} | Gain {:.2f}%",
                    symbol, price, target, stop, gain)
        return signal

    def describe(
        self,
        symbol: str,
        price: float,
        or_high: Optional[float],
        or_low: Optional[float],
    ) -> Optional[MonitorStatus]:
        """Describe the current monitoring state for a symbol without mutating it.

        Call *after* evaluate() so the returned state reflects this tick.
        """
        if or_high is None or or_low is None:
            return None

        state = self._state(symbol)

        if price > or_high:
            position_text = "Above high"
        elif price < or_low:
            position_text = "Below low"
        else:
            position_text = "Inside range"

        retest_zone_low  = or_high - self._threshold * 3
        retest_zone_high = or_high + self._threshold
        in_zone = retest_zone_low <= price <= retest_zone_high

        if state.signal_fired_today or state.position_open:
            retest_text = "Entry triggered"
            phase = "ENTERED"
        elif not state.breakout_confirmed:
            if price < or_low:
                retest_text = "Below OR low — no long setup"
                phase = "BELOW_LOW"
            else:
                retest_text = "Waiting for breakout above OR high"
                phase = "PRE"
        elif in_zone:
            retest_text = f"Retesting OR high ({state.retest_candles}/{self._retest_candles})"
            phase = "RETEST"
        else:
            retest_text = "Broke out — awaiting retest"
            phase = "BREAKOUT"

        return MonitorStatus(
            symbol=symbol,
            price=round(price, 4),
            or_high=or_high,
            or_low=or_low,
            position_text=position_text,
            retest_text=retest_text,
            phase=phase,
        )

    def record_position_closed(self, symbol: str) -> None:
        """Call when a stop loss is hit externally."""
        state = self._state(symbol)
        state.position_open = False
