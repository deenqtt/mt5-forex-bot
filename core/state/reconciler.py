"""
Reconciler — synchronize local PositionStore with live MT5 state.

Why this exists:
  Local state (positions.json) is a cache of what the bot thinks is open.
  MT5 is the source of truth. These can diverge when:

  A) Position closed on MT5 (SL/TP hit, manual close on terminal, connection drop)
     but bot didn't detect it → stale entry in local store
     Risk: bot skips symbol thinking it has open trade, misses re-entry opportunity.
           PnL never recorded in journal.

  B) Position opened on MT5 (manual trade, another EA, hedge) not in local store
     → ExposureGate doesn't count it → real exposure exceeds computed exposure
     Risk: bot opens additional positions, exceeding intended risk budget.

  C) Bot crashed mid-execution after order_send() succeeded but before open_position()
     → position in MT5, not in store (orphan)
     Risk: same as B.

  D) positions.json corrupted (disk full, kill -9 during write)
     → store loads empty → all positions become type-B orphans
     → reconciler recovers them as orphans within one cycle

Reconcile is called:
  1. At startup (before any job runs)
  2. Every 5 minutes as periodic safety net
  3. After any connection recovery
"""

from __future__ import annotations

import logging

import MetaTrader5 as mt5

from core.connection.mt5_session import session
from core.state.position_store import (
    add_orphan, get_all_positions, remove_position,
)
from core.state.trade_journal import (
    classify_exit, pnl_from_deal_history, record_close,
)

log = logging.getLogger(__name__)


def reconcile() -> dict:
    """
    Compare MT5 live positions vs local store. Resolve all discrepancies.

    Returns summary dict:
        matched  — positions present in both (normal state)
        closed   — positions found in local but not MT5 (SL/TP hit, manual close)
        orphans  — positions found in MT5 but not in local (added to store)
        errors   — positions where close processing failed

    This function mutates PositionStore and appends to TradeJournal.
    It is idempotent — safe to run multiple times without duplicating records.
    """
    # Snapshot both states atomically
    mt5_positions = {str(p.ticket): p for p in session.get_all_positions()}
    local_positions = get_all_positions()  # returns a copy

    matched = closed = orphans = errors = 0

    # ── Case A: In local store but not in MT5 → closed by SL/TP or terminal ──
    for ticket_str, record in local_positions.items():
        if ticket_str in mt5_positions:
            matched += 1
            continue

        ticket = int(ticket_str)
        log.info(
            "Reconciler: ticket %d (%s %s) not in MT5 — recording close",
            ticket, record["side"].upper(), record["symbol"],
        )

        try:
            _process_closed_position(ticket, record)
            closed += 1
        except Exception as exc:
            log.error("Reconciler: failed to process closed ticket %d: %s", ticket, exc)
            errors += 1

    # ── Case B/C: In MT5 but not in local → orphan recovery ──────────────────
    for ticket_str, pos in mt5_positions.items():
        if ticket_str in local_positions:
            continue

        side = "buy" if pos.type == mt5.POSITION_TYPE_BUY else "sell"
        add_orphan(
            ticket=pos.ticket,
            symbol=pos.symbol,
            side=side,
            fill_price=pos.price_open,
            lot_size=pos.volume,
        )
        orphans += 1

    summary = {
        "matched": matched,
        "closed":  closed,
        "orphans": orphans,
        "errors":  errors,
    }
    if closed > 0 or orphans > 0:
        log.warning("Reconcile result: %s", summary)
    else:
        log.debug("Reconcile clean: %s", summary)

    return summary


def _process_closed_position(ticket: int, record: dict) -> None:
    """
    Handle a position that was in local store but is no longer in MT5.

    Priority for PnL:
    1. MT5 deal history (broker's actual recorded profit) — most accurate
    2. Falls back to 0.0 with pnl_source="unknown" if history unavailable

    Removes from PositionStore FIRST, then writes to journal.
    If journal write fails, position is still removed (we don't want to
    re-process it next reconcile cycle).
    """
    # Query MT5 deal history for this position
    pnl_usd, pnl_source = pnl_from_deal_history(ticket)

    # Find exit price from deal history
    deals = session.get_deals_by_position(ticket)
    exit_price = record.get("fill_price", 0.0)  # fallback

    if deals:
        close_deal = next(
            (d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)),
            None,
        )
        if close_deal:
            exit_price = close_deal.price

    exit_reason = classify_exit(
        side=record["side"],
        fill=float(record.get("fill_price", 0)),
        exit_p=exit_price,
        sl=float(record.get("planned_sl", 0)),
        tp=float(record.get("planned_tp", 0)),
    )

    # Remove from store first (prevents double-processing on next reconcile)
    remove_position(ticket)

    # Write to journal
    record_close(
        record=record,
        exit_price=exit_price,
        pnl_usd=pnl_usd,
        exit_reason=exit_reason,
        pnl_source=pnl_source,
    )

    log.info(
        "Reconciler: closed ticket=%d %s %s @ %.5f  pnl=$%.2f  reason=%s",
        ticket, record["side"].upper(), record["symbol"],
        exit_price, pnl_usd, exit_reason,
    )
