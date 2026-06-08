"""
BotState — persist auto_scan and auto_exec flags across restarts.

Problem this solves:
  context.bot_data is in-memory only. On bot restart:
  - auto_exec_active resets to False (safe, conservative default)
  - But if the circuit breaker tripped and stopped the bot, user doesn't know why
  - If auto_exec was legitimately on, user must re-enable manually after every restart

  More critically: circuit_breaker triggered → sets auto_exec_active=False →
  bot restarts → auto_exec_active=False (lost) → user re-enables not knowing why it stopped
  → circuit breaker trips again → same loop

This module persists:
  1. Whether auto modes were active
  2. Whether they were stopped by circuit breaker (vs manual stop)
  3. The reason (for user awareness on restart)

Usage in main.py startup:
    state = load_bot_state()
    context.bot_data["auto_mode_active"] = state["auto_scan"]
    context.bot_data["auto_exec_active"] = state["auto_exec"]
    if state.get("stopped_by_circuit_breaker"):
        await send alert to user

Usage when state changes:
    save_bot_state(auto_scan=True, auto_exec=False,
                   stopped_by_cb=True, cb_reason="Daily RR -2.0")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_STATE_FILE = Path("data/bot_state.json")


def save_bot_state(
    auto_scan: bool,
    auto_exec: bool,
    stopped_by_circuit_breaker: bool = False,
    circuit_breaker_reason: str = "",
    scan_alerted: dict | None = None,
) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "auto_scan":               auto_scan,
        "auto_exec":               auto_exec,
        "stopped_by_cb":           stopped_by_circuit_breaker,
        "cb_reason":               circuit_breaker_reason,
        "scan_alerted":            scan_alerted or {},
        "saved_at":                datetime.utcnow().isoformat(),
    }
    try:
        _STATE_FILE.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        log.error("Failed to save bot state: %s", exc)


def load_bot_state() -> dict:
    """
    Returns saved state or safe defaults (both modes OFF).
    Always returns a complete dict with all keys.
    """
    defaults = {
        "auto_scan": False,
        "auto_exec": False,
        "stopped_by_cb": False,
        "cb_reason": "",
        "scan_alerted": {},
        "saved_at": "",
    }
    if not _STATE_FILE.exists():
        return defaults
    try:
        data = json.loads(_STATE_FILE.read_text())
        return {**defaults, **data}
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load bot state: %s — using safe defaults", exc)
        return defaults


def clear_circuit_breaker_flag() -> None:
    """Call when user manually re-enables auto_exec after CB trip."""
    state = load_bot_state()
    state["stopped_by_cb"] = False
    state["cb_reason"]     = ""
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass
