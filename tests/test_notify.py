"""Run with:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test_notify.db"))

import notify  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_accepts_a_real_looking_token(self):
        self.assertTrue(notify.token_looks_valid("8677475385:" + "A" * 35))

    def test_rejects_junk(self):
        for bad in ("", "hej", "1234", "abc:def", "123:" + "A" * 5):
            self.assertFalse(notify.token_looks_valid(bad), bad)

    def test_normalize_strips_chat_escaping_and_spaces(self):
        self.assertEqual(notify.normalize_token(" 8677475385:AAG9\\_lcG7 "), "8677475385:AAG9_lcG7")
        self.assertEqual(notify.normalize_token('"123:abc"'), "123:abc")


class ChatIdTests(unittest.TestCase):
    def test_parses_private_chat(self):
        payload = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 123456789, "first_name": "Akram",
                                                  "type": "private"}}},
        ]}
        chats = notify.parse_chat_ids(payload)
        self.assertEqual(chats[0]["chat_id"], 123456789)
        self.assertEqual(chats[0]["name"], "Akram")

    def test_deduplicates_and_handles_empty(self):
        payload = {"ok": True, "result": [
            {"message": {"chat": {"id": 42, "type": "private"}}},
            {"message": {"chat": {"id": 42, "type": "private"}}},
            {"edited_message": {"chat": {"id": 99, "title": "grupp", "type": "group"}}},
        ]}
        chats = notify.parse_chat_ids(payload)
        self.assertEqual([c["chat_id"] for c in chats], [42, 99])
        self.assertEqual(notify.parse_chat_ids({"ok": True, "result": []}), [])

    def test_survives_weird_updates(self):
        self.assertEqual(notify.parse_chat_ids({"result": [{"foo": "bar"}, None]}), [])


class EnvFileTests(unittest.TestCase):
    def test_updates_existing_lines_and_keeps_the_rest(self):
        path = os.path.join(tempfile.mkdtemp(), ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# comment\nDRY_RUN=true\nTELEGRAM_BOT_TOKEN=old\n")
        notify.upsert_env(path, {"TELEGRAM_BOT_TOKEN": "new", "TELEGRAM_CHAT_ID": "42"})
        text = open(path, encoding="utf-8").read()
        self.assertIn("# comment", text)
        self.assertIn("DRY_RUN=true", text)
        self.assertIn("TELEGRAM_BOT_TOKEN=new", text)
        self.assertIn("TELEGRAM_CHAT_ID=42", text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=old", text)
        self.assertEqual(text.count("TELEGRAM_BOT_TOKEN="), 1)

    def test_creates_file_when_missing(self):
        path = os.path.join(tempfile.mkdtemp(), ".env")
        notify.upsert_env(path, {"TELEGRAM_CHAT_ID": "7"})
        self.assertEqual(open(path, encoding="utf-8").read().strip(), "TELEGRAM_CHAT_ID=7")

    def test_apply_to_process_sets_env(self):
        notify.apply_to_process({"TELEGRAM_CHAT_ID": "555"})
        self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "555")


if __name__ == "__main__":
    unittest.main(verbosity=2)
