import os
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

from config import settings

# Minimum prediction confidence — below this returns "neutral"
CONFIDENCE_THRESHOLD = 0.60


class MLModel:
    def __init__(self, symbol: str | None = None) -> None:
        if symbol:
            # Ensure data directory exists
            os.makedirs("data", exist_ok=True)
            self.model_path = f"data/ml_model_{symbol.upper()}.joblib"
        else:
            self.model_path = settings.ML_MODEL_PATH
            
        self._feature_cols: list[str] = []  # populated after train
        self.model = self._load_model()

    def _load_model(self) -> Any:
        if os.path.exists(self.model_path):
            try:
                payload = joblib.load(self.model_path)
                if isinstance(payload, dict):
                    self._feature_cols = payload.get("feature_cols", [])
                    return payload["model"]
                return payload  # legacy format
            except Exception:
                pass
        return RandomForestClassifier(
            n_estimators=settings.ML_RANDOM_FOREST_ESTIMATORS,
            max_depth=4,
            min_samples_leaf=20,
            max_features="sqrt",
            random_state=42,
        )

    def _select_features(self, df: pd.DataFrame) -> list[str]:
        """Base features + non-zero CDL columns only."""
        base = list(settings.ML_FEATURE_COLUMNS)
        cdl_cols = [
            col for col in df.columns
            if col.startswith("CDL_") and df[col].abs().sum() > 0
        ]
        return base + cdl_cols

    def prepare_features(self, df: pd.DataFrame, feature_cols: list[str] | None = None) -> pd.DataFrame:
        cols = feature_cols or self._feature_cols or self._select_features(df)
        return df.reindex(columns=cols, fill_value=0).fillna(0)

    def train(self, df: pd.DataFrame) -> dict:
        """
        Train with 80/20 split. Target: price rises by ≥ 0.5×ATR in next N candles.
        Returns metrics dict for reporting.
        """
        df = df.copy()
        lookahead = settings.ML_TARGET_LOOKAHEAD_PERIODS

        # ATR-scaled target — filters noise better than raw up/down
        atr = df.get("ATRr_14", pd.Series(dtype=float))
        if atr.isna().all() or (atr == 0).all():
            # Fallback: simple directional target
            df["target"] = np.where(df["close"].shift(-lookahead) > df["close"], 1, 0)
        else:
            min_move = atr * 0.5
            future_change = df["close"].shift(-lookahead) - df["close"]
            df["target"] = np.where(future_change >= min_move, 1, 0)

        df_train = df[:-lookahead].copy()
        self._feature_cols = self._select_features(df_train)

        X = self.prepare_features(df_train, self._feature_cols)
        y = df_train["target"]

        if len(X) < 50:
            return {"error": "Not enough data (need ≥ 50 rows after indicators)."}

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False  # no shuffle — time series
        )

        self.model = RandomForestClassifier(
            n_estimators=settings.ML_RANDOM_FOREST_ESTIMATORS,
            max_depth=4,           # shallow — prevent memorizing
            min_samples_leaf=20,   # each leaf needs ≥20 samples
            max_features="sqrt",   # use sqrt(n_features) per split
            random_state=42,
        )
        self.model.fit(X_train, y_train)

        # Evaluate
        y_pred  = self.model.predict(X_test)
        acc     = accuracy_score(y_test, y_pred)
        report  = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

        # Feature importance — top 10
        importances = sorted(
            zip(self._feature_cols, self.model.feature_importances_),
            key=lambda x: x[1], reverse=True,
        )[:10]

        # Save model + feature list together
        joblib.dump({"model": self.model, "feature_cols": self._feature_cols}, self.model_path)
        print(f"Model trained. Accuracy: {acc:.2%}  Train: {len(X_train)}  Test: {len(X_test)}")

        return {
            "train_size":    len(X_train),
            "test_size":     len(X_test),
            "accuracy":      round(acc, 4),
            "precision_1":   round(report.get("1", {}).get("precision", 0), 4),
            "recall_1":      round(report.get("1", {}).get("recall", 0), 4),
            "feature_count": len(self._feature_cols),
            "top_features":  importances,
        }

    def predict(self, df: pd.DataFrame) -> str:
        if not hasattr(self.model, "classes_"):
            return "neutral"

        X = self.prepare_features(df.tail(1))
        proba = self.model.predict_proba(X)[0]
        classes = list(self.model.classes_)

        if 1 in classes:
            bull_prob = proba[classes.index(1)]
            bear_prob = proba[classes.index(0)] if 0 in classes else 0.0
        else:
            return "neutral"

        if bull_prob >= CONFIDENCE_THRESHOLD:
            return "bullish"
        if bear_prob >= CONFIDENCE_THRESHOLD:
            return "bearish"
        return "neutral"
