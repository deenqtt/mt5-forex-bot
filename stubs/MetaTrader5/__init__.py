"""
MetaTrader5 stub for Linux development environment.

Purpose:
  MetaTrader5 Python package is Windows-only (connects to local MT5 terminal via COM).
  This stub provides the same API surface so that:
  - Import resolution works (IDE, linter, type checker)
  - AST parsing and syntax validation pass
  - Unit tests can mock MT5 behavior without a live connection

  This stub is NOT installed in the venv — it lives in stubs/ and is added to
  PYTHONPATH only in development. On Windows production, the real MetaTrader5
  package is installed from requirements.txt and takes precedence.

Usage (Linux dev):
  export PYTHONPATH="${PYTHONPATH}:$(pwd)/stubs"
  python main.py   ← will import this stub instead of real MT5

WARNING: all functions return None / empty / False.
         Do NOT use this stub for any real trading logic verification.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# ── Return code constants ──────────────────────────────────────────────────
TRADE_RETCODE_DONE           = 10009
TRADE_RETCODE_REQUOTE        = 10004
TRADE_RETCODE_CONNECTION     = 10006
TRADE_RETCODE_PRICE_CHANGED  = 10015
TRADE_RETCODE_PRICE_OFF      = 10021
TRADE_RETCODE_TIMEOUT        = 10010
TRADE_RETCODE_SERVER_DISCON  = 10033
TRADE_RETCODE_NO_MONEY       = 10019
TRADE_RETCODE_INVALID_STOPS  = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED  = 10018
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_POSITION_CLOSED = 10009

# ── Order types ────────────────────────────────────────────────────────────
ORDER_TYPE_BUY  = 0
ORDER_TYPE_SELL = 1

# ── Position types ─────────────────────────────────────────────────────────
POSITION_TYPE_BUY  = 0
POSITION_TYPE_SELL = 1

# ── Order filling ──────────────────────────────────────────────────────────
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_BOC = 2

# ── Order time ─────────────────────────────────────────────────────────────
ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1

# ── Trade actions ──────────────────────────────────────────────────────────
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
TRADE_ACTION_REMOVE = 8

# ── Timeframes ─────────────────────────────────────────────────────────────
TIMEFRAME_M1  = 1
TIMEFRAME_M5  = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1  = 60 * 16385      # MT5 internal encoding
TIMEFRAME_H4  = 60 * 4 * 16385
TIMEFRAME_D1  = 16408

# ── Deal entry ─────────────────────────────────────────────────────────────
DEAL_ENTRY_IN    = 0
DEAL_ENTRY_OUT   = 1
DEAL_ENTRY_INOUT = 2


# ── Stub data classes ──────────────────────────────────────────────────────

@dataclass
class AccountInfo:
    login:    int   = 0
    balance:  float = 10000.0
    equity:   float = 10000.0
    margin:   float = 0.0
    server:   str   = "Stub-Server"
    currency: str   = "USD"
    leverage: int   = 100


@dataclass
class SymbolInfo:
    name:    str  = ""
    visible: bool = True
    digits:  int  = 5
    point:   float = 0.00001


@dataclass
class Tick:
    bid:   float = 1.08000
    ask:   float = 1.08002
    last:  float = 1.08001
    time:  int   = 0
    flags: int   = 0


@dataclass
class Position:
    ticket:        int   = 0
    symbol:        str   = ""
    type:          int   = POSITION_TYPE_BUY
    volume:        float = 0.01
    price_open:    float = 0.0
    price_current: float = 0.0
    profit:        float = 0.0
    sl:            float = 0.0
    tp:            float = 0.0
    magic:         int   = 0
    comment:       str   = ""


@dataclass
class OrderSendResult:
    retcode: int   = TRADE_RETCODE_DONE
    order:   int   = 999999
    price:   float = 1.08001
    volume:  float = 0.01
    comment: str   = "stub"


@dataclass
class Deal:
    ticket:   int   = 0
    order:    int   = 0
    position_id: int = 0
    entry:    int   = DEAL_ENTRY_IN
    price:    float = 0.0
    profit:   float = 0.0
    symbol:   str   = ""
    volume:   float = 0.01


# ── Stub functions ─────────────────────────────────────────────────────────

def initialize(*args, **kwargs) -> bool:
    return True

def shutdown() -> None:
    pass

def last_error() -> tuple[int, str]:
    return (0, "stub — no error")

def account_info() -> AccountInfo:
    return AccountInfo()

def symbol_info(symbol: str) -> SymbolInfo:
    return SymbolInfo(name=symbol)

def symbol_info_tick(symbol: str) -> Tick:
    return Tick()

def copy_rates_from_pos(symbol: str, timeframe: int, start: int, count: int):
    import numpy as np
    import time as _time
    dtype = [
        ("time", "i8"), ("open", "f8"), ("high", "f8"),
        ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"),
    ]
    data = np.zeros(count, dtype=dtype)
    base = 1.08000
    now  = int(_time.time())
    for i in range(count):
        data[i] = (now - (count - i) * 3600, base, base+0.001, base-0.001, base, 100)
    return data

def positions_get(symbol: str | None = None, ticket: int | None = None) -> tuple:
    return ()

def orders_get(symbol: str | None = None) -> tuple:
    return ()

def order_send(request: dict) -> OrderSendResult:
    return OrderSendResult()

def order_calc_profit(
    action: int, symbol: str, volume: float,
    open_price: float, close_price: float
) -> float:
    # Rough approximation for stub — not accurate
    diff = (close_price - open_price) if action == ORDER_TYPE_BUY else (open_price - close_price)
    return round(diff * volume * 100000, 2)

def history_deals_get(date_from=None, date_to=None, position: int | None = None) -> tuple:
    return ()
