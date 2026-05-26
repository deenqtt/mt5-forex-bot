"""
SpreadFilter — reject entries when spread is abnormally wide.

Original had no spread check. Entering during:
- News release: spread can spike 5–50× normal
- Market open (Sunday 17:00 ET): spread wide until liquidity builds
- Low-liquidity hours: Asian session for EUR/GBP pairs

Risk: SL might be inside the spread at entry, meaning the position
      is immediately negative by more than the intended risk.
      With SL = 50 pip and spread = 20 pip, effective SL = 30 pip
      but position starts 20 pip in the red.

Max spread values are conservative — tighten for lower-spread brokers.
Exness Raw/Zero accounts have tighter spreads than Standard.
"""

from __future__ import annotations

import logging

from config import settings
from core.connection.mt5_session import session

log = logging.getLogger(__name__)

# Max acceptable spread in pips per symbol (or DEFAULT for unlisted)
_MAX_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 2.0,
    "GBPUSD": 2.5,
    "USDJPY": 2.0,
    "USDCHF": 2.5,
    "AUDUSD": 2.5,
    "USDCAD": 2.5,
    "NZDUSD": 3.0,
    "EURGBP": 2.5,
    "EURJPY": 2.5,
    "GBPJPY": 3.0,
    "XAUUSD": 40.0,  # Gold: 1 pip = $0.01, normal spread ~30–50 pip equivalent
    "DEFAULT": 4.0,
}


def is_spread_acceptable(symbol: str) -> bool:
    """
    Returns True if current spread ≤ configured max for this symbol.
    Returns True (allow) if tick data is unavailable — fail open to not block
    execution during data gaps. Caller should log if needed.
    """
    tick = session.get_tick(symbol)
    if tick is None:
        log.warning("SpreadFilter: no tick data for %s — allowing", symbol)
        return True

    spread_price = tick.ask - tick.bid
    if spread_price <= 0:
        return True

    # Convert price difference to pips
    if symbol in settings.JPY_PAIRS:
        pip_size = 0.01
    elif "XAU" in symbol:
        pip_size = 0.01   # XAUUSD: 1 pip = $0.01 price move
    else:
        pip_size = 0.0001

    spread_pips = spread_price / pip_size
    max_pips    = _MAX_SPREAD_PIPS.get(symbol, _MAX_SPREAD_PIPS["DEFAULT"])

    if spread_pips > max_pips:
        log.warning(
            "SpreadFilter REJECT: %s spread=%.1f pip > max=%.1f pip",
            symbol, spread_pips, max_pips,
        )
        return False

    return True


def get_spread_pips(symbol: str) -> float | None:
    """Return current spread in pips, or None if tick unavailable."""
    tick = session.get_tick(symbol)
    if tick is None:
        return None
    if symbol in settings.JPY_PAIRS:
        pip_size = 0.01
    elif "XAU" in symbol:
        pip_size = 0.01
    else:
        pip_size = 0.0001
    return round((tick.ask - tick.bid) / pip_size, 1)
