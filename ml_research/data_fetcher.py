"""
DataFetcher — fetch and cache OHLCV + indicators from MT5.

Cache TTL: 24 hours. Fetches once per day, reuses CSV otherwise.
Needs enough history for WFO: at minimum 8 months (6m train + 1m val + 1m OOS).
Default fetch: ~13 months of 5m candles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from core.connection.mt5_session import session
from strategy.indicators import IndicatorManager

log = logging.getLogger(__name__)

CACHE_DIR       = Path("ml_research/data")
CACHE_TTL_HOURS = 24
DEFAULT_CANDLES = 65_000   # ~13.5 months of 5m (includes weekends → actual ~10 months trading)

_TF_MAP: dict[str, int] = {
    "1m":  mt5.TIMEFRAME_M1,
    "5m":  mt5.TIMEFRAME_M5,
    "15m": mt5.TIMEFRAME_M15,
    "30m": mt5.TIMEFRAME_M30,
    "1h":  mt5.TIMEFRAME_H1,
    "4h":  mt5.TIMEFRAME_H4,
    "1d":  mt5.TIMEFRAME_D1,
}


def fetch(
    symbol: str,
    timeframe: str = "5m",
    n_candles: int = DEFAULT_CANDLES,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Return OHLCV + indicators DataFrame. Uses disk cache if fresh.

    Raises RuntimeError if MT5 returns no data.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{symbol.upper()}_{timeframe}.csv"

    if not force_refresh and cache_path.exists():
        mtime   = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
        age_h   = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        if age_h < CACHE_TTL_HOURS:
            log.info("Cache hit: %s %s (age %.1fh)", symbol, timeframe, age_h)
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df

    tf_int = _TF_MAP.get(timeframe)
    if tf_int is None:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Valid: {list(_TF_MAP)}")

    log.info("Fetching %d candles: %s %s ...", n_candles, symbol, timeframe)
    rates = session.get_rates(symbol, tf_int, n_candles)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"MT5 returned no data for {symbol} {timeframe}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")[["open", "high", "low", "close", "tick_volume"]]
    df = df.rename(columns={"tick_volume": "volume"})

    log.info("Computing indicators on %d candles ...", len(df))
    df = IndicatorManager.add_indicators(df)

    df.to_csv(cache_path)
    log.info("Cached: %s (%d rows)", cache_path, len(df))

    return df
