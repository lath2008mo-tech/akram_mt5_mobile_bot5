"""
Notifications: send a trade setup to Telegram and/or SMS.

Both channels are plain HTTPS calls with urllib, so there is nothing extra to
install and no SDK that can break. If a channel is not configured it is simply
skipped — the scanner keeps working and the signal still shows up in the app.

Telegram (free, instant, best on a phone)
    1. In Telegram: talk to @BotFather -> /newbot -> copy the token
    2. Send any message to your new bot
    3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates and copy the
       "chat":{"id": ...} number
    4. .env:  TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...

SMS via Twilio (paid, costs a few cents per message)
    1. Create a Twilio account, buy/verify a sender number
    2. .env:  TWILIO_ACCOUNT_SID=...  TWILIO_AUTH_TOKEN=...
              TWILIO_FROM=+46...       SMS_TO=+46...
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("Notify")

TIMEOUT = 10


@dataclass
class ChannelResult:
    channel: str
    ok: bool
    detail: str = ""


def _post(url: str, data: bytes, headers: dict) -> tuple[bool, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            return 200 <= resp.status < 300, body[:300]
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except Exception as exc:  # network, DNS, timeout
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- Telegram

def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(text: str) -> ChannelResult:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return ChannelResult("telegram", False, "not configured")

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    ok, detail = _post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        payload,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    return ChannelResult("telegram", ok, detail)


# ---------------------------------------------------------------- SMS (Twilio)

def sms_configured() -> bool:
    return all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                                      "TWILIO_FROM", "SMS_TO"))


def send_sms(text: str) -> ChannelResult:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_FROM", "").strip()
    to = os.getenv("SMS_TO", "").strip()
    if not all([sid, token, sender, to]):
        return ChannelResult("sms", False, "not configured")

    payload = urllib.parse.urlencode({"From": sender, "To": to, "Body": text[:1500]}).encode()
    basic = base64.b64encode(f"{sid}:{token}".encode()).decode()
    ok, detail = _post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        payload,
        {"Content-Type": "application/x-www-form-urlencoded",
         "Authorization": f"Basic {basic}"},
    )
    return ChannelResult("sms", ok, detail)


# ---------------------------------------------------------------- dispatcher

def channels() -> list[str]:
    """Which channels to use, from NOTIFY_CHANNELS (default: everything set up)."""
    wanted = [c.strip().lower() for c in os.getenv("NOTIFY_CHANNELS", "").split(",") if c.strip()]
    available = []
    if telegram_configured():
        available.append("telegram")
    if sms_configured():
        available.append("sms")
    if not wanted:
        return available
    return [c for c in wanted if c in available]


def send(text: str) -> list[ChannelResult]:
    results: list[ChannelResult] = []
    for channel in channels():
        if channel == "telegram":
            results.append(send_telegram(text))
        elif channel == "sms":
            # SMS is not HTML, keep it plain and short
            results.append(send_sms(_strip_html(text)))
    for r in results:
        if r.ok:
            log.info("Sent via %s", r.channel)
        else:
            log.warning("Could not send via %s: %s", r.channel, r.detail)
    return results


def _strip_html(text: str) -> str:
    out, skip = [], False
    for ch in text:
        if ch == "<":
            skip = True
        elif ch == ">":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- formatting

def format_signal(sig: dict) -> str:
    """One message per setup: entry, SL, TP, R:R, score and why."""
    side = sig.get("side", "?")
    arrow = "🟢 BUY" if side == "BUY" else "🔴 SELL"
    rr = sig.get("rr")
    rr_txt = f"1:{rr:.1f}" if rr else "–"
    reasons = sig.get("reasons") or []
    why = " • ".join(reasons[:3])

    return (
        f"<b>{arrow} {sig.get('symbol','?')}</b>  <i>{sig.get('timeframe','')}</i>\n"
        f"Entry <b>{_fmt(sig.get('entry'))}</b>\n"
        f"SL {_fmt(sig.get('sl'))}   TP {_fmt(sig.get('tp'))}   R:R {rr_txt}\n"
        f"Setup {sig.get('score','?')}/{sig.get('max_score','7')} · {sig.get('regime','')}\n"
        f"{why}\n"
        f"<i>Signal only — you decide.</i>"
    )


def _fmt(value) -> str:
    if value is None:
        return "–"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.5f}".rstrip("0").rstrip(".")
    return f"{v:.5f}"
