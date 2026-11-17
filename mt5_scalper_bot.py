"""
Short-Timeframe FX Trading Bot — MetaTrader 5
Educational / engineering template. Use a DEMO account first.

Design:
- MT5 execution adapter
- 1m–30m timeframes
- EMA trend + RSI + ATR + breakout confirmation
- ATR-based stop loss / take profit
- Position sizing from % equity risk
- Spread, daily-loss, max-trades, cooldown and exposure guards
- No martingale / no averaging down
- Closed-candle signals to reduce intrabar noise
- Dry-run mode by default

Install:
    pip install MetaTrader5 pandas numpy python-dotenv

Run:
    1. Install MetaTrader 5 desktop and log into a DEMO account.
    2. Copy .env.example to .env and set your credentials if needed.
    3. Set DRY_RUN=true for testing.
    4. Run: python mt5_scalper_bot.py

IMPORTANT:
No trading strategy can guarantee "extreme precision" or profits. Slippage,
spread changes, news shocks, outages and broker execution can cause losses.
"""

from __future__ import annotations

import os
import time
import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from dotenv import load_dotenv

try:  # shared start/stop/emergency flags written by the mobile API
    import control
except Exception:  # pragma: no cover - keep the bot runnable standalone
    control = None

import execution
import strategy as strat


# ---------------- CONFIG ----------------

load_dotenv()

@dataclass
class Config:
    symbols: tuple[str, ...] = tuple(
        s.strip() for s in os.getenv(
            "SYMBOLS", "EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD"
        ).split(",") if s.strip()
    )

    timeframe: int = int(os.getenv("TIMEFRAME_MINUTES", "5"))
    bars: int = int(os.getenv("BARS", "300"))

    risk_per_trade: float = float(os.getenv("RISK_PER_TRADE", "0.0025"))  # 0.25%
    max_daily_loss: float = float(os.getenv("MAX_DAILY_LOSS", "0.02"))     # 2%
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    max_positions_per_symbol: int = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1"))

    max_spread_points: float = float(os.getenv("MAX_SPREAD_POINTS", "25"))
    cooldown_minutes: int = int(os.getenv("COOLDOWN_MINUTES", "10"))

    atr_period: int = int(os.getenv("ATR_PERIOD", "14"))
    atr_sl_mult: float = float(os.getenv("ATR_SL_MULT", "1.5"))
    atr_tp_mult: float = float(os.getenv("ATR_TP_MULT", "2.0"))

    fast_ema: int = int(os.getenv("FAST_EMA", "9"))
    slow_ema: int = int(os.getenv("SLOW_EMA", "21"))
    trend_ema: int = int(os.getenv("TREND_EMA", "50"))
    rsi_period: int = int(os.getenv("RSI_PERIOD", "14"))

    min_atr_points: float = float(os.getenv("MIN_ATR_POINTS", "30"))
    deviation_points: int = int(os.getenv("DEVIATION_POINTS", "10"))

    magic: int = int(os.getenv("MAGIC", "26091701"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes"}

    poll_seconds: int = int(os.getenv("POLL_SECONDS", "2"))


CFG = Config()

# Strategy parameters. Every filter is expressed in ATR, so the same numbers
# work for EURUSD and XAUUSD. Tune with backtest.py before trusting them.
STRAT = strat.StrategyParams(
    fast_ema=CFG.fast_ema,
    slow_ema=CFG.slow_ema,
    trend_ema=CFG.trend_ema,
    htf_ema=int(os.getenv("HTF_EMA", "100")),
    rsi_period=CFG.rsi_period,
    atr_period=CFG.atr_period,
    breakout_lookback=20,
    min_score=int(os.getenv("MIN_SCORE", "4")),
    trend_adx=float(os.getenv("TREND_ADX", "22")),
    range_adx=float(os.getenv("RANGE_ADX", "18")),
    session_start_hour=int(os.getenv("SESSION_START_HOUR", "7")),
    session_end_hour=int(os.getenv("SESSION_END_HOUR", "20")),
    trade_friday=os.getenv("TRADE_FRIDAY", "true").lower() in {"1", "true", "yes"},
    allow_range_trades=os.getenv("ALLOW_RANGE_TRADES", "true").lower() in {"1", "true", "yes"},
    min_atr_points=CFG.min_atr_points,
    sl_atr_mult=CFG.atr_sl_mult,
    tp_atr_mult=CFG.atr_tp_mult,
    partial_at_r=float(os.getenv("PARTIAL_AT_R", "1.0")),
    partial_fraction=float(os.getenv("PARTIAL_FRACTION", "0.5")),
    breakeven_at_r=float(os.getenv("BREAKEVEN_AT_R", "1.0")),
    trail_start_r=float(os.getenv("TRAIL_START_R", "1.2")),
    trail_atr_mult=float(os.getenv("TRAIL_ATR_MULT", "1.2")),
)

MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
BREAKER_COOLDOWN_MIN = int(os.getenv("BREAKER_COOLDOWN_MINUTES", "60"))
MAX_USD_EXPOSURE = int(os.getenv("MAX_USD_EXPOSURE", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("MT5Bot")


# ---------------- MT5 CONNECTION ----------------

def connect() -> None:
    if mt5.initialize():
        log.info("Connected to MetaTrader 5")
        return

    path = os.getenv("MT5_PATH", "").strip()
    if path and mt5.initialize(path=path):
        log.info("Connected to MetaTrader 5 via configured path")
        return

    raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def shutdown() -> None:
    mt5.shutdown()


# ---------------- DATA ----------------

TIMEFRAMES = {
    1: mt5.TIMEFRAME_M1,
    2: mt5.TIMEFRAME_M2,
    3: mt5.TIMEFRAME_M3,
    4: mt5.TIMEFRAME_M4,
    5: mt5.TIMEFRAME_M5,
    6: mt5.TIMEFRAME_M6,
    10: mt5.TIMEFRAME_M10,
    12: mt5.TIMEFRAME_M12,
    15: mt5.TIMEFRAME_M15,
    20: mt5.TIMEFRAME_M20,
    30: mt5.TIMEFRAME_M30,
}


def get_rates(symbol: str) -> Optional[pd.DataFrame]:
    tf = TIMEFRAMES.get(CFG.timeframe)
    if tf is None:
        raise ValueError("TIMEFRAME_MINUTES must be one of: 1,2,3,4,5,6,10,12,15,20,30")

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, CFG.bars)
    if rates is None or len(rates) < max(CFG.trend_ema + 10, 100):
        log.warning("%s: insufficient market data", symbol)
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

    # Exclude the currently-forming candle.
    return df.iloc[:-1].copy()


# ---------------- INDICATORS ----------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Indicators come from strategy.py so live and backtest stay identical."""
    return strat.add_indicators(df, STRAT)


# ---------------- SIGNAL ENGINE ----------------

def decide_for(df: pd.DataFrame, symbol: str = "") -> strat.Decision:
    """Full decision with filters: regime, session, setup score."""
    ratio = 0.0
    if symbol:
        try:
            tick = mt5.symbol_info_tick(symbol)
            atr = float(df.iloc[-1]["atr"])
            if tick is not None and atr > 0:
                ratio = (float(tick.ask) - float(tick.bid)) / atr
        except Exception:
            ratio = 0.0
    return strat.decide(df, STRAT, spread_atr_ratio=ratio)


def signal(df: pd.DataFrame) -> Optional[str]:
    """Kept for the /signals endpoint in main.py."""
    return strat.decide(df, STRAT).side


# ---------------- RISK ENGINE ----------------

def equity() -> float:
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("Could not read account information")
    return float(info.equity)


def daily_start_equity() -> float:
    """
    For production, persist this value in a database/file across restarts.
    This in-memory implementation resets when the process starts.
    """
    return daily_start_equity.value


daily_start_equity.value = equity() if mt5.account_info() else 0.0


def daily_loss_limit_hit() -> bool:
    start = daily_start_equity.value
    if start <= 0:
        return False

    current = equity()
    loss_fraction = max(0.0, (start - current) / start)
    if loss_fraction >= CFG.max_daily_loss:
        log.error("DAILY LOSS LIMIT HIT: %.2f%%", loss_fraction * 100)
        return True

    return False


def normalize_volume(symbol: str, volume: float) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0

    step = float(info.volume_step)
    vmin = float(info.volume_min)
    vmax = float(info.volume_max)

    if step <= 0:
        return 0.0

    volume = min(max(volume, vmin), vmax)
    volume = math.floor(volume / step) * step

    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return round(volume, decimals)


def calculate_volume(symbol: str, entry: float, stop: float) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0

    risk_money = equity() * CFG.risk_per_trade
    distance = abs(entry - stop)

    if distance <= 0:
        return 0.0

    # OrderCalcProfit gives a broker-aware monetary estimate for one lot.
    one_lot_loss = mt5.order_calc_profit(
        mt5.ORDER_TYPE_BUY,
        symbol,
        1.0,
        entry,
        stop
    )

    if one_lot_loss is None:
        log.warning("%s: order_calc_profit failed: %s", symbol, mt5.last_error())
        return 0.0

    loss_per_lot = abs(float(one_lot_loss))
    if loss_per_lot <= 0:
        return 0.0

    raw = risk_money / loss_per_lot
    return normalize_volume(symbol, raw)


# ---------------- GUARDS ----------------

last_trade_time: dict[str, float] = {}


def get_positions(symbol: Optional[str] = None):
    if symbol:
        return mt5.positions_get(symbol=symbol) or ()
    return mt5.positions_get() or ()


def has_position(symbol: str) -> bool:
    positions = [
        p for p in get_positions(symbol)
        if int(getattr(p, "magic", 0)) == CFG.magic
    ]
    return len(positions) >= CFG.max_positions_per_symbol


def spread_ok(symbol: str) -> bool:
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)

    if tick is None or info is None:
        return False

    spread_points = (tick.ask - tick.bid) / info.point

    if spread_points > CFG.max_spread_points:
        log.info("%s: spread %.1f > max %.1f points",
                 symbol, spread_points, CFG.max_spread_points)
        return False

    return True


def cooldown_ok(symbol: str) -> bool:
    t = last_trade_time.get(symbol, 0)
    return (time.time() - t) >= CFG.cooldown_minutes * 60


# ---------------- EXECUTION ----------------

def send_order(symbol: str, side: str, sl: float, tp: float, volume: float):
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if info is None or tick is None:
        return None

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": CFG.deviation_points,
        "magic": CFG.magic,
        "comment": "STF-AutoBot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if CFG.dry_run:
        log.warning(
            "DRY RUN | %s %s %.2f lots | entry=%s SL=%s TP=%s",
            symbol, side, volume, price, sl, tp
        )
        return {"dry_run": True, "request": request}

    result = mt5.order_send(request)

    if result is None:
        log.error("order_send returned None: %s", mt5.last_error())
        return None

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(
            "ORDER FAILED | %s %s | retcode=%s comment=%s",
            symbol, side, result.retcode, result.comment
        )
        return result

    log.info(
        "ORDER EXECUTED | %s %s %.2f lots | price=%s SL=%s TP=%s",
        symbol, side, volume, result.price, sl, tp
    )
    last_trade_time[symbol] = time.time()
    return result


def trade_symbol(symbol: str) -> None:
    if control is not None and not control.trading_allowed():
        return

    if daily_loss_limit_hit():
        return

    if len([
        p for p in get_positions()
        if int(getattr(p, "magic", 0)) == CFG.magic
    ]) >= CFG.max_open_positions:
        return

    if has_position(symbol):
        return

    if not cooldown_ok(symbol):
        return

    if not spread_ok(symbol):
        return

    df = get_rates(symbol)
    if df is None:
        return

    df = add_indicators(df)
    x = df.iloc[-1]

    if not np.isfinite(x.atr) or x.atr <= 0:
        return

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return

    atr_points = x.atr / info.point
    if atr_points < CFG.min_atr_points:
        log.info("%s: ATR %.1f points below minimum %.1f",
                 symbol, atr_points, CFG.min_atr_points)
        return

    # Regime + session + setup scoring. This is the same call the backtester
    # makes, so what you measure is what the bot trades.
    d = decide_for(df, symbol)
    if not d.ok:
        log.debug("%s: no trade (%s, score %s)", symbol, d.blocked, d.score)
        return

    # Portfolio-level guard: do not stack correlated USD risk.
    open_symbols = [p.symbol for p in get_positions()
                    if int(getattr(p, "magic", 0)) == CFG.magic]
    if not execution.correlation_guard(open_symbols, symbol, MAX_USD_EXPOSURE):
        log.info("%s: correlation guard blocked another similar exposure", symbol)
        return

    side = d.side
    entry = tick.ask if side == "BUY" else tick.bid

    sl, tp = strat.stops_for(side, entry, d.atr, STRAT)

    # Respect broker's minimum stop distance.
    min_stop = float(info.trade_stops_level) * info.point
    if abs(entry - sl) < min_stop:
        sl = entry - min_stop if side == "BUY" else entry + min_stop

    if abs(tp - entry) < min_stop:
        tp = entry + min_stop if side == "BUY" else entry - min_stop

    digits = int(info.digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    volume = calculate_volume(symbol, entry, sl)
    if volume <= 0:
        log.warning("%s: calculated volume is zero; skipping", symbol)
        return

    log.info(
        "SIGNAL | %s | %s | score=%s/%s regime=%s | RSI=%.1f ATR=%s volume=%.2f | %s",
        symbol, side, d.score, STRAT.min_score, d.regime, x.rsi, x.atr, volume,
        "; ".join(d.reasons[:3]),
    )

    result = send_order(symbol, side, sl, tp, volume)

    if control is not None:
        try:
            dry = bool(getattr(result, "dry_run", False)) or (isinstance(result, dict) and result.get("dry_run"))
            control.log_event(
                "SIGNAL_DRY_RUN" if dry else "ORDER",
                symbol, side, entry, sl, tp, volume,
                message=(f"score={d.score} regime={d.regime} "
                         f"RSI={x.rsi:.1f} ATR={x.atr:.6g} | {'; '.join(d.reasons[:3])}"),
            )
        except Exception:
            log.exception("Could not write event to the shared database")


# ---------------- MAIN LOOP ----------------

def main():
    if CFG.timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")

    connect()

    for symbol in CFG.symbols:
        if not mt5.symbol_select(symbol, True):
            log.warning("%s: could not select symbol", symbol)

    log.info(
        "STARTED | TF=M%s | risk=%.2f%% | max daily loss=%.2f%% | dry_run=%s | "
        "min_score=%s | sessions=%s-%s UTC | SL %.1fxATR / TP %.1fxATR",
        CFG.timeframe, CFG.risk_per_trade * 100, CFG.max_daily_loss * 100, CFG.dry_run,
        STRAT.min_score, STRAT.session_start_hour, STRAT.session_end_hour,
        STRAT.sl_atr_mult, STRAT.tp_atr_mult,
    )

    # Daily loss limit survives restarts (stored in bot_state.db).
    start_equity = execution.daily_start_equity(equity())
    log.info("Daily start equity: %.2f | circuit breaker: %s losses",
             start_equity, MAX_CONSECUTIVE_LOSSES)

    # Process once per newly closed candle.
    last_closed_bar: dict[str, pd.Timestamp] = {}
    atr_cache: dict[str, float] = {}

    last_allowed = None

    try:
        while True:
            # START / STOP / EMERGENCY-STOP from the phone is stored in the
            # shared SQLite state file and checked here on every iteration.
            allowed = control.trading_allowed() if control is not None else True
            if allowed != last_allowed:
                if allowed:
                    log.info("CONTROL | trading enabled (start from phone/CLI)")
                else:
                    flags = control.get_flags() if control is not None else {}
                    log.warning(
                        "CONTROL | trading disabled (emergency_stop=%s, running=%s)",
                        flags.get("emergency_stop"), flags.get("bot_running"),
                    )
                last_allowed = allowed

            if not allowed:
                time.sleep(CFG.poll_seconds)
                continue

            hit, loss = execution.daily_loss_hit(equity(), CFG.max_daily_loss)
            if hit:
                log.error("DAILY LOSS LIMIT HIT (%.2f%%). No new trades today.", loss * 100)
                time.sleep(30)
                continue

            if execution.circuit_breaker_active(MAX_CONSECUTIVE_LOSSES, BREAKER_COOLDOWN_MIN):
                log.warning("Circuit breaker active after %s losses in a row.",
                            MAX_CONSECUTIVE_LOSSES)
                time.sleep(30)
                continue

            # Break-even / partial profit / trailing on everything already open.
            try:
                actions = execution.manage_open_positions(
                    mt5, CFG.magic, STRAT, atr_cache, log=log, dry_run=CFG.dry_run)
                if actions:
                    log.info("Position management: %s action(s)", actions)
            except Exception:
                log.exception("Position management failed")

            for symbol in CFG.symbols:
                try:
                    df = get_rates(symbol)
                    if df is None:
                        continue

                    closed_bar = df.iloc[-1]["time"]

                    if last_closed_bar.get(symbol) == closed_bar:
                        continue

                    last_closed_bar[symbol] = closed_bar

                    try:
                        atr_cache[symbol] = float(
                            strat.atr_wilder(df, STRAT.atr_period).iloc[-1])
                    except Exception:
                        pass

                    trade_symbol(symbol)

                except Exception:
                    log.exception("Unhandled error while processing %s", symbol)

            time.sleep(CFG.poll_seconds)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        shutdown()


if __name__ == "__main__":
    main()
