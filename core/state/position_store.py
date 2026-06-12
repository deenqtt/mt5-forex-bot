"""
PositionStore — atomic JSON persistence for open positions.

Critical differences from the original trade_logger.py:

1. Key = ticket (int), not symbol.
   Original keyed by symbol → only 1 position per symbol tracked.
   MT5 can have multiple positions per symbol (manual + bot, or hedge mode).
   Key by ticket: every position has a unique, stable identity.

2. Atomic writes: write to .tmp → fsync → rename.
   Original: open(path, "w") — if process dies mid-write, file is corrupt.
   Fix: POSIX rename() is atomic. File is either old version or new version,
        never a partial write. fsync() ensures OS buffer is flushed to disk
        before rename so power loss doesn't leave .tmp behind with no .json.

3. fill_price not entry_price.
   Renamed to be explicit: this is the actual broker fill, not a pre-order estimate.
   Original stored ticker["last"] captured before order_send(). After slippage
   and spread, actual fill could differ by 0.5–2 pips — enough to misclassify
   SL/TP hits and miscalculate realized PnL.

4. Thread-safe reads: _STORE_LOCK prevents concurrent modifications.
   Multiple jobs (tp_monitor_job + auto_execute_job) run in the same process.
   Python's asyncio is single-threaded but job functions can be suspended between
   _load() and _save() calls, leaving a window for interleaving writes.
   Lock ensures read-modify-write is atomic at the Python level.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_POSITIONS_FILE = Path("data/positions.json")
_TMP_FILE       = Path("data/positions.json.tmp")
_STORE_LOCK     = threading.Lock()  # guards all read-modify-write operations


# ── Internal I/O ──────────────────────────────────────────────────────────

def _load() -> dict[str, dict]:
    """
    Load positions dict. Returns {} on missing or corrupt file.
    Corruption is logged as error — reconciler will rebuild state from MT5.
    """
    if not _POSITIONS_FILE.exists():
        return {}
    try:
        with open(_POSITIONS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Expected dict at top level")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.error(
            "PositionStore corrupt (%s) — starting empty, reconciler will recover",
            exc,
        )
        return {}


def _save(data: dict) -> None:
    """
    Atomic write: temp file → fsync → rename.
    Caller must hold _STORE_LOCK.
    """
    _TMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_TMP_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())  # ensure kernel buffer → disk before rename
    
    # Use os.replace instead of rename on Windows to avoid FileExistsError
    os.replace(_TMP_FILE, _POSITIONS_FILE)


# ── Public API ────────────────────────────────────────────────────────────

def open_position(
    ticket:         int,
    symbol:         str,
    side:           str,
    fill_price:     float,
    sl_price:       float,
    tp_price:       float,
    lot_size:       float,
    risk_usd:       float,
    equity_at_open: float,
    atr_at_open:    float = 0.0,
) -> None:
    """
    Record a newly opened position.
    fill_price MUST be result.price from FillResult — not a pre-order estimate.
    """
    with _STORE_LOCK:
        data = _load()
        key  = str(ticket)

        if key in data:
            log.warning(
                "open_position: ticket %d already in store — overwriting (duplicate?)",
                ticket,
            )

        data[key] = {
            "ticket":         ticket,
            "symbol":         symbol,
            "side":           side,
            "fill_price":     fill_price,
            "planned_sl":     sl_price,
            "planned_tp":     tp_price,
            "lot_size":       lot_size,
            "risk_usd":       risk_usd,
            "equity_at_open": equity_at_open,
            "atr_at_open":    atr_at_open,
            "be_applied":     False,
            "opened_at":      datetime.utcnow().isoformat(),
            "date":           date.today().isoformat(),
            "source":         "bot",
        }
        _save(data)
        log.info(
            "PositionStore: opened ticket=%d %s %s @ %.5f  SL=%.5f TP=%.5f ATR=%.5f",
            ticket, side.upper(), symbol, fill_price, sl_price, tp_price, atr_at_open,
        )


def update_position_field(ticket: int, field: str, value: any) -> bool:
    """
    Updates a specific field in a position record (e.g., be_applied=True).
    Returns True if updated, False if ticket not found.
    """
    with _STORE_LOCK:
        data = _load()
        key  = str(ticket)
        if key in data:
            data[key][field] = value
            _save(data)
            return True
        return False


def remove_position(ticket: int) -> dict | None:
    """
    Remove position from store.
    Returns the removed record (for journal logging) or None if not found.
    Called ONLY after MT5 confirms the position is closed.
    """
    with _STORE_LOCK:
        data   = _load()
        record = data.pop(str(ticket), None)
        if record is not None:
            _save(data)
            log.info("PositionStore: removed ticket=%d", ticket)
        else:
            log.warning("PositionStore: remove_position ticket=%d not found", ticket)
        return record


def add_orphan(
    ticket:     int,
    symbol:     str,
    side:       str,
    fill_price: float,
    lot_size:   float,
) -> None:
    """
    Add a position found in MT5 but not in local store.
    Called by Reconciler on startup and periodic sync.
    Risk fields are unknown — risk_usd=0 marks it as an orphan.
    """
    with _STORE_LOCK:
        data = _load()
        key  = str(ticket)
        if key in data:
            return  # already tracked
        data[key] = {
            "ticket":         ticket,
            "symbol":         symbol,
            "side":           side,
            "fill_price":     fill_price,
            "planned_sl":     0.0,
            "planned_tp":     0.0,
            "lot_size":       lot_size,
            "risk_usd":       0.0,   # unknown — was not opened by this bot
            "equity_at_open": 0.0,
            "opened_at":      datetime.utcnow().isoformat(),
            "date":           date.today().isoformat(),
            "source":         "orphan",
        }
        _save(data)
        log.warning(
            "PositionStore: orphan recovered ticket=%d %s %s @ %.5f",
            ticket, side.upper(), symbol, fill_price,
        )


# ── Queries ───────────────────────────────────────────────────────────────

def get_position(ticket: int) -> dict | None:
    with _STORE_LOCK:
        return _load().get(str(ticket))


def get_all_positions() -> dict[str, dict]:
    """Returns {ticket_str: record}. Snapshot — safe to iterate."""
    with _STORE_LOCK:
        return dict(_load())


def get_positions_for_symbol(symbol: str) -> list[dict]:
    with _STORE_LOCK:
        return [p for p in _load().values() if p["symbol"] == symbol]


def has_open_position(symbol: str) -> bool:
    """True if any position exists for this symbol (any side, any ticket)."""
    return len(get_positions_for_symbol(symbol)) > 0


def count_open_positions() -> int:
    with _STORE_LOCK:
        return len(_load())


def get_total_risk_usd() -> float:
    """Sum of risk_usd across all open positions. Used by ExposureGate."""
    with _STORE_LOCK:
        return sum(float(p.get("risk_usd", 0)) for p in _load().values())
