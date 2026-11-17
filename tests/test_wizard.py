"""Run with:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test_wizard.db"))

import setup_wizard  # noqa: E402


class EnvReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self._original = setup_wizard.ENV_PATH
        setup_wizard.ENV_PATH = self.env
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(key, None)

    def tearDown(self):
        setup_wizard.ENV_PATH = self._original

    def test_missing_file_is_empty(self):
        self.assertEqual(setup_wizard._read_env_file(self.env), {})

    def test_parses_keys_and_skips_comments(self):
        self.env.write_text("# c\nA=1\n\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
        values = setup_wizard._read_env_file(self.env)
        self.assertEqual(values["A"], "1")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "42")

    def test_existing_values_come_from_the_file(self):
        self.env.write_text("TELEGRAM_BOT_TOKEN=123:" + "A" * 35 + "\n"
                            "TELEGRAM_CHAT_ID=987\n", encoding="utf-8")
        self.assertTrue(setup_wizard.existing_token().startswith("123:"))
        self.assertEqual(setup_wizard.existing_chat_id(), "987")

    def test_save_writes_both_values_and_keeps_other_lines(self):
        self.env.write_text("DRY_RUN=true\n", encoding="utf-8")
        setup_wizard.save("123:" + "A" * 35, "555")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("DRY_RUN=true", text)
        self.assertIn("TELEGRAM_CHAT_ID=555", text)
        self.assertIn("NOTIFY_CHANNELS=telegram", text)

    def test_save_without_chat_id_leaves_it_out(self):
        setup_wizard.save("123:" + "A" * 35, "")
        self.assertNotIn("TELEGRAM_CHAT_ID", self.env.read_text(encoding="utf-8"))

    def test_check_returns_failure_when_unconfigured(self):
        sys.argv = ["setup_wizard.py", "--check"]
        self.assertEqual(setup_wizard.main(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
