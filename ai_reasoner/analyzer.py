from __future__ import annotations

from dataclasses import dataclass

from config import settings

DISCLAIMER = "Ini bukan sinyal trading. Keputusan eksekusi sepenuhnya ada pada trader."


@dataclass
class _Score:
    """Single indicator evaluation result."""
    value: int    # -1 = bearish, 0 = neutral, +1 = bullish
    label: str
    detail: str


class MarketAnalyzer:
    """
    Regime-aware market context analyzer.

    Trending (ADX ≥ 28): EMA direction + RSI 50-line + DI divergence
    Ranging  (ADX < 20): RSI extreme zones + Bollinger Band position
    Transition (20–28) : RSI only, max ±1
    """

    # ------------------------------------------------------------------ #
    #  Trending regime scores                                              #
    # ------------------------------------------------------------------ #

    def _score_ema(self, ema_trend: str, ema_20: float, ema_50: float) -> _Score:
        if ema_50 == 0:
            return _Score(0, "Data tidak tersedia", "EMA 50 bernilai nol.")
        gap_pct = abs(ema_20 - ema_50) / ema_50 * 100
        if gap_pct < settings.EMA_RANGING_GAP_PCT:
            return _Score(0, "Ranging", f"Gap EMA {gap_pct:.3f}% — terlalu sempit.")
        if ema_trend == "bullish":
            return _Score(1, "Bullish", f"EMA 20 ({ema_20:.4f}) > EMA 50 ({ema_50:.4f}), gap {gap_pct:.2f}%.")
        return _Score(-1, "Bearish", f"EMA 20 ({ema_20:.4f}) < EMA 50 ({ema_50:.4f}), gap {gap_pct:.2f}%.")

    def _score_rsi_trend(self, rsi: float) -> _Score:
        """RSI 50-line bias — used in trending regime (not OB/OS logic)."""
        if rsi >= 55:
            return _Score(1, "Bullish Momentum", f"RSI {rsi:.1f} — di atas 55, momentum positif.")
        if rsi <= 45:
            return _Score(-1, "Bearish Momentum", f"RSI {rsi:.1f} — di bawah 45, momentum negatif.")
        return _Score(0, "Neutral", f"RSI {rsi:.1f} — zona netral (45–55).")

    def _score_di(self, dmp: float, dmn: float) -> _Score:
        """DI+/DI- divergence — directional confirmation from ADX calculation."""
        if dmp > dmn:
            return _Score(1, "DI+ Dominan", f"DI+ ({dmp:.1f}) > DI- ({dmn:.1f}) — tekanan beli lebih kuat.")
        if dmn > dmp:
            return _Score(-1, "DI- Dominan", f"DI- ({dmn:.1f}) > DI+ ({dmp:.1f}) — tekanan jual lebih kuat.")
        return _Score(0, "Seimbang", f"DI+ ({dmp:.1f}) = DI- ({dmn:.1f}) — tidak ada dominasi.")

    # ------------------------------------------------------------------ #
    #  Ranging regime scores                                               #
    # ------------------------------------------------------------------ #

    def _score_rsi_range(self, rsi: float) -> _Score:
        """RSI extreme zones — mean-reversion logic for ranging market."""
        if rsi <= 35:
            return _Score(1, "Oversold", f"RSI {rsi:.1f} — zona oversold, potensi bounce naik.")
        if rsi >= 65:
            return _Score(-1, "Overbought", f"RSI {rsi:.1f} — zona overbought, potensi koreksi.")
        return _Score(0, "Neutral", f"RSI {rsi:.1f} — di tengah range, tidak ada sinyal ekstrem.")

    def _score_bb(self, bb_percent: float) -> _Score:
        """Bollinger Band position — mean-reversion signal in ranging market."""
        if bb_percent <= 0.15:
            return _Score(1, "Dekat Lower Band", f"BBP {bb_percent:.2f} — harga di bawah, potensi bounce.")
        if bb_percent >= 0.85:
            return _Score(-1, "Dekat Upper Band", f"BBP {bb_percent:.2f} — harga di atas, potensi fade.")
        return _Score(0, "Mid Range", f"BBP {bb_percent:.2f} — harga di tengah band.")

    # ------------------------------------------------------------------ #
    #  Transition (neutral) regime                                         #
    # ------------------------------------------------------------------ #

    def _score_rsi_neutral(self, rsi: float) -> _Score:
        """Broad RSI scoring — used in transition regime only."""
        if rsi >= settings.RSI_OVERBOUGHT:
            return _Score(1, "Overbought", f"RSI {rsi:.1f}")
        if rsi <= settings.RSI_OVERSOLD:
            return _Score(-1, "Oversold", f"RSI {rsi:.1f}")
        if rsi >= settings.RSI_BULLISH_ZONE:
            return _Score(1, "Bullish Zone", f"RSI {rsi:.1f}")
        if rsi <= settings.RSI_BEARISH_ZONE:
            return _Score(-1, "Bearish Zone", f"RSI {rsi:.1f}")
        return _Score(0, "Neutral", f"RSI {rsi:.1f}")

    # ------------------------------------------------------------------ #
    #  Context helpers                                                     #
    # ------------------------------------------------------------------ #

    def _regime_label(self, regime: str, adx: float, adx_rising: bool) -> str:
        rising_tag = "↑ naik" if adx_rising else "↓ turun"
        if regime == "trending":
            return f"TRENDING  (ADX {adx:.1f} {rising_tag}) — sinyal tren aktif."
        if regime == "transition":
            return f"TRANSISI  (ADX {adx:.1f} {rising_tag}) — tidak ada bias dominan, kurangi size."
        return f"RANGING   (ADX {adx:.1f} {rising_tag}) — sinyal mean-reversion aktif."

    def _atr_context(self, atr: float, atr_expanding: bool) -> str:
        exp = "mengembang ↑" if atr_expanding else "menyempit ↓"
        return f"ATR {atr:.4f} ({exp}) — volatilitas {'meningkat' if atr_expanding else 'menurun'}."

    def _bb_context(self, bb_percent: float) -> str:
        if bb_percent >= 1 - settings.BB_NEAR_BAND:
            return f"Harga dekat Upper Band (BBP {bb_percent:.2f}) — area extended atas."
        if bb_percent <= settings.BB_NEAR_BAND:
            return f"Harga dekat Lower Band (BBP {bb_percent:.2f}) — area extended bawah."
        return f"Harga di tengah Bollinger Band (BBP {bb_percent:.2f})."

    def _cross_event_note(self, ema_cross_event: str) -> str:
        if ema_cross_event == "bullish_cross":
            return "EMA 20 baru saja memotong ke atas EMA 50 pada candle ini."
        if ema_cross_event == "bearish_cross":
            return "EMA 20 baru saja memotong ke bawah EMA 50 pada candle ini."
        return ""

    def _composite_label(self, score: int) -> str:
        labels = {
            3: "Strong Bullish",   2: "Bullish",   1: "Weak Bullish",
            0: "Neutral",
            -1: "Weak Bearish",   -2: "Bearish",  -3: "Strong Bearish",
        }
        return labels.get(score, "Neutral")

    # ------------------------------------------------------------------ #
    #  Main entry point                                                    #
    # ------------------------------------------------------------------ #

    def analyze(self, symbol: str, signals: dict, prediction: str) -> str:
        regime       = signals.get("regime", "transition")
        adx          = signals.get("adx", 0.0)
        dmp          = signals.get("dmp", 0.0)
        dmn          = signals.get("dmn", 0.0)
        adx_rising   = signals.get("adx_rising", False)
        atr          = signals.get("atr", 0.0)
        atr_expanding = signals.get("atr_expanding", False)
        rsi          = signals.get("rsi", 50.0)
        bb_percent   = signals.get("bb_percent", 0.5)
        ema_trend    = signals.get("ema_trend", "ranging")
        ema_20       = signals.get("ema_20", 0.0)
        ema_50       = signals.get("ema_50", 0.0)
        ema_cross    = signals.get("ema_cross_event", "none")
        patterns     = signals.get("patterns", [])

        # ── Regime-conditional composite score ──────────────────────────
        if regime == "trending":
            ema_s  = self._score_ema(ema_trend, ema_20, ema_50)
            rsi_s  = self._score_rsi_trend(rsi)
            di_s   = self._score_di(dmp, dmn)
            composite = ema_s.value + rsi_s.value + di_s.value
            max_score = 3
            indicator_lines = [
                f"Trend  (EMA)  : [{ema_s.label}]  {ema_s.detail}",
                f"Momentum(RSI) : [{rsi_s.label}]  {rsi_s.detail}",
                f"Arah   (DI)   : [{di_s.label}]  {di_s.detail}",
            ]
            disabled_note = "ℹ️  MACD dinonaktifkan — confirmation bias dengan EMA di regime trending."

        elif regime == "ranging":
            rsi_s  = self._score_rsi_range(rsi)
            bb_s   = self._score_bb(bb_percent)
            composite = rsi_s.value + bb_s.value
            max_score = 2
            indicator_lines = [
                f"Momentum(RSI) : [{rsi_s.label}]  {rsi_s.detail}",
                f"Posisi (BB)   : [{bb_s.label}]  {bb_s.detail}",
            ]
            disabled_note = "ℹ️  EMA & MACD dinonaktifkan — tidak valid di pasar sideways."

        else:  # transition
            rsi_s  = self._score_rsi_neutral(rsi)
            composite = rsi_s.value
            max_score = 1
            indicator_lines = [
                f"Momentum(RSI) : [{rsi_s.label}]  {rsi_s.detail}",
            ]
            disabled_note = "⚠️  Regime transisi — kurangi size, hindari entry baru jika tidak yakin."

        bias = self._composite_label(composite)
        cross_note = self._cross_event_note(ema_cross)
        pattern_note = (
            f"Pattern aktif: {', '.join(patterns)}"
            if patterns else "Tidak ada candlestick pattern signifikan."
        )

        sections: list[str] = [
            f"MARKET ANALYSIS — {symbol}",
            "",
            "═══ Market Regime ═══",
            self._regime_label(regime, adx, adx_rising),
            self._atr_context(atr, atr_expanding),
            "",
            "═══ Composite Score ═══",
            f"Score  : {composite:+d} / {max_score}  →  {bias}",
            f"Regime : {regime.upper()}",
            f"ML     : {prediction.capitalize()}",
        ]

        if cross_note:
            sections.append(f"Cross  : {cross_note}")

        sections += [
            "",
            "═══ Indicator Summary ═══",
            *indicator_lines,
            disabled_note,
            "",
            "═══ Volatility Context ═══",
            self._bb_context(bb_percent),
            "",
            "═══ Pattern Detection ═══",
            pattern_note,
            "",
            f"⚠️  {DISCLAIMER}",
        ]

        return "\n".join(sections)


# Backward-compatible alias — bot_handlers.py masih pakai nama AIAnalyzer
AIAnalyzer = MarketAnalyzer
