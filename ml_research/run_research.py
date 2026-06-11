"""
run_research.py — AutoML Research Pipeline entry point.

Usage:
    python ml_research/run_research.py
    python ml_research/run_research.py --symbols EURUSD GBPUSD XAUUSD
    python ml_research/run_research.py --symbols EURUSD --tf 5m --trials 50
    python ml_research/run_research.py --dry-run        # run but don't overwrite models

Workflow per symbol:
    1. Connect MT5
    2. Fetch + cache OHLCV with indicators (24h cache)
    3. Walk-Forward Optimization: 6m train / 1m val / 1m OOS, rolling monthly
    4. Per fold: Optuna HPO → backtest OOS with spread penalty
    5. Best model = highest OOS composite score
    6. Deploy: if ALL folds pass spread check → export to data/ml_model_{SYMBOL}.joblib
    7. Skip: if any fold fails → keep existing model, log reason

Output format matches strategy/ml_model.py loader:
    {"model": <fitted_sklearn_model>, "feature_cols": [...]}
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib

from core.connection.mt5_session import session

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("run_research")

DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DEFAULT_TF      = "5m"
MODEL_DIR       = Path("data")

# Spread estimates per symbol (pips) — used in backtest penalty
SPREAD_MAP: dict[str, float] = {
    "EURUSD": 1.5,
    "GBPUSD": 1.8,
    "USDJPY": 1.5,
    "USDCHF": 1.8,
    "AUDUSD": 1.8,
    "USDCAD": 2.0,
    "NZDUSD": 2.5,
    "EURJPY": 2.0,
    "GBPJPY": 2.5,
    "XAUUSD": 35.0,
    "DEFAULT": 2.0,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 AutoML Research Pipeline")
    parser.add_argument("--symbols",  nargs="+", default=DEFAULT_SYMBOLS,
                        help="Symbols to research (default: EURUSD GBPUSD USDJPY XAUUSD)")
    parser.add_argument("--tf",       default=DEFAULT_TF,
                        help="Timeframe (default: 5m)")
    parser.add_argument("--trials",   type=int, default=30,
                        help="Optuna trials per WFO fold (default: 30)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Run full pipeline but don't overwrite .joblib models")
    parser.add_argument("--refresh",  action="store_true",
                        help="Force re-fetch data even if cache is fresh")
    args = parser.parse_args()

    # Patch N_TRIALS before importing tuner
    import ml_research.hpo_tuner as _tuner
    _tuner.N_TRIALS = args.trials

    log.info("=" * 65)
    log.info("MT5 AutoML Research Pipeline")
    log.info("Symbols : %s", args.symbols)
    log.info("TF      : %s  |  Trials: %d  |  DryRun: %s", args.tf, args.trials, args.dry_run)
    log.info("=" * 65)

    log.info("Connecting to MT5 ...")
    if not session.connect():
        log.critical("Cannot connect to MT5 — aborting")
        sys.exit(1)

    MODEL_DIR.mkdir(exist_ok=True)

    results: dict[str, dict] = {}

    for symbol in args.symbols:
        log.info("")
        log.info("─" * 65)
        log.info("SYMBOL: %s", symbol)
        log.info("─" * 65)

        try:
            result = _process_symbol(symbol, args.tf, args.dry_run, args.refresh)
            results[symbol] = result
        except Exception as exc:
            log.error("FAILED %s: %s", symbol, exc, exc_info=True)
            results[symbol] = {"status": "ERROR", "reason": str(exc)}

    _print_summary(results)


def _process_symbol(
    symbol: str,
    timeframe: str,
    dry_run: bool,
    force_refresh: bool,
) -> dict:
    from ml_research.data_fetcher import fetch
    from ml_research.wfo_trainer import run_wfo

    # 1. Fetch data
    df = fetch(symbol, timeframe, force_refresh=force_refresh)
    log.info(
        "Data: %d candles  %s → %s",
        len(df),
        df.index[0].strftime("%Y-%m-%d"),
        df.index[-1].strftime("%Y-%m-%d"),
    )

    # 2. Run WFO
    spread = SPREAD_MAP.get(symbol, SPREAD_MAP["DEFAULT"])
    wfo    = run_wfo(df, symbol, spread_pips=spread)

    # 3. Decision
    if not wfo.passes_all or wfo.best_fold.model is None:
        reason = (
            "Not all OOS folds passed spread check"
            if not wfo.passes_all
            else "No valid model produced"
        )
        log.warning("❌ %s: SKIPPED — %s. Existing model kept.", symbol, reason)
        return {
            "status":      "SKIPPED",
            "reason":      reason,
            "mean_pf":     wfo.mean_oos_pf,
            "mean_wr":     wfo.mean_oos_wr,
            "mean_exp":    wfo.mean_oos_exp_pip,
            "folds":       len(wfo.folds),
        }

    # 4. Export
    out_path = MODEL_DIR / f"ml_model_{symbol.upper()}.joblib"
    payload  = {
        "model":        wfo.best_fold.model,
        "feature_cols": wfo.best_fold.feature_cols,
    }

    if not dry_run:
        joblib.dump(payload, out_path)
        log.info("✅ %s: Model deployed → %s", symbol, out_path)
        log.info(
            "   Best fold=%d composite=%.3f | mean OOS PF=%.2f WR=%.1f%% Exp=%.1fpip",
            wfo.best_fold.fold, wfo.best_fold.composite,
            wfo.mean_oos_pf, wfo.mean_oos_wr * 100, wfo.mean_oos_exp_pip,
        )
    else:
        log.info("⚠️  %s: DRY RUN — model NOT written (passed, composite=%.3f)",
                 symbol, wfo.best_fold.composite)

    return {
        "status":   "DEPLOYED" if not dry_run else "DRY_RUN",
        "mean_pf":  wfo.mean_oos_pf,
        "mean_wr":  wfo.mean_oos_wr,
        "mean_exp": wfo.mean_oos_exp_pip,
        "folds":    len(wfo.folds),
        "composite":wfo.best_fold.composite,
    }


def _print_summary(results: dict) -> None:
    log.info("")
    log.info("=" * 65)
    log.info("FINAL SUMMARY")
    log.info("=" * 65)
    header = f"{'Symbol':<10} {'Status':<10} {'Folds':>5} {'PF':>6} {'WR':>7} {'Exp(p)':>7} {'Score':>7}"
    log.info(header)
    log.info("-" * len(header))
    for sym, r in results.items():
        if r.get("status") == "ERROR":
            log.info("%-10s %-10s", sym, "ERROR")
            continue
        log.info(
            "%-10s %-10s %5d %6.2f %6.1f%% %7.1f %7.3f",
            sym,
            r.get("status", "-"),
            r.get("folds", 0),
            r.get("mean_pf", 0),
            r.get("mean_wr", 0) * 100,
            r.get("mean_exp", 0),
            r.get("composite", 0),
        )
    log.info("")


if __name__ == "__main__":
    main()
