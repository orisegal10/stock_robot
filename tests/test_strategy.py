"""
Unit tests for the ORB strategy engine.
No IBKR connection required — uses synthetic price sequences.
"""
import pytest
from src.strategy import StrategyEngine, Action


OR_HIGH = 100.0
OR_LOW = 98.0
STOP_PCT = 1.0   # 1% stop loss


@pytest.fixture
def engine():
    return StrategyEngine()


def test_no_signal_before_breakout(engine):
    # Price below OR high → no signal
    result = engine.evaluate("AAPL", 99.0, OR_HIGH, OR_LOW, STOP_PCT)
    assert result is None


def test_no_signal_exactly_at_or_high(engine):
    result = engine.evaluate("AAPL", 100.0, OR_HIGH, OR_LOW, STOP_PCT)
    assert result is None


def test_breakout_detected_no_immediate_signal(engine):
    # Price above OR high but no retest yet
    result = engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    assert result is None   # breakout detected, waiting for retest


def test_signal_after_retest(engine):
    # 1. Breakout
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    # 2. Retest — price dips back to OR high zone
    for _ in range(6):
        engine.evaluate("AAPL", 100.02, OR_HIGH, OR_LOW, STOP_PCT)
    # 3. Price moves above → signal should fire
    result = engine.evaluate("AAPL", 100.15, OR_HIGH, OR_LOW, STOP_PCT)
    assert result is not None
    assert result.action == Action.LONG
    assert result.symbol == "AAPL"
    assert result.or_high == OR_HIGH
    assert result.stop_price < result.entry_price


def test_signal_fired_only_once_per_day(engine):
    # Trigger a signal
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    for _ in range(6):
        engine.evaluate("AAPL", 100.02, OR_HIGH, OR_LOW, STOP_PCT)
    first = engine.evaluate("AAPL", 100.15, OR_HIGH, OR_LOW, STOP_PCT)
    # Second potential signal — must be None
    second = engine.evaluate("AAPL", 100.50, OR_HIGH, OR_LOW, STOP_PCT)
    assert first is not None
    assert second is None


def test_reset_allows_new_signal(engine):
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    for _ in range(6):
        engine.evaluate("AAPL", 100.02, OR_HIGH, OR_LOW, STOP_PCT)
    engine.evaluate("AAPL", 100.15, OR_HIGH, OR_LOW, STOP_PCT)

    engine.reset_day()

    # After reset a new signal can fire
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    for _ in range(6):
        engine.evaluate("AAPL", 100.02, OR_HIGH, OR_LOW, STOP_PCT)
    result = engine.evaluate("AAPL", 100.15, OR_HIGH, OR_LOW, STOP_PCT)
    assert result is not None


def test_no_signal_without_or_data(engine):
    result = engine.evaluate("AAPL", 100.0, None, None, STOP_PCT)
    assert result is None


def test_stop_price_calculated_correctly(engine):
    entry = 100.15
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    for _ in range(6):
        engine.evaluate("AAPL", 100.02, OR_HIGH, OR_LOW, STOP_PCT)
    result = engine.evaluate("AAPL", entry, OR_HIGH, OR_LOW, STOP_PCT)
    if result:
        expected_stop = round(result.entry_price * (1 - STOP_PCT / 100), 2)
        assert result.stop_price == expected_stop


def test_multiple_symbols_independent(engine):
    # AAPL breaks out
    engine.evaluate("AAPL", 100.10, OR_HIGH, OR_LOW, STOP_PCT)
    # MSFT should still be in HOLD
    result_msft = engine.evaluate("MSFT", 99.0, OR_HIGH, OR_LOW, STOP_PCT)
    assert result_msft is None
