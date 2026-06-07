"""
ExposureGate — pre-execution exposure check.

Layers (in order):
  1. Symbol: no existing position for this symbol (checks MT5 directly, not just local store)
  2. Count:  total open positions < MAX_OPEN_POSITIONS
  3. Equity: total open risk% ≤ MAX_TOTAL_RISK_PCT
  4. Corr:   positions in same correlation group ≤ MAX_CORRELATED
  5. Margin: margin level ≥ MIN_MARGIN_LEVEL_PCT
  6. Stops:  SL/TP distance ≥ broker's minimum stop level for this symbol

Why Layer 1 checks MT5 directly (not only local store):
  Local positions.json is a cache. It can be out of date if:
  - A position was opened manually in MT5 terminal
  - Bot crashed before writing to JSON
  - Reconciler hasn't run yet (runs every 5 min)
  Checking MT5 directly is the only authoritative source.
  Cost: one extra MT5 API call per symbol per scan cycle (~1ms). Acceptable.

Why Layer 5 (margin) matters:
  Exness Standard: margin call at 100% margin level.
  If margin level drops to 150% and bot opens another position,
  one bad candle could trigger margin call on ALL positions simultaneously.
  Buffer of 300% gives ~3× headroom before margin call.

Why Layer 6 (stop level) matters:
  Every broker has a minimum distance between entry price and SL/TP.
  If violated: order rejected with TRADE_RETCODE_INVALID_STOPS (fatal — no retry).
  Exness forex pairs: usually 0 stop level (no minimum), but can be non-zero
  during volatile sessions. Exotic pairs and metals can have significant stop levels.
  Checking proactively avoids sending a doomed order and gives a clear skip reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import MetaTrader5 as mt5

from config import settings
from core.connection.mt5_session import session
from core.state.position_store import (
    count_open_positions,
    get_all_positions,
    get_total_risk_usd,
)

log = logging.getLogger(__name__)

# ── Configurable limits (read from settings with fallback) ────────────────
MAX_OPEN_POSITIONS = getattr(settings, "MAX_OPEN_POSITIONS", 5)
MAX_TOTAL_RISK_PCT = getattr(settings, "MAX_TOTAL_RISK_PCT", 5.0)
MAX_CORRELATED     = getattr(settings, "MAX_CORRELATED", 2)
MIN_MARGIN_LEVEL   = getattr(settings, "MIN_MARGIN_LEVEL_PCT", 300.0)

# Correlation groups — pairs sharing a dominant currency move together.
# Entering more than MAX_CORRELATED from the same group multiplies hidden exposure.
CORRELATION_GROUPS: list[frozenset[str]] = [
    frozenset({"EURUSD", "GBPUSD", "EURGBP"}),
    frozenset({"EURUSD", "EURJPY", "EURCHF", "EURAUD"}),
    frozenset({"GBPUSD", "GBPJPY", "EURGBP"}),
    frozenset({"USDJPY", "EURJPY", "GBPJPY", "CADJPY", "AUDJPY"}),
    frozenset({"AUDUSD", "NZDUSD", "AUDJPY"}),
    frozenset({"USDCAD", "CADJPY"}),
]


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason:  str


def check_entry(symbol: str, risk_usd: float, sl: float = 0.0, tp: float = 0.0) -> GateResult:
    """
    Multi-layer pre-execution check. All layers must pass.

    Args:
        symbol:   Trading symbol (e.g. "EURUSD")
        risk_usd: Calculated risk in USD for this trade
        sl:       Planned stop-loss price (used for stop level validation)
        tp:       Planned take-profit price (used for stop level validation)

    Returns GateResult(allowed=True, reason="ok") if all pass.
    Returns GateResult(allowed=False, reason=<first failure>) on any failure.
    """

    # ── Layer 1: No existing position for this symbol ─────────────────────
    # Check MT5 directly — authoritative, not dependent on local store sync state
    mt5_symbol_positions = session.get_positions_by_symbol(symbol)
    if mt5_symbol_positions:
        tickets = [p.ticket for p in mt5_symbol_positions]
        return GateResult(
            False,
            f"{symbol} already has {len(mt5_symbol_positions)} position(s) in MT5: {tickets}",
        )

    # ── Layer 2: Total open position count ───────────────────────────────
    # Use local store for count (faster, and Layer 1 already verified MT5 state)
    open_count = count_open_positions()
    if open_count >= MAX_OPEN_POSITIONS:
        return GateResult(
            False,
            f"Jumlah posisi maksimal tercapai ({open_count}/{MAX_OPEN_POSITIONS}).",
        )

    # ── Layer 3: Total risk % of equity ──────────────────────────────────
    account = session.get_account_info()
    if account:
        equity     = float(account.equity)
        if equity <= 0:
            return GateResult(False, "Equity nol atau negatif.")

        total_risk = get_total_risk_usd() + risk_usd
        risk_pct   = (total_risk / equity) * 100
        if risk_pct > MAX_TOTAL_RISK_PCT:
            return GateResult(
                False,
                f"Total risiko {risk_pct:.1f}% melampaui batas {MAX_TOTAL_RISK_PCT}%.",
            )

    # ── Layer 4: Correlation group cap ───────────────────────────────────
    open_symbols = {p["symbol"] for p in get_all_positions().values()}

    for group in CORRELATION_GROUPS:
        if symbol not in group:
            continue
        overlap = open_symbols & group
        if len(overlap) >= MAX_CORRELATED:
            return GateResult(
                False,
                f"Limit korelasi: {symbol} searah dengan {sorted(overlap)}.",
            )

    # ── Layer 5: Margin level protection ─────────────────────────────────
    # Only relevant if positions are already open (margin > 0)
    if account and account.margin > 0:
        margin_level = (account.equity / account.margin) * 100
        if margin_level < MIN_MARGIN_LEVEL:
            return GateResult(
                False,
                f"Margin level terlalu rendah ({margin_level:.0f}% < {MIN_MARGIN_LEVEL:.0f}%).",
            )

    # ── Layer 6: Broker stop level validation ────────────────────────────
    # Only run if SL and TP prices are provided (non-zero)
    if sl > 0 and tp > 0:
        stop_result = _check_stop_levels(symbol, sl, tp)
        if stop_result is not None:
            return stop_result

    log.debug(
        "ExposureGate PASS: %s positions=%d risk=$%.2f",
        symbol, open_count, risk_usd,
    )
    return GateResult(True, "ok")


def _check_stop_levels(symbol: str, sl: float, tp: float) -> GateResult | None:
    """
    Validate that SL and TP are at least `stops_level` points away from current price.

    Returns GateResult(False, ...) if too close.
    Returns None if validation passes or cannot be performed.

    Why this matters:
    MT5 brokers define a minimum distance between the current price and any
    pending stop or limit order. If violated, the order is rejected with
    TRADE_RETCODE_INVALID_STOPS. This is a fatal error — no retry will help.
    Checking proactively avoids the round-trip to the broker and gives a clear reason.

    Exness specifics:
    - Most major forex pairs: stops_level = 0 (no minimum)
    - During high volatility: can temporarily increase
    - Metals (XAUUSD): can have stops_level > 0
    - Check value from symbol_info, not hardcoded
    """
    info = session.get_symbol_info(symbol)
    if info is None:
        return None  # Cannot validate — allow and let broker reject if needed

    stops_level = int(info.trade_stops_level)
    if stops_level == 0:
        return None  # Broker has no minimum — nothing to check

    tick = session.get_tick(symbol)
    if tick is None:
        return None  # Cannot validate without price

    mid_price  = (tick.bid + tick.ask) / 2
    point      = float(info.point)    # smallest price movement for this symbol
    min_dist   = stops_level * point  # minimum distance in price units

    sl_dist = abs(mid_price - sl)
    tp_dist = abs(mid_price - tp)

    if sl_dist < min_dist:
        return GateResult(
            False,
            f"SL too close: {sl:.5f} is {sl_dist/point:.0f} points from price "
            f"(broker minimum: {stops_level} points). "
            f"Order would be rejected with INVALID_STOPS.",
        )

    if tp_dist < min_dist:
        return GateResult(
            False,
            f"TP too close: {tp:.5f} is {tp_dist/point:.0f} points from price "
            f"(broker minimum: {stops_level} points). "
            f"Order would be rejected with INVALID_STOPS.",
        )

    return None
