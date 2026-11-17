"""
Strategy engine for the MT5 bot — pure pandas, no MetaTrader5 import.

Ideas and structure are borrowed from well-run open-source MT5 bots and from
standard desk practice (see RESEARCH.md for the sources):

* several independent setups behind one decision function
  (trend continuation, breakout, pullback, range reversion)
* every filter expressed in ATR so one setting works across FX, gold and indices
* ADX regime gate: trend setups only in a trend, reversion only in a range
* higher-timeframe alignment
* session filter, so we do not trade thin liquidity
* Wilder-smoothed RSI/ATR/ADX (a very common bug is a plain running average,
  which makes ADX look ~14x too large and silently disables the filter)

Nothing here predicts the future. The only claim is that entries are filtered
and measured, so you can backtest the same code you trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- parameters

@dataclass
class StrategyParams:
    fast_ema: int = 9
    slow_ema: int = 21
    trend_ema: int = 50
    htf_ema: int = 100

    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14

    breakout_lookback: int = 20
    pullback_tolerance_atr: float = 0.35

    # ADX thresholds
    trend_adx: float = 22.0
    range_adx: float = 18.0

    # score needed to allow an entry (max ~7)
    min_score: int = 4

    # filters
    session_start_hour: int = 7      # UTC
    session_end_hour: int = 20       # UTC
    trade_friday: bool = True
    allow_range_trades: bool = True
    min_atr_points: float = 0.0
    max_spread_atr_ratio: float = 0.25

    # exits in R multiples
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    partial_at_r: float = 1.0
    partial_fraction: float = 0.5
    breakeven_at_r: float = 1.0
    breakeven_buffer_atr: float = 0.10
    trail_start_r: float = 1.2
    trail_atr_mult: float = 1.2


# ---------------------------------------------------------------- indicators

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _wilder(s: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (RMA): alpha = 1/period, seeded with the first value."""
    return s.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder(-delta.clip(upper=0), period)

    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # No losses at all -> RSI is 100 (a plain division would give NaN and,
    # after a naive fillna(50), silently disable every momentum filter).
    rsi = rsi.where(loss > 0, np.where(gain > 0, 100.0, 50.0))
    return pd.Series(rsi, index=close.index).fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _wilder(true_range(df), period)


def adx_wilder(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (adx, plus_di, minus_di), all Wilder-smoothed."""
    up = df["high"].diff()
    down = -df["low"].diff()

    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = _wilder(true_range(df), period).replace(0, np.nan)
    plus_di = 100 * _wilder(pd.Series(plus_dm, index=df.index), period) / tr
    minus_di = 100 * _wilder(pd.Series(minus_dm, index=df.index), period) / tr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx, period), plus_di, minus_di


def add_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()

    out["ema_fast"] = _ema(out["close"], p.fast_ema)
    out["ema_slow"] = _ema(out["close"], p.slow_ema)
    out["ema_trend"] = _ema(out["close"], p.trend_ema)
    out["htf_ema"] = _ema(out["close"], p.htf_ema)

    out["rsi"] = rsi_wilder(out["close"], p.rsi_period)
    out["atr"] = atr_wilder(out, p.atr_period)

    adx, plus_di, minus_di = adx_wilder(out, p.adx_period)
    out["adx"] = adx
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    n = p.breakout_lookback
    out["high_n"] = out["high"].rolling(n).max().shift(1)
    out["low_n"] = out["low"].rolling(n).min().shift(1)

    return out


# ---------------------------------------------------------------- regime

def market_regime(row, p: StrategyParams) -> str:
    """trend_up / trend_down / range / chop — coarse but robust."""
    if not np.isfinite(row.adx):
        return "chop"

    stacked_up = row.ema_fast > row.ema_slow > row.ema_trend and row.close > row.ema_trend
    stacked_dn = row.ema_fast < row.ema_slow < row.ema_trend and row.close < row.ema_trend

    if row.adx >= p.trend_adx and stacked_up:
        return "trend_up"
    if row.adx >= p.trend_adx and stacked_dn:
        return "trend_down"
    if row.adx <= p.range_adx:
        return "range"
    return "chop"


def session_ok(ts: pd.Timestamp, p: StrategyParams) -> bool:
    hour = ts.hour
    if not (p.session_start_hour <= hour < p.session_end_hour):
        return False
    if not p.trade_friday and ts.dayofweek == 4:
        return False
    return True


# ---------------------------------------------------------------- scoring

def _long_score(df: pd.DataFrame, p: StrategyParams) -> tuple[int, list[str]]:
    x = df.iloc[-1]
    prev = df.iloc[-2]
    score, why = 0, []

    trend_up = x.ema_fast > x.ema_slow > x.ema_trend and x.close > x.ema_trend
    if trend_up:
        score += 2
        why.append("EMA-stack up")
    if x.close > x.htf_ema:
        score += 1
        why.append("above HTF EMA")

    if np.isfinite(x.high_n) and x.close > x.high_n and prev.close <= prev.high_n:
        score += 2
        why.append("breakout above range")

    near_slow = abs(x.close - x.ema_slow) <= p.pullback_tolerance_atr * x.atr
    if near_slow and x.close > x.open and trend_up:
        score += 2
        why.append("pullback to EMA20 reclaimed")

    if 52 <= x.rsi <= 70:
        score += 1
        why.append("RSI momentum")

    if np.isfinite(x.adx) and x.adx >= p.trend_adx and x.plus_di > x.minus_di:
        score += 1
        why.append("DI+ dominant")

    return score, why


def _short_score(df: pd.DataFrame, p: StrategyParams) -> tuple[int, list[str]]:
    x = df.iloc[-1]
    prev = df.iloc[-2]
    score, why = 0, []

    trend_dn = x.ema_fast < x.ema_slow < x.ema_trend and x.close < x.ema_trend
    if trend_dn:
        score += 2
        why.append("EMA-stack down")
    if x.close < x.htf_ema:
        score += 1
        why.append("below HTF EMA")

    if np.isfinite(x.low_n) and x.close < x.low_n and prev.close >= prev.low_n:
        score += 2
        why.append("breakdown below range")

    near_slow = abs(x.close - x.ema_slow) <= p.pullback_tolerance_atr * x.atr
    if near_slow and x.close < x.open and trend_dn:
        score += 2
        why.append("pullback to EMA20 rejected")

    if 30 <= x.rsi <= 48:
        score += 1
        why.append("RSI momentum")

    if np.isfinite(x.adx) and x.adx >= p.trend_adx and x.minus_di > x.plus_di:
        score += 1
        why.append("DI- dominant")

    return score, why


def _range_score(df: pd.DataFrame, p: StrategyParams) -> tuple[Optional[str], int, list[str]]:
    """Mean reversion, only valid in a range regime."""
    x = df.iloc[-1]
    if not p.allow_range_trades:
        return None, 0, []

    if x.rsi <= 28 and np.isfinite(x.low_n) and x.close <= x.low_n * 1.0:
        return "BUY", 3, ["range low + oversold RSI"]
    if x.rsi >= 72 and np.isfinite(x.high_n) and x.close >= x.high_n * 1.0:
        return "SELL", 3, ["range high + overbought RSI"]
    return None, 0, []


@dataclass
class Decision:
    side: Optional[str] = None
    score: int = 0
    regime: str = "chop"
    reasons: list[str] = field(default_factory=list)
    atr: float = 0.0
    blocked: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.side is not None and not self.blocked


def decide(df: pd.DataFrame, p: StrategyParams,
           spread_atr_ratio: float = 0.0) -> Decision:
    """Single decision point used by both the live bot and the backtester."""
    if len(df) < max(p.htf_ema + 5, 60):
        return Decision(blocked="not enough history")

    x = df.iloc[-1]
    if not np.isfinite(x.atr) or x.atr <= 0:
        return Decision(blocked="no ATR")

    regime = market_regime(x, p)
    d = Decision(regime=regime, atr=float(x.atr))

    if spread_atr_ratio > p.max_spread_atr_ratio:
        d.blocked = f"spread {spread_atr_ratio:.2f}x ATR too wide"
        return d

    try:
        ts = pd.Timestamp(x["time"])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    except Exception:
        ts = None
    if ts is not None and not session_ok(ts, p):
        d.blocked = "outside session"
        return d

    if regime == "range":
        side, score, why = _range_score(df, p)
        d.score, d.reasons = score, why
        if side and score >= 3:
            d.side = side
            return d
        d.blocked = "range: no reversion edge"
        return d

    long_score, long_why = _long_score(df, p)
    short_score, short_why = _short_score(df, p)

    if regime == "trend_up" and long_score >= short_score:
        d.score, d.reasons = long_score, long_why
        if long_score >= p.min_score:
            d.side = "BUY"
            return d
        d.blocked = f"score {long_score} < {p.min_score}"
        return d

    if regime == "trend_down" and short_score >= long_score:
        d.score, d.reasons = short_score, short_why
        if short_score >= p.min_score:
            d.side = "SELL"
            return d
        d.blocked = f"score {short_score} < {p.min_score}"
        return d

    d.score = max(long_score, short_score)
    d.reasons = long_why if long_score >= short_score else short_why
    d.blocked = f"regime {regime}: no aligned setup"
    return d


# ---------------------------------------------------------------- trade maths

def stops_for(side: str, entry: float, atr: float, p: StrategyParams) -> tuple[float, float]:
    if side == "BUY":
        return entry - p.sl_atr_mult * atr, entry + p.tp_atr_mult * atr
    return entry + p.sl_atr_mult * atr, entry - p.tp_atr_mult * atr


def r_multiple(side: str, entry: float, sl: float, price: float) -> float:
    """How far price has travelled, measured in units of the initial risk."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    move = (price - entry) if side == "BUY" else (entry - price)
    return move / risk


def breakeven_stop(side: str, entry: float, atr: float, p: StrategyParams) -> float:
    buf = p.breakeven_buffer_atr * atr
    return entry + buf if side == "BUY" else entry - buf


def trailing_stop(side: str, price: float, atr: float, p: StrategyParams) -> float:
    dist = p.trail_atr_mult * atr
    return price - dist if side == "BUY" else price + dist


def tighten(side: str, current_sl: float, candidate: float) -> float:
    """Stops only ever move in the protective direction."""
    if side == "BUY":
        return max(current_sl, candidate)
    return min(current_sl, candidate) if current_sl > 0 else candidate
