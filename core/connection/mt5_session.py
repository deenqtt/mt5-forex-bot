"""
MT5Session — Singleton MT5 connection manager.

Rules:
- Only ONE mt5.initialize() call exists in this entire codebase.
- Only ONE mt5.shutdown() call exists (at process exit).
- All MT5 operations go through this module — never import MetaTrader5 elsewhere
  and call mt5.* directly for connection lifecycle or data access.
- Thread-safe: _op_lock guards reconnect logic.
- Auto-reconnects on health check failure.

Why singleton:
  The old design created MT5BrokerManager() inside each job function (every 30-60s).
  Each constructor called mt5.initialize(). Each destructor called mt5.shutdown().
  Python's GC is non-deterministic — shutdown() fired while another job was in-flight,
  silently killing the active connection. Positions went undetected. Orders failed silently.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any

import MetaTrader5 as mt5

from config import settings

log = logging.getLogger(__name__)

# MT5 return codes we can safely retry on (transient broker/network issues)
RETRYABLE_CODES: frozenset[int] = frozenset({
    mt5.TRADE_RETCODE_REQUOTE,        # 10004 — price moved, retry with fresh price
    mt5.TRADE_RETCODE_CONNECTION,     # 10031 — connection lost
    mt5.TRADE_RETCODE_PRICE_CHANGED,  # 10020 — stale price
    mt5.TRADE_RETCODE_PRICE_OFF,      # 10021 — price off quotes
    mt5.TRADE_RETCODE_TIMEOUT,        # 10012 — request timed out
})

# Errors that will never succeed on retry — stop immediately
FATAL_CODES: frozenset[int] = frozenset({
    mt5.TRADE_RETCODE_NO_MONEY,        # 10019 — insufficient margin
    mt5.TRADE_RETCODE_INVALID_STOPS,   # 10016 — SL/TP violates stop level
    mt5.TRADE_RETCODE_TRADE_DISABLED,  # 10017 — trading disabled for symbol
    mt5.TRADE_RETCODE_MARKET_CLOSED,   # 10018 — market closed
    mt5.TRADE_RETCODE_INVALID_VOLUME,  # 10014 — volume out of range
    mt5.TRADE_RETCODE_POSITION_CLOSED, # 10036 — position already closed
})


class MT5Session:
    """
    Thread-safe singleton MT5 connection.

    Usage:
        from core.connection.mt5_session import session
        session.connect()           # at startup
        tick = session.get_tick("EURUSD")
        session.shutdown()          # at exit (registered via atexit)

    Never instantiate directly. Use the module-level `session` object.
    """

    _instance: "MT5Session | None" = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "MT5Session":
        with cls._creation_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._connected  = False
                obj._op_lock    = threading.Lock()  # guards reconnect, not data ops
                obj._symbol_mapping = {}            # maps "EURUSD" -> "EURUSDm"
                cls._instance   = obj
        return cls._instance

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Establish connection. Idempotent — safe to call when already connected.
        Tries up to 3 times with exponential backoff (1s, 2s, 4s).
        Returns True on success, False if all attempts fail.
        """
        with self._op_lock:
            if self._connected and self._ping():
                return True
            return self._do_connect()

    def _do_connect(self) -> bool:
        """Inner connect — caller must hold _op_lock."""
        for attempt in range(3):
            if attempt > 0:
                delay = 2 ** attempt  # 2s, 4s
                log.warning("MT5 connect attempt %d/3 — waiting %ds", attempt + 1, delay)
                time.sleep(delay)

            ok = mt5.initialize(
                login=settings.MT5_LOGIN,
                password=settings.MT5_PASSWORD,
                server=settings.MT5_SERVER,
            )
            if ok:
                # Verify we are actually authorized and connected to the server
                info = mt5.account_info()
                term = mt5.terminal_info()
                
                if info is not None and term is not None and term.connected:
                    self._connected = True
                    log.info(
                        "MT5 connected: server=%s login=%d balance=%.2f",
                        info.server,
                        settings.MT5_LOGIN,
                        info.balance,
                    )
                    return True
                
                code, msg = mt5.last_error()
                log.warning(
                    "MT5 init OK but session not ready (attempt %d): info=%s, connected=%s, error=[%d] %s",
                    attempt + 1, "OK" if info else "None", 
                    term.connected if term else "None",
                    code, msg
                )
                # If we are here, initialize succeeded but we are not yet connected/authorized.
                # We'll try again or let the loop continue.
            else:
                code, msg = mt5.last_error()
                log.error("MT5 init failed (attempt %d): [%d] %s", attempt + 1, code, msg)

        self._connected = False
        return False

    def ensure_connected(self) -> bool:
        """
        Lightweight guard — call before every MT5 data/order operation.
        Returns False if connection cannot be restored (caller should abort).
        Does NOT hold _op_lock during health ping to avoid blocking data ops.
        """
        if self._connected and self._ping():
            return True

        log.warning("MT5 health check failed (disconnected) — attempting reconnect")
        with self._op_lock:
            # Re-check after acquiring lock — another thread may have reconnected
            if self._connected and self._ping():
                return True
            self._connected = False
            return self._do_connect()

    def _ping(self) -> bool:
        """
        Health check — verify terminal is initialized AND connected to server.
        account_info() alone can return cached data even if disconnected.
        """
        info = mt5.account_info()
        if info is None:
            return False
        
        term = mt5.terminal_info()
        if term is None or not term.connected:
            return False
            
        return True

    def _ensure_symbol(self, symbol: str) -> str | None:
        """
        Ensure symbol is visible in Market Watch. 
        Case-insensitive and automatically tries suffixes.
        """
        sym_upper = symbol.upper()
        if sym_upper in self._symbol_mapping:
            return self._symbol_mapping[sym_upper]

        # 1. Try variations of the base name
        for name in [sym_upper, symbol.lower(), symbol]:
            info = mt5.symbol_info(name)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(name, True)
                self._symbol_mapping[sym_upper] = name
                return name

        # 2. Try common suffixes (both upper and lower)
        suffixes = ["m", "M", ".k", ".i", ".x", "r", "v"]
        for sfx in suffixes:
            for base in [sym_upper, symbol.lower()]:
                alt_name = f"{base}{sfx}"
                info = mt5.symbol_info(alt_name)
                if info is not None:
                    log.info("Detected symbol: %s -> %s", symbol, alt_name)
                    if not info.visible:
                        mt5.symbol_select(alt_name, True)
                    self._symbol_mapping[sym_upper] = alt_name
                    return alt_name

        log.error("Symbol %s not found in any variation.", symbol)
        return None

    def shutdown(self) -> None:
        """
        Called once at process exit via atexit.
        Never call this manually during normal operation.
        """
        with self._op_lock:
            if self._connected:
                mt5.shutdown()
                self._connected = False
                log.info("MT5 shutdown complete")

    # ── Market Data ───────────────────────────────────────────────────────

    def get_tick(self, symbol: str) -> Any | None:
        """Returns mt5.Tick or None. Logs on failure."""
        if not self.ensure_connected():
            return None
        
        real_symbol = self._ensure_symbol(symbol)
        if not real_symbol:
            return None

        tick = mt5.symbol_info_tick(real_symbol)
        if tick is None:
            code, msg = mt5.last_error()
            log.warning("get_tick(%s) failed: [%d] %s", real_symbol, code, msg)
        return tick

    def get_symbol_info(self, symbol: str) -> Any | None:
        if not self.ensure_connected():
            return None
        
        real_symbol = self._ensure_symbol(symbol)
        if not real_symbol:
            return None

        return mt5.symbol_info(real_symbol)

    def get_tick_info(self, symbol: str) -> tuple[float, float] | None:
        """
        Returns (trade_tick_value, trade_tick_size) for the symbol.
        Used for accurate lot sizing across different asset classes (Forex, Crypto, Gold).
        """
        info = self.get_symbol_info(symbol)
        if info is None:
            return None
        
        return float(info.trade_tick_value), float(info.trade_tick_size)

    def get_rates(self, symbol: str, timeframe: int, count: int) -> Any | None:
        if not self.ensure_connected():
            return None
        
        real_symbol = self._ensure_symbol(symbol)
        if not real_symbol:
            return None

        rates = mt5.copy_rates_from_pos(real_symbol, timeframe, 0, count)
        if rates is None:
            code, msg = mt5.last_error()
            log.warning("get_rates(%s) failed: [%d] %s", real_symbol, code, msg)
        return rates

    # ── Account ───────────────────────────────────────────────────────────

    def get_account_info(self) -> Any | None:
        if not self.ensure_connected():
            return None
        return mt5.account_info()

    # ── Positions ─────────────────────────────────────────────────────────

    def get_all_positions(self) -> list:
        if not self.ensure_connected():
            return []
        pos = mt5.positions_get()
        return list(pos) if pos else []

    def get_position_by_ticket(self, ticket: int) -> Any | None:
        if not self.ensure_connected():
            return None
        pos = mt5.positions_get(ticket=ticket)
        return pos[0] if pos else None

    def get_positions_by_symbol(self, symbol: str) -> list:
        if not self.ensure_connected():
            return []
        real_symbol = self._ensure_symbol(symbol)
        if not real_symbol:
            return []
        pos = mt5.positions_get(symbol=real_symbol)
        return list(pos) if pos else []

    # ── Orders ────────────────────────────────────────────────────────────

    def get_pending_orders(self, symbol: str | None = None) -> list:
        if not self.ensure_connected():
            return []
        
        real_symbol = None
        if symbol:
            real_symbol = self._ensure_symbol(symbol)
            if not real_symbol:
                return []
                
        orders = mt5.orders_get(symbol=real_symbol) if real_symbol else mt5.orders_get()
        return list(orders) if orders else []

    def send_order(self, request: dict) -> Any | None:
        """
        Raw order dispatch. Use OrderEngine for production execution —
        it handles retries, fill price capture, and logging.
        """
        if not self.ensure_connected():
            return None
        
        # If request contains a symbol, ensure it's mapped correctly
        if "symbol" in request:
            real_symbol = self._ensure_symbol(request["symbol"])
            if real_symbol:
                request["symbol"] = real_symbol
                
        return mt5.order_send(request)

    # ── History ───────────────────────────────────────────────────────────

    def get_deals_by_position(self, position_id: int) -> list:
        """
        Fetch all deals associated with a position ticket.
        Used by reconciler and position monitor to find actual close price + PnL.
        """
        if not self.ensure_connected():
            return []
        # date_from must be far enough back to capture the deal
        from datetime import datetime, timezone, timedelta
        date_from = datetime(2000, 1, 1, tzinfo=timezone.utc)
        date_to   = datetime.now(timezone.utc) + timedelta(days=1)
        deals = mt5.history_deals_get(date_from, date_to, position=position_id)
        return list(deals) if deals else []

    # ── Profit calculation ─────────────────────────────────────────────────

    def calc_profit(
        self,
        action: int,
        symbol: str,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        """
        Ask MT5 to calculate profit — handles cross-currency conversion correctly.
        Returns None if calculation fails.
        This is the ONLY correct way to compute PnL for non-EURUSD pairs.
        """
        if not self.ensure_connected():
            return None
        
        real_symbol = self._ensure_symbol(symbol)
        if not real_symbol:
            return None

        result = mt5.order_calc_profit(action, real_symbol, volume, open_price, close_price)
        if result is None:
            log.warning(
                "calc_profit failed: %s %s vol=%.2f %.5f→%.5f err=%s",
                real_symbol, "BUY" if action == mt5.ORDER_TYPE_BUY else "SELL",
                volume, open_price, close_price, mt5.last_error(),
            )
        return result

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def is_retryable(retcode: int) -> bool:
        return retcode in RETRYABLE_CODES

    @staticmethod
    def is_fatal(retcode: int) -> bool:
        return retcode in FATAL_CODES


# Module-level singleton — import this everywhere
session = MT5Session()

# Register clean shutdown at process exit (only fires once)
atexit.register(session.shutdown)
