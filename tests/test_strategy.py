"""Run with:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# keep the shared state out of the real bot_state.db during tests
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test_state.db"))

import execution  # noqa: E402
import strategy as strat  # noqa: E402


def trending(n: int = 400, step: float = 0.0002, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(np.full(n, step) + rng.normal(0, step / 6, n))
    open_ = np.concatenate([[1.10], close[:-1]])
    high = np.maximum(open_, close) + abs(rng.normal(0, step / 3, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, step / 3, n))
    return pd.DataFrame({
        "time": pd.date_range("2024-01-02 08:00", periods=n, freq="5min", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
    })


class IndicatorTests(unittest.TestCase):
    def test_rsi_saturates_on_one_way_move(self):
        s = pd.Series(np.arange(100, dtype=float))
        self.assertGreater(strat.rsi_wilder(s, 14).iloc[-1], 99)

    def test_atr_positive_and_scales_with_range(self):
        df = trending()
        atr = strat.atr_wilder(df, 14).iloc[-1]
        self.assertTrue(np.isfinite(atr) and atr > 0)

    def test_adx_high_in_trend_and_di_directions(self):
        df = strat.add_indicators(trending(step=0.0004), strat.StrategyParams())
        last = df.iloc[-1]
        self.assertGreater(last.adx, 22)
        self.assertGreater(last.plus_di, last.minus_di)

    def test_adx_is_not_a_running_sum(self):
        """A classic bug makes ADX ~14x too large; keep it in 0..100."""
        df = strat.add_indicators(trending(), strat.StrategyParams())
        self.assertLessEqual(float(df["adx"].max()), 100.0)


class RegimeTests(unittest.TestCase):
    def test_uptrend_detected(self):
        p = strat.StrategyParams()
        df = strat.add_indicators(trending(step=0.0004), p)
        self.assertEqual(strat.market_regime(df.iloc[-1], p), "trend_up")

    def test_flat_market_is_not_a_trend(self):
        p = strat.StrategyParams()
        df = trending(step=0.0)
        df[["open", "high", "low", "close"]] = 1.10
        ind = strat.add_indicators(df, p)
        self.assertIn(strat.market_regime(ind.iloc[-1], p), {"range", "chop"})


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.p = strat.StrategyParams()

    def test_needs_history(self):
        d = strat.decide(trending(30), self.p)
        self.assertFalse(d.ok)
        self.assertEqual(d.blocked, "not enough history")

    def test_session_filter_blocks_night(self):
        df = trending()
        # rebuild the clock so the last bars sit at 23:00 UTC
        df["time"] = pd.date_range(end=pd.Timestamp("2024-01-04 23:00", tz="UTC"),
                                   periods=len(df), freq="5min")
        ind = strat.add_indicators(df, self.p)
        d = strat.decide(ind, self.p)
        self.assertFalse(d.ok)
        self.assertEqual(d.blocked, "outside session")

    def test_session_filter_allows_london(self):
        df = trending()
        df["time"] = pd.date_range(end=pd.Timestamp("2024-01-04 09:00", tz="UTC"),
                                   periods=len(df), freq="5min")
        ind = strat.add_indicators(df, self.p)
        self.assertNotEqual(strat.decide(ind, self.p).blocked, "outside session")

    def test_wide_spread_blocks(self):
        ind = strat.add_indicators(trending(step=0.0004), self.p)
        d = strat.decide(ind, self.p, spread_atr_ratio=0.9)
        self.assertFalse(d.ok)
        self.assertIn("spread", d.blocked)

    def test_trend_gives_a_side_and_reasons(self):
        ind = strat.add_indicators(trending(step=0.0004), self.p)
        d = strat.decide(ind, self.p)
        self.assertIn(d.side, {"BUY", "SELL", None})
        if d.side:
            self.assertGreaterEqual(d.score, self.p.min_score)
            self.assertTrue(d.reasons)


class TradeMathTests(unittest.TestCase):
    def setUp(self):
        self.p = strat.StrategyParams()

    def test_stops_are_symmetric_for_both_sides(self):
        buy_sl, buy_tp = strat.stops_for("BUY", 1.1000, 0.0010, self.p)
        sell_sl, sell_tp = strat.stops_for("SELL", 1.1000, 0.0010, self.p)
        self.assertAlmostEqual(buy_sl, 1.0985)
        self.assertAlmostEqual(buy_tp, 1.1030)
        self.assertAlmostEqual(sell_sl, 1.1015)
        self.assertAlmostEqual(sell_tp, 1.0970)

    def test_r_multiple(self):
        self.assertAlmostEqual(strat.r_multiple("BUY", 1.10, 1.09, 1.12), 2.0)
        self.assertAlmostEqual(strat.r_multiple("SELL", 1.10, 1.11, 1.08), 2.0)
        self.assertEqual(strat.r_multiple("BUY", 1.10, 1.10, 1.12), 0.0)

    def test_stops_only_tighten(self):
        self.assertEqual(strat.tighten("BUY", 1.090, 1.080), 1.090)
        self.assertEqual(strat.tighten("BUY", 1.090, 1.095), 1.095)
        self.assertEqual(strat.tighten("SELL", 1.110, 1.120), 1.110)
        self.assertEqual(strat.tighten("SELL", 1.110, 1.105), 1.105)


class ExecutionTests(unittest.TestCase):
    def test_correlation_guard(self):
        self.assertFalse(execution.correlation_guard(["EURUSD", "GBPUSD"], "XAUUSD", 2))
        self.assertTrue(execution.correlation_guard(["EURUSD"], "XAUUSD", 2))
        self.assertTrue(execution.correlation_guard([], "XAUUSD", 2))

    def test_circuit_breaker_trips_and_cools_down(self):
        import control
        control.set("consecutive_losses", "0")
        control.set("breaker_until", "")
        self.assertFalse(execution.circuit_breaker_active(3, 60))
        for _ in range(3):
            execution.register_trade_result(-10.0)
        self.assertTrue(execution.circuit_breaker_active(3, 60))
        # a win resets the streak
        execution.register_trade_result(5.0)
        self.assertEqual(control.get("consecutive_losses"), "0")

    def test_daily_loss_limit_is_persisted(self):
        import control
        control.set("daily_start_equity:" + __import__("datetime").date.today().isoformat(), "1000")
        hit, loss = execution.daily_loss_hit(970, 0.02)
        self.assertTrue(hit)
        self.assertAlmostEqual(loss, 0.03, places=3)
        hit2, _ = execution.daily_loss_hit(995, 0.02)
        self.assertFalse(hit2)


class BacktestTests(unittest.TestCase):
    def test_runs_and_reports(self):
        import backtest
        df = backtest.synthetic(n=2500, seed=11)
        res = backtest.run_backtest(df, strat.StrategyParams())
        self.assertIn("trades", res)
        if res["trades"]:
            self.assertIn("expectancy_r", res)
            self.assertIn("profit_factor", res)
            self.assertGreaterEqual(res["max_drawdown_pct"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
