"""
Live trade management: the part that decides whether a bot survives.

Borrowed from how professional bots are built (see RESEARCH.md):
* risk is expressed in R (multiples of the initial stop), never in pips
* partial profit at +1R, stop to break-even at +1R, ATR trail after that
* stops only ever tighten
* the daily loss limit is persisted, so a restart does not reset the kill switch
* a circuit breaker pauses trading after N consecutive losses
* everything is written to the audit log that shows up on the phone

Every function that talks to MT5 takes the module as an argument so this file
can be imported and unit-tested on Linux, where MetaTrader5 does not exist.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Optional

import control
from strategy import StrategyParams, breakeven_stop, r_multiple, tighten, trailing_stop


# ---------------------------------------------------------------- risk state

def daily_start_equity(equity: float) -> float:
    """Persist today's starting equity so restarts cannot reset the loss limit."""
    key = f"daily_start_equity:{date.today().isoformat()}"
    saved = control.get(key)
    if saved:
        try:
            return float(saved)
        except ValueError:
            pass
    control.set(key, repr(float(equity)))
    return float(equity)


def daily_loss_hit(equity: float, max_daily_loss: float) -> tuple[bool, float]:
    start = daily_start_equity(equity)
    if start <= 0:
        return False, 0.0
    loss = max(0.0, (start - equity) / start)
    return loss >= max_daily_loss, loss


def register_trade_result(pnl: float) -> int:
    """Track consecutive losses for the circuit breaker. Returns the streak."""
    streak = int(control.get("consecutive_losses", "0") or 0)
    streak = streak + 1 if pnl < 0 else 0
    control.set("consecutive_losses", str(streak))
    return streak


def circuit_breaker_active(max_consecutive: int, cooldown_minutes: int) -> bool:
    if max_consecutive <= 0:
        return False
    streak = int(control.get("consecutive_losses", "0") or 0)
    if streak < max_consecutive:
        return False

    paused_until = control.get("breaker_until")
    now = datetime.now(timezone.utc)

    if paused_until:
        try:
            if now < datetime.fromisoformat(paused_until):
                return True
        except ValueError:
            pass

    until = now.timestamp() + cooldown_minutes * 60
    control.set("breaker_until", datetime.fromtimestamp(until, timezone.utc).isoformat())
    control.log_event("CIRCUIT_BREAKER", message=(
        f"{streak} losses in a row, paused {cooldown_minutes} min"))
    return True


def correlation_guard(open_symbols: list[str], candidate: str, max_usd_exposure: int = 2) -> bool:
    """Blocks yet another long-USD style trade when the book is already loaded."""
    usd_block = {"EURUSD", "GBPUSD", "AUDUSD", "XAUUSD", "USDCHF", "USDCAD", "USDJPY"}
    if candidate not in usd_block:
        return True
    same = [s for s in open_symbols if s in usd_block]
    return len(same) < max_usd_exposure


# ---------------------------------------------------------------- MT5 side

def _log(log, msg: str, *args) -> None:
    if log is not None:
        log.info(msg, *args)


def modify_sl(mt5, position, new_sl: float, log=None, dry_run: bool = True) -> bool:
    """Move a stop loss; refuses anything that would loosen it."""
    side = "BUY" if int(position.type) == 0 else "SELL"
    improved = tighten(side, float(position.sl), new_sl)
    if abs(improved - float(position.sl)) < 1e-9:
        return False
    if dry_run:
        _log(log, "DRY RUN | would move SL #%s %s -> %s", position.ticket, position.sl, improved)
        return False

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": int(position.ticket),
        "sl": float(improved),
        "tp": float(position.tp),
        "magic": int(getattr(position, "magic", 0)),
    }
    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        control.log_event("SL_MOVED", position.symbol, side,
                          message=f"#{position.ticket} SL {position.sl} -> {improved}")
    return ok


def partial_close(mt5, position, fraction: float, log=None, dry_run: bool = True) -> bool:
    info = mt5.symbol_info(position.symbol)
    if info is None:
        return False

    step = float(info.volume_step) or 0.01
    volume = math.floor(float(position.volume) * fraction / step) * step
    if volume <= 0 or volume >= float(position.volume):
        return False
    volume = round(volume, 2)

    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False

    is_buy = int(position.type) == 0
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": int(position.ticket),
        "price": float(tick.bid if is_buy else tick.ask),
        "deviation": 20,
        "magic": int(getattr(position, "magic", 0)),
        "comment": "partial-tp-1R",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if dry_run:
        _log(log, "DRY RUN | would close %.2f of #%s at +1R", volume, position.ticket)
        return False

    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        control.set(f"partial_done:{position.ticket}", "1")
        control.log_event("PARTIAL_TP", position.symbol, "SELL" if is_buy else "BUY",
                          volume=volume, message=f"#{position.ticket} closed at +1R")
    return ok


def manage_open_positions(mt5, magic: int, p: StrategyParams, atr_by_symbol: dict,
                          log=None, dry_run: bool = True) -> int:
    """Run on every loop: partials, break-even, trailing. Returns actions taken."""
    try:
        positions = mt5.positions_get() or ()
    except Exception:
        return 0

    actions = 0
    for pos in positions:
        if int(getattr(pos, "magic", 0)) != magic:
            continue

        side = "BUY" if int(pos.type) == 0 else "SELL"
        atr = atr_by_symbol.get(pos.symbol)
        if not atr or not math.isfinite(atr) or atr <= 0:
            continue

        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            continue
        price = float(tick.bid if side == "BUY" else tick.ask)

        entry = float(pos.price_open)
        sl0 = float(pos.sl) if pos.sl else entry - p.sl_atr_mult * atr
        r = r_multiple(side, entry, sl0, price)

        # 1) take half off at +1R, once
        if r >= p.partial_at_r and not control.get(f"partial_done:{pos.ticket}"):
            if partial_close(mt5, pos, p.partial_fraction, log, dry_run):
                actions += 1

        # 2) stop to break-even at +1R
        if r >= p.breakeven_at_r:
            if modify_sl(mt5, pos, breakeven_stop(side, entry, atr, p), log, dry_run):
                actions += 1

        # 3) ATR trail once the trade has room to breathe
        if r >= p.trail_start_r:
            if modify_sl(mt5, pos, trailing_stop(side, price, atr, p), log, dry_run):
                actions += 1

    return actions
