"""
TradeJournal — append-only CSV trade history.

Critical fix from original trade_logger.py:

PnL formula was:
    pnl_usd = (exit_price - entry) * lot * 100000

This is only correct for EURUSD. Problems:
- USDJPY: 1 pip = 0.01 JPY, pip value changes with exchange rate (~$6.9 at 145)
- XAUUSD: 1 lot = 100 troy oz, pip = $0.01, not $10
- GBPUSD, AUDUSD: near-correct but USD quote pairs have slight error
- Cross pairs (EURJPY, GBPJPY): require two-step conversion

Fix: use mt5.order_calc_profit() which MT5 computes internally with full
     knowledge of contract specs and live exchange rates.
     Fallback: query history_deals_get(position=ticket) for the actual
     realized PnL recorded by the broker — this is 100% accurate.

get_daily_stats() replaces get_daily_risk_usd().
Returns a rich dict instead of a fragile single float.
CircuitBreaker uses rr_cumulative from this dict.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path

import MetaTrader5 as mt5

from core.connection.mt5_session import session

log = logging.getLogger(__name__)

_JOURNAL_PATH = Path("data/trade_history.csv")
_FIELDS = [
    "date", "ticket", "symbol", "side",
    "fill_price", "exit_price",
    "planned_sl", "planned_tp",
    "lot_size", "risk_usd",
    "pnl_usd",        # from MT5 deal history (accurate) or calc_profit (fallback)
    "pnl_pct",        # pnl_usd / risk_usd × 100 — meaningful only if risk_usd > 0
    "exit_reason",    # TP_Hit | SL_Hit | Manual_Early | Manual_Late | Unknown
    "duration_min",
    "equity_at_open",
    "pnl_source",     # "deal_history" | "calc_profit" | "unknown" — audit trail
]


def record_close(
    record:      dict,       # from position_store.remove_position()
    exit_price:  float,
    pnl_usd:     float,
    exit_reason: str,
    pnl_source:  str = "unknown",
) -> dict:
    """
    Append one closed trade to journal.

    pnl_usd should come from:
    1. deal history (highest accuracy — broker's actual recorded profit)
    2. calc_profit (session.calc_profit — accurate, needs live connection)
    3. 0.0 with pnl_source="unknown" if neither available

    Never compute pnl_usd here — the caller is responsible for accuracy.
    """
    risk        = float(record.get("risk_usd") or 0)
    pnl_pct     = round((pnl_usd / risk) * 100, 2) if risk > 0 else 0.0
    duration    = _calc_duration(record.get("opened_at", ""))

    row = {
        "date":          record.get("date", date.today().isoformat()),
        "ticket":        record.get("ticket", ""),
        "symbol":        record.get("symbol", ""),
        "side":          record.get("side", ""),
        "fill_price":    record.get("fill_price", 0.0),
        "exit_price":    round(exit_price, 5),
        "planned_sl":    record.get("planned_sl", 0.0),
        "planned_tp":    record.get("planned_tp", 0.0),
        "lot_size":      record.get("lot_size", 0.0),
        "risk_usd":      risk,
        "pnl_usd":       round(pnl_usd, 2),
        "pnl_pct":       pnl_pct,
        "exit_reason":   exit_reason,
        "duration_min":  duration,
        "equity_at_open": record.get("equity_at_open", 0.0),
        "pnl_source":    pnl_source,
    }

    _append(row)
    log.info(
        "Journal: closed ticket=%s %s %s pnl=$%.2f reason=%s [%s]",
        row["ticket"], row["side"].upper(), row["symbol"],
        pnl_usd, exit_reason, pnl_source,
    )
    return row


# ── PnL calculation helpers ───────────────────────────────────────────────

def pnl_from_deal_history(position_id: int) -> tuple[float, str]:
    """
    Query MT5 deal history for the realized PnL of a closed position.
    Returns (pnl_usd, source_tag). Normalized if account is IDR.
    """
    deals = session.get_deals_by_position(position_id)
    if not deals:
        return 0.0, "unknown"

    # Filter to only trade deals (ignore separate commission/swap deals if present)
    # MT5 DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1
    trade_deals = [d for d in deals if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)]
    
    # Closing deal has entry = DEAL_ENTRY_OUT (1)
    close_deal = next(
        (d for d in trade_deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)),
        None
    )

    raw_pnl = 0.0
    if close_deal:
        # Sum profit + swap + commission from the trade deals
        raw_pnl = sum(float(d.profit) + float(d.swap) + float(d.commission) for d in trade_deals)
        source  = "deal_history"
    else:
        # Fallback: sum everything
        raw_pnl = sum(float(d.profit) + float(d.swap) + float(d.commission) for d in deals)
        source  = "deal_history_sum"

    # Normalize to USD if account is IDR
    if settings.ACCOUNT_CURRENCY == "IDR":
        pnl_usd = round(raw_pnl / settings.IDR_TO_USD_RATE, 2)
    else:
        pnl_usd = round(raw_pnl, 2)

    return pnl_usd, source


def pnl_from_calc(
    symbol: str,
    side:   str,
    lot:    float,
    open_p: float,
    close_p: float,
) -> tuple[float, str]:
    """
    Use MT5's built-in profit calculator.
    Accurate for live positions — handles cross-currency conversion.
    Requires active MT5 connection.
    """
    action = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    result = session.calc_profit(action, symbol, lot, open_p, close_p)
    if result is not None:
        return round(result, 2), "calc_profit"
    return 0.0, "unknown"


def classify_exit(
    symbol: str, side: str, fill: float, exit_p: float, sl: float, tp: float
) -> str:
    """
    Classify why a trade closed.
    Uses a dynamic tolerance based on symbol's point size to handle
    broker rounding differences (usually 2-3 points).
    """
    sym_info = session.get_symbol_info(symbol)
    point    = float(sym_info.point) if sym_info else 0.00001
    tol      = point * 10  # 10 points tolerance for crypto/volatility

    if side == "buy":
        if tp > 0 and exit_p >= tp - tol:  return "TP_Hit"
        if sl > 0 and exit_p <= sl + tol:  return "SL_Hit"
        return "Manual_Early" if exit_p > fill else "Manual_Late"
    else:
        if tp > 0 and exit_p <= tp + tol:  return "TP_Hit"
        if sl > 0 and exit_p >= sl - tol:  return "SL_Hit"
        return "Manual_Early" if exit_p < fill else "Manual_Late"


# ── Stats for circuit breaker and reports ────────────────────────────────

def get_daily_stats(target_date: str | None = None) -> dict:
    """
    Compute daily trading stats from journal.
    Used by CircuitBreaker and /report command.

    Returns:
        count          — number of closed trades today
        pnl_usd        — total net PnL in USD
        rr_cumulative  — total PnL / avg risk per trade (dimensionless RR ratio)
        win_rate       — % of trades with pnl_usd > 0
        wins           — count winning trades
        losses         — count losing trades
    """
    today = target_date or date.today().isoformat()
    if not _JOURNAL_PATH.exists():
        return _empty_stats()

    trades = []
    with open(_JOURNAL_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") == today:
                trades.append(row)

    if not trades:
        return _empty_stats()

    pnls  = [float(t["pnl_usd"]) for t in trades]
    risks = [float(t["risk_usd"]) for t in trades if float(t["risk_usd"]) > 0]

    total_pnl = sum(pnls)
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p < 0]
    avg_risk  = (sum(risks) / len(risks)) if risks else 1.0

    return {
        "count":         len(trades),
        "pnl_usd":       round(total_pnl, 2),
        "rr_cumulative": round(total_pnl / avg_risk, 2) if avg_risk > 0 else 0.0,
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "wins":          len(wins),
        "losses":        len(losses),
    }


def get_all_history() -> list[dict]:
    if not _JOURNAL_PATH.exists():
        return []
    with open(_JOURNAL_PATH, newline="") as f:
        return list(csv.DictReader(f))


# ── Internal helpers ──────────────────────────────────────────────────────

def _calc_duration(opened_at: str) -> float:
    try:
        t = datetime.fromisoformat(opened_at)
        return round((datetime.utcnow() - t).total_seconds() / 60, 1)
    except Exception:
        return 0.0


def _empty_stats() -> dict:
    return {
        "count": 0, "pnl_usd": 0.0, "rr_cumulative": 0.0,
        "win_rate": 0.0, "wins": 0, "losses": 0,
    }


def _append(row: dict) -> None:
    _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = _JOURNAL_PATH.exists()
    with open(_JOURNAL_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)
