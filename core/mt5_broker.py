"""
MT5BrokerManager — thin data/account facade over MT5Session.

What changed vs original:
- No more __init__ calling mt5.initialize() — session handles this
- No more __del__ calling mt5.shutdown() — atexit handler in session handles this
- All data ops delegate to the module-level `session` singleton
- Order execution removed — handled by OrderEngine (core/execution/order_engine.py)
- close_position / close_all kept here for backward compat with bot_handlers until
  those are migrated to OrderEngine

Why this split:
  Data (OHLCV, tick, account) belongs here — it's market data concern.
  Execution (order send, retry, fill tracking) belongs in OrderEngine.
  Connection lifecycle belongs in MT5Session.
  Keeping them separate prevents the original bug where constructing a "broker"
  silently restarted the MT5 connection.
"""

from __future__ import annotations

import logging

import MetaTrader5 as mt5
import pandas as pd

from config import settings
from core.connection.mt5_session import session

log = logging.getLogger(__name__)

TIMEFRAME_MAP: dict[str, int] = {
    "1m":  mt5.TIMEFRAME_M1,
    "5m":  mt5.TIMEFRAME_M5,
    "15m": mt5.TIMEFRAME_M15,
    "30m": mt5.TIMEFRAME_M30,
    "1h":  mt5.TIMEFRAME_H1,
    "4h":  mt5.TIMEFRAME_H4,
    "1d":  mt5.TIMEFRAME_D1,
}

FOREX_MAJORS: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "USDCAD", "NZDUSD",
]
FOREX_MINORS: list[str] = [
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY",
    "CADJPY", "CHFJPY", "EURCHF", "EURAUD",
    "XAUUSD",
]


class MT5BrokerManager:
    """
    Market data + account information facade.

    Instantiation is now free — no MT5 connection created or destroyed here.
    Multiple instances are safe because they all share the same MT5Session.

    Order execution (market_order, close_position) delegates to OrderEngine
    where retry, fill tracking, and idempotency are properly handled.
    The legacy wrappers below are kept during migration; prefer OrderEngine directly.
    """

    # ── Market Data ───────────────────────────────────────────────────────

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> pd.DataFrame:
        tf    = TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_H1)
        rates = session.get_rates(symbol, tf, limit)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s")
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def fetch_ticker(self, symbol: str) -> dict | None:
        tick = session.get_tick(symbol)
        if tick is None:
            return None
        mid = (tick.bid + tick.ask) / 2
        return {"last": mid, "bid": tick.bid, "ask": tick.ask, "spread": tick.ask - tick.bid}

    # ── Account ───────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        info = session.get_account_info()
        return float(info.balance) if info else 0.0

    def get_equity(self) -> float:
        info = session.get_account_info()
        return float(info.equity) if info else 0.0

    # ── Positions ─────────────────────────────────────────────────────────

    def fetch_open_positions(self) -> list[dict]:
        positions = session.get_all_positions()
        result = []
        for p in positions:
            result.append({
                "ticket":        p.ticket,
                "symbol":        p.symbol,
                "side":          "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume":        p.volume,
                "entryPrice":    p.price_open,
                "markPrice":     p.price_current,
                "unrealizedPnl": p.profit,
                "sl":            p.sl,
                "tp":            p.tp,
                "magic":         p.magic,
            })
        return result

    # ── Symbol Discovery ──────────────────────────────────────────────────

    def get_top_symbols(self, n: int = 10) -> list[str]:
        """Returns available symbols: majors first, then minors."""
        available = []
        for s in FOREX_MAJORS + FOREX_MINORS:
            info = session.get_symbol_info(s)
            if info is not None and info.visible:
                available.append(s)
        return available[:n]

    # ── Legacy execution wrappers (prefer OrderEngine directly) ───────────

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "forex_bot",
    ) -> dict | None:
        """
        Legacy wrapper — delegates to OrderEngine.
        Returns minimal dict for backward compat: {"id", "price", "volume"}.
        New code should call OrderEngine.market_order() directly to get FillResult.
        """
        from core.execution.order_engine import OrderEngine
        engine = OrderEngine()
        fill = engine.market_order(symbol, side, amount, sl, tp, comment=comment)
        if fill is None:
            return None
        return {"id": fill.ticket, "price": fill.fill_price, "volume": fill.volume}

    def modify_position_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       sl,
            "tp":       tp,
        }
        result = session.send_order(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def close_position(self, ticket: int) -> dict | None:
        """
        Legacy wrapper — delegates to OrderEngine.
        Returns {"id", "price"} or None.
        """
        from core.execution.order_engine import OrderEngine
        engine = OrderEngine()
        fill = engine.close_position(ticket)
        if fill is None:
            return None
        return {"id": fill.ticket, "price": fill.fill_price}

    def close_all_positions(self) -> int:
        from core.execution.order_engine import OrderEngine
        engine  = OrderEngine()
        count   = 0
        for pos in session.get_all_positions():
            if engine.close_position(pos.ticket):
                count += 1
        return count

    def cancel_all_orders(self, symbol: str) -> int:
        count = 0
        for o in session.get_pending_orders(symbol=symbol):
            req    = {"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
            result = session.send_order(req)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                count += 1
        return count
