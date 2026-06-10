"""
SessionFilter — skip low-liquidity trading windows.

Original scanned 24/7 including:
- Weekends (market closed — any signal is noise)
- Asian session for EUR/GBP pairs (thin liquidity, choppy)
- Sunday open (wide spread period)
- Friday close (position risk before weekly gap)

This is distinct from weekend_close_job (which closes positions).
This prevents OPENING new positions during bad hours.

Filter is permissive by default: if a symbol is not recognized,
allow entry (fail open). Tight times only for known pairs.

All times are UTC.
"""

from __future__ import annotations

import MetaTrader5 as mt5
from datetime import datetime, timezone
from core.connection.mt5_session import session


def is_valid_session(symbol: str) -> bool:
    """
    Returns True if current UTC time is within acceptable trading hours
    AND the broker explicitly reports the symbol as open for trading.
    """
    # 1. Check Broker Trade Mode (Authoritative)
    info = session.get_symbol_info(symbol)
    if info is not None:
        # trade_mode 0 = Disabled, 4 = Full access (Buy/Sell allowed)
        # We only want to scan/trade if full access is enabled
        if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            return False

    # 2. Heuristic Session Filters (Time-based)
    now     = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    hour    = now.hour

    # Crypto Exemption: BTC, XRP, ETH, LTC, etc usually trade 24/7
    # If the broker says it's OPEN (trade_mode=4 above), we allow it.
    crypto_keywords = ("BTC", "XRP", "ETH", "LTC", "SOL", "ADA", "DOGE")
    if any(k in symbol.upper() for k in crypto_keywords):
        return True

    # Weekend: market closed (For Forex/Gold)
    if weekday == 5:   # Saturday — always closed
        return False
    if weekday == 6 and hour < 21:  # Sunday until ~21:00 UTC (market opens)
        return False
    if weekday == 4 and hour >= 21:  # Friday after 21:00 UTC — avoid new positions
        return False

    # Symbol-specific session windows
    if any(x in symbol for x in ("EUR", "GBP", "CHF")):
        # European + NY overlap: London open 07:00 – NY close 21:00
        return 7 <= hour < 21

    if "JPY" in symbol:
        # Tokyo: 00:00–09:00, London: 07:00–16:00
        return hour < 16 or 0 <= hour < 9

    if "XAU" in symbol:
        # Gold trades 23/5. Allow all except daily rollover gap (21:00-22:00 UTC)
        # and weekend (handled by global filter above).
        return hour < 21 or hour >= 22

    if any(x in symbol for x in ("AUD", "NZD")):
        # Sydney: 22:00–07:00 UTC, London overlap: 07:00–09:00
        return hour >= 22 or hour < 9 or 7 <= hour < 16

    if any(x in symbol for x in ("CAD", "USD")):
        # NY session: 13:00–21:00 UTC
        return 13 <= hour < 21

    # Unknown symbol — allow
    return True


def describe_session(symbol: str) -> str:
    """Human-readable description for /status display."""
    now     = datetime.now(timezone.utc)
    allowed = is_valid_session(symbol)
    return (
        f"{'✅' if allowed else '⛔'} {symbol} session "
        f"({'OK' if allowed else 'CLOSED'}) — {now.strftime('%H:%M')} UTC"
    )
