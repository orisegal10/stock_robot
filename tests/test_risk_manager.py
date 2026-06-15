"""
Unit tests for RiskManager.
No IBKR connection required.
"""
import pytest
from src.risk_manager import RiskManager


PORTFOLIO = 50_000.0


@pytest.fixture
def risk():
    return RiskManager(portfolio_value=PORTFOLIO)


def test_trade_allowed_basic(risk):
    allowed, reason, shares = risk.check_trade_allowed(
        entry_price=100.0, stop_price=99.0, max_position_usd=10_000.0
    )
    assert allowed
    assert shares > 0


def test_trade_blocked_when_max_positions_reached(risk):
    for _ in range(4):
        risk.record_position_opened()
    allowed, reason, shares = risk.check_trade_allowed(100.0, 99.0, 10_000.0)
    assert not allowed
    assert "Max open positions" in reason


def test_trade_blocked_when_daily_loss_exceeded(risk):
    # Simulate 3% loss of portfolio = $1,500
    risk.record_position_closed(pnl=-1_600.0)
    allowed, reason, shares = risk.check_trade_allowed(100.0, 99.0, 10_000.0)
    assert not allowed
    assert "Daily loss limit" in reason


def test_trade_blocked_when_stop_above_entry(risk):
    allowed, reason, shares = risk.check_trade_allowed(
        entry_price=100.0, stop_price=101.0, max_position_usd=10_000.0
    )
    assert not allowed


def test_position_size_capped_by_max_usd(risk):
    # With max_position_usd=1000 and entry=100 → max 10 shares
    _, _, shares = risk.check_trade_allowed(100.0, 99.0, 1_000.0)
    assert shares <= 10


def test_position_size_respects_risk_percent(risk):
    # risk_per_trade = 0.75% of $50k = $375 max risk
    # risk per share = $100 - $99 = $1 → max 375 shares (but capped by max_position_usd)
    _, _, shares = risk.check_trade_allowed(100.0, 99.0, 50_000.0)
    expected_max = int((PORTFOLIO * 0.0075) / 1.0)  # 375
    assert shares <= expected_max


def test_reset_day_clears_state(risk):
    risk.record_position_opened()
    risk.record_position_opened()
    risk.record_position_closed(pnl=-500.0)
    risk.reset_day()
    assert risk.open_positions == 0
    assert risk.daily_loss == 0.0


def test_portfolio_value_update(risk):
    risk.update_portfolio_value(100_000.0)
    # With doubled portfolio, risk budget doubles
    _, _, shares_new = risk.check_trade_allowed(100.0, 99.0, 50_000.0)
    original_risk = RiskManager(portfolio_value=PORTFOLIO)
    _, _, shares_old = original_risk.check_trade_allowed(100.0, 99.0, 50_000.0)
    assert shares_new >= shares_old
