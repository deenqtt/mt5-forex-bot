from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from config import settings


class IndicatorManager:
    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df

        df["ema_20"] = ta.ema(df["close"], length=20)
        df["ema_50"] = ta.ema(df["close"], length=50)
        df["rsi"] = ta.rsi(df["close"], length=14)

        bbands = ta.bbands(df["close"], length=20, std=2)
        df = pd.concat([df, bbands], axis=1)

        macd = ta.macd(df["close"])
        df = pd.concat([df, macd], axis=1)

        # ADX: adds ADX_14, DMP_14, DMN_14
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=settings.ADX_LENGTH)
        df = pd.concat([df, adx_df], axis=1)

        # ATR: adds ATRr_14 (Wilder's RMA)
        df["ATRr_14"] = ta.atr(df["high"], df["low"], df["close"], length=settings.ATR_LENGTH)
        df["atr_ma20"] = df["ATRr_14"].rolling(20).mean()

        df.ta.cdl_pattern(name="all", append=True)

        return df

    @staticmethod
    def get_latest_signals(df: pd.DataFrame) -> dict | None:
        if df.empty or len(df) < 2:
            return None

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # Guard: critical indicators must not be NaN.
        # EMA-50 needs 50 candles, ADX needs ~27, RSI needs 14.
        # If any is NaN, data is insufficient — return None so caller skips this symbol.
        critical = ["ema_20", "ema_50", "rsi", "ADX_14", "ATRr_14"]
        if any(pd.isna(latest.get(col)) for col in critical):
            return None

        ema_20: float = float(latest["ema_20"])
        ema_50: float = float(latest["ema_50"])

        # Current trend state — not just the crossover moment
        if ema_20 > ema_50:
            ema_trend = "bullish"
        elif ema_20 < ema_50:
            ema_trend = "bearish"
        else:
            ema_trend = "ranging"

        # Crossover event — only True on the candle where the cross happened
        ema_cross_event: str
        if latest["ema_20"] > latest["ema_50"] and prev["ema_20"] <= prev["ema_50"]:
            ema_cross_event = "bullish_cross"
        elif latest["ema_20"] < latest["ema_50"] and prev["ema_20"] >= prev["ema_50"]:
            ema_cross_event = "bearish_cross"
        else:
            ema_cross_event = "none"

        macd_hist: float = float(latest.get("MACDh_12_26_9", 0.0))
        prev_macd_hist: float = float(prev.get("MACDh_12_26_9", 0.0))

        if macd_hist > 0 and prev_macd_hist <= 0:
            macd_signal = "bullish"
        elif macd_hist < 0 and prev_macd_hist >= 0:
            macd_signal = "bearish"
        elif macd_hist > prev_macd_hist:
            macd_signal = "rising"
        elif macd_hist < prev_macd_hist:
            macd_signal = "falling"
        else:
            macd_signal = "neutral"

        # Bollinger Band percent B: 0 = at lower band, 1 = at upper band
        bb_percent: float = float(latest.get("BBP_20_2.0", 0.5))

        # ADX regime detection
        adx_val: float = float(latest.get("ADX_14", 0.0))
        dmp_val: float = float(latest.get("DMP_14", 0.0))
        dmn_val: float = float(latest.get("DMN_14", 0.0))
        prev_adx: float = float(prev.get("ADX_14", 0.0))
        adx_rising: bool = adx_val > prev_adx

        if adx_val >= settings.ADX_TRENDING_THRESHOLD and adx_rising:
            regime = "trending"
        elif adx_val >= settings.ADX_RANGING_THRESHOLD:
            regime = "transition"
        else:
            regime = "ranging"

        # ATR
        atr_val: float = float(latest.get("ATRr_14", 0.0))
        atr_ma: float = float(latest.get("atr_ma20", atr_val))
        atr_expanding: bool = atr_val > atr_ma if atr_ma > 0 else False

        active_patterns: list[str] = [
            col.replace("CDL_", "")
            for col in df.columns
            if col.startswith("CDL_") and latest[col] != 0
        ]

        return {
            "ema_20":          ema_20,
            "ema_50":          ema_50,
            "ema_trend":       ema_trend,
            "ema_cross_event": ema_cross_event,
            "rsi":             float(latest["rsi"]),
            "macd_hist":       macd_hist,
            "macd_signal":     macd_signal,
            "bb_percent":      bb_percent,
            "patterns":        active_patterns,
            # Regime
            "regime":          regime,
            "adx":             adx_val,
            "dmp":             dmp_val,
            "dmn":             dmn_val,
            "adx_rising":      adx_rising,
            "atr":             atr_val,
            "atr_expanding":   atr_expanding,
        }

    @staticmethod
    def get_htf_bias(df: pd.DataFrame) -> dict:
        """
        Higher-timeframe bias summary (designed for 4h df).
        Returns: bias ("bullish"|"bearish"|"neutral"), regime, adx, ema_trend, dmp, dmn.
        Used as a filter — only enter 1h trades when HTF bias agrees.
        """
        if df.empty or len(df) < 2:
            return {"bias": "neutral", "regime": "unknown", "adx": 0.0, "ema_trend": "ranging"}

        latest = df.iloc[-1]
        prev   = df.iloc[-2]

        adx  = float(latest.get("ADX_14", 0.0))
        dmp  = float(latest.get("DMP_14", 0.0))
        dmn  = float(latest.get("DMN_14", 0.0))
        adx_rising = adx > float(prev.get("ADX_14", 0.0))

        ema_20 = float(latest.get("ema_20", 0.0))
        ema_50 = float(latest.get("ema_50", 0.0))
        ema_trend = "bullish" if ema_20 > ema_50 else ("bearish" if ema_20 < ema_50 else "ranging")

        if adx >= settings.ADX_TRENDING_THRESHOLD and adx_rising:
            regime = "trending"
        elif adx >= settings.ADX_RANGING_THRESHOLD:
            regime = "transition"
        else:
            regime = "ranging"

        # Bias logic
        if regime == "trending":
            if ema_trend == "bullish" and dmp > dmn:
                bias = "bullish"
            elif ema_trend == "bearish" and dmn > dmp:
                bias = "bearish"
            else:
                bias = "neutral"
        elif regime == "ranging":
            rsi = float(latest.get("rsi", 50.0))
            bbp = float(latest.get("BBP_20_2.0", 0.5))
            if rsi <= 35 and bbp <= 0.2:
                bias = "bullish"
            elif rsi >= 65 and bbp >= 0.8:
                bias = "bearish"
            else:
                bias = "neutral"
        else:
            bias = "neutral"

        return {
            "bias":      bias,
            "regime":    regime,
            "adx":       round(adx, 1),
            "ema_trend": ema_trend,
            "dmp":       round(dmp, 1),
            "dmn":       round(dmn, 1),
        }
