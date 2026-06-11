"""
Backtest engine — vectorized simulation with spread + slippage penalty.

Key rules (from AUTO_ML_SYSTEM.md Section 8):
  1. No look-ahead: enter at OPEN of candle AFTER signal candle.
  2. Spread penalty: expectancy must be >= SPREAD_REJECT_MULT × spread_pips.
     If not → model rejected, NOT deployed to production.
  3. Slippage: +0.5 pip added to spread at entry (conservative).

Trade logic (BUY signals only — strategy is long-only for simplicity):
  Entry  : open[i+1] + spread + slippage
  SL     : entry - ATR × sl_mult
  TP     : entry + ATR × tp_mult
  Resolve: scan forward up to max_candles, check low ≤ SL or high ≥ TP
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config import settings

log = logging.getLogger(__name__)

SPREAD_REJECT_MULT = 1.5    # expectancy must beat 1.5× spread to deploy
SLIPPAGE_PIPS      = 0.5    # conservative slippage estimate
MAX_CANDLES_HOLD   = 12     # max candles to resolve a trade before closing flat


def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,       # index-aligned with df, 1 = bullish signal
    symbol: str,
    spread_pips: float = 2.0,
    sl_mult: float | None = None,
    tp_mult: float | None = None,
) -> dict:
    """
    Vectorized backtest. Returns metrics dict.

    signals: 1 = enter BUY on next candle open, 0 = no trade.
    All index positions must align with df.
    """
    sl_m = sl_mult if sl_mult is not None else settings.ATR_SL_MULTIPLIER
    tp_m = tp_mult if tp_mult is not None else settings.ATR_TP_MULTIPLIER

    pip_size = _pip_size(symbol)
    total_cost_price = (spread_pips + SLIPPAGE_PIPS) * pip_size

    df = df.copy().reset_index(drop=True)
    sig = signals.values if hasattr(signals, "values") else np.array(signals)

    pnl_list: list[float] = []

    for i in range(len(df) - 1):
        if sig[i] != 1:
            continue

        # Enter at next candle open + spread + slippage
        entry = float(df["open"].iloc[i + 1]) + total_cost_price
        atr   = float(df["ATRr_14"].iloc[i]) if "ATRr_14" in df.columns else 0.0

        if atr <= 0 or np.isnan(atr) or np.isnan(entry):
            continue

        sl = entry - sl_m * atr
        tp = entry + tp_m * atr

        outcome = _resolve(df, i + 1, entry, sl, tp, MAX_CANDLES_HOLD)
        if outcome is not None:
            pnl_list.append(outcome)

    return _compute_metrics(pnl_list, symbol, spread_pips, pip_size)


def _resolve(
    df: pd.DataFrame,
    start: int,
    entry: float,
    sl: float,
    tp: float,
    max_candles: int,
) -> float | None:
    """
    Scan forward from `start`. Return PnL in price units on first SL/TP hit.
    Return None if unresolved within max_candles.
    """
    end = min(start + max_candles, len(df))
    for i in range(start, end):
        low  = float(df["low"].iloc[i])
        high = float(df["high"].iloc[i])
        # Check SL first (conservative — assumes worst-case wick order)
        if low <= sl:
            return sl - entry
        if high >= tp:
            return tp - entry
    return None


def _compute_metrics(
    pnl: list[float],
    symbol: str,
    spread_pips: float,
    pip_size: float,
) -> dict:
    if not pnl or len(pnl) < 5:
        return _empty_metrics()

    arr   = np.array(pnl)
    wins  = arr[arr > 0]
    losses= arr[arr < 0]

    n           = len(arr)
    win_rate    = len(wins) / n
    avg_win     = float(wins.mean())  if len(wins)   else 0.0
    avg_loss    = float(losses.mean()) if len(losses) else 0.0
    gross_profit= float(wins.sum())   if len(wins)   else 0.0
    gross_loss  = abs(float(losses.sum())) if len(losses) else 1e-9

    profit_factor  = gross_profit / gross_loss
    expectancy_pip = ((win_rate * avg_win) + ((1 - win_rate) * avg_loss)) / pip_size
    sharpe         = _sharpe(arr)
    max_dd         = _max_drawdown(np.cumsum(arr))
    passes         = expectancy_pip >= SPREAD_REJECT_MULT * spread_pips

    if not passes:
        log.warning(
            "Backtest REJECT %s: expectancy=%.2f pip < threshold=%.1f pip "
            "(%.1f × %.1f spread)",
            symbol, expectancy_pip, SPREAD_REJECT_MULT * spread_pips,
            SPREAD_REJECT_MULT, spread_pips,
        )

    return {
        "trades":              n,
        "win_rate":            round(win_rate, 4),
        "profit_factor":       round(profit_factor, 3),
        "expectancy_pips":     round(expectancy_pip, 2),
        "sharpe":              round(sharpe, 3),
        "max_dd":              round(max_dd, 4),
        "passes_spread_check": passes,
    }


def _empty_metrics() -> dict:
    return {
        "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
        "expectancy_pips": 0.0, "sharpe": 0.0, "max_dd": 1.0,
        "passes_spread_check": False,
    }


def _pip_size(symbol: str) -> float:
    sym = symbol.upper()
    if "JPY" in sym:
        return 0.01
    if "XAU" in sym:
        return 0.01
    return 0.0001


def _sharpe(returns: np.ndarray, periods_per_year: int = 105_120) -> float:
    """Annualized Sharpe. 105_120 = 252 trading days × 6.5h × 12 per h (5m bars)."""
    std = returns.std()
    if std == 0:
        return 0.0
    return float((returns.mean() / std) * np.sqrt(periods_per_year))


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 1.0
    peak = np.maximum.accumulate(equity)
    dd   = np.where(peak > 0, (peak - equity) / peak, 0.0)
    return float(dd.max())
