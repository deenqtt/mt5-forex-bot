"""
main.py — Bot entry point and job scheduler.

Architecture changes from original:

1. Single MT5 connection:
   session.connect() called once here at startup.
   All modules share the same MT5Session singleton.
   No module creates or destroys MT5 connections independently.

2. Startup reconciliation:
   reconcile() runs before any job fires.
   Detects positions closed during downtime (SL/TP hit while bot was offline).
   Detects orphan positions opened outside the bot.

3. Session equity baseline:
   circuit_breaker.initialize_session() records starting equity once.
   All subsequent drawdown calculations are relative to this baseline.

4. Execution pipeline (auto_execute_job):
   circuit_breaker.check()   → halt if daily loss/drawdown limits hit
   exposure_gate.check_entry() → halt if position/risk/correlation limits hit
   spread_filter              → skip if spread too wide
   session_filter             → skip if outside trading hours for pair
   order_engine.market_order() → execute with retry, capture actual fill price
   position_store.open_position() → atomic write with actual fill price

5. Periodic reconciler job (every 5 minutes):
   Catches any positions that slipped through normal monitor.
   Safety net against state desync.

6. Bot_data removed from position tracking:
   All state is in position_store (file) and circuit_breaker (singleton).
   Telegram context.bot_data is only used for UI state (auto_mode_active,
   auto_exec_active, top_symbols_cache, scan_alerted cooldowns).
"""

import logging
import time
import json
import os
from datetime import datetime, timezone

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

from config import settings
from core.connection.mt5_session import session
from core.monitoring.position_monitor import run as monitor_run
from core.risk.circuit_breaker import circuit_breaker
from core.risk.exposure_gate import check_entry
from core.risk.session_filter import is_valid_session
from core.risk.spread_filter import is_spread_acceptable
from core.state.position_store import (
    has_open_position, open_position,
)
from core.state.reconciler import reconcile
from core.state.bot_state import load_bot_state, save_bot_state, clear_circuit_breaker_flag
from telegram_interface import bot_handlers

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
# Suppress TA-Lib requirement warnings from pandas_ta
logging.getLogger("pandas_ta").setLevel(logging.ERROR)

log = logging.getLogger(__name__)

AUTO_SCAN_INTERVAL    = 15  # Scalping: faster scans (15s)
TP_MONITOR_INTERVAL   = 10  # Scalping: monitor TP/SL every 10s
RECONCILE_INTERVAL    = settings.RECONCILE_INTERVAL  # 300s
TOP_SYMBOLS_N         = settings.TOP_SYMBOLS_N
TOP_SYMBOLS_CACHE_TTL = settings.TOP_SYMBOLS_CACHE_TTL


# ── Symbol cache helpers ────────────────────────────────────────────────────

def _get_scan_symbols(context, broker) -> list[str]:
    cache = context.bot_data.setdefault("top_symbols_cache", {})
    if time.time() - cache.get("refreshed_at", 0) > TOP_SYMBOLS_CACHE_TTL:
        symbols = broker.get_top_symbols(n=TOP_SYMBOLS_N)
        if symbols:
            cache["symbols"]      = symbols
            cache["refreshed_at"] = time.time()
            log.info("Symbols refreshed: %s", symbols)
    return cache.get("symbols") or ["EURUSD", "GBPUSD", "USDJPY"]


# ── Composite signal scoring ────────────────────────────────────────────────

def _compute_composite(signals: dict) -> tuple[int, int]:
    """Returns (composite_score, threshold). |score| must reach threshold to act."""
    regime = signals.get("regime", "transition")
    rsi    = signals.get("rsi", 50.0)

    if regime == "trending":
        ema_s = 1 if signals["ema_trend"] == "bullish" else (-1 if signals["ema_trend"] == "bearish" else 0)
        rsi_s = 1 if rsi >= 55 else (-1 if rsi <= 45 else 0)
        dmp, dmn = signals.get("dmp", 0), signals.get("dmn", 0)
        di_s  = 1 if dmp > dmn else (-1 if dmn > dmp else 0)
        return ema_s + rsi_s + di_s, 2

    elif regime == "ranging":
        rsi_s = 1 if rsi <= 35 else (-1 if rsi >= 65 else 0)
        bb_s  = 1 if signals.get("bb_percent", 0.5) <= 0.15 else (-1 if signals.get("bb_percent", 0.5) >= 0.85 else 0)
        return rsi_s + bb_s, 2

    else:  # transition — no entry
        return 0, 99


# ── Job: TP/SL monitor ──────────────────────────────────────────────────────

async def tp_monitor_job(context) -> None:
    """
    Detect positions closed by SL/TP on broker side.
    Send proximity alerts for approaching TP/SL.
    Uses deal history for accurate exit price and PnL.
    """
    chat_id = settings.TELEGRAM_CHAT_ID

    async def send(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    try:
        await monitor_run(send)
    except Exception as exc:
        log.error("tp_monitor_job error: %s", exc, exc_info=True)


# ── Job: Periodic reconciler ────────────────────────────────────────────────

async def reconcile_job(context) -> None:
    """
    Periodic safety net: sync local state vs MT5 every 5 minutes.
    Handles positions missed by tp_monitor (connection gaps, etc).
    """
    try:
        summary = reconcile()
        if summary["closed"] > 0 or summary["orphans"] > 0:
            await context.bot.send_message(
                chat_id=settings.TELEGRAM_CHAT_ID,
                text=(
                    f"🔄 Rekonsiliasi otomatis:\n"
                    f"Ditutup : {summary['closed']}\n"
                    f"Orphan  : {summary['orphans']}\n"
                    f"Match   : {summary['matched']}"
                ),
            )
    except Exception as exc:
        log.error("reconcile_job error: %s", exc, exc_info=True)


# ── Job: Alert-only scan ────────────────────────────────────────────────────

async def auto_trading_job(context) -> None:
    """Scan symbols and send analysis alerts — no order execution."""
    if not context.bot_data.get("auto_mode_active", False):
        return
    if context.bot_data.get("auto_exec_active", False):
        return  # auto_execute_job covers same scan — avoid double MT5 calls

    from strategy.indicators import IndicatorManager
    from strategy.ml_model import MLModel
    from ai_reasoner.analyzer import AIAnalyzer
    from core.mt5_broker import MT5BrokerManager

    broker   = MT5BrokerManager()
    ml_model = MLModel()
    analyzer = AIAnalyzer()
    chat_id  = settings.TELEGRAM_CHAT_ID

    for symbol in _get_scan_symbols(context, broker):
        if not is_valid_session(symbol):
            continue
        try:
            # Symbol-specific ML model
            ml_model = MLModel(symbol=symbol)
            df = broker.fetch_ohlcv(symbol, timeframe="5m", limit=200)
            if df.empty:
                continue
            df      = IndicatorManager.add_indicators(df)
            signals = IndicatorManager.get_latest_signals(df)
            if signals is None:
                continue

            composite, threshold = _compute_composite(signals)
            if abs(composite) < threshold:
                continue

            df_htf   = broker.fetch_ohlcv(symbol, timeframe="1h", limit=100)
            htf_bias = "neutral"
            if not df_htf.empty:
                df_htf   = IndicatorManager.add_indicators(df_htf)
                htf_bias = IndicatorManager.get_htf_bias(df_htf)["bias"]

            if composite > 0 and htf_bias != "bullish":
                continue
            if composite < 0 and htf_bias != "bearish":
                continue

            # 4-hour cooldown per symbol
            scan_alerts = context.bot_data.setdefault("scan_alerted", {})
            if time.time() - scan_alerts.get(symbol, 0) < 4 * 3600:
                continue
            scan_alerts[symbol] = time.time()
            save_bot_state(
                auto_scan=True,
                auto_exec=context.bot_data.get("auto_exec_active", False),
                scan_alerted=scan_alerts,
            )

            prediction = ml_model.predict(df)
            analysis   = analyzer.analyze(symbol, signals, prediction)
            bias_emoji = {"bullish": "🟢", "bearish": "🔴"}.get(htf_bias, "⚪")

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 AUTO SCAN — {symbol}\n{bias_emoji} 4h: {htf_bias.upper()}\n\n{analysis}",
            )
        except Exception as exc:
            log.error("auto_trading_job error on %s: %s", symbol, exc, exc_info=True)


# ── Job: Auto execute ───────────────────────────────────────────────────────

async def auto_execute_job(context) -> None:
    """
    Full autonomous BUY/SELL execution pipeline.

    Gate sequence (all must pass):
    1. circuit_breaker.check()    — daily loss / drawdown limits
    2. exposure_gate.check_entry() — position count / total risk% / correlation
    3. is_valid_session()          — trading hours for this pair
    4. is_spread_acceptable()      — current spread within limits
    5. Signal composite threshold  — indicator agreement
    6. HTF 4H alignment            — higher timeframe confirmation
    """
    if not context.bot_data.get("auto_exec_active", False):
        return

    from strategy.indicators import IndicatorManager
    from strategy.ml_model import MLModel
    from ai_reasoner.analyzer import AIAnalyzer
    from core.mt5_broker import MT5BrokerManager
    from core.risk_manager import RiskManager
    from core.execution.order_engine import OrderEngine

    broker    = MT5BrokerManager()
    analyzer  = AIAnalyzer()
    engine    = OrderEngine()
    chat_id   = settings.TELEGRAM_CHAT_ID

    # Gate 1: circuit breaker — check before scanning any symbols
    allowed, reason = circuit_breaker.check()
    if not allowed:
        log.warning("Auto-exec blocked by circuit breaker: %s", reason)
        context.bot_data["auto_exec_active"] = False
        # Persist: next restart user sees WHY auto_exec was disabled
        save_bot_state(
            auto_scan=context.bot_data.get("auto_mode_active", False),
            auto_exec=False,
            stopped_by_circuit_breaker=True,
            circuit_breaker_reason=reason,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🚫 Circuit breaker — Auto Execute DIMATIKAN.\n\n"
                f"Alasan: {reason}\n\n"
                f"Gunakan /auto_exec_on untuk aktifkan kembali setelah mengevaluasi."
            ),
        )
        return

    for symbol in _get_scan_symbols(context, broker):
        if not is_valid_session(symbol):
            continue
        try:
            # Symbol-specific ML model
            ml_model = MLModel(symbol=symbol)
            await _execute_for_symbol(
                symbol, context, broker, ml_model, analyzer, engine, chat_id
            )
        except Exception as exc:
            log.error("auto_execute_job error on %s: %s", symbol, exc, exc_info=True)


async def _execute_for_symbol(
    symbol, context, broker, ml_model, analyzer, engine, chat_id
) -> None:
    from strategy.indicators import IndicatorManager
    from core.risk_manager import RiskManager
    from core.execution.order_engine import get_symbol_lock

    # Per-symbol async lock: prevents duplicate order race condition.
    # Now that engine.market_order() is async (has await points), asyncio can
    # switch tasks between check_entry() and open_position(). The lock ensures
    # only one execution path runs per symbol at any given time.
    lock = get_symbol_lock(symbol)
    if lock.locked():
        # Another coroutine is already executing for this symbol — skip silently
        return

    async with lock:
        await _execute_for_symbol_locked(
            symbol, context, broker, ml_model, analyzer, engine, chat_id
        )


async def _execute_for_symbol_locked(
    symbol, context, broker, ml_model, analyzer, engine, chat_id
) -> None:
    """Inner execution — called while holding per-symbol lock."""
    from strategy.indicators import IndicatorManager
    from core.risk_manager import RiskManager

    # Gate 2 (partial): symbol-level position check
    if has_open_position(symbol):
        return

    # Gate 3: session filter
    if not is_valid_session(symbol):
        return

    # Gate 4: spread filter
    if not is_spread_acceptable(symbol):
        return

    # Signal pipeline
    df = broker.fetch_ohlcv(symbol, timeframe="5m", limit=200)
    if df.empty:
        return
    df      = IndicatorManager.add_indicators(df)
    signals = IndicatorManager.get_latest_signals(df)
    if signals is None:
        return

    composite, threshold = _compute_composite(signals)
    if abs(composite) < threshold:
        return

    # HTF 1H confirmation (Scalping: 1H is high enough)
    df_htf   = broker.fetch_ohlcv(symbol, timeframe="1h", limit=100)
    htf_bias = "neutral"
    if not df_htf.empty:
        df_htf   = IndicatorManager.add_indicators(df_htf)
        htf_bias = IndicatorManager.get_htf_bias(df_htf)["bias"]

    if composite > 0 and htf_bias != "bullish":
        return
    if composite < 0 and htf_bias != "bearish":
        return

    side = "buy" if composite > 0 else "sell"

    # ML gate: skip if trained model confident in opposite direction
    prediction = ml_model.predict(df)
    if prediction == "bearish" and side == "buy":
        log.info("ML gate: skip BUY %s — model predicts bearish", symbol)
        return
    if prediction == "bullish" and side == "sell":
        log.info("ML gate: skip SELL %s — model predicts bullish", symbol)
        return

    # Calculate SL/TP using current price estimate for sizing
    tick = session.get_tick(symbol)
    if tick is None:
        return

    estimated_entry = tick.ask if side == "buy" else tick.bid
    balance         = broker.get_balance()
    risk_mgr        = RiskManager(balance)
    atr             = signals.get("atr", 0)

    from core.risk_manager import RiskManager as RM
    sl_price, tp_price = (
        RM.get_sl_tp_atr(estimated_entry, atr, side)
        if atr > 0
        else RM.get_sl_tp_pips(symbol, estimated_entry, side)
    )

    lot_size = risk_mgr.calculate_position_size(symbol, estimated_entry, sl_price)
    risk_usd = risk_mgr.calculate_risk_usd(symbol, estimated_entry, sl_price, lot_size)

    # Gate 2 (full): exposure check with actual risk_usd + SL/TP for stop level validation
    gate = check_entry(symbol, risk_usd, sl=sl_price, tp=tp_price)
    if not gate.allowed:
        log.info("ExposureGate rejected %s: %s", symbol, gate.reason)
        # Notify user why it was skipped
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️  SKIPPED — {side.upper()} {symbol}\n\n"
                f"Sinyal valid, tapi dibatalkan karena:\n"
                f"• {gate.reason}"
            ),
        )
        return

    # Fresh tick just before execution — recalculate SL/TP to avoid stale anchor price
    tick_fresh = session.get_tick(symbol)
    if tick_fresh is None:
        return
    entry_price = tick_fresh.ask if side == "buy" else tick_fresh.bid
    sl_price, tp_price = (
        RM.get_sl_tp_atr(entry_price, atr, side)
        if atr > 0
        else RM.get_sl_tp_pips(symbol, entry_price, side)
    )

    # Execute order — async retry, does not block event loop during backoff
    fill = await engine.market_order(symbol, side, lot_size, sl=sl_price, tp=tp_price)
    if fill is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ AUTO EXEC gagal: {symbol} {side.upper()} — lihat log.",
        )
        return

    # Record with ACTUAL fill price (not estimated_entry)
    equity = broker.get_equity()
    open_position(
        ticket=fill.ticket,
        symbol=symbol,
        side=side,
        fill_price=fill.fill_price,   # ← actual fill from broker
        sl_price=sl_price,
        tp_price=tp_price,
        lot_size=fill.volume,
        risk_usd=risk_usd,
        equity_at_open=equity,
    )

    # Notification — prediction already computed above (ML gate)
    analysis   = analyzer.analyze(symbol, signals, prediction)
    from core.risk_manager import RiskManager as RM2
    pip_mult   = RM2.pip_multiplier(symbol)
    sl_pips    = abs(fill.fill_price - sl_price) / pip_mult
    tp_pips    = abs(tp_price - fill.fill_price) / pip_mult
    direction  = "BUY ↑" if side == "buy" else "SELL ↓"

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🤖 AUTO EXEC — {direction} {symbol}\n\n"
            f"Fill  : {fill.fill_price:.5f}\n"
            f"SL    : {sl_price:.5f}  ({sl_pips:.0f} pip)\n"
            f"TP    : {tp_price:.5f}  ({tp_pips:.0f} pip)\n"
            f"Lot   : {fill.volume:.2f}\n"
            f"Risk  : ${risk_usd:.2f}\n"
            f"Ticket: {fill.ticket}\n\n"
            f"Score : {composite:+d}  Regime: {signals.get('regime','?').upper()}\n"
            f"ML    : {prediction}\n\n"
            f"/close {symbol} untuk exit manual."
        ),
    )


async def auto_train_job(context) -> None:
    """
    Automatic ML training based on config/training.json.
    Runs on scheduled day/hour (usually Saturday).
    """
    config_path = "config/training.json"
    if not os.path.exists(config_path):
        return

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as exc:
        log.error("Failed to load training config: %s", exc)
        return

    if not config.get("enabled", False):
        return

    now = datetime.now(timezone.utc)
    # schedule_day: 0=Mon, 4=Fri, 5=Sat, 6=Sun
    if now.weekday() != config.get("schedule_day", 5):
        return
    if now.hour != config.get("schedule_hour", 12):
        return
    # Run only once in the hour
    if now.minute > 5:
        return

    from strategy.indicators import IndicatorManager
    from strategy.ml_model import MLModel
    from core.mt5_broker import MT5BrokerManager

    broker = MT5BrokerManager()
    symbols = config.get("symbols", ["EURUSD"])
    tf = config.get("timeframe", "1h")
    limit = config.get("limit", 2000)

    log.info("Starting Auto-Training for symbols: %s", symbols)
    results = []

    for symbol in symbols:
        try:
            df = broker.fetch_ohlcv(symbol, tf, limit)
            if df.empty:
                results.append(f"❌ {symbol}: No data")
                continue
            
            df = IndicatorManager.add_indicators(df)
            ml_model = MLModel(symbol=symbol)
            metrics = ml_model.train(df)
            
            if "error" in metrics:
                results.append(f"❌ {symbol}: {metrics['error']}")
            else:
                results.append(f"✅ {symbol}: Acc {metrics['accuracy']:.2%}")
        except Exception as exc:
            log.error("Auto-train error for %s: %s", symbol, exc)
            results.append(f"❌ {symbol}: Error")

    if results:
        await context.bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text="🤖 AUTO-TRAINING REPORT\n\n" + "\n".join(results)
        )


# ── Job: Weekend position close ─────────────────────────────────────────────

async def weekend_close_job(context) -> None:
    """Close all positions Friday 14:00 UTC to avoid weekend gap risk."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 4:          # not Friday
        return
    if now.hour != settings.WEEKEND_CLOSE_HOUR_UTC:
        return
    if now.minute > 5:              # only within first 5 minutes of the hour
        return

    from core.mt5_broker import MT5BrokerManager
    broker    = MT5BrokerManager()
    positions = broker.fetch_open_positions()
    if not positions:
        return

    await context.bot.send_message(
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=f"⏰ WEEKEND CLOSE — menutup {len(positions)} posisi.",
    )
    count = await broker.close_all_positions()
    # Reconcile immediately after mass close to update position store and journal
    reconcile()
    await context.bot.send_message(
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=f"✅ {count} posisi ditutup. State tersinkron.",
    )


# ── Auto mode toggle commands ────────────────────────────────────────────────

async def auto_exec_on(update, context) -> None:
    context.bot_data["auto_exec_active"] = True
    clear_circuit_breaker_flag()
    save_bot_state(
        auto_scan=context.bot_data.get("auto_mode_active", False),
        auto_exec=True,
    )
    cb_status  = circuit_breaker.status()
    cached     = context.bot_data.get("top_symbols_cache", {}).get("symbols")
    scan_label = ", ".join(cached) if cached else "loading..."
    await update.message.reply_text(
        f"⚡ AUTO EXECUTE AKTIF (BUY & SELL).\n\n"
        f"Gate aktif:\n"
        f"  • Circuit breaker (RR limit {cb_status['rr_limit']:+.1f}, DD {cb_status['dd_limit']}%)\n"
        f"  • Exposure gate (max 5 posisi, 5% total risk)\n"
        f"  • Spread filter\n"
        f"  • Session filter\n"
        f"  • Composite ≥ threshold + 4H konfirmasi\n\n"
        f"Scan: {scan_label}\n"
        f"⚠️  Monitor posisi secara berkala!\n\n"
        f"/auto_exec_off untuk matikan."
    )


async def auto_exec_off(update, context) -> None:
    context.bot_data["auto_exec_active"] = False
    save_bot_state(
        auto_scan=context.bot_data.get("auto_mode_active", False),
        auto_exec=False,
    )
    await update.message.reply_text("🛑 Auto Execute DIMATIKAN.")


async def auto_on(update, context) -> None:
    context.bot_data["auto_mode_active"] = True
    save_bot_state(auto_scan=True, auto_exec=context.bot_data.get("auto_exec_active", False))
    cached     = context.bot_data.get("top_symbols_cache", {}).get("symbols")
    scan_label = ", ".join(cached) if cached else "loading..."
    await update.message.reply_text(
        f"✅ Auto Scan AKTIF.\nScan: {scan_label}\n\n/auto_exec_on untuk eksekusi."
    )


async def auto_off(update, context) -> None:
    context.bot_data["auto_mode_active"] = False
    save_bot_state(auto_scan=False, auto_exec=context.bot_data.get("auto_exec_active", False))
    await update.message.reply_text("🛑 Auto Scan DIMATIKAN.")


async def restore_state_job(context) -> None:
    """
    Runs once at startup (first=5s) to restore persisted bot state.
    context.bot_data is only accessible inside jobs/handlers, not __main__.
    """
    state = load_bot_state()
    context.bot_data["auto_mode_active"] = state["auto_scan"]
    context.bot_data["auto_exec_active"] = state["auto_exec"]
    context.bot_data["scan_alerted"]     = state.get("scan_alerted", {})
    log.info(
        "State restored: auto_scan=%s auto_exec=%s scan_alerted_count=%d",
        state["auto_scan"], state["auto_exec"], len(state.get("scan_alerted", {})),
    )
    if state.get("stopped_by_cb") and state.get("cb_reason"):
        await context.bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=(
                f"⚠️ Restart terdeteksi.\n"
                f"Auto Execute sebelumnya dihentikan circuit breaker:\n\n"
                f"{state['cb_reason']}\n\n"
                f"Gunakan /auto_exec_on untuk aktifkan kembali setelah evaluasi."
            ),
        )


def _should_weekend_close_now() -> bool:
    """
    True if it's Friday AFTER the close hour (handles restart edge case).
    Original weekend_close_job only triggers at exactly the configured hour.
    If bot restarts at 15:00 Friday (after 14:00 close window), it misses close.
    This check fires on startup: if Friday + past close time + positions open → close now.
    """
    now = datetime.now(timezone.utc)
    return (
        now.weekday() == 4 and           # Friday
        now.hour >= settings.WEEKEND_CLOSE_HOUR_UTC
    )


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Step 1: Establish MT5 connection — fail fast if cannot connect
    log.info("Connecting to MT5...")
    if not session.connect():
        log.critical("Cannot connect to MT5 — aborting startup")
        raise SystemExit(1)

    # Step 2: Record session baseline equity for drawdown tracking
    circuit_breaker.initialize_session()

    # Step 3: Reconcile local state vs MT5 before any job fires
    log.info("Running startup reconciliation...")
    startup_sync = reconcile()
    log.info("Startup reconcile: %s", startup_sync)

    # Step 4: Weekend close startup check
    # Handles case where bot was restarted on Friday after the close window.
    # Normal weekend_close_job would not fire until next Friday.
    if _should_weekend_close_now():
        from core.mt5_broker import MT5BrokerManager as _MB
        _broker   = _MB()
        _open_pos = _broker.fetch_open_positions()
        if _open_pos:
            log.warning(
                "Startup weekend close: %d positions still open on Friday post-close-time",
                len(_open_pos),
            )
            import asyncio as _asyncio
            _asyncio.run(_broker.close_all_positions())
            reconcile()
            log.info("Startup weekend close: positions closed and reconciled")

    # Step 4: Build Telegram application
    application = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start",         bot_handlers.start))
    application.add_handler(CommandHandler("balance",       bot_handlers.balance))
    application.add_handler(CommandHandler("analyze",       bot_handlers.analyze_command))
    application.add_handler(CommandHandler("status",        bot_handlers.status_command))
    application.add_handler(CommandHandler("sync",          bot_handlers.sync_command))
    application.add_handler(CommandHandler("close",         bot_handlers.close_command))
    application.add_handler(CommandHandler("closeall",      bot_handlers.closeall_command))
    application.add_handler(CommandHandler("train",         bot_handlers.train_command))
    application.add_handler(CommandHandler("report",        bot_handlers.report_command))
    application.add_handler(CommandHandler("auto_on",       auto_on))
    application.add_handler(CommandHandler("auto_off",      auto_off))
    application.add_handler(CommandHandler("auto_exec_on",  auto_exec_on))
    application.add_handler(CommandHandler("auto_exec_off", auto_exec_off))
    application.add_handler(CallbackQueryHandler(bot_handlers.button_handler))

    # Register recurring jobs
    jq = application.job_queue
    jq.run_once(restore_state_job,                                     when=5)
    jq.run_repeating(tp_monitor_job,   interval=TP_MONITOR_INTERVAL,  first=15)
    jq.run_repeating(reconcile_job,    interval=RECONCILE_INTERVAL,   first=60)
    jq.run_repeating(auto_trading_job, interval=AUTO_SCAN_INTERVAL,   first=10)
    jq.run_repeating(auto_execute_job, interval=AUTO_SCAN_INTERVAL,   first=20)
    jq.run_repeating(auto_train_job,   interval=60,                   first=40)
    jq.run_repeating(weekend_close_job, interval=60,                  first=30)

    log.info("Forex Bot MT5 starting — polling Telegram")
    application.run_polling()
