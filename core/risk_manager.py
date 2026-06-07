from __future__ import annotations

import logging
from config import settings
from core.connection.mt5_session import session

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, balance: float) -> None:
        """
        balance should be in USD. 
        If account is IDR, balance passed here is already normalized via settings.IDR_TO_USD_RATE.
        """
        self.balance = balance

    @staticmethod
    def pip_multiplier(symbol: str) -> float:
        """
        JPY pairs: 1 pip = 0.01. Others: 1 pip = 0.0001.
        Used primarily for display/logging pips.
        """
        sym_upper = symbol.upper()
        if "JPY" in sym_upper:
            return 0.01
        if "XAU" in sym_upper:
            return 0.1   # Gold: 1 pip = $0.10 (standard)
        return 0.0001

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        risk_pct: float = settings.DEFAULT_RISK_PERCENT,
    ) -> float:
        """
        Returns lot size clamped to [MIN_LOT, MAX_LOT].
        Uses dynamic trade_tick_value from broker for 100% accuracy on all assets.
        """
        sl_distance = abs(entry_price - sl_price)
        if sl_distance <= 0:
            return settings.MIN_LOT

        # 1. Fetch broker-authoritative tick and volume info
        sym_info = session.get_symbol_info(symbol)
        if not sym_info:
            log.warning("Could not fetch symbol info for %s, falling back to safe min lot", symbol)
            return settings.MIN_LOT
        
        tick_val  = float(sym_info.trade_tick_value)
        tick_size = float(sym_info.trade_tick_size)
        vol_min   = float(sym_info.volume_min)
        vol_step  = float(sym_info.volume_step)
        vol_max   = float(sym_info.volume_max)
        
        # trade_tick_value is the profit/loss in ACCOUNT CURRENCY for 1 LOT when price moves by 1 TICK.
        # If account is IDR, tick_val is in IDR. We MUST normalize it to USD to match self.balance.
        if settings.ACCOUNT_CURRENCY == "IDR":
            tick_val_usd = tick_val / settings.IDR_TO_USD_RATE
        else:
            tick_val_usd = tick_val

        # 2. Calculate risk in USD
        risk_usd = self.balance * risk_pct

        # 3. Formula: Lot = Risk_USD / ( (SL_Dist / Tick_Size) * Tick_Val_USD )
        ticks_in_sl = sl_distance / tick_size
        if ticks_in_sl <= 0:
            return vol_min

        lot_size = risk_usd / (ticks_in_sl * tick_val_usd)
        
        # Ensure lot size follows broker's volume step and min/max limits
        import math
        # Calculation: floor to nearest volume_step to stay conservative with risk
        stepped_lot = math.floor(lot_size / vol_step) * vol_step
        final_lot   = round(max(vol_min, min(vol_max, stepped_lot)), 2)
        
        # If even the minimum volume exceeds our risk budget, we should ideally not trade.
        # But for now, we return vol_min and let ExposureGate or the broker handle it.
        
        log.info(
            "Risk Calc %s: Bal=$%.2f Risk=$%.2f SL_Dist=%.5f Ticks=%.1f Lot=%.2f (Min: %.2f)",
            symbol, self.balance, risk_usd, sl_distance, ticks_in_sl, final_lot, vol_min
        )
        return final_lot

    def calculate_risk_usd(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        lot: float,
    ) -> float:
        """Returns the actual USD risk for a given lot size and SL distance."""
        sl_distance = abs(entry_price - sl_price)
        tick_info   = session.get_tick_info(symbol)
        if not tick_info:
            return 0.0
        
        tick_val, tick_size = tick_info
        
        if settings.ACCOUNT_CURRENCY == "IDR":
            tick_val_usd = tick_val / settings.IDR_TO_USD_RATE
        else:
            tick_val_usd = tick_val

        ticks_in_sl = sl_distance / tick_size
        return round(ticks_in_sl * tick_val_usd * lot, 2)

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
