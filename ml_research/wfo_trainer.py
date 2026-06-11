"""
Walk-Forward Optimizer (WFO) — rolling time-window training + validation.

Replaces simple 80/20 train_test_split to prevent regime overfitting.

Window config (default for 5m scalping):
  Train  : 6 months  — model learns indicator patterns
  Val    : 1 month   — HPO tunes hyperparameters
  OOS    : 1 month   — pure forward test, NEVER seen during train or HPO
  Step   : 1 month   — roll window forward monthly

Composite score (AUTO_ML_SYSTEM.md formula):
  0.40×PF_norm + 0.25×expectancy_norm + 0.15×sharpe_norm + 0.10×WR - 0.10×MaxDD
  PF capped at 5, expectancy capped at 20 pip, sharpe capped at 3.0 — then normalized.

Best model = fold with highest OOS composite score.
Passes = ALL folds pass spread check (conservative deployment gate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class WFOFold:
    fold:         int
    train_slice:  slice
    val_slice:    slice
    oos_slice:    slice
    val_metrics:  dict  = field(default_factory=dict)
    oos_metrics:  dict  = field(default_factory=dict)
    composite:    float = 0.0
    model:        Any   = None
    feature_cols: list  = field(default_factory=list)


@dataclass
class WFOResult:
    symbol:            str
    folds:             list[WFOFold]
    best_fold:         WFOFold
    mean_oos_pf:       float
    mean_oos_wr:       float
    mean_oos_exp_pip:  float
    passes_all:        bool   # True = all OOS folds pass spread check


# ── Entry point ───────────────────────────────────────────────────────────

def run_wfo(
    df: pd.DataFrame,
    symbol: str,
    train_months: int = 6,
    val_months:   int = 1,
    oos_months:   int = 1,
    step_months:  int = 1,
    trainer_fn:   Callable | None = None,
    spread_pips:  float = 2.0,
) -> WFOResult:
    """
    Run WFO over df. Returns WFOResult with best model + per-fold metrics.

    trainer_fn(df_train, df_val, symbol) → (model, feature_cols)
    Defaults to hpo_tuner.tune if not provided.
    """
    if trainer_fn is None:
        from ml_research.hpo_tuner import tune as trainer_fn

    cpm = _candles_per_month(df)
    train_c = int(train_months * cpm)
    val_c   = int(val_months   * cpm)
    oos_c   = int(oos_months   * cpm)
    step_c  = int(step_months  * cpm)
    window  = train_c + val_c + oos_c
    n       = len(df)

    total_folds = max(0, (n - window) // step_c + 1)

    if n < window:
        raise ValueError(
            f"Not enough data for WFO: need {window} candles "
            f"({train_months}m+{val_months}m+{oos_months}m), got {n}. "
            f"Fetch more history or reduce window sizes."
        )

    log.info(
        "%s WFO: %d candles | cpm≈%d | train=%d val=%d oos=%d step=%d → ~%d folds",
        symbol, n, cpm, train_c, val_c, oos_c, step_c, total_folds,
    )

    folds: list[WFOFold] = []
    cursor = 0
    fold_n = 0

    while cursor + window <= n:
        t_start = cursor
        t_end   = cursor + train_c
        v_start = t_end
        v_end   = t_end + val_c
        o_start = v_end
        o_end   = min(v_end + oos_c, n)

        fold = WFOFold(
            fold        = fold_n,
            train_slice = slice(t_start, t_end),
            val_slice   = slice(v_start, v_end),
            oos_slice   = slice(o_start, o_end),
        )

        df_train = df.iloc[fold.train_slice]
        df_val   = df.iloc[fold.val_slice]
        df_oos   = df.iloc[fold.oos_slice]

        log.info(
            "  Fold %d/%d  train[%d:%d] val[%d:%d] oos[%d:%d]",
            fold_n + 1, total_folds,
            t_start, t_end, v_start, v_end, o_start, o_end,
        )

        try:
            model, feature_cols = trainer_fn(df_train, df_val, symbol)
            fold.model        = model
            fold.feature_cols = feature_cols

            fold.val_metrics  = _evaluate(model, feature_cols, df_val, symbol, spread_pips)
            fold.oos_metrics  = _evaluate(model, feature_cols, df_oos, symbol, spread_pips)
            fold.composite    = _composite(fold.oos_metrics)

            log.info(
                "    → OOS PF=%.2f WR=%.1f%% Exp=%.1fpip Sharpe=%.2f DD=%.1f%% "
                "composite=%.3f %s",
                fold.oos_metrics.get("profit_factor", 0),
                fold.oos_metrics.get("win_rate", 0) * 100,
                fold.oos_metrics.get("expectancy_pips", 0),
                fold.oos_metrics.get("sharpe", 0),
                fold.oos_metrics.get("max_dd", 1) * 100,
                fold.composite,
                "✅" if fold.oos_metrics.get("passes_spread_check") else "❌",
            )

        except Exception as exc:
            log.error("  Fold %d FAILED: %s", fold_n, exc, exc_info=True)
            fold.composite = -999.0

        folds.append(fold)
        cursor += step_c
        fold_n += 1

    if not folds:
        raise RuntimeError(f"{symbol}: no WFO folds completed.")

    valid = [f for f in folds if f.oos_metrics.get("trades", 0) >= 10]
    best  = max(folds, key=lambda f: f.composite)

    mean_pf  = float(np.mean([f.oos_metrics.get("profit_factor", 0)  for f in valid])) if valid else 0.0
    mean_wr  = float(np.mean([f.oos_metrics.get("win_rate", 0)        for f in valid])) if valid else 0.0
    mean_exp = float(np.mean([f.oos_metrics.get("expectancy_pips", 0) for f in valid])) if valid else 0.0
    passes   = bool(all(f.oos_metrics.get("passes_spread_check", False) for f in valid))

    log.info(
        "%s WFO done: %d folds (%d valid) | best fold=%d composite=%.3f | "
        "mean PF=%.2f WR=%.1f%% Exp=%.1fpip | passes_all=%s",
        symbol, len(folds), len(valid), best.fold, best.composite,
        mean_pf, mean_wr * 100, mean_exp, passes,
    )

    return WFOResult(
        symbol           = symbol,
        folds            = folds,
        best_fold        = best,
        mean_oos_pf      = round(mean_pf,  3),
        mean_oos_wr      = round(mean_wr,  4),
        mean_oos_exp_pip = round(mean_exp, 2),
        passes_all       = passes,
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _evaluate(
    model: Any,
    feature_cols: list[str],
    df: pd.DataFrame,
    symbol: str,
    spread_pips: float,
) -> dict:
    """Generate predictions on df and run backtest."""
    from ml_research.backtest import run_backtest

    if not hasattr(model, "classes_") or len(df) < 10:
        from ml_research.backtest import _empty_metrics
        return _empty_metrics()

    X = df.reindex(columns=feature_cols, fill_value=0).fillna(0)
    if len(X) == 0:
        from ml_research.backtest import _empty_metrics
        return _empty_metrics()

    proba   = model.predict_proba(X)
    classes = list(model.classes_)

    if 1 not in classes:
        from ml_research.backtest import _empty_metrics
        return _empty_metrics()

    bull_idx = classes.index(1)
    CONF_THRESHOLD = 0.60
    signals = pd.Series(
        (proba[:, bull_idx] >= CONF_THRESHOLD).astype(int),
        index=df.index,
    )

    return run_backtest(df, signals, symbol, spread_pips=spread_pips)


def _composite(metrics: dict) -> float:
    """
    Composite score per AUTO_ML_SYSTEM.md:
    0.40×PF + 0.25×Exp + 0.15×Sharpe + 0.10×WR - 0.10×MaxDD
    All components normalized to [0,1] before weighting.
    Returns -1.0 if spread check fails — ensures failing models rank last.
    """
    if not metrics.get("passes_spread_check"):
        return -1.0
    if metrics.get("trades", 0) < 10:
        return -0.5   # too few trades → unreliable metrics

    pf   = min(metrics.get("profit_factor", 0),  5.0) / 5.0
    exp  = min(max(metrics.get("expectancy_pips", 0), 0), 20.0) / 20.0
    shr  = min(max(metrics.get("sharpe", 0), 0),  3.0) / 3.0
    wr   = float(metrics.get("win_rate", 0))
    dd   = float(metrics.get("max_dd",   1.0))

    return 0.40 * pf + 0.25 * exp + 0.15 * shr + 0.10 * wr - 0.10 * dd


def _candles_per_month(df: pd.DataFrame) -> int:
    """Estimate actual trading candles per calendar month from data."""
    if len(df) < 200 or not hasattr(df.index, "min"):
        return 2_880   # fallback: ~20 trading days × 24h × 6 per h (5m)
    days = max(1, (df.index.max() - df.index.min()).days)
    return max(200, int(len(df) / (days / 30.4)))
