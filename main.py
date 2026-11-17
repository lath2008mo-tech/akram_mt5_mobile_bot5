"""
MT5 Mobile Bot API — remote control for the MT5 scalper bot from a phone.

Runs on the Windows machine/VPS where MetaTrader 5 is installed and logged in.
The phone only needs a browser (the built-in web app at "/"), no Android app,
no APK, no GitHub build.

    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Security model: every data/trading endpoint needs the API token (sent as
`X-API-Token` header, `Authorization: Bearer ...`, or `?token=...`).
The HTML shell itself is public, it contains no data.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import control
import notify
import scanner
import strategy as strat

load_dotenv()

# MetaTrader5 only exists on Windows. Importing it lazily keeps the API usable
# (status / settings / paper trades) on Linux or macOS for testing.
try:  # pragma: no cover - platform dependent
    import MetaTrader5 as mt5

    MT5_IMPORTED = True
except Exception:  # pragma: no cover
    mt5 = None
    MT5_IMPORTED = False

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

# Read-only access without a token, only where there is no MT5 at all
# (Linux/macOS preview + UI development). Never True on a Windows trading box.
PUBLIC_READ_ONLY = (not MT5_IMPORTED) and os.getenv(
    "PUBLIC_READ_ONLY", "true"
).strip().lower() in {"1", "true", "yes"}

DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD"]


def _load_token() -> str:
    """Token from the environment, otherwise generate one once and keep it."""
    token = (os.getenv("API_TOKEN") or "").strip()
    if token and token.lower() not in {"change-me", "changeme", "replace-with-a-long-random-token"}:
        return token

    token_file = BASE_DIR / ".api_token"
    if token_file.exists():
        saved = token_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    new_token = secrets.token_urlsafe(24)
    token_file.write_text(new_token, encoding="utf-8")
    print(f"[setup] API_TOKEN was missing, generated one and stored it in {token_file}")
    return new_token


API_TOKEN = _load_token()

app = FastAPI(title="Akram MT5 Mobile Bot API", version="2.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in (os.getenv("CORS_ORIGINS") or "*").split(",") if o],
    allow_methods=["*"],
    allow_headers=["*"],
)

_settings_lock = threading.Lock()
_mt5_lock = threading.Lock()  # the MetaTrader5 package is not thread-safe


class BotSettings(BaseModel):
    symbols: list[str] = DEFAULT_SYMBOLS
    timeframe: int = Field(5, description="1,2,3,4,5,6,10,12,15,20,30")
    risk_per_trade: float = Field(0.0025, gt=0, le=0.05)
    max_daily_loss: float = Field(0.02, gt=0, le=0.20)
    max_open_positions: int = Field(3, ge=1, le=50)
    max_spread_points: float = Field(25, gt=0)
    cooldown_minutes: int = Field(10, ge=0)
    atr_sl_mult: float = Field(1.5, gt=0)
    atr_tp_mult: float = Field(2.0, gt=0)
    dry_run: bool = True


ALLOWED_TIMEFRAMES = {1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30}


def current_settings() -> BotSettings:
    saved = control.get_settings()
    if not saved:
        return BotSettings()
    try:
        return BotSettings(**saved)
    except Exception:
        return BotSettings()


# ---------------------------------------------------------------- auth

def _supplied_token(x_api_token, authorization, token) -> Optional[str]:
    supplied = x_api_token or token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return supplied


def auth(
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None, description="Fallback for simple mobile clients"),
) -> None:
    supplied = _supplied_token(x_api_token, authorization, token)
    if not supplied or not API_TOKEN:
        raise HTTPException(401, "Missing API token")
    if not hmac.compare_digest(supplied, API_TOKEN):
        raise HTTPException(401, "Invalid API token")


def auth_read(
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    """Auth for read endpoints.

    Without a MetaTrader5 install (Linux/macOS preview, CI, UI development)
    there is no account data to protect, so reads are open and the settings
    screen can be inspected. On Windows PUBLIC_READ_ONLY is False and this is
    exactly as strict as `auth`.
    """
    if PUBLIC_READ_ONLY:
        supplied = _supplied_token(x_api_token, authorization, token)
        if supplied:
            auth(x_api_token, authorization, token)  # a wrong token still fails
        return
    auth(x_api_token, authorization, token)


# ---------------------------------------------------------------- MT5 helpers

def mt5_available() -> bool:
    if not MT5_IMPORTED or mt5 is None:
        return False
    try:
        return bool(mt5.terminal_info())
    except Exception:
        return False


def account_info():
    if not MT5_IMPORTED:
        return None
    try:
        with _mt5_lock:
            return mt5.account_info()
    except Exception:
        return None


def positions_raw():
    if not MT5_IMPORTED:
        return ()
    try:
        with _mt5_lock:
            return mt5.positions_get() or ()
    except Exception:
        return ()


def position_rows() -> list[dict]:
    rows = []
    for p in positions_raw():
        rows.append({
            "ticket": int(p.ticket),
            "symbol": p.symbol,
            "type": int(p.type),
            "side": "BUY" if int(p.type) == 0 else "SELL",
            "volume": float(p.volume),
            "price_open": float(p.price_open),
            "sl": float(p.sl),
            "tp": float(p.tp),
            "profit": float(getattr(p, "profit", 0.0)),
            "comment": getattr(p, "comment", ""),
        })
    return rows


def _close_position(ticket: int) -> dict:
    if not mt5_available():
        raise HTTPException(503, "MT5 is not connected on the server")
    with _mt5_lock:
        positions = mt5.positions_get(ticket=ticket) or ()
        if not positions:
            raise HTTPException(404, "Position not found")
        p = positions[0]
        tick = mt5.symbol_info_tick(p.symbol)
        info = mt5.symbol_info(p.symbol)
        if tick is None or info is None:
            raise HTTPException(502, f"No market data for {p.symbol}")
        is_buy = int(p.type) == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": int(p.ticket),
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": 20,
            "magic": int(getattr(p, "magic", 0)),
            "comment": "close-from-mobile",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
    if result is None:
        raise HTTPException(502, f"order_send failed: {mt5.last_error()}")
    ok = result.retcode == mt5.TRADE_RETCODE_DONE
    control.log_event(
        "CLOSE" if ok else "CLOSE_FAILED", p.symbol, "SELL" if is_buy else "BUY",
        price=float(getattr(result, "price", 0.0)), volume=float(p.volume),
        message=f"ticket={ticket} retcode={result.retcode} {result.comment}",
    )
    return {"ok": ok, "ticket": ticket, "retcode": int(result.retcode), "comment": result.comment}


# ---------------------------------------------------------------- public routes

@app.get("/health")
def health():
    return {
        "ok": True,
        "mt5_imported": MT5_IMPORTED,
        "mt5_connected": mt5_available(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/ping", include_in_schema=False)
def ping():
    """Plain-text check: if this opens, the server and preview tunnel work."""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(f"OK - MT5 Mobile Bot API is running. public_read_only={PUBLIC_READ_ONLY}")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse({
        "name": "MT5 Mobile Bot",
        "short_name": "MT5 Bot",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b1220",
        "theme_color": "#0b1220",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    })


@app.get("/icon.svg", include_in_schema=False)
def icon():
    return FileResponse(WEB_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # browsers ask for this automatically; serve the SVG instead of a 404
    return FileResponse(WEB_DIR / "icon.svg", media_type="image/svg+xml")


# ---------------------------------------------------------------- account / status

@app.get("/status")
def status(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
           authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    info = account_info()
    rows = position_rows()
    flags = control.get_flags()
    settings = current_settings()
    return {
        "running": flags["bot_running"],
        "emergency_stop": flags["emergency_stop"],
        "dry_run": settings.dry_run,
        "balance": float(info.balance) if info else None,
        "equity": float(info.equity) if info else None,
        "margin_free": float(info.margin_free) if info else None,
        "currency": info.currency if info else None,
        "login": int(info.login) if info else None,
        "server": info.server if info else None,
        "profit": float(sum(p["profit"] for p in rows)),
        "open_positions": len(rows),
        "mt5_connected": mt5_available(),
        "public_read_only": PUBLIC_READ_ONLY,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/positions")
def positions(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
              authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    return position_rows()


@app.post("/positions/{ticket}/close")
def close_position(ticket: int, x_api_token: Optional[str] = Header(default=None),
                   token: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    return _close_position(ticket)


class CloseAllRequest(BaseModel):
    confirm: str = Field(..., description="Must be exactly CLOSE ALL")


@app.post("/positions/close-all")
def close_all(body: CloseAllRequest, x_api_token: Optional[str] = Header(default=None),
              token: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    if body.confirm.strip() != "CLOSE ALL":
        raise HTTPException(400, "Confirmation text must be 'CLOSE ALL'")
    results = []
    for row in position_rows():
        try:
            results.append(_close_position(row["ticket"]))
        except HTTPException as exc:
            results.append({"ok": False, "ticket": row["ticket"], "error": exc.detail})
    return {"results": results}


# ---------------------------------------------------------------- bot control

@app.post("/bot/start")
def start(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
          authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    control.set_flag("emergency_stop", False)
    flags = control.set_flag("bot_running", True)
    control.log_event("START", message="Bot started from phone")
    return {"running": flags["bot_running"], "emergency_stop": flags["emergency_stop"],
            "message": "Bot monitoring started"}


@app.post("/bot/stop")
def stop(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
         authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    flags = control.set_flag("bot_running", False)
    control.log_event("STOP", message="Bot stopped from phone; open positions untouched")
    return {"running": flags["bot_running"], "emergency_stop": flags["emergency_stop"],
            "message": "Bot stopped; existing positions are not closed"}


@app.post("/bot/emergency-stop")
def emergency_stop(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
                   authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    control.set_flag("bot_running", False)
    flags = control.set_flag("emergency_stop", True)
    control.log_event("EMERGENCY_STOP", message="Emergency stop from phone")
    return {"running": flags["bot_running"], "emergency_stop": flags["emergency_stop"],
            "message": "Emergency stop active. No new orders will be placed."}


# ---------------------------------------------------------------- settings / data

@app.get("/settings")
def get_settings(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
                 authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    return current_settings().model_dump()


@app.put("/settings")
def update_settings(body: BotSettings, x_api_token: Optional[str] = Header(default=None),
                    token: Optional[str] = None,
                    authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    if body.timeframe not in ALLOWED_TIMEFRAMES:
        raise HTTPException(400, "Unsupported timeframe")
    with _settings_lock:
        control.save_settings(body.model_dump())
    control.log_event("SETTINGS", message="Settings updated from phone")
    return body.model_dump()


@app.get("/signals")
def signals(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
            authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    settings = current_settings()
    try:
        import mt5_scalper_bot as bot
    except Exception as exc:
        return {"error": f"Strategy module unavailable: {exc}", "signals": []}

    out = []
    with _settings_lock:
        old_tf = bot.CFG.timeframe
        try:
            bot.CFG.timeframe = settings.timeframe
            for symbol in settings.symbols:
                try:
                    df = bot.get_rates(symbol)
                    if df is None:
                        continue
                    df = bot.add_indicators(df)
                    x = df.iloc[-1]
                    out.append({
                        "symbol": symbol,
                        "signal": bot.signal(df) or "NONE",
                        "rsi": float(x.rsi),
                        "atr": float(x.atr),
                        "ema_fast": float(x.ema_fast),
                        "ema_slow": float(x.ema_slow),
                        "ema_trend": float(x.ema_trend),
                        "close": float(x.close),
                        "time": str(x.time),
                    })
                except Exception as exc:
                    out.append({"symbol": symbol, "error": str(exc), "signal": "ERROR"})
        finally:
            bot.CFG.timeframe = old_tf
    return out


@app.get("/history")
def history(limit: int = 50, x_api_token: Optional[str] = Header(default=None),
            token: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    return control.recent_events(limit)


class PaperTrade(BaseModel):
    symbol: str
    side: str
    price: float
    sl: float
    tp: float
    volume: float


@app.get("/scan")
def scan_market(min_score: int = 5, timeframes: str = "5,15,60",
                symbols: Optional[str] = None, push: bool = False,
                x_api_token: Optional[str] = Header(default=None),
                token: Optional[str] = None,
                authorization: Optional[str] = Header(default=None)):
    """Scan the market and return setups with entry / SL / TP, best first."""
    auth_read(x_api_token, authorization, token)

    if not mt5_available():
        stored = control.get("scan_last")
        return {
            "mt5": False,
            "signals": json.loads(stored) if stored else [],
            "note": "MetaTrader 5 is not connected on this server; showing the last scan.",
            "updated": control.get("scan_updated"),
        }

    try:
        tf_list = [int(x) for x in timeframes.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "timeframes must look like 5,15,60")

    provider = scanner.MT5Provider(mt5)
    provider.start()
    symbol_list = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
                   if symbols else provider.symbols())

    with _mt5_lock:
        found = scanner.scan(provider, symbol_list, tf_list,
                             strat.StrategyParams(), min_score=min_score)
        fresh = scanner.unseen(found) if push else []

    for sig in fresh:
        notify.send(notify.format_signal(sig.__dict__ | {"max_score": sig.max_score}))
        control.log_event("SCANNER", sig.symbol, sig.side, sig.entry, sig.sl, sig.tp,
                          message=f"{sig.timeframe} score {sig.score}/{sig.max_score} "
                                  f"{sig.regime} | {'; '.join(sig.reasons[:3])}")

    payload = [sig.__dict__ for sig in found]
    if payload:
        control.set("scan_last", json.dumps(payload[:50]))
    control.set("scan_updated", datetime.now(timezone.utc).isoformat())

    return {"mt5": True, "symbols": len(symbol_list),
            "timeframes": tf_list, "min_score": min_score,
            "signals": payload, "pushed": len(fresh)}


# ---------------------------------------------------------------- scanner control

_scanner_proc: Optional[subprocess.Popen] = None
SCANNER_LOG = BASE_DIR / "scanner.log"


def _scanner_running() -> bool:
    global _scanner_proc
    if _scanner_proc is not None and _scanner_proc.poll() is None:
        return True
    pid = control.get("scanner_pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            pass
    return False


def _log_tail(lines: int = 15) -> list[str]:
    try:
        data = SCANNER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return data[-lines:]
    except Exception:
        return []


class ScanStart(BaseModel):
    loop_seconds: int = Field(300, ge=30, le=3600)
    min_score: int = Field(6, ge=2, le=9)
    timeframes: str = "5,15,60"
    notify: bool = True


@app.get("/scanner/status")
def scanner_status(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
                   authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    return {"running": _scanner_running(), "pid": control.get("scanner_pid"),
            "channels": notify.channels(), "log": _log_tail()}


@app.post("/scanner/start")
def scanner_start(body: ScanStart, x_api_token: Optional[str] = Header(default=None),
                  token: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None)):
    global _scanner_proc
    auth(x_api_token, authorization, token)

    if not MT5_IMPORTED:
        raise HTTPException(
            503,
            "MetaTrader 5 finns inte på den här maskinen. Starta skannern på "
            "Windows-datorn där MT5 är inloggat.",
        )
    if _scanner_running():
        return {"running": True, "pid": control.get("scanner_pid"), "message": "Redan igång"}

    cmd = [sys.executable, str(BASE_DIR / "scanner.py"),
           "--loop", str(body.loop_seconds),
           "--min-score", str(body.min_score),
           "--timeframes", body.timeframes]
    if body.notify:
        cmd.append("--notify")

    try:
        log_file = open(SCANNER_LOG, "ab")
        _scanner_proc = subprocess.Popen(cmd, cwd=str(BASE_DIR),
                                         stdout=log_file, stderr=subprocess.STDOUT)
    except Exception as exc:
        raise HTTPException(500, f"Kunde inte starta skannern: {exc}")

    control.set("scanner_pid", _scanner_proc.pid)
    control.log_event("SCANNER_START", message=(
        f"loop={body.loop_seconds}s min_score={body.min_score} "
        f"tf={body.timeframes} notify={body.notify}"))
    return {"running": True, "pid": _scanner_proc.pid,
            "message": f"Skannern kör var {body.loop_seconds}:e sekund"}


@app.post("/scanner/stop")
def scanner_stop(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
                 authorization: Optional[str] = Header(default=None)):
    global _scanner_proc
    auth(x_api_token, authorization, token)

    stopped = False
    if _scanner_proc is not None and _scanner_proc.poll() is None:
        _scanner_proc.terminate()
        try:
            _scanner_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _scanner_proc.kill()
        stopped = True
        _scanner_proc = None

    pid = control.get("scanner_pid")
    if pid and not stopped:
        try:
            os.kill(int(pid), 15)
            stopped = True
        except OSError:
            pass

    control.set("scanner_pid", "")
    control.log_event("SCANNER_STOP", message="Skanner stoppad från mobilen")
    return {"running": False, "stopped": stopped}


# ---------------------------------------------------------------- channel setup

class NotifyConfig(BaseModel):
    token: str = Field(..., description="Telegram bot token from @BotFather")
    chat_id: Optional[str] = Field(None, description="Leave empty to detect it automatically")
    channel: str = Field("telegram", description="telegram or sms")


@app.get("/notify/status")
def notify_status(x_api_token: Optional[str] = Header(default=None), token: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None)):
    auth_read(x_api_token, authorization, token)
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return {
        "channels": notify.channels(),
        "telegram": {
            "configured": notify.telegram_configured(),
            "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
            "token_preview": (tg_token[:8] + "…" + tg_token[-4:]) if tg_token else "",
        },
        "sms": {"configured": notify.sms_configured()},
    }


@app.post("/notify/configure")
def notify_configure(body: NotifyConfig, x_api_token: Optional[str] = Header(default=None),
                     token: Optional[str] = None,
                     authorization: Optional[str] = Header(default=None)):
    """Save the Telegram token from the phone and figure out the chat id.

    One paste + one tap, no file editing and no restart.
    """
    auth(x_api_token, authorization, token)

    tg_token = notify.normalize_token(body.token)
    if not notify.token_looks_valid(tg_token):
        raise HTTPException(400, "Det ser inte ut som en Telegram-token "
                                 "(ska vara siffror:lång sträng från @BotFather).")

    chat_id = notify.normalize_token(body.chat_id or "")
    detected = ""
    if not chat_id:
        found, message = notify.detect_chat_id(tg_token)
        detected = message
        if found is None:
            # keep the token anyway so the user only has to press again
            notify.upsert_env(BASE_DIR / ".env", {"TELEGRAM_BOT_TOKEN": tg_token})
            notify.apply_to_process({"TELEGRAM_BOT_TOKEN": tg_token})
            return {"ok": False, "saved_token": True, "chat_id": None,
                    "message": message}
        chat_id = str(found)

    path = notify.upsert_env(BASE_DIR / ".env", {
        "TELEGRAM_BOT_TOKEN": tg_token,
        "TELEGRAM_CHAT_ID": chat_id,
        "NOTIFY_CHANNELS": "telegram",
    })
    notify.apply_to_process({
        "TELEGRAM_BOT_TOKEN": tg_token,
        "TELEGRAM_CHAT_ID": chat_id,
        "NOTIFY_CHANNELS": "telegram",
    })

    test = notify.send("✅ MT5-boten är ansluten. Nu kommer signalerna hit.")
    ok = all(r.ok for r in test) if test else False
    control.log_event("NOTIFY_SETUP", message=f"Telegram chat {chat_id} konfigurerad")
    return {"ok": ok, "chat_id": chat_id, "detected": detected, "env": path,
            "message": ("Klart! Testmeddelande skickat." if ok else
                        "Sparat, men testmeddelandet gick inte fram. Kolla chat id."),
            "results": [{"channel": r.channel, "ok": r.ok, "detail": r.detail} for r in test]}


@app.post("/notify/test")
def notify_test(x_api_token: Optional[str] = Header(default=None),
                token: Optional[str] = None,
                authorization: Optional[str] = Header(default=None)):
    """Send a test message to every configured channel (Telegram / SMS)."""
    auth(x_api_token, authorization, token)
    configured = notify.channels()
    if not configured:
        return {"ok": False, "channels": [],
                "message": "No channel configured. Set TELEGRAM_BOT_TOKEN + "
                           "TELEGRAM_CHAT_ID, or the TWILIO_* + SMS_TO variables."}
    results = notify.send("✅ Test från MT5-boten: notiser fungerar.")
    return {"ok": all(r.ok for r in results), "channels": configured,
            "results": [{"channel": r.channel, "ok": r.ok, "detail": r.detail}
                        for r in results]}


@app.post("/paper-trade")
def paper_trade(body: PaperTrade, x_api_token: Optional[str] = Header(default=None),
                token: Optional[str] = None,
                authorization: Optional[str] = Header(default=None)):
    auth(x_api_token, authorization, token)
    if body.side.upper() not in {"BUY", "SELL"}:
        raise HTTPException(400, "side must be BUY or SELL")
    control.log_event("PAPER", body.symbol, body.side.upper(), body.price, body.sl,
                      body.tp, body.volume, message="Demo/paper trade")
    return {"ok": True}
