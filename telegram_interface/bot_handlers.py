"""
bot_handlers.py — Telegram command and callback handlers.

Changes from original:

1. No global broker instance.
   Original: broker = MT5BrokerManager() at module level → called mt5.initialize().
   Fix: MT5BrokerManager() is now free to instantiate (no connection side effects).
   Still instantiated per handler call for clarity, or shared within a request.

2. close_command: only logs close if MT5 close succeeds.
   Original: log_close_trade() ran even if close_position() returned None.
   Fix: position_store.remove_position() called ONLY after FillResult received.

3. button_handler confirm_: same atomic close → log pattern.

4. closeall_command: reconcile() after mass close instead of manual loop.

5. /status command: shows circuit breaker stats and exposure summary.

6. Trade logging uses new PositionStore + TradeJournal (accurate PnL).
"""

from __future__ import annotations

import csv
import uuid
from functools import wraps
from pathlib import Path
from typing import TypedDict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai_reasoner.analyzer import AIAnalyzer
from config import settings
from core.execution.order_engine import OrderEngine
from core.mt5_broker import MT5BrokerManager
from core.risk.circuit_breaker import circuit_breaker
from core.risk.session_filter import is_valid_session
from core.risk_manager import RiskManager
from core.state.position_store import (
    get_all_positions,
    get_positions_for_symbol,
    has_open_position,
    open_position,
    remove_position,
)
from core.state.trade_journal import (
    classify_exit,
    get_all_history,
    get_daily_stats,
    pnl_from_calc,
    record_close,
)
from strategy.indicators import IndicatorManager
from strategy.ml_model import MLModel

# Singletons — no MT5 init side effects now
_ml_model   = MLModel()
_analyzer   = AIAnalyzer()
_engine     = OrderEngine()

DAILY_LOSS_LIMIT_RR = settings.AUTO_EXEC_DAILY_LOSS_LIMIT


# ── Authorization ─────────────────────────────────────────────────────────

def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        allowed = {str(cid).strip() for cid in str(settings.TELEGRAM_CHAT_ID).split(",")}
        user_id = str(update.effective_user.id) if update.effective_user else ""
        if user_id not in allowed:
            if update.message:
                await update.message.reply_text("Akses ditolak.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ── TypedDicts & helpers ──────────────────────────────────────────────────

class PendingTrade(TypedDict):
    symbol:      str
    side:        str
    entry_price: float
    sl_price:    float
    tp_price:    float
    lot_size:    float
    risk_usd:    float


def _build_action_keyboard(symbol: str, has_pos: bool) -> InlineKeyboardMarkup:
    if has_pos:
        rows = [[InlineKeyboardButton("TUTUP POSISI", callback_data=f"close_{symbol}")],
                [InlineKeyboardButton("Batal",        callback_data="cancel")]]
    else:
        rows = [[InlineKeyboardButton("BUY ↑",  callback_data=f"buy_{symbol}")],
                [InlineKeyboardButton("SELL ↓", callback_data=f"sell_{symbol}")],
                [InlineKeyboardButton("Batal",  callback_data="cancel")]]
    return InlineKeyboardMarkup(rows)


def _build_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ya",    callback_data=f"confirm_{token}"),
        InlineKeyboardButton("Batal", callback_data=f"cancel_{token}"),
    ]])


def _build_risk_summary(
    symbol: str, side: str, entry: float,
    sl: float, tp: float, lot: float, risk: float,
    atr: float | None = None,
) -> str:
    pip_m   = RiskManager.pip_multiplier(symbol)
    sl_pips = abs(entry - sl) / pip_m
    tp_pips = abs(tp - entry) / pip_m
    rr      = tp_pips / sl_pips if sl_pips > 0 else 0
    method  = f"ATR×{settings.ATR_SL_MULTIPLIER} ({atr:.5f})" if atr else f"Fixed {settings.DEFAULT_SL_PIPS}pip"
    return (
        f"📊 RISK SUMMARY — {symbol}\n"
        f"Arah   : {side.upper()}\n"
        f"Entry  : {entry:.5f}\n"
        f"SL     : {sl:.5f}  ({sl_pips:.0f} pip)  [{method}]\n"
        f"TP     : {tp:.5f}  ({tp_pips:.0f} pip)\n"
        f"RR     : 1:{rr:.1f}\n"
        f"Lot    : {lot:.2f}\n"
        f"Risk   : ${risk:.2f}\n\n"
        f"Konfirmasi? Ya / Batal"
    )


def _pending_orders(context: ContextTypes.DEFAULT_TYPE) -> dict:
    po = context.user_data.get("pending_orders")
    if not isinstance(po, dict):
        po = {}
        context.user_data["pending_orders"] = po
    return po


# ── Commands ──────────────────────────────────────────────────────────────

@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 Forex Bot MT5 Aktif\n\n"
        "/analyze [SYMBOL]     — Analisa + tombol eksekusi\n"
        "/status               — Posisi terbuka + circuit breaker\n"
        "/sync                 — Sinkronisasi paksa dengan MT5\n"
        "/close [SYMBOL]       — Tutup posisi manual\n"
        "/closeall             — Tutup SEMUA posisi\n"
        "/balance              — Cek saldo\n"
        "/train [SYMBOL] [TF]  — Training ML model\n\n"
        "── Auto Mode ──\n"
        "/auto_on              — Alert scan aktif\n"
        "/auto_off             — Alert scan mati\n"
        "/auto_exec_on         — ⚡ Auto execute aktif\n"
        "/auto_exec_off        — Auto execute mati\n\n"
        "── Laporan ──\n"
        "/report               — Performance summary\n"
    )


@authorized_only
async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger reconciliation."""
    await update.message.reply_text("Memulai sinkronisasi paksa...")
    from core.state.reconciler import reconcile
    summary = reconcile()
    
    text = (
        f"🔄 Sinkronisasi Selesai:\n"
        f"• Cocok    : {summary['matched']}\n"
        f"• Ditutup  : {summary['closed']}\n"
        f"• Orphan   : {summary['orphans']}\n"
        f"• Error    : {summary['errors']}"
    )
    if summary["closed"] > 0 or summary["orphans"] > 0:
        text += "\n\n⚠️ State telah diperbarui untuk mencocokkan MT5."
    else:
        text += "\n\n✅ State sudah sesuai dengan MT5."
        
    await update.message.reply_text(text)


@authorized_only
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    broker = MT5BrokerManager()
    bal    = broker.get_balance()
    equity = broker.get_equity()
    await update.message.reply_text(
        f"💰 Balance : ${bal:,.2f}\n"
        f"📊 Equity  : ${equity:,.2f}"
    )


@authorized_only
async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Contoh: /analyze EURUSD")
        return

    symbol_input = context.args[0]
    
    # Check if market is open before analyzing
    if not is_valid_session(symbol_input):
        await update.message.reply_text(
            f"⛔ Market {symbol_input.upper()} sedang tutup atau libur. "
            "Analisa hanya tersedia saat market aktif."
        )
        return

    await update.message.reply_text(f"Menganalisa {symbol_input.upper()}...")

    broker = MT5BrokerManager()
    df     = broker.fetch_ohlcv(symbol_input, "1h", 100)
    
    if df.empty:
        # Check if the connection is actually alive
        from core.connection.mt5_session import session
        if not session.ensure_connected():
            await update.message.reply_text("❌ Terputus dari MT5. Cek koneksi terminal.")
        else:
            await update.message.reply_text(f"❓ Simbol {symbol_input.upper()} tidak ditemukan atau tidak ada data.")
        return

    # Use the first timestamp to confirm we got data
    df_4h   = broker.fetch_ohlcv(symbol_input, "4h", 60)
    df      = IndicatorManager.add_indicators(df)
    signals = IndicatorManager.get_latest_signals(df)
    if signals is None:
        await update.message.reply_text(f"Data {symbol_input.upper()} tidak cukup.")
        return

    # Get the mapped symbol name (e.g. EURUSDm) for internal logic
    from core.connection.mt5_session import session
    real_symbol = session._ensure_symbol(symbol_input) or symbol_input

    htf = {"bias": "neutral", "regime": "unknown", "adx": 0.0, "ema_trend": "ranging", "dmp": 0, "dmn": 0}
    if not df_4h.empty:
        df_4h = IndicatorManager.add_indicators(df_4h)
        htf   = IndicatorManager.get_htf_bias(df_4h)

    prediction    = _ml_model.predict(df)
    analysis_text = _analyzer.analyze(real_symbol, signals, prediction)

    bias_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(htf["bias"], "⚪")
    analysis_text += "\n" + "\n".join([
        "",
        "═══ Higher Timeframe (4h) ═══",
        f"{bias_emoji} Bias    : {htf['bias'].upper()}",
        f"   Regime : {htf['regime'].upper()}  (ADX {htf['adx']})",
        f"   EMA    : {htf['ema_trend']}  |  DI+ {htf['dmp']} / DI- {htf['dmn']}",
    ])

    if signals.get("atr", 0) > 0:
        context.user_data[f"atr_{real_symbol}"] = signals["atr"]
    context.user_data[f"htf_{real_symbol}"] = htf["bias"]

    has_pos = has_open_position(real_symbol)
    await update.message.reply_text(
        analysis_text, reply_markup=_build_action_keyboard(real_symbol, has_pos)
    )


@authorized_only
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data    = query.data
    pending = _pending_orders(context)

    # ── Cancel ──────────────────────────────────────────────────────────
    if data == "cancel":
        await query.edit_message_text("Dibatalkan.")
        return

    # ── Close from analysis keyboard ──────────────────────────────────
    if data.startswith("close_") and "_" not in data[6:]:
        symbol    = data[6:]
        positions = get_positions_for_symbol(symbol)
        if not positions:
            await query.edit_message_text(f"Tidak ada posisi {symbol} di log.")
            return

        # Close all tickets for this symbol (usually just 1)
        broker = MT5BrokerManager()
        lines  = []
        for record in positions:
            ticket = int(record["ticket"])
            fill   = await _engine.close_position(ticket)

            if fill is None:
                lines.append(f"⚠️ Gagal menutup {symbol} ticket={ticket} di MT5.")
                lines.append("Posisi tetap terbuka — log tidak diubah.")
                continue

            # MT5 confirmed close → now update state
            removed = remove_position(ticket)
            if removed:
                pnl_usd, src = pnl_from_calc(
                    symbol, record["side"], record["lot_size"],
                    record["fill_price"], fill.fill_price,
                )
                exit_reason = classify_exit(
                    record["side"], record["fill_price"], fill.fill_price,
                    record["planned_sl"], record["planned_tp"],
                )
                row = record_close(removed, fill.fill_price, pnl_usd, exit_reason, src)
                sign = "+" if pnl_usd >= 0 else ""
                lines += [
                    f"✅ {symbol} ticket={ticket} ditutup.",
                    f"Fill  : {fill.fill_price:.5f}",
                    f"PnL   : {sign}${pnl_usd:.2f}",
                    f"Reason: {exit_reason}",
                ]

        await query.message.reply_text("\n".join(lines) if lines else f"✅ {symbol} ditutup.")
        return

    # ── BUY / SELL from analysis keyboard ────────────────────────────
    if data.startswith(("buy_", "sell_")):
        action, symbol = data.split("_", 1)
        if has_open_position(symbol):
            await query.edit_message_text(
                f"⚠️ Posisi {symbol} sudah terbuka. Tutup dulu."
            )
            return

        broker     = MT5BrokerManager()
        ticker     = broker.fetch_ticker(symbol)
        if not ticker:
            await query.edit_message_text(f"Gagal ambil harga {symbol}.")
            return

        balance    = broker.get_balance()
        risk_mgr   = RiskManager(balance)
        cached_atr = context.user_data.get(f"atr_{symbol}")

        entry = float(ticker["last"])
        if cached_atr and cached_atr > 0:
            sl, tp = RiskManager.get_sl_tp_atr(entry, cached_atr, action)
        else:
            sl, tp = RiskManager.get_sl_tp_pips(symbol, entry, action)

        lot  = risk_mgr.calculate_position_size(symbol, entry, sl)
        risk = risk_mgr.calculate_risk_usd(symbol, entry, sl, lot)

        token = uuid.uuid4().hex[:8]
        pending[token] = PendingTrade(
            symbol=symbol, side=action, entry_price=entry,
            sl_price=sl, tp_price=tp, lot_size=lot, risk_usd=risk,
        )
        summary = _build_risk_summary(symbol, action, entry, sl, tp, lot, risk, cached_atr)
        await query.edit_message_text(summary, reply_markup=_build_confirm_keyboard(token))
        return

    # ── Cancel pending order ──────────────────────────────────────────
    if data.startswith("cancel_"):
        pending.pop(data.split("_", 1)[1], None)
        await query.edit_message_text("Dibatalkan.")
        return

    # ── Confirm pending order ─────────────────────────────────────────
    if data.startswith("confirm_"):
        token      = data.split("_", 1)[1]
        trade_data = pending.pop(token, None)
        if not trade_data:
            await query.edit_message_text("Sesi expired.")
            return

        # Circuit breaker check
        allowed, reason = circuit_breaker.check()
        if not allowed:
            await query.edit_message_text(f"🚫 Circuit breaker: {reason}")
            return

        symbol   = trade_data["symbol"]
        side     = trade_data["side"]
        lot      = trade_data["lot_size"]
        sl       = trade_data["sl_price"]
        tp       = trade_data["tp_price"]
        risk_usd = trade_data["risk_usd"]

        fill = await _engine.market_order(symbol, side, lot, sl=sl, tp=tp)
        if fill is None:
            await query.edit_message_text(f"❌ Order gagal {symbol}.")
            return

        broker = MT5BrokerManager()
        equity = broker.get_equity()

        open_position(
            ticket=fill.ticket,
            symbol=symbol,
            side=side,
            fill_price=fill.fill_price,   # actual fill
            sl_price=sl,
            tp_price=tp,
            lot_size=fill.volume,
            risk_usd=risk_usd,
            equity_at_open=equity,
        )

        await query.edit_message_text(
            f"✅ {side.upper()} {symbol}\n"
            f"Ticket : {fill.ticket}\n"
            f"Fill   : {fill.fill_price:.5f}\n"
            f"Lot    : {fill.volume:.2f}\n"
            f"SL     : {sl:.5f}\n"
            f"TP     : {tp:.5f}\n"
            f"Risk   : ${risk_usd:.2f}\n\n"
            f"SL/TP terpasang di broker."
        )
        return

    await query.edit_message_text("Perintah tidak dikenali.")


# ── /status ───────────────────────────────────────────────────────────────

@authorized_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    auto_scan   = context.bot_data.get("auto_mode_active", False)
    auto_exec   = context.bot_data.get("auto_exec_active", False)
    cached_syms = context.bot_data.get("top_symbols_cache", {}).get("symbols")
    scan_label  = f"Top {len(cached_syms)}" if cached_syms else "loading..."

    broker = MT5BrokerManager()
    bal    = broker.get_balance()
    equity = broker.get_equity()
    cb     = circuit_breaker.status()

    lines = [
        "📊 STATUS AKUN\n",
        f"🔍 Auto Scan : {'🟢 ON' if auto_scan else '🔴 OFF'}",
        f"⚡ Auto Exec : {'🟢 ON' if auto_exec else '🔴 OFF'}",
        f"📊 Scan      : {scan_label}\n",
        f"💰 Balance   : ${bal:,.2f}",
        f"📈 Equity    : ${equity:,.2f}",
        "",
        "── Circuit Breaker ──",
        f"RR Hari ini : {cb['rr_today']:+.2f}  (limit {cb['rr_limit']:+.1f})",
        f"PnL Hari ini: ${cb['pnl_usd']:+.2f}",
        f"Drawdown    : {cb['dd_pct']:.1f}%  (limit {cb['dd_limit']:.0f}%)",
        f"Trade       : {cb['trades_today']} ({cb['wins']}W/{cb['losses']}L)",
    ]

    # MT5 live positions
    mt5_positions = broker.fetch_open_positions()
    local_store   = get_all_positions()

    if not mt5_positions:
        lines.append("\nTidak ada posisi terbuka di MT5.")
    else:
        lines.append(f"\n📈 Posisi Terbuka ({len(mt5_positions)}):")
        for p in mt5_positions:
            raw_pnl = float(p["unrealizedPnl"])
            # Normalize unrealized PnL to USD if account is IDR
            if settings.ACCOUNT_CURRENCY == "IDR":
                pnl = raw_pnl / settings.IDR_TO_USD_RATE
            else:
                pnl = raw_pnl
                
            sign = "+" if pnl >= 0 else ""
            local = local_store.get(str(p["ticket"]), {})
            risk  = local.get("risk_usd", "?")
            lines.append(
                f"\n{p['symbol']} {p['side'].upper()}\n"
                f"  Ticket : {p['ticket']}\n"
                f"  Entry  : {float(p['entryPrice']):.5f}\n"
                f"  Now    : {float(p['markPrice']):.5f}\n"
                f"  SL     : {float(p['sl']):.5f}\n"
                f"  TP     : {float(p['tp']):.5f}\n"
                f"  PnL    : {sign}${pnl:.2f}\n"
                f"  Risk   : ${risk}"
            )

    await update.message.reply_text("\n".join(lines))


# ── /close ────────────────────────────────────────────────────────────────

@authorized_only
async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Contoh: /close EURUSD")
        return

    symbol    = context.args[0].upper()
    positions = get_positions_for_symbol(symbol)
    if not positions:
        await update.message.reply_text(f"Tidak ada posisi {symbol} di log.")
        return

    lines = []
    for record in positions:
        ticket = int(record["ticket"])
        fill   = await _engine.close_position(ticket)

        if fill is None:
            lines.append(
                f"⚠️ Close MT5 gagal untuk ticket={ticket}.\n"
                f"Posisi tetap terbuka — log tidak diubah."
            )
            continue

        # Only update state if MT5 confirmed close
        removed = remove_position(ticket)
        if removed:
            pnl_usd, src = pnl_from_calc(
                symbol, record["side"], record["lot_size"],
                record["fill_price"], fill.fill_price,
            )
            exit_reason = classify_exit(
                record["side"], record["fill_price"], fill.fill_price,
                record["planned_sl"], record["planned_tp"],
            )
            record_close(removed, fill.fill_price, pnl_usd, exit_reason, src)
            sign = "+" if pnl_usd >= 0 else ""
            lines += [
                f"✅ {symbol} ticket={ticket} ditutup.",
                f"Exit  : {fill.fill_price:.5f}",
                f"PnL   : {sign}${pnl_usd:.2f}",
                f"Reason: {exit_reason}",
            ]

    await update.message.reply_text("\n".join(lines) if lines else f"✅ {symbol} ditutup.")


# ── /closeall ─────────────────────────────────────────────────────────────

@authorized_only
async def closeall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Menutup semua posisi...")
    broker = MT5BrokerManager()
    count  = await broker.close_all_positions()
    # Reconcile cleans up position store and journals all closed trades
    from core.state.reconciler import reconcile
    reconcile()
    await update.message.reply_text(f"✅ {count} posisi ditutup di MT5. State tersinkron.")


# ── /train ────────────────────────────────────────────────────────────────

@authorized_only
async def train_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args      = context.args
    symbol    = args[0].upper() if args else "EURUSD"
    timeframe = args[1] if len(args) > 1 else "1h"
    limit     = int(args[2]) if len(args) > 2 else 1000

    await update.message.reply_text(f"Training ML... {symbol} {timeframe} {limit} candles")

    broker = MT5BrokerManager()
    df     = broker.fetch_ohlcv(symbol, timeframe, limit)
    if df.empty:
        await update.message.reply_text(f"Gagal ambil data {symbol}.")
        return

    df      = IndicatorManager.add_indicators(df)
    metrics = _ml_model.train(df)

    if "error" in metrics:
        await update.message.reply_text(f"❌ {metrics['error']}")
        return

    top_f = "\n".join(
        f"  {i+1}. {name} ({imp:.3f})"
        for i, (name, imp) in enumerate(metrics["top_features"][:5])
    )
    await update.message.reply_text(
        f"✅ Model trained!\n"
        f"Data     : {len(df)} candle\n"
        f"Accuracy : {metrics['accuracy']:.2%}\n"
        f"Features : {metrics['feature_count']}\n\n"
        f"Top 5:\n{top_f}"
    )


# ── /report ───────────────────────────────────────────────────────────────

@authorized_only
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    trades = get_all_history()
    if not trades:
        await update.message.reply_text("Belum ada history.")
        return

    total  = len(trades)
    pnls   = [float(t["pnl_usd"]) for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr     = len(wins) / total * 100 if total > 0 else 0
    pf     = sum(wins) / abs(sum(losses)) if losses else float("inf")
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"

    grades: dict[str, int] = {"A": 0, "B": 0, "C": 0, "F": 0}
    for t in trades:
        r = t.get("exit_reason", "")
        g = "A" if r == "TP_Hit" else "B" if r == "SL_Hit" else "C" if r == "Manual_Early" else "F"
        grades[g] += 1

    today_stats = get_daily_stats()

    await update.message.reply_text(
        f"📊 PERFORMANCE REPORT\n"
        f"{'─'*28}\n"
        f"Total        : {total}\n"
        f"Win Rate     : {wr:.1f}%  ({len(wins)}W/{len(losses)}L)\n"
        f"Total PnL    : {'+'if sum(pnls)>=0 else ''}${sum(pnls):.2f}\n"
        f"Profit Factor: {pf_str}\n\n"
        f"Hari Ini     : {today_stats['count']} trade  ${today_stats['pnl_usd']:+.2f}\n\n"
        f"🏅 Grade\n"
        f"  A (TP_Hit)      : {grades['A']}\n"
        f"  B (SL_Hit)      : {grades['B']}\n"
        f"  C (Manual_Early): {grades['C']}\n"
        f"  F (Manual_Late) : {grades['F']}"
    )
