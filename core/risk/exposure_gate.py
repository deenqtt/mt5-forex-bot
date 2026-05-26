"""
ExposureGate — pre-execution exposure check.

Problems this fixes:

1. No max open positions limit in original.
   With 10 symbols scanned, bot could open all 10 simultaneously.
   At 1% risk each = 10% equity at risk. Acceptable per trade, not per session.

2. No correlation awareness.
   EURUSD and GBPUSD move together ~85% of the time.
   Opening both long = 2% explicit risk but 2% correlated exposure.
   In a risk-off event, both stop out together.
   Limit: max 2 positions from the same correlation group.

3. No total risk % cap.
   If balance grows, 1% per trade in absolute terms grows proportionally.
   But concurrent open positions multiply total exposure.
   Hard cap: total open risk ≤ 5% of equity.

4. Symbol-level duplicate guard moved here from ad-hoc checks in job functions.
   One central gate, one place to audit.

check_entry() is called synchronously before every order_engine.market_order().
Returns (allowed, reason) — caller logs/alerts on rejection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.connection.mt5_session import session
from core.state.position_store import (
    count_open_positions,
    get_positions_for_symbol,
    get_total_risk_usd,
)

log = logging.getLogger(__name__)

# ── Configurable limits ───────────────────────────────────────────────────
MAX_OPEN_POSITIONS = 5      # absolute cap on simultaneous positions
MAX_TOTAL_RISK_PCT = 5.0    # total open risk as % of equity
MAX_CORRELATED     = 2      # max positions from same correlation group

# Pairs that move together — entering multiple from one group multiplies hidden exposure.
# Conservative grouping: pairs sharing a dominant currency.
CORRELATION_GROUPS: list[frozenset[str]] = [
    frozenset({"EURUSD", "GBPUSD", "EURGBP"}),      # EUR/GBP complex
    frozenset({"EURUSD", "EURJPY", "EURCHF", "EURAUD"}),  # EUR crosses
    frozenset({"GBPUSD", "GBPJPY", "EURGBP"}),      # GBP crosses
    frozenset({"USDJPY", "EURJPY", "GBPJPY", "CADJPY", "AUDJPY"}),  # JPY crosses
    frozenset({"AUDUSD", "NZDUSD", "AUDJPY"}),      # commodity currencies
    frozenset({"USDCAD", "CADJPY"}),                 # CAD pairs
]


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason:  str


def check_entry(symbol: str, risk_usd: float) -> GateResult:
    """
    Multi-layer entry check. All layers must pass.

    Layer 1: Symbol — no existing position for this exact symbol
    Layer 2: Count  — total open positions < MAX_OPEN_POSITIONS
    Layer 3: Equity — total risk% would stay ≤ MAX_TOTAL_RISK_PCT
    Layer 4: Corr   — not too many positions in same correlation group

    Returns GateResult(allowed=True) if all pass.
    Returns GateResult(allowed=False, reason=<first failure>) otherwise.
    """

    # Layer 1: Symbol-level duplicate guard
    existing = get_positions_for_symbol(symbol)
    if existing:
        tickets = [p["ticket"] for p in existing]
        return GateResult(
            False,
            f"{symbol} already has position(s) {tickets} — skip",
        )

    # Layer 2: Position count cap
    open_count = count_open_positions()
    if open_count >= MAX_OPEN_POSITIONS:
        return GateResult(
            False,
            f"Max positions reached ({open_count}/{MAX_OPEN_POSITIONS})",
        )

    # Layer 3: Total risk % cap
    account = session.get_account_info()
    if account:
        equity     = float(account.equity)
        total_risk = get_total_risk_usd() + risk_usd
        risk_pct   = (total_risk / equity) * 100 if equity > 0 else 0.0
        if risk_pct > MAX_TOTAL_RISK_PCT:
            return GateResult(
                False,
                f"Total risk {risk_pct:.1f}% would exceed {MAX_TOTAL_RISK_PCT}% cap",
            )

    # Layer 4: Correlation group cap
    from core.state.position_store import get_all_positions
    open_symbols = {p["symbol"] for p in get_all_positions().values()}

    for group in CORRELATION_GROUPS:
        if symbol not in group:
            continue
        overlap = open_symbols & group
        if len(overlap) >= MAX_CORRELATED:
            return GateResult(
                False,
                f"Correlation limit: {symbol} shares group with open {sorted(overlap)}",
            )

    log.debug(
        "ExposureGate: %s allowed — positions=%d risk_usd=%.2f",
        symbol, open_count, risk_usd,
    )
    return GateResult(True, "ok")
