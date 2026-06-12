"""
HPO Tuner — Optuna hyperparameter optimization for one WFO fold.

Model candidates: RandomForest (always available), LightGBM (if installed).
Optuna is optional — falls back to default RF params if not installed.

Objective per trial: F1-score on class 1 (bullish) on val set.
F1 chosen over accuracy: class imbalance is common (fewer buy signals than no-signal),
accuracy would be misleading. F1 optimizes precision + recall of actual signals.

After HPO: retrain on train+val combined using best params before returning.
WFO eval (OOS backtest) happens externally in wfo_trainer.py.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from config import settings

log = logging.getLogger(__name__)

N_TRIALS = 30   # Optuna trials per fold — increase for better tuning at cost of time


def tune(
    df_train: pd.DataFrame,
    df_val: pd.DataFrame,
    symbol: str,
) -> tuple[Any, list[str]]:
    """
    HPO on df_train, eval on df_val, retrain on combined.
    Returns (fitted_model, feature_cols).
    """
    feature_cols = _select_feature_cols(df_train)
    X_train, y_train = _build_xy(df_train, feature_cols)
    X_val,   y_val   = _build_xy(df_val,   feature_cols)

    if len(X_train) < 50 or y_train.nunique() < 2:
        log.warning(
            "%s: insufficient data (%d rows, %d classes) — using default RF",
            symbol, len(X_train), y_train.nunique(),
        )
        return _fit_default_rf(X_train, y_train), feature_cols

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        has_optuna = True
    except ImportError:
        log.warning("Optuna not installed — using default RF (pip install optuna)")
        has_optuna = False

    if has_optuna:
        import optuna
        from sklearn.metrics import f1_score

        def objective(trial: optuna.Trial) -> float:
            model = _build_trial_model(trial)
            model.fit(X_train, y_train)
            y_pred = model.predict(X_val)
            return f1_score(y_val, y_pred, pos_label=1, zero_division=0)

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=False)
        best_params = study.best_params
        log.info(
            "%s HPO done: best_val_f1=%.3f params=%s",
            symbol, study.best_value, best_params,
        )
    else:
        best_params = None

    # Retrain on train + val combined for maximum data
    df_full  = pd.concat([df_train, df_val])
    X_full, y_full = _build_xy(df_full, feature_cols)

    if best_params is not None:
        model = _build_model_from_params(best_params)
    else:
        model = _build_default_rf()

    model.fit(X_full, y_full)
    return model, feature_cols


# ── Model builders ────────────────────────────────────────────────────────

def _build_trial_model(trial) -> Any:
    """Build a model from an Optuna trial."""
    model_type = trial.suggest_categorical("model_type", ["rf", "lgbm"])

    if model_type == "lgbm":
        try:
            import lightgbm as lgb
            return lgb.LGBMClassifier(
                n_estimators     = trial.suggest_int("n_estimators", 50, 300),
                max_depth        = trial.suggest_int("max_depth", 2, 8),
                num_leaves       = trial.suggest_int("num_leaves", 15, 63),
                learning_rate    = trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
                min_child_samples= trial.suggest_int("min_child_samples", 10, 50),
                random_state     = 42,
                verbose          = -1,
                n_jobs           = -1,
            )
        except ImportError:
            pass  # LightGBM not installed → fall through to RF

    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(
        n_estimators    = trial.suggest_int("n_estimators", 50, 300),
        max_depth       = trial.suggest_int("max_depth", 2, 8),
        min_samples_leaf= trial.suggest_int("min_samples_leaf", 10, 50),
        max_features    = trial.suggest_categorical("max_features", ["sqrt", "log2"]),
        random_state    = 42,
        n_jobs          = -1,
    )


def _build_model_from_params(params: dict) -> Any:
    """Reconstruct a model from best_params dict."""
    p = dict(params)
    model_type = p.pop("model_type", "rf")

    if model_type == "lgbm":
        try:
            import lightgbm as lgb
            return lgb.LGBMClassifier(**p, random_state=42, verbose=-1, n_jobs=-1)
        except (ImportError, TypeError):
            pass

    from sklearn.ensemble import RandomForestClassifier
    # Drop LightGBM-only params that RF doesn't accept
    for key in ("learning_rate", "num_leaves", "min_child_samples"):
        p.pop(key, None)
    try:
        return RandomForestClassifier(**p, random_state=42, n_jobs=-1)
    except TypeError:
        return _build_default_rf()


def _build_default_rf() -> Any:
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(
        n_estimators=100, max_depth=4, min_samples_leaf=20,
        max_features="sqrt", random_state=42, n_jobs=-1,
    )


def _fit_default_rf(X, y) -> Any:
    model = _build_default_rf()
    model.fit(X, y)
    return model


# ── Feature & target helpers ──────────────────────────────────────────────

def _select_feature_cols(df: pd.DataFrame) -> list[str]:
    """Base settings features + non-zero CDL columns present in df."""
    base = list(settings.ML_FEATURE_COLUMNS)
    cdl  = [
        c for c in df.columns
        if c.startswith("CDL_") and df[c].abs().sum() > 0
    ]
    return [c for c in base + cdl if c in df.columns]


def _build_xy(df: pd.DataFrame, feature_cols: list[str]) -> tuple[pd.DataFrame, pd.Series]:
    """
    Build feature matrix + ATR-scaled binary target.
    Target: price rises by >= 1.0×ATR in next ML_TARGET_LOOKAHEAD_PERIODS candles.
    Features use current candle data — execution on next candle open (handled in backtest).
    """
    df = df.copy()
    lookahead = settings.ML_TARGET_LOOKAHEAD_PERIODS

    atr = df.get("ATRr_14", pd.Series(dtype=float))
    if atr.isna().all() or (atr == 0).all():
        df["_target"] = np.where(df["close"].shift(-lookahead) > df["close"], 1, 0)
    else:
        future_change = df["close"].shift(-lookahead) - df["close"]
        df["_target"] = np.where(future_change >= atr * 1.0, 1, 0)

    df = df.iloc[:-lookahead].copy()
    df = df.dropna(subset=feature_cols + ["_target"])

    X = df.reindex(columns=feature_cols, fill_value=0).fillna(0)
    y = df["_target"]

    return X, y
