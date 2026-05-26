"""
OrderEngine — production-grade order execution.

What this fixes vs original create_market_order():

1. Fill price: original logged ticker["last"] captured BEFORE order submission.
   Slippage + spread + timing meant actual fill was different.
   Fix: log result.price (actual fill) from order_send() response.

2. No retry: original returned None on any failure including transient requotes.
   Fix: retry loop with per-attempt fresh price fetch, exponential backoff,
        retryable vs fatal error distinction.

3. Tick fetch race: original fetched tick, then used that price in request.
   If tick() call inside order_send() fetched a newer price, deviation check
   could reject even valid orders.
   Fix: fetch tick immediately before each attempt, minimize window.

4. Close race condition: original called symbol_info_tick() without None check.
   Fix: tick fetch guarded, AttributeError impossible.

5. Duplicate close guard: if close_position called twice (bug path), second call
   finds no position in MT5 and returns None cleanly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import MetaTrader5 as mt5

from core.connection.mt5_session import session

log = logging.getLogger(__name__)

_MAX_RETRIES   = 3
_RETRY_DELAY_S = 0.5   # base delay — multiplied by attempt number
_MAGIC         = 234001
_DEVIATION     = 20    # max slippage in points (~2 pip for 5-digit brokers)


@dataclass(frozen=True)
class FillResult:
    """
    Immutable record of actual execution. fill_price is the actual fill
    from result.price — not an estimate from a pre-order ticker call.

    Always use fill_price for:
    - logging the actual entry price in PositionStore
    - computing SL distance for risk verification
    - PnL calculation baseline
    """
    ticket:     int
    symbol:     str
    side:       str
    fill_price: float   # result.price — actual fill, NOT pre-order estimate
    volume:     float
    sl:         float
    tp:         float
    retcode:    int


class OrderEngine:
    """
    Stateless execution engine. Thread-safe (no shared state).
    All instances share the same MT5Session singleton.
    """

    # ── Market Order ──────────────────────────────────────────────────────

    def market_order(
        self,
        symbol:  str,
        side:    str,              # "buy" | "sell"
        volume:  float,
        sl:      float,
        tp:      float,
        magic:   int = _MAGIC,
        comment: str = "forex_bot",
    ) -> FillResult | None:
        """
        Submit market order with retry on transient failures.

        Retry policy:
        - On RETRYABLE_CODES: re-fetch price, wait _RETRY_DELAY_S × attempt, retry
        - On FATAL_CODES: return None immediately (no point retrying)
        - On None result (connection lost): ensure_connected() then retry

        Returns FillResult with fill_price = actual execution price.
        Returns None on fatal error or retry exhaustion.
        """
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        for attempt in range(1, _MAX_RETRIES + 1):
            # Always get fresh price immediately before submission
            tick = session.get_tick(symbol)
            if tick is None:
                log.error("[%d/%d] Cannot fetch tick for %s", attempt, _MAX_RETRIES, symbol)
                time.sleep(_RETRY_DELAY_S * attempt)
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
                "comment":      comment[:31],  # MT5 truncates at 31 chars
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = session.send_order(request)

            if result is None:
                log.error(
                    "[%d/%d] send_order returned None for %s %s — connection issue",
                    attempt, _MAX_RETRIES, side.upper(), symbol,
                )
                session.ensure_connected()
                time.sleep(_RETRY_DELAY_S * attempt)
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
                    fill_price=result.price,  # ← actual fill price from broker
                    volume=result.volume,
                    sl=sl,
                    tp=tp,
                    retcode=result.retcode,
                )

            if session.is_fatal(result.retcode):
                log.error(
                    "FATAL order error [%d] — %s %s: %s. No retry.",
                    result.retcode, side.upper(), symbol, result.comment,
                )
                return None

            if session.is_retryable(result.retcode):
                log.warning(
                    "Retryable error [%d] attempt %d/%d — %s %s: %s",
                    result.retcode, attempt, _MAX_RETRIES,
                    side.upper(), symbol, result.comment,
                )
                time.sleep(_RETRY_DELAY_S * attempt)
                continue

            # Unexpected retcode — log details, do not retry
            log.error(
                "Unexpected retcode [%d] — %s %s: %s",
                result.retcode, side.upper(), symbol, result.comment,
            )
            return None

        log.error(
            "ORDER EXHAUSTED %d retries: %s %s vol=%.2f",
            _MAX_RETRIES, side.upper(), symbol, volume,
        )
        return None

    # ── Close Position ────────────────────────────────────────────────────

    def close_position(self, ticket: int) -> FillResult | None:
        """
        Close position by MT5 ticket.

        Safe properties:
        - Fetches position from MT5 first — if not found, returns None cleanly
          (covers "already closed" case without crashing)
        - Fetches tick inside retry loop — no stale price race
        - Tick None-check guarded — no AttributeError on low-liquidity close

        IMPORTANT: caller must only call position_store.close_position(ticket)
        AFTER this returns a non-None FillResult. Never update local state on failure.
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            # Re-fetch position state each attempt — it may close between retries
            pos = session.get_position_by_ticket(ticket)
            if pos is None:
                log.info("close_position: ticket %d not found in MT5 — already closed", ticket)
                return None

            close_type = (
                mt5.ORDER_TYPE_SELL
                if pos.type == mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )
            close_side = "sell" if pos.type == mt5.POSITION_TYPE_BUY else "buy"

            tick = session.get_tick(pos.symbol)
            if tick is None:
                log.error(
                    "[%d/%d] Cannot fetch tick to close %s ticket=%d",
                    attempt, _MAX_RETRIES, pos.symbol, ticket,
                )
                time.sleep(_RETRY_DELAY_S * attempt)
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
                time.sleep(_RETRY_DELAY_S * attempt)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    "CLOSED: ticket=%d %s @ %.5f",
                    ticket, pos.symbol, result.price,
                )
                return FillResult(
                    ticket=result.order,
                    symbol=pos.symbol,
                    side=close_side,
                    fill_price=result.price,
                    volume=result.volume,
                    sl=0.0,
                    tp=0.0,
                    retcode=result.retcode,
                )

            if session.is_fatal(result.retcode):
                log.error(
                    "FATAL close error [%d] ticket=%d: %s",
                    result.retcode, ticket, result.comment,
                )
                return None

            log.warning(
                "Close retryable [%d] attempt %d/%d ticket=%d: %s",
                result.retcode, attempt, _MAX_RETRIES, ticket, result.comment,
            )
            time.sleep(_RETRY_DELAY_S * attempt)

        log.error("Close exhausted %d retries: ticket=%d", _MAX_RETRIES, ticket)
        return None

    # ── Modify SL/TP ─────────────────────────────────────────────────────

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       round(sl, 5),
            "tp":       round(tp, 5),
        }
        result = session.send_order(request)
        success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if not success:
            log.warning(
                "modify_sl_tp failed ticket=%d: %s",
                ticket,
                result.comment if result else mt5.last_error(),
            )
        return success
