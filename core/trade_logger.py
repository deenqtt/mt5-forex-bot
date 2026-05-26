"""
Trade logger: persists open trades and completed trade history.

open_trades.json  — keyed by symbol, written at entry
trade_history.csv — append-only, written at exit
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path


OPEN_TRADES_PATH   = Path("data/open_trades.json")
TRADE_HISTORY_PATH = Path("data/trade_history.csv")

HISTORY_FIELDS = [
    "date", "symbol", "side", "entry_price", "exit_price",
    "planned_sl", "planned_tp", "lot_size", "risk_usd",
    "pnl_usd", "pnl_pct", "exit_reason", "ticket", "duration_min",
]


def _load_open_trades() -> dict:
    if OPEN_TRADES_PATH.exists():
        with open(OPEN_TRADES_PATH) as f:
            return json.load(f)
    return {}


def _save_open_trades(data: dict) -> None:
    OPEN_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OPEN_TRADES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _ensure_history_header() -> None:
    TRADE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TRADE_HISTORY_PATH.exists():
        with open(TRADE_HISTORY_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=HISTORY_FIELDS).writeheader()


def log_open_trade(
    symbol: str,
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    lot_size: float,
    risk_usd: float,
    ticket: int,
    equity: float,
) -> None:
    trades = _load_open_trades()
    trades[symbol] = {
        "symbol":      symbol,
        "side":        side,
        "entry_price": entry_price,
        "planned_sl":  sl_price,
        "planned_tp":  tp_price,
        "lot_size":    lot_size,
        "risk_usd":    risk_usd,
        "ticket":      ticket,
        "equity":      equity,
        "opened_at":   datetime.utcnow().isoformat(),
        "date":        date.today().isoformat(),
    }
    _save_open_trades(trades)


def get_open_trade(symbol: str) -> dict | None:
    return _load_open_trades().get(symbol)


def get_all_open_trades() -> dict:
    return _load_open_trades()


def log_close_trade(symbol: str, exit_price: float, exit_reason: str | None = None) -> dict | None:
    trades = _load_open_trades()
    trade  = trades.pop(symbol, None)
    if trade is None:
        return None

    _save_open_trades(trades)

    entry  = float(trade["entry_price"])
    sl     = float(trade["planned_sl"])
    tp     = float(trade["planned_tp"])
    lot    = float(trade["lot_size"])
    risk   = float(trade["risk_usd"])
    side   = trade["side"]

    if side == "buy":
        pnl_usd = (exit_price - entry) * lot * 100000  # rough pip PnL
    else:
        pnl_usd = (entry - exit_price) * lot * 100000

    pnl_pct    = (pnl_usd / risk) * 100 if risk > 0 else 0.0
    reason     = exit_reason or _classify_exit(side, entry, exit_price, sl, tp)

    try:
        opened_at    = datetime.fromisoformat(trade["opened_at"])
        duration_min = round((datetime.utcnow() - opened_at).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0.0

    row = {
        "date":         trade["date"],
        "symbol":       symbol,
        "side":         side,
        "entry_price":  entry,
        "exit_price":   exit_price,
        "planned_sl":   sl,
        "planned_tp":   tp,
        "lot_size":     lot,
        "risk_usd":     risk,
        "pnl_usd":      round(pnl_usd, 2),
        "pnl_pct":      round(pnl_pct, 2),
        "exit_reason":  reason,
        "ticket":       trade.get("ticket", ""),
        "duration_min": duration_min,
    }

    _ensure_history_header()
    with open(TRADE_HISTORY_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=HISTORY_FIELDS).writerow(row)

    return {**trade, **row}


def get_daily_risk_usd(session_date: str | None = None) -> float:
    """Cumulative RR for today."""
    target = session_date or date.today().isoformat()
    if not TRADE_HISTORY_PATH.exists():
        return 0.0
    risks, total = [], 0.0
    with open(TRADE_HISTORY_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") == target:
                r = float(row.get("risk_usd", 0))
                if r > 0:
                    risks.append(r)
                    total += float(row.get("pnl_usd", 0))
    if not risks:
        return 0.0
    avg_risk = sum(risks) / len(risks)
    return round(total / avg_risk, 2)


def _classify_exit(side: str, entry: float, exit_price: float, sl: float, tp: float) -> str:
    if side == "buy":
        if exit_price >= tp:
            return "TP_Hit"
        if exit_price <= sl:
            return "SL_Hit"
        if exit_price > entry:
            return "Manual_Early"
        return "Manual_Late"
    else:
        if exit_price <= tp:
            return "TP_Hit"
        if exit_price >= sl:
            return "SL_Hit"
        if exit_price < entry:
            return "Manual_Early"
        return "Manual_Late"
