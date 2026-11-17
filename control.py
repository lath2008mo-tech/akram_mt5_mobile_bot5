"""
Shared control state for the MT5 mobile bot.

Both processes (the FastAPI server in main.py and the trading engine in
mt5_scalper_bot.py) run on the same Windows machine, so they coordinate
through a tiny SQLite file. That is what makes the START / STOP /
EMERGENCY-STOP buttons on the phone actually stop the engine.

Keys written here:
    bot_running      "1" / "0"   -> engine may look for new trades
    emergency_stop   "1" / "0"   -> hard halt, ignores bot_running
    settings         JSON        -> settings saved from the phone
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

DB_PATH = os.getenv("DB_PATH", "bot_state.db")

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT, kind TEXT, symbol TEXT, side TEXT,
    price REAL, sl REAL, tp REAL, volume REAL, profit REAL, message TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    """Open the shared database (created on first use)."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        with connect() as con:
            row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except sqlite3.Error:
        return default


def set(key: str, value: Any) -> None:  # noqa: A001 - tiny helper, name kept short
    with _lock:
        with connect() as con:
            con.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            con.commit()


def get_flags() -> dict:
    return {
        "bot_running": get("bot_running", "1") == "1",
        "emergency_stop": get("emergency_stop", "0") == "1",
        "updated": get("flags_updated"),
    }


def set_flag(name: str, value: bool) -> dict:
    if name not in {"bot_running", "emergency_stop"}:
        raise ValueError(f"Unknown flag: {name}")
    set(name, "1" if value else "0")
    set("flags_updated", datetime.now(timezone.utc).isoformat())
    return get_flags()


def trading_allowed() -> bool:
    """Called by the trading engine before it looks for new entries."""
    f = get_flags()
    return f["bot_running"] and not f["emergency_stop"]


def get_settings(default: Optional[dict] = None) -> Optional[dict]:
    raw = get("settings")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def save_settings(settings: dict) -> None:
    set("settings", json.dumps(settings, sort_keys=True))


def log_event(kind: str, symbol: str = "", side: str = "", price: float = 0.0,
              sl: float = 0.0, tp: float = 0.0, volume: float = 0.0,
              profit: float = 0.0, message: str = "") -> None:
    """Append to the audit trail shown on the phone."""
    with _lock:
        with connect() as con:
            con.execute(
                "INSERT INTO events(ts,kind,symbol,side,price,sl,tp,volume,profit,message) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, symbol, side,
                 price, sl, tp, volume, profit, message),
            )
            con.commit()


def recent_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    cols = ["id", "ts", "kind", "symbol", "side", "price", "sl", "tp",
            "volume", "profit", "message"]
    with connect() as con:
        rows = con.execute(
            f"SELECT {','.join(cols)} FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


if __name__ == "__main__":  # small CLI, handy for testing
    import sys

    if len(sys.argv) == 1:
        print(json.dumps({"flags": get_flags(), "settings": get_settings()}, indent=2))
    elif sys.argv[1] in {"start", "stop", "panic", "resume"}:
        action = sys.argv[1]
        if action == "start":
            set_flag("emergency_stop", False)
            print(set_flag("bot_running", True))
        elif action == "stop":
            print(set_flag("bot_running", False))
        elif action == "panic":
            print(set_flag("emergency_stop", True))
        else:
            print(set_flag("emergency_stop", False))
    else:
        print(__doc__)
    time.sleep(0)
