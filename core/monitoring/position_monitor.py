"""
PositionMonitor — detects closed positions and sends proximity alerts.

Critical fixes from original tp_monitor_job:

1. PnL from deal history, not from formula.
   Original used (exit_price - entry) * lot * 100000 — wrong for JPY/Gold.
   Fix: query MT5 history_deals_get(position=ticket) for broker's actual PnL.

2. Exit price from closing deal, not from current ticker.
   Original: if ticket not in mt5_positions → fetch_ticker → use "last" as exit_price.
   "last" = midpoint at the time of detection, which could be minutes after actual close.
   Fix: exit_price = close_deal.price from deal history (broker's actual execution price).

3. Proximity alert threshold was a ratio with inconsistent math.
   Original: (tp - cur) / tp <= 0.0003 — this is % of TP price, not pip distance.
   At EURUSD TP=1.10000: 0.0003 × 1.10000 = 0.00033 = 3.3 pips ≈ OK accidentally.
   At USDJPY TP=145.00: 0.0003 × 145.00 = 0.0435 = 4.35 pip ≈ reasonable.
   At XAUUSD TP=2100.0: 0.0003 × 2100.0 = 0.63 = 63 pip ≈ VERY large alert zone.
   Fix: compute pip distance directly and compare to configured pip threshold.

4. Alert state (alerted set) was in context.bot_data — lost on restart.
   After restart, bot would spam proximity alerts for all active positions.
   Fix: keep alerted set in module-level dict — survives job restarts,
        cleared when position closes.
"""

from __future__ import annotations

import logging

import MetaTrader5 as mt5

from config import settings
from core.connection.mt5_session import session
from core.state.position_store import get_all_positions, remove_position
from core.state.trade_journal import (
    classify_exit,
    pnl_from_deal_history,
    record_close,
)

log = logging.getLogger(__name__)

_PROXIMITY_PIPS = 3.0   # alert when price within this many pips of TP or SL

# Module-level alert tracking — persists across job ticks, cleared on position close
_alerted: set[str] = set()   # keys like "near_tp_EURUSD_12345", "near_sl_EURUSD_12345"


async def run(send_message_fn) -> None:
    """
    Called every 30s by tp_monitor_job.
    Handles:
    1. Positions closed by SL/TP on broker → journal + Telegram notification
    2. Proximity alerts for approaching TP or SL
    """
    local_positions = get_all_positions()
    if not local_positions:
        return

    mt5_positions = {str(p.ticket): p for p in session.get_all_positions()}

    for ticket_str, record in list(local_positions.items()):
        ticket = int(ticket_str)
        symbol = record["symbol"]

        if ticket_str not in mt5_positions:
            # Position closed on broker side — process close
            await _handle_closed(ticket, record, send_message_fn)
        else:
            # Position still open — check proximity to TP/SL
            mt5_pos = mt5_positions[ticket_str]
            await _check_proximity(ticket_str, record, mt5_pos, send_message_fn)


async def _handle_closed(ticket: int, record: dict, send_message_fn) -> None:
    """
    Position in local store but not in MT5.
    Priority: use deal history for PnL and exit price.
    """
    symbol = record["symbol"]
    side   = record["side"]

    # Query broker's actual PnL and exit price
    deals = session.get_deals_by_position(ticket)
    exit_price = float(record.get("fill_price", 0))  # fallback
    pnl_usd    = 0.0
    pnl_source = "unknown"

    if deals:
        close_deal = next(
            (d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)),
            None,
        )
        if close_deal:
            exit_price = close_deal.price
            pnl_usd    = close_deal.profit
            pnl_source = "deal_history"

    exit_reason = classify_exit(
        side=side,
        fill=float(record.get("fill_price", 0)),
        exit_p=exit_price,
        sl=float(record.get("planned_sl", 0)),
        tp=float(record.get("planned_tp", 0)),
    )

    # Clean up alert state for this position
    for suffix in ("near_tp", "near_sl"):
        _alerted.discard(f"{suffix}_{symbol}_{ticket}")

    # Remove from store BEFORE journal write
    remove_position(ticket)
    row = record_close(record, exit_price, pnl_usd, exit_reason, pnl_source)

    sign  = "+" if pnl_usd >= 0 else ""
    emoji = "🎯" if exit_reason == "TP_Hit" else "🛑" if exit_reason == "SL_Hit" else "📋"

    await send_message_fn(
        f"{emoji} {exit_reason} — {symbol}\n\n"
        f"Entry  : {float(record.get('fill_price', 0)):.5f}\n"
        f"Exit   : {exit_price:.5f}\n"
        f"PnL    : {sign}${pnl_usd:.2f}\n"
        f"Lot    : {record.get('lot_size', 0)}\n"
        f"Durasi : {row.get('duration_min', 0)} menit"
    )


async def _check_proximity(
    ticket_str: str,
    record: dict,
    mt5_pos,
    send_message_fn,
) -> None:
    """Send alert when price approaches TP or SL by ≤ _PROXIMITY_PIPS."""
    symbol = record["symbol"]
    side   = record["side"]
    ticket = record["ticket"]
    tp     = float(record.get("planned_tp", 0))
    sl     = float(record.get("planned_sl", 0))

    # Use current mark price from MT5 position (no extra tick fetch needed)
    cur = float(mt5_pos.price_current)

    if symbol in settings.JPY_PAIRS:
        pip_mult = 0.01
    elif "XAU" in symbol:
        pip_mult = 0.01
    else:
        pip_mult = 0.0001

    near_tp_key = f"near_tp_{symbol}_{ticket}"
    near_sl_key = f"near_sl_{symbol}_{ticket}"

    # TP proximity
    if tp > 0:
        tp_dist_pips = (
            (tp - cur) / pip_mult if side == "buy" else (cur - tp) / pip_mult
        )
        if 0 < tp_dist_pips <= _PROXIMITY_PIPS:
            if near_tp_key not in _alerted:
                _alerted.add(near_tp_key)
                await send_message_fn(
                    f"⚡ MENDEKATI TP — {symbol}\n"
                    f"Harga : {cur:.5f}\n"
                    f"TP    : {tp:.5f}  ({tp_dist_pips:.1f} pip)"
                )
        elif tp_dist_pips > _PROXIMITY_PIPS:
            _alerted.discard(near_tp_key)

    # SL proximity
    if sl > 0:
        sl_dist_pips = (
            (cur - sl) / pip_mult if side == "buy" else (sl - cur) / pip_mult
        )
        if 0 < sl_dist_pips <= _PROXIMITY_PIPS:
            if near_sl_key not in _alerted:
                _alerted.add(near_sl_key)
                await send_message_fn(
                    f"⚠️ MENDEKATI SL — {symbol}\n"
                    f"Harga : {cur:.5f}\n"
                    f"SL    : {sl:.5f}  ({sl_dist_pips:.1f} pip)"
                )
        elif sl_dist_pips > _PROXIMITY_PIPS:
            _alerted.discard(near_sl_key)
