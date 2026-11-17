"""
Ett-stegs-uppstart: klistra in Telegram-token EN gång, sedan sköter den allt.

    start_bot.bat                 (dubbelklicka på Windows)
    python setup_wizard.py        (samma sak manuellt)

Vad den gör:
1. läser befintlig token från miljön eller .env
2. frågar bara om den saknas
3. hämtar chat id automatiskt från Telegram
4. sparar i .env (övriga rader orörda)
5. skickar ett testmeddelande
6. startar skannern som skickar signalerna

Flaggor för den som vill slippa frågor helt:
    python setup_wizard.py --token 123:ABC... --loop 300 --min-score 6
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import notify

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _ask(prompt: str) -> str:
    """input() that never explodes when there is no terminal (batched/Task Scheduler)."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _read_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def existing_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or _read_env_file(ENV_PATH)
            .get("TELEGRAM_BOT_TOKEN") or "").strip()


def existing_chat_id() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID") or _read_env_file(ENV_PATH)
            .get("TELEGRAM_CHAT_ID") or "").strip()


def ask_token(current: str = "") -> str:
    if current and notify.token_looks_valid(current):
        print(f"  Token hittad: {current[:8]}…{current[-4:]}")
        answer = _ask("  Använd den? [J/n] ").strip().lower()
        if answer in ("", "j", "ja", "y", "yes"):
            return current

    print()
    print("  Klistra in din Telegram-token från @BotFather och tryck Enter.")
    while True:
        raw = _ask("  Token: ").strip()
        token = notify.normalize_token(raw)
        if notify.token_looks_valid(token):
            return token
        print("  Det där ser inte ut som en token (format: 123456789:AAF…). Försök igen.")


def ask_chat_id(token: str) -> str:
    current = existing_chat_id()
    if current:
        print(f"  Chat id hittat: {current}")
        return current

    print()
    print("  Letar efter din chatt i Telegram…")
    found, message = notify.detect_chat_id(token)
    if found:
        print(f"  ✓ {message}")
        return str(found)

    print(f"  {message}")
    print("  Öppna Telegram, skicka 'hej' till din bot, tryck Enter här.")
    _ask("")
    found, message = notify.detect_chat_id(token)
    if found:
        print(f"  ✓ {message}")
        return str(found)

    print("  Hittar fortfarande ingen chatt.")
    manual = _ask("  Klistra in chat id själv (eller Enter för att hoppa över): ").strip()
    return manual


def save(token: str, chat_id: str) -> None:
    values = {"TELEGRAM_BOT_TOKEN": token, "NOTIFY_CHANNELS": "telegram"}
    if chat_id:
        values["TELEGRAM_CHAT_ID"] = chat_id
    notify.upsert_env(ENV_PATH, values)
    notify.apply_to_process(values)
    print(f"  ✓ Sparat i {ENV_PATH}")


def send_test() -> bool:
    results = notify.send("✅ Klar! Nu kommer mina signaler hit.")
    for r in results:
        print(f"  {'✓' if r.ok else '✗'} {r.channel}: {r.detail or 'skickat'}")
    return bool(results) and all(r.ok for r in results)


def start_scanner(loop_seconds: int, min_score: int, timeframes: str) -> int:
    cmd = [sys.executable, str(BASE_DIR / "scanner.py"),
           "--loop", str(loop_seconds),
           "--min-score", str(min_score),
           "--timeframes", timeframes,
           "--notify"]
    print()
    print("  Startar skannern. Låt fönstret vara öppet — Ctrl+C stoppar.")
    print("  " + " ".join(cmd))
    print()
    try:
        return subprocess.call(cmd, cwd=str(BASE_DIR))
    except KeyboardInterrupt:
        print("\n  Stoppad.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sätt upp Telegram-signaler och starta skannern")
    ap.add_argument("--token", help="Bot-token (annars frågas du)")
    ap.add_argument("--chat-id", help="Chat id (annars hittas det automatiskt)")
    ap.add_argument("--loop", type=int, default=300, help="Sekunder mellan skanningar")
    ap.add_argument("--min-score", type=int, default=6, help="Minsta poäng för en signal")
    ap.add_argument("--timeframes", default="5,15,60")
    ap.add_argument("--no-start", action="store_true", help="Spara bara, starta inte skannern")
    ap.add_argument("--check", action="store_true", help="Visa bara nuvarande inställning")
    args = ap.parse_args()

    print()
    print("  ==============================================")
    print("   MT5-boten — Telegram-signaler till mobilen")
    print("  ==============================================")

    if args.check:
        token, chat = existing_token(), existing_chat_id()
        print(f"  Token:   {'ja (' + token[:8] + '…)' if token else 'nej'}")
        print(f"  Chat id: {chat or 'nej'}")
        print(f"  Kanaler: {notify.channels() or 'inga'}")
        return 0 if (token and chat) else 1

    token = notify.normalize_token(args.token) if args.token else ""
    if not notify.token_looks_valid(token):
        token = ask_token(existing_token())

    chat_id = (args.chat_id or "").strip() or ask_chat_id(token)
    save(token, chat_id)

    ok = send_test()
    if not ok:
        print()
        print("  Testmeddelandet gick inte fram. Vanligaste orsaker:")
        print("   • du har inte skickat 'hej' till boten än")
        print("   • token är återkallad hos @BotFather")
        print("   • chat id:t tillhör en annan chatt")
        answer = _ask("\n  Försök igen med ny token? [j/N] ").strip().lower()
        if answer in ("j", "ja", "y"):
            return main()

    if args.no_start:
        print("\n  Klart. Starta skannern med: python scanner.py --loop 300 --notify")
        return 0

    return start_scanner(args.loop, args.min_score, args.timeframes)


if __name__ == "__main__":
    sys.exit(main())
