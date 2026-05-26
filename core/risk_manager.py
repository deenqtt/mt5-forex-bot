from __future__ import annotations

from config import settings


class RiskManager:
    def __init__(self, balance: float) -> None:
        self.balance = balance

    @staticmethod
    def pip_multiplier(symbol: str) -> float:
        """JPY pairs: 1 pip = 0.01. Others: 1 pip = 0.0001."""
        return 0.01 if symbol.upper() in settings.JPY_PAIRS else 0.0001

    @staticmethod
    def pip_value_usd(symbol: str, lot: float = 1.0) -> float:
        """
        Approximate pip value in USD per lot.
        EURUSD/GBPUSD/etc: $10/pip/lot standard.
        USDJPY: ~$9.3/pip/lot (varies with rate — use $10 as approx).
        XAUUSD: $1/pip/lot (1 pip = $0.01 for gold).
        """
        if "XAU" in symbol:
            return 1.0 * lot
        return settings.PIP_VALUE_USD * lot

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        risk_pct: float = settings.DEFAULT_RISK_PERCENT,
    ) -> float:
        """Returns lot size clamped to [MIN_LOT, MAX_LOT]."""
        sl_distance = abs(entry_price - sl_price)
        pip_mult    = self.pip_multiplier(symbol)
        sl_pips     = sl_distance / pip_mult
        if sl_pips <= 0:
            return settings.MIN_LOT

        risk_usd  = self.balance * risk_pct
        pip_val   = self.pip_value_usd(symbol, lot=1.0)
        lot_size  = risk_usd / (sl_pips * pip_val)
        return round(max(settings.MIN_LOT, min(settings.MAX_LOT, lot_size)), 2)

    def calculate_risk_usd(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        lot: float,
    ) -> float:
        sl_distance = abs(entry_price - sl_price)
        pip_mult    = self.pip_multiplier(symbol)
        sl_pips     = sl_distance / pip_mult
        return round(sl_pips * self.pip_value_usd(symbol, lot), 2)

    @staticmethod
    def get_sl_tp_atr(
        entry_price: float,
        atr: float,
        side: str = "buy",
        sl_mult: float | None = None,
        tp_mult: float | None = None,
    ) -> tuple[float, float]:
        sl_m = settings.ATR_SL_MULTIPLIER if sl_mult is None else sl_mult
        tp_m = settings.ATR_TP_MULTIPLIER if tp_mult is None else tp_mult
        if side.lower() == "buy":
            sl = entry_price - sl_m * atr
            tp = entry_price + tp_m * atr
        else:
            sl = entry_price + sl_m * atr
            tp = entry_price - tp_m * atr
        return round(sl, 5), round(tp, 5)

    @staticmethod
    def get_sl_tp_pips(
        symbol: str,
        entry_price: float,
        side: str,
        sl_pips: int = settings.DEFAULT_SL_PIPS,
        tp_pips: int = settings.DEFAULT_TP_PIPS,
    ) -> tuple[float, float]:
        mult = RiskManager.pip_multiplier(symbol)
        if side.lower() == "buy":
            sl = entry_price - sl_pips * mult
            tp = entry_price + tp_pips * mult
        else:
            sl = entry_price + sl_pips * mult
            tp = entry_price - tp_pips * mult
        return round(sl, 5), round(tp, 5)
