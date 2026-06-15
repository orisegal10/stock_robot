"""Loads and filters the stock universe from universe/universe.csv."""
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List

from loguru import logger

from src.config import config

UNIVERSE_PATH = Path("universe/universe.csv")


@dataclass
class UniverseStock:
    symbol: str
    volatility_hv: float
    stop_loss_pct: float
    atr_14: float
    min_volume: int
    rsi_14: float
    max_position_usd: float
    notes: str
    active: bool


def load_universe() -> List[UniverseStock]:
    if not UNIVERSE_PATH.exists():
        raise FileNotFoundError(f"Universe file not found: {UNIVERSE_PATH}")

    vol_min = config.get("filters", "volatility_min", default=70)
    vol_max = config.get("filters", "volatility_max", default=110)
    rsi_min = config.get("filters", "rsi_14_min", default=35)
    rsi_max = config.get("filters", "rsi_14_max", default=70)
    min_vol = config.get("filters", "min_avg_volume", default=500_000)
    min_price = config.get("filters", "min_price", default=5.0)
    max_price = config.get("filters", "max_price", default=500.0)

    stocks = []
    with open(UNIVERSE_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["active"].strip().lower() != "true":
                continue
            s = UniverseStock(
                symbol=row["symbol"].strip(),
                volatility_hv=float(row["volatility_hv"]),
                stop_loss_pct=float(row["stop_loss_pct"]),
                atr_14=float(row["atr_14"]),
                min_volume=int(row["min_volume"]),
                rsi_14=float(row["rsi_14"]),
                max_position_usd=float(row["max_position_usd"]),
                notes=row.get("notes", "").strip(),
                active=True,
            )
            # Apply global filters
            if not (vol_min <= s.volatility_hv <= vol_max):
                logger.debug("{} filtered out: volatility {}", s.symbol, s.volatility_hv)
                continue
            if not (rsi_min <= s.rsi_14 <= rsi_max):
                logger.debug("{} filtered out: RSI {}", s.symbol, s.rsi_14)
                continue
            stocks.append(s)

    logger.info("Universe loaded: {} active symbols", len(stocks))
    return stocks
