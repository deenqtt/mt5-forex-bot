"""
SessionDetector — identify current Forex market session and return
session-specific trading parameters.

Session hours (UTC) — from SESSION_STRATEGY.md blueprint:
  Asia/Tokyo  : 23:00 – 08:00   Low volatility, ranging, scalping
  London      : 07:00 – 16:00   High liquidity, trending
  New York    : 12:00 – 21:00   High volatility, aggressive
  Overlap     : 12:00 – 16:00   Peak liquidity — "Golden Hour" ⭐
  Off hours   : 21:00 – 23:00   Dead zone — no new entries

Priority (checked in order): Overlap > London > New York > Asia > Off Hours

Parameters per session:
  max_positions : session-specific cap (used by ExposureGate, ≤ global MAX_OPEN_POSITIONS)
  spread_mult   : multiplier on base symbol spread limits
                  < 1.0 = stricter (scalping needs tight spread)
                  > 1.0 = looser   (high-vol sessions allow wider spread)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SessionParams:
    name:          str    # "asia" | "london" | "new_york" | "overlap" | "off_hours"
    label:         str    # human-readable (used in Telegram /status)
    max_positions: int    # max simultaneous entries this session (0 = no new entries)
    spread_mult:   float  # applied to per-symbol spread limits in SpreadFilter


# ── Session definitions ───────────────────────────────────────────────────

ASIA = SessionParams(
    name="asia",
    label="Asia/Tokyo 🌏",
    max_positions=3,
    spread_mult=0.75,   # scalping mean-reversion needs tight spread
)
LONDON = SessionParams(
    name="london",
    label="London 🇬🇧",
    max_positions=5,
    spread_mult=1.0,
)
NEW_YORK = SessionParams(
    name="new_york",
    label="New York 🗽",
    max_positions=4,
    spread_mult=1.1,    # higher volatility — slightly looser spread acceptable
)
OVERLAP = SessionParams(
    name="overlap",
    label="London/NY Overlap ⭐",
    max_positions=5,    # peak liquidity — full position budget
    spread_mult=1.0,
)
OFF_HOURS = SessionParams(
    name="off_hours",
    label="Off Hours 🌙",
    max_positions=0,    # no new entries — ExposureGate blocks via count cap
    spread_mult=1.0,
)


# ── Core function ─────────────────────────────────────────────────────────

def get_current_session() -> SessionParams:
    """
    Return session params for current UTC hour.

    Overlap (12-16) checked FIRST — it is a subset of both London and NY.
    Asia wraps midnight: hour >= 23 OR hour < 8.
    """
    hour = datetime.now(timezone.utc).hour

    if 12 <= hour < 16:          # London/NY overlap — "Golden Hour"
        return OVERLAP
    if 7 <= hour < 16:           # London (non-overlap hours 07-12)
        return LONDON
    if 12 <= hour < 21:          # New York (non-overlap hours 16-21)
        return NEW_YORK
    if hour >= 23 or hour < 8:   # Asia/Tokyo (23:00-08:00 UTC)
        return ASIA
    return OFF_HOURS             # 21:00-23:00 UTC dead zone


def session_summary() -> str:
    """One-line summary for Telegram /status display."""
    s = get_current_session()
    now = datetime.now(timezone.utc)
    return (
        f"{s.label}  "
        f"[max {s.max_positions} pos | spread ×{s.spread_mult}]  "
        f"— {now.strftime('%H:%M')} UTC"
    )
