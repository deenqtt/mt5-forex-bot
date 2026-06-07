"""
CircuitBreaker — multi-level loss protection for auto-execution.

Original had one circuit breaker: cumulative RR ≤ -2.0.
Problems:
- Only triggered after journal entries exist (no protection on very first loss)
- RR calculation was fragile (division by avg risk)
- No equity drawdown tracking
- No absolute USD loss cap option

New design: 3 independent checks, any one can halt execution.

Layer 1: Daily cumulative RR (trades × average risk-reward)
Layer 2: Session equity drawdown % from start-of-session baseline
Layer 3: Optional absolute USD daily loss cap

CircuitBreaker is a singleton to preserve session_start_equity across job ticks.
Initialize once at startup via circuit_breaker.initialize_session().
"""

from __future__ import annotations

import logging
import threading

from config import settings
from core.connection.mt5_session import session
from core.state.trade_journal import get_daily_stats

log = logging.getLogger(__name__)

# ── Thresholds (can override via settings) ────────────────────────────────
_DAILY_RR_LIMIT     = getattr(settings, "AUTO_EXEC_DAILY_LOSS_LIMIT", -2.0)
_DAILY_USD_LIMIT    = getattr(settings, "AUTO_EXEC_DAILY_USD_LIMIT", None)
_EQUITY_DD_PCT      = getattr(settings, "AUTO_EXEC_EQUITY_DRAWDOWN_PCT", 5.0)


class CircuitBreaker:
    """
    Singleton — preserves session equity baseline across all job ticks.
    Use module-level `circuit_breaker` instance, not direct instantiation.
    """
    _instance: "CircuitBreaker | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "CircuitBreaker":
        with cls._lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._session_equity: float | None = None
                cls._instance = obj
        return cls._instance

    def initialize_session(self) -> None:
        """
        Call once at startup. Records equity baseline for drawdown tracking.
        If called again (restart), resets the baseline.
        """
        from core.mt5_broker import MT5BrokerManager
        broker = MT5BrokerManager()
        equity = broker.get_equity()
        if equity > 0:
            self._session_equity = equity
            log.info(
                "CircuitBreaker: session baseline equity = $%.2f",
                self._session_equity,
            )
        else:
            log.warning("CircuitBreaker: cannot get account info or equity is 0 at startup")

    def check(self) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        """
        stats = get_daily_stats()

        # Layer 1: Cumulative RR
        rr = stats["rr_cumulative"]
        if rr <= _DAILY_RR_LIMIT:
            return False, (
                f"Daily RR limit: {rr:.2f} ≤ {_DAILY_RR_LIMIT}  "
                f"({stats['losses']}L/{stats['wins']}W  ${stats['pnl_usd']:.2f})"
            )

        # Layer 2: Absolute USD loss (optional)
        if _DAILY_USD_LIMIT is not None and stats["pnl_usd"] <= _DAILY_USD_LIMIT:
            return False, (
                f"Daily USD limit: ${stats['pnl_usd']:.2f} ≤ ${_DAILY_USD_LIMIT:.2f}"
            )

        # Layer 3: Session equity drawdown
        if self._session_equity and self._session_equity > 0:
            from core.mt5_broker import MT5BrokerManager
            broker = MT5BrokerManager()
            equity = broker.get_equity()
            dd_pct = (self._session_equity - equity) / self._session_equity * 100
            if dd_pct >= _EQUITY_DD_PCT:
                return False, (
                    f"Equity drawdown {dd_pct:.1f}% ≥ limit {_EQUITY_DD_PCT}%  "
                    f"(start=${self._session_equity:.2f}  now=${equity:.2f})"
                )

        return True, "ok"

    def status(self) -> dict:
        """For /status command display."""
        stats   = get_daily_stats()
        from core.mt5_broker import MT5BrokerManager
        broker  = MT5BrokerManager()
        equity  = broker.get_equity()
        
        dd_pct  = 0.0
        if self._session_equity and self._session_equity > 0:
            dd_pct = (self._session_equity - equity) / self._session_equity * 100

        return {
            "rr_today":     stats["rr_cumulative"],
            "pnl_usd":      stats["pnl_usd"],
            "trades_today": stats["count"],
            "wins":         stats["wins"],
            "losses":       stats["losses"],
            "dd_pct":       round(dd_pct, 2),
            "rr_limit":     _DAILY_RR_LIMIT,
            "dd_limit":     _EQUITY_DD_PCT,
        }


# Module-level singleton
circuit_breaker = CircuitBreaker()
