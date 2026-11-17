"""
Event-driven backtester for the same strategy the live bot uses.

    python backtest.py --symbol EURUSD --tf 5 --bars 50000
    python backtest.py --csv data/EURUSD_M5.csv --spread-points 12

Why bother: a win rate means nothing on its own. What matters is expectancy
per trade in R, profit factor and drawdown. If the numbers are bad here they
will be worse live, after spread, slippage and swaps.

Assumptions (deliberately pessimistic):
* entry at the next bar's open, not at the signal price
* spread charged on entry and exit
* stop losses are checked before take profits inside a bar
* one position at a time, fixed fractional risk
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Optional

import numpy as np
import pandas as pd

from strategy import (
    StrategyParams,
    add_indicators,
    breakeven_stop,
    decide,
    r_multiple,
    stops_for,
    tighten,
    trailing_stop,
)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    else:
        df["time"] = pd.date_range("2020-01-01", periods=len(df), freq="5min", tz="UTC")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def load_mt5(symbol: str, tf_minutes: int, bars: int) -> pd.DataFrame:  # pragma: no cover
    import MetaTrader5 as mt5

    tf_map = {1: mt5.TIMEFRAME_M1, 5: mt5.TIMEFRAME_M5, 15: mt5.TIMEFRAME_M15,
              30: mt5.TIMEFRAME_M30, 60: mt5.TIMEFRAME_H1, 240: mt5.TIMEFRAME_H4}
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select {symbol}")
    rates = mt5.copy_rates_from_pos(symbol, tf_map.get(tf_minutes, mt5.TIMEFRAME_M5), 0, bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No history for {symbol}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)


def run_backtest(df: pd.DataFrame, params: StrategyParams, *,
                 balance: float = 10_000.0,
                 risk_frac: float = 0.0025,
                 spread: float = 0.0,
                 commission_r: float = 0.0) -> dict:
    ind = add_indicators(df, params)
    warmup = max(params.htf_ema + 5, 60)

    equity = balance
    peak = balance
    max_dd = 0.0
    trades: list[dict] = []
    pos: Optional[dict] = None
    last_close = df["close"].to_numpy()

    for i in range(warmup, len(ind) - 1):
        window = ind.iloc[: i + 1]

        # ---- manage an open position on this bar ----
        if pos is not None:
            bar = ind.iloc[i]
            side = pos["side"]

            if side == "BUY":
                hit_sl = bar.low <= pos["sl"]
                hit_tp = bar.high >= pos["tp"]
            else:
                hit_sl = bar.high >= pos["sl"]
                hit_tp = bar.low <= pos["tp"]

            if hit_sl:  # pessimistic: stop first
                trades.append(_close(pos, pos["sl"], "SL", commission_r))
                equity += trades[-1]["pnl_r"] * pos["risk_money"]
                pos = None
            elif hit_tp:
                trades.append(_close(pos, pos["tp"], "TP", commission_r))
                equity += trades[-1]["pnl_r"] * pos["risk_money"]
                pos = None
            else:
                r = r_multiple(side, pos["entry"], pos["sl0"], float(bar.close))
                if (not pos["partial_done"]) and r >= params.partial_at_r:
                    pos["realised_r"] += params.partial_fraction * r
                    pos["partial_done"] = True
                if r >= params.breakeven_at_r:
                    pos["sl"] = tighten(side, pos["sl"], breakeven_stop(side, pos["entry"], bar.atr, params))
                if r >= params.trail_start_r:
                    pos["sl"] = tighten(side, pos["sl"], trailing_stop(side, float(bar.close), bar.atr, params))

                equity_now = equity + pos["realised_r"] * pos["risk_money"] + \
                    (1 - params.partial_fraction) * r * pos["risk_money"]
                peak = max(peak, equity_now)
                if peak > 0:
                    max_dd = max(max_dd, (peak - equity_now) / peak)

        if pos is not None:
            continue

        # ---- look for a new entry ----
        d = decide(window, params)
        if not d.ok:
            continue

        nxt = ind.iloc[i + 1]
        entry = float(nxt.open) + (spread if d.side == "BUY" else 0.0)
        sl, tp = stops_for(d.side, entry, d.atr, params)
        risk_money = equity * risk_frac

        pos = {"side": d.side, "entry": entry, "sl": sl, "sl0": sl, "tp": tp,
               "risk_money": risk_money, "realised_r": 0.0, "partial_done": False,
               "partial_fraction": params.partial_fraction,
               "open_index": i + 1, "open_time": nxt["time"], "score": d.score,
               "regime": d.regime}

    if pos is not None:
        trades.append(_close(pos, float(last_close[-1]), "OPEN_AT_END", commission_r))

    return _summarise(trades, balance, max_dd)


def _close(pos: dict, price: float, reason: str, commission_r: float) -> dict:
    r = r_multiple(pos["side"], pos["entry"], pos["sl0"], price)
    realised = pos["realised_r"] + (1 - pos["partial_fraction"]) * r
    return {
        "side": pos["side"], "entry": pos["entry"], "exit": price, "reason": reason,
        "r": r, "pnl_r": realised - commission_r, "score": pos["score"],
        "regime": pos["regime"], "open_time": str(pos["open_time"]),
    }


def _summarise(trades: list[dict], balance: float, max_dd: float) -> dict:
    if not trades:
        return {"trades": 0, "note": "no trades produced by these settings",
                "max_drawdown_pct": 0.0}

    rs = np.array([t["pnl_r"] for t in trades], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs < 0]

    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())

    # longest losing streak
    streak = worst = 0
    for r in rs:
        if r < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0

    return {
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1),
        "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_r": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "total_r": round(float(rs.sum()), 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "worst_losing_streak": worst,
        "exit_reasons": {r: int((np.array([t["reason"] for t in trades]) == r).sum())
                         for r in {t["reason"] for t in trades}},
    }


def synthetic(n: int = 5000, seed: int = 7, start: float = 1.10,
              vol: float = 0.0006) -> pd.DataFrame:
    """Random walk with mild trend persistence — for smoke tests only."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, vol / 8, n // 200 + 1), 200)[:n]
    rets = rng.normal(0, vol, n) + drift
    close = start * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start], close[:-1]])
    spread = np.abs(rng.normal(0, vol / 2, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    return pd.DataFrame({
        "time": pd.date_range("2023-01-01", periods=n, freq="5min", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
    })


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(description="Backtest the MT5 bot strategy")
    ap.add_argument("--csv", help="CSV with time,open,high,low,close")
    ap.add_argument("--symbol", help="Pull history straight from MT5 (Windows)")
    ap.add_argument("--tf", type=int, default=5, help="Timeframe in minutes")
    ap.add_argument("--bars", type=int, default=50_000)
    ap.add_argument("--balance", type=float, default=10_000)
    ap.add_argument("--risk", type=float, default=0.0025, help="Risk per trade (0.0025 = 0.25%%)")
    ap.add_argument("--spread-points", type=float, default=10)
    ap.add_argument("--synthetic", action="store_true", help="Smoke test on random data")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.csv:
        df = load_csv(args.csv)
    elif args.symbol:
        df = load_mt5(args.symbol, args.tf, args.bars)
    elif args.synthetic:
        df = synthetic(args.bars)
    else:
        ap.error("choose --csv, --symbol or --synthetic")

    point = float(df["close"].iloc[-1]) * 1e-5 if df["close"].iloc[-1] > 20 else 1e-5
    spread = args.spread_points * point

    result = run_backtest(df, StrategyParams(), balance=args.balance,
                          risk_frac=args.risk, spread=spread)
    result["bars"] = len(df)
    result["spread"] = spread

    print(json.dumps(result, indent=2) if args.json else _pretty(result))


def _pretty(r: dict) -> str:
    lines = ["", "  Backtest result", "  " + "-" * 38]
    for k, v in r.items():
        lines.append(f"  {k:<22} {v}")
    lines += ["", "  Reminder: positive numbers here are a hypothesis, not a promise.",
              "  Re-run per symbol/timeframe and keep DRY_RUN on until it holds up.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
