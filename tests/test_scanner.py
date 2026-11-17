"""Run with:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test_scanner.db"))

import notify  # noqa: E402
import scanner  # noqa: E402
import strategy as strat  # noqa: E402


class FakeProvider:
    """Behaves like MT5Provider but serves deterministic frames."""

    def __init__(self, trend: float = 0.0006, symbols=("EURUSD", "XAUUSD"), n: int = 400):
        self._trend = trend
        self._symbols = list(symbols)
        self._n = n
        self.calls = 0

    def symbols(self):
        return list(self._symbols)

    def rates(self, symbol, tf_min, bars):
        self.calls += 1
        rng = np.random.default_rng(hash((symbol, tf_min)) % 2**32)
        n = min(self._n, bars)
        step = self._trend
        close = 1.10 + np.cumsum(np.full(n, step) + rng.normal(0, step / 6, n))
        open_ = np.concatenate([[1.10], close[:-1]])
        high = np.maximum(open_, close) + abs(rng.normal(0, step / 3, n))
        low = np.minimum(open_, close) - abs(rng.normal(0, step / 3, n))
        return pd.DataFrame({
            "time": pd.date_range(end=pd.Timestamp("2024-01-04 09:00", tz="UTC"),
                                  periods=n, freq=f"{tf_min}min"),
            "open": open_, "high": high, "low": low, "close": close,
        })

    def price(self, symbol, side):
        # tight spread on purpose: a wide one is correctly rejected by the filter
        return 1.10002 if side == "BUY" else 1.10000


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.p = strat.StrategyParams()

    def test_signal_has_entry_stops_and_score(self):
        sig = scanner.evaluate(FakeProvider(), "EURUSD", 5, self.p)
        if sig is None:
            self.skipTest("synthetic data produced no setup")
        self.assertIn(sig.side, {"BUY", "SELL"})
        self.assertGreater(sig.score, 0)
        if sig.side == "BUY":
            self.assertLess(sig.sl, sig.entry)
            self.assertGreater(sig.tp, sig.entry)
        else:
            self.assertGreater(sig.sl, sig.entry)
            self.assertLess(sig.tp, sig.entry)
        self.assertAlmostEqual(sig.rr, self.p.tp_atr_mult / self.p.sl_atr_mult, places=1)

    def test_entry_comes_from_the_right_side_of_the_book(self):
        sig = scanner.evaluate(FakeProvider(), "EURUSD", 5, self.p)
        if sig is None:
            self.skipTest("no setup")
        expected = 1.10002 if sig.side == "BUY" else 1.10000
        self.assertAlmostEqual(sig.entry, expected, places=5)

    def test_outside_session_yields_nothing(self):
        class Night(FakeProvider):
            def rates(self, symbol, tf_min, bars):
                df = super().rates(symbol, tf_min, bars)
                df["time"] = pd.date_range(end=pd.Timestamp("2024-01-04 23:00", tz="UTC"),
                                           periods=len(df), freq=f"{tf_min}min")
                return df

        self.assertIsNone(scanner.evaluate(Night(), "EURUSD", 5, self.p))

    def test_unknown_timeframe_gets_a_readable_label(self):
        sig = scanner.evaluate(FakeProvider(), "EURUSD", 7, self.p)
        if sig is not None:
            self.assertEqual(sig.timeframe, "M7")


class ScanTests(unittest.TestCase):
    def test_scan_filters_and_sorts(self):
        prov = FakeProvider()
        loose = scanner.scan(prov, prov.symbols(), [5, 15, 60], min_score=0)
        strict = scanner.scan(prov, prov.symbols(), [5, 15, 60], min_score=99)
        self.assertGreaterEqual(len(loose), len(strict))
        self.assertEqual(strict, [])
        scores = [s.score for s in loose]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_dedupe_sends_each_setup_once(self):
        prov = FakeProvider()
        sigs = scanner.scan(prov, prov.symbols(), [5], min_score=0)
        if not sigs:
            self.skipTest("no setups")
        first = scanner.unseen(sigs)
        second = scanner.unseen(sigs)
        self.assertEqual(len(first), len(sigs))
        self.assertEqual(second, [])

    def test_table_is_human_readable(self):
        sig = scanner.Signal("EURUSD", "M5", "BUY", 1.1002, 1.0985, 1.1036, 2.0,
                             6, scanner.MAX_SCORE, "trend_up", ["EMA-stack up"])
        table = scanner.format_table([sig])
        self.assertIn("EURUSD", table)
        self.assertIn("BUY", table)
        self.assertIn("6/9", table)


class NotifyTests(unittest.TestCase):
    def test_signal_message_contains_the_numbers(self):
        sig = scanner.Signal("XAUUSD", "M15", "SELL", 2347.8, 2352.6, 2338.2, 2.0,
                             6, scanner.MAX_SCORE, "trend_down",
                             ["breakdown below range", "DI- dominant"])
        text = notify.format_signal(sig.__dict__ | {"max_score": sig.max_score})
        self.assertIn("XAUUSD", text)
        self.assertIn("SELL", text)
        self.assertIn("2347.8", text)
        self.assertIn("SL", text)
        self.assertIn("TP", text)
        self.assertIn("6/9", text)

    def test_html_is_stripped_for_sms(self):
        self.assertEqual(notify._strip_html("<b>BUY</b> EURUSD"), "BUY EURUSD")

    def test_unconfigured_channels_are_skipped(self):
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO"):
            os.environ.pop(key, None)
        self.assertFalse(notify.telegram_configured())
        self.assertFalse(notify.sms_configured())
        self.assertEqual(notify.channels(), [])
        self.assertEqual(notify.send("<b>test</b>"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
