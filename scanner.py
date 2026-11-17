"""
Market scanner: walk the whole symbol list, score every setup, and send the
best ones to Telegram/SMS with entry, SL and TP.

    python scanner.py --once                 # one pass, print the table
    python scanner.py --loop 300             # scan every 5 minutes
    python scanner.py --once --notify        # also push to Telegram/SMS
    python scanner.py --symbols EURUSD,XAUUSD --min-score 6 --once

Design notes
* nothing is executed — this is a signal desk, you decide what to take
* the same `strategy.decide()` as the live bot and the backtester is used here,
  so a signal in Telegram is exactly what the bot would trade
* every signal is de-duplicated per symbol/timeframe/direction/candle, so you
  get one message per setup, not one per polling loop
* the whole scan is written to the audit log, so it shows up in the app
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import pandas as pd

import control
import notify
import strategy as strat

log = logging.getLogger("Scanner")

TF_LABELS = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4"}
MAX_SCORE = 9  # trend 2 + htf 1 + breakout 2 + pullback 2 + rsi 1 + di 1


@dataclass
class Signal:
    symbol: str
    timeframe: str
    side: str
    entry: float
    sl: float
    tp: float
    rr: float
    score: int
    max_score: int
    regime: str
    reasons: list[str] = field(default_factory=list)
    atr: float = 0.0
    bar_time: str = ""

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.side}|{self.bar_time}"


# ---------------------------------------------------------------- providers

class MT5Provider:
    """Thin wrapper so the scanner logic can be tested without MetaTrader5."""

    def __init__(self, mt5_module=None, max_symbols: int = 40):  # pragma: no cover
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # noqa: F401
        self.mt5 = mt5_module
        self.max_symbols = max_symbols

    def start(self) -> bool:  # pragma: no cover
        if self.mt5.terminal_info() is not None:
            return True
        return bool(self.mt5.initialize())

    def symbols(self) -> list[str]:  # pragma: no cover
        try:
            all_symbols = self.mt5.symbols_get() or []
        except Exception:
            return []
        names = [s.name for s in all_symbols if getattr(s, "visible", False)]
        if not names:
            names = [s.name for s in all_symbols]
        # prefer things that actually move and are cheap to quote
        preferred = ("EUR", "GBP", "USD", "JPY", "XAU", "XAG", "AUD", "CHF", "CAD", "NAS", "US30", "GER")
        names.sort(key=lambda n: (0 if any(p in n for p in preferred) else 1, len(n)))
        return names[: self.max_symbols]

    def rates(self, symbol: str, tf_min: int, bars: int) -> Optional[pd.DataFrame]:  # pragma: no cover
        tf_map = {1: self.mt5.TIMEFRAME_M1, 5: self.mt5.TIMEFRAME_M5,
                  15: self.mt5.TIMEFRAME_M15, 30: self.mt5.TIMEFRAME_M30,
                  60: self.mt5.TIMEFRAME_H1, 240: self.mt5.TIMEFRAME_H4}
        tf = tf_map.get(tf_min)
        if tf is None:
            return None
        try:
            self.mt5.symbol_select(symbol, True)
            rates = self.mt5.copy_rates_from_pos(symbol, tf, 0, bars)
        except Exception:
            return None
        if rates is None or len(rates) < 60:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.iloc[:-1].copy()  # drop the candle that is still forming

    def price(self, symbol: str, side: str) -> Optional[float]:  # pragma: no cover
        try:
            tick = self.mt5.symbol_info_tick(symbol)
        except Exception:
            return None
        if tick is None:
            return None
        return float(tick.ask if side == "BUY" else tick.bid)


# ---------------------------------------------------------------- core logic

def evaluate(provider, symbol: str, tf_min: int, params: strat.StrategyParams,
             bars: int = 400) -> Optional[Signal]:
    """Score one symbol/timeframe. Returns None when there is no tradeable setup."""
    df = provider.rates(symbol, tf_min, bars)
    if df is None or len(df) < max(params.htf_ema + 5, 60):
        return None

    ind = strat.add_indicators(df, params)

    spread_ratio = 0.0
    try:
        bid = provider.price(symbol, "SELL")
        ask = provider.price(symbol, "BUY")
        atr = float(ind.iloc[-1]["atr"])
        if bid and ask and atr > 0:
            spread_ratio = (ask - bid) / atr
    except Exception:
        spread_ratio = 0.0

    d = strat.decide(ind, params, spread_atr_ratio=spread_ratio)
    if not d.ok:
        return None

    entry = provider.price(symbol, d.side)
    if entry is None:
        return None

    sl, tp = strat.stops_for(d.side, float(entry), d.atr, params)
    risk = abs(float(entry) - sl)
    reward = abs(tp - float(entry))

    return Signal(
        symbol=symbol,
        timeframe=TF_LABELS.get(tf_min, f"M{tf_min}"),
        side=d.side,
        entry=round(float(entry), 6),
        sl=round(float(sl), 6),
        tp=round(float(tp), 6),
        rr=round(reward / risk, 2) if risk > 0 else 0.0,
        score=int(d.score),
        max_score=MAX_SCORE,
        regime=d.regime,
        reasons=list(d.reasons),
        atr=float(d.atr),
        bar_time=str(ind.iloc[-1]["time"]),
    )


def scan(provider, symbols: Iterable[str], timeframes: Iterable[int],
         params: Optional[strat.StrategyParams] = None,
         min_score: int = 5, bars: int = 400) -> list[Signal]:
    params = params or strat.StrategyParams()
    out: list[Signal] = []

    for symbol in symbols:
        for tf_min in timeframes:
            try:
                sig = evaluate(provider, symbol, tf_min, params, bars)
            except Exception as exc:
                log.debug("%s %s: %s", symbol, tf_min, exc)
                continue
            if sig and sig.score >= min_score:
                out.append(sig)

    out.sort(key=lambda s: (-s.score, -s.rr, s.symbol))
    return out


def unseen(signals: list[Signal], mark: bool = True) -> list[Signal]:
    """Drop setups we have already alerted on (keyed by symbol/tf/side/candle)."""
    fresh = []
    for sig in signals:
        key = f"scan_sent:{sig.key}"
        if control.get(key):
            continue
        fresh.append(sig)
        if mark:
            control.set(key, datetime.now(timezone.utc).isoformat())
    return fresh


def format_table(signals: list[Signal]) -> str:
    if not signals:
        return "No setups above the score threshold right now."
    lines = [f"{'SYMBOL':<10}{'TF':<5}{'SIDE':<6}{'ENTRY':>11}{'SL':>11}{'TP':>11}"
             f"{'R:R':>6}{'SCORE':>7}  REGIME"]
    for s in signals:
        lines.append(f"{s.symbol:<10}{s.timeframe:<5}{s.side:<6}{s.entry:>11.5f}"
                     f"{s.sl:>11.5f}{s.tp:>11.5f}{s.rr:>6.1f}{s.score:>4}/{s.max_score:<3}"
                     f" {s.regime}")
    return "\n".join(lines)


def run_once(provider, symbols, timeframes, params, min_score, bars,
             push: bool) -> list[Signal]:
    signals = scan(provider, symbols, timeframes, params, min_score, bars)
    print(format_table(signals))

    fresh = unseen(signals) if push else []
    for sig in fresh:
        text = notify.format_signal(asdict(sig) | {"max_score": sig.max_score})
        notify.send(text)
        control.log_event(
            "SCANNER", sig.symbol, sig.side, sig.entry, sig.sl, sig.tp,
            message=f"{sig.timeframe} score {sig.score}/{sig.max_score} "
                    f"{sig.regime} | {'; '.join(sig.reasons[:3])}",
        )

    if signals:
        control.set("scan_last", json.dumps([asdict(s) for s in signals][:50]))
    control.set("scan_updated", datetime.now(timezone.utc).isoformat())
    return signals


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Scan the market for setups")
    ap.add_argument("--once", action="store_true", help="single pass")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="repeat every N seconds")
    ap.add_argument("--symbols", help="comma separated, default: MT5 Market Watch")
    ap.add_argument("--timeframes", default=os.getenv("SCAN_TIMEFRAMES", "5,15,60"))
    ap.add_argument("--min-score", type=int, default=int(os.getenv("SCAN_MIN_SCORE", "5")))
    ap.add_argument("--max-symbols", type=int, default=int(os.getenv("SCAN_MAX_SYMBOLS", "40")))
    ap.add_argument("--bars", type=int, default=400)
    ap.add_argument("--notify", action="store_true", help="push new setups to Telegram/SMS")
    args = ap.parse_args()

    timeframes = [int(t) for t in args.timeframes.split(",") if t.strip()]
    provider = MT5Provider(max_symbols=args.max_symbols)
    if not provider.start():
        raise SystemExit("Could not connect to MetaTrader 5 — is the terminal running?")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else provider.symbols())
    log.info("Scanning %s symbols on %s — min score %s",
             len(symbols), [TF_LABELS.get(t, t) for t in timeframes], args.min_score)
    if args.notify:
        log.info("Notify channels: %s", notify.channels() or "none configured")

    while True:
        try:
            run_once(provider, symbols, timeframes, strat.StrategyParams(),
                     args.min_score, args.bars, push=args.notify)
        except Exception:
            log.exception("Scan failed")

        if args.once or not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
