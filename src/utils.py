import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
from loguru import logger

from src.config import config

ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(ET)


def setup_logging() -> None:
    log_level = config.get("ui", "log_level", default="INFO")
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logger.remove()
    logger.add(sys.stdout, level=log_level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")

    if config.get("ui", "daily_log_files", default=True):
        log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        logger.add(str(log_file), level="DEBUG", rotation="00:00",
                   retention="30 days", encoding="utf-8",
                   format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")

    logger.info("Logging initialised — level={}, file={}", log_level,
                config.get("ui", "daily_log_files", default=True))


def is_trading_day() -> bool:
    allowed = config.get("trading", "trade_dow",
                         default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    return _now_et().strftime("%A") in allowed


def is_trading_hours() -> bool:
    now = _now_et().time()
    start = time.fromisoformat(config.get("trading", "start_time", default="09:30"))
    end = time.fromisoformat(config.get("trading", "end_time", default="16:00"))
    return start <= now <= end


def is_opening_range_window() -> bool:
    now = _now_et().time()
    start = time.fromisoformat(config.get("trading", "start_time", default="09:30"))
    duration = config.get("opening_range", "duration_minutes", default=15)
    from datetime import timedelta
    end_dt = datetime.combine(datetime.today(), start) + timedelta(minutes=duration)
    return start <= now <= end_dt.time()


def calc_commission(shares: float) -> float:
    per_share = config.get("commissions", "per_share", default=0.005)
    minimum = config.get("commissions", "min_per_order", default=1.0)
    return max(shares * per_share, minimum)
