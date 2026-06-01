"""
OrderEngine — production-grade async order execution.

Changes in this version:

1. market_order() and close_position() are now ASYNC.
   Previously they used time.sleep() for retry backoff.
   time.sleep() inside an asyncio coroutine blocks the ENTIRE event loop:
   - tp_monitor_job cannot run during a 4-second retry window
   - Telegram commands go unanswered while retrying
   - Position closed by SL/TP cannot be detected during the block

   Fix: await asyncio.sleep() yields control back to event loop during backoff.
   Other coroutines (monitor, handlers) can run freely between retry attempts.

2. Per-symbol async lock added.
   Now that market_order is async (has await points), it's possible for the
   asyncio event loop to switch tasks between check_entry() and open_position().
   Scenario without lock:
     Task A: check_entry("EURUSD") → allowed
     [event loop switches at await asyncio.sleep()]
     Task B: check_entry("EURUSD") → allowed (A hasn't written to store yet)
     Task A: open_position() → ticket 111
     Task B: open_position() → ticket 222 ← duplicate!
   Fix: per-symbol lock held from check-to-record in the caller (main.py).
   Lock is defined here, acquired in _execute_for_symbol in main.py.

3. All other logic unchanged from synchronous version.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import MetaTrader5 as mt5

from core.connection.mt5_session import session

log = logging.getLogger(__name__)

_MAX_RETRIES   = 3
_RETRY_DELAY_S = 0.5   # base delay — multiplied by attempt number
_MAGIC         = 234001
_DEVIATION     = 20    # max slippage in points (~2 pip for 5-digit brokers)

# Per-symbol async lock — prevents duplicate order race condition.
# Acquired in main.py before check_entry() + order, released after open_position().
# Only needed because market_order is now async (has await points).
_symbol_locks: dict[str, asyncio.Lock] = {}


def get_symbol_lock(symbol: str) -> asyncio.Lock:
    """Return the async lock for this symbol. Creates it on first access."""
    if symbol not in _symbol_locks:
        _symbol_locks[symbol] = asyncio.Lock()
    return _symbol_locks[symbol]


@dataclass(frozen=True)
class FillResult:
    """
    Immutable record of actual execution.
    fill_price is result.price from order_send() — always the actual broker fill,
    never a pre-order price estimate.
    """
    ticket:     int
    symbol:     str
    side:       str
    fill_price: float
    volume:     float
    sl:         float
    tp:         float
    retcode:    int


class OrderEngine:
    """
    Stateless async execution engine.
    All instances share the same MT5Session singleton.
    MT5 API calls are still synchronous (C DLL) but backoff is non-blocking.
    """

    # ── Market Order ──────────────────────────────────────────────────────

    async def market_order(
        self,
        symbol:  str,
        side:    str,
        volume:  float,
        sl:      float,
        tp:      float,
        magic:   int = _MAGIC,
        comment: str = "forex_bot",
    ) -> FillResult | None:
        """
        Submit market order. Async — backoff uses await asyncio.sleep().
        Returns FillResult with actual fill_price on success.
        Returns None on fatal error or retry exhaustion.
        """
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        for attempt in range(1, _MAX_RETRIES + 1):
            tick = session.get_tick(symbol)
            if tick is None:
                log.error("[%d/%d] No tick for %s", attempt, _MAX_RETRIES, symbol)
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
                continue

            price = tick.ask if side == "buy" else tick.bid

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       round(float(volume), 2),
                "type":         order_type,
                "price":        price,
                "sl":           round(float(sl), 5),
                "tp":           round(float(tp), 5),
                "deviation":    _DEVIATION,
                "magic":        magic,
                "comment":      comment[:31],
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = session.send_order(request)

            if result is None:
                log.error("[%d/%d] send_order None for %s %s", attempt, _MAX_RETRIES, side.upper(), symbol)
                session.ensure_connected()
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    "ORDER FILLED: %s %s %.2f lot @ %.5f  SL=%.5f TP=%.5f  ticket=%d",
                    side.upper(), symbol, result.volume,
                    result.price, sl, tp, result.order,
                )
                return FillResult(
                    ticket=result.order,
                    symbol=symbol,
                    side=side,
                    fill_price=result.price,
                    volume=result.volume,
                    sl=sl,
                    tp=tp,
                    retcode=result.retcode,
                )

            if session.is_fatal(result.retcode):
                log.error(
                    "FATAL order error [%d] %s %s: %s",
                    result.retcode, side.upper(), symbol, result.comment,
                )
                return None

            if session.is_retryable(result.retcode):
                log.warning(
                    "Retryable [%d] attempt %d/%d %s %s: %s",
                    result.retcode, attempt, _MAX_RETRIES,
                    side.upper(), symbol, result.comment,
                )
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
                continue

            log.error("Unexpected retcode [%d] %s %s: %s",
                      result.retcode, side.upper(), symbol, result.comment)
            return None

        log.error("ORDER EXHAUSTED %d retries: %s %s", _MAX_RETRIES, side.upper(), symbol)
        return None

    # ── Close Position ─────────────────────────────────────────────────────

    async def close_position(self, ticket: int) -> FillResult | None:
        """
        Close position by ticket. Async — uses await asyncio.sleep() for backoff.

        Safe: if position not found in MT5, returns None cleanly.
        Caller must update local state ONLY after receiving non-None FillResult.
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            pos = session.get_position_by_ticket(ticket)
            if pos is None:
                log.info("close_position: ticket %d not in MT5 — already closed", ticket)
                return None

            close_type = (
                mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )
            close_side = "sell" if pos.type == mt5.POSITION_TYPE_BUY else "buy"

            tick = session.get_tick(pos.symbol)
            if tick is None:
                log.error("[%d/%d] No tick to close %s ticket=%d",
                          attempt, _MAX_RETRIES, pos.symbol, ticket)
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
                continue

            price = tick.bid if close_side == "sell" else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "position":     ticket,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "price":        price,
                "deviation":    _DEVIATION,
                "magic":        _MAGIC,
                "comment":      "close",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = session.send_order(request)

            if result is None:
                session.ensure_connected()
                await asyncio.sleep(_RETRY_DELAY_S * attempt)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("CLOSED: ticket=%d %s @ %.5f", ticket, pos.symbol, result.price)
                return FillResult(
                    ticket=result.order,
                    symbol=pos.symbol,
                    side=close_side,
                    fill_price=result.price,
                    volume=result.volume,
                    sl=0.0, tp=0.0,
                    retcode=result.retcode,
                )

            if session.is_fatal(result.retcode):
                log.error("FATAL close [%d] ticket=%d: %s",
                          result.retcode, ticket, result.comment)
                return None

            log.warning("Retryable close [%d] attempt %d/%d ticket=%d: %s",
                        result.retcode, attempt, _MAX_RETRIES, ticket, result.comment)
            await asyncio.sleep(_RETRY_DELAY_S * attempt)

        log.error("Close exhausted retries: ticket=%d", ticket)
        return None

    # ── Modify SL/TP ──────────────────────────────────────────────────────

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        """Synchronous — no retry needed, not a time-critical operation."""
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       round(sl, 5),
            "tp":       round(tp, 5),
        }
        result = session.send_order(request)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            log.warning("modify_sl_tp failed ticket=%d: %s",
                        ticket, result.comment if result else mt5.last_error())
        return ok
