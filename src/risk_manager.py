"""
Risk Manager — decides whether a trade is allowed and calculates position size.

Enforces:
  - max_open_positions
  - max_daily_loss_percent
  - max_position_usd (per symbol from universe.csv)
  - net P&L after commissions must be positive at minimum target
"""
from loguru import logger

from src.config import config
from src.utils import calc_commission


class RiskManager:
    def __init__(self, portfolio_value: float):
        self._portfolio_value = portfolio_value
        self._daily_loss = 0.0
        self._open_positions = 0
        self._max_positions = config.get("risk", "max_open_positions", default=4)
        self._max_daily_loss_pct = config.get("risk", "max_daily_loss_percent", default=3.0)
        self._risk_per_trade_pct = config.get("risk", "risk_per_trade_percent", default=0.75)

    # ------------------------------------------------------------------
    # State updates (called by execution module)
    # ------------------------------------------------------------------

    def record_position_opened(self) -> None:
        self._open_positions += 1

    def record_position_closed(self, pnl: float) -> None:
        self._open_positions = max(0, self._open_positions - 1)
        if pnl < 0:
            self._daily_loss += abs(pnl)

    def reset_day(self) -> None:
        self._daily_loss = 0.0
        self._open_positions = 0
        logger.info("Risk manager reset for new trading day")

    # ------------------------------------------------------------------
    # Approval check
    # ------------------------------------------------------------------

    def check_trade_allowed(
        self,
        entry_price: float,
        stop_price: float,
        max_position_usd: float,
    ) -> tuple[bool, str, int]:
        """
        Returns (allowed, reason, shares).
        shares is 0 if not allowed.
        """
        # 1. Daily loss limit
        loss_pct = (self._daily_loss / self._portfolio_value) * 100
        if loss_pct >= self._max_daily_loss_pct:
            return False, f"Daily loss limit reached ({loss_pct:.1f}%)", 0

        # 2. Max open positions
        if self._open_positions >= self._max_positions:
            return False, f"Max open positions reached ({self._open_positions})", 0

        # 3. Position sizing
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return False, "Stop price >= entry price", 0

        max_risk_usd = self._portfolio_value * (self._risk_per_trade_pct / 100)
        shares_by_risk = int(max_risk_usd / risk_per_share)
        shares_by_cap = int(max_position_usd / entry_price)
        shares = min(shares_by_risk, shares_by_cap)

        if shares < 1:
            return False, "Position size rounds to 0 shares", 0

        # 4. Commission check — ensure we're not trading for near-zero net gain
        commission = calc_commission(shares) * 2  # entry + exit
        gross_value = shares * entry_price
        if commission / gross_value > 0.02:  # commission > 2% of position = not worth it
            return False, f"Commission too high relative to position: ${commission:.2f}", 0

        logger.info("Risk approved: {} shares | risk/share=${:.2f} | max_risk=${:.2f} | commission=${:.2f}",
                    shares, risk_per_share, max_risk_usd, commission)
        return True, "OK", shares

    def update_portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    @property
    def open_positions(self) -> int:
        return self._open_positions
