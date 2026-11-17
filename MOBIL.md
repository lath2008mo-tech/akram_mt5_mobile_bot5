# Koppla mobilen till MT5-boten 📱

Kort svar på det viktigaste först:

> **MetaTrader 5-appen på mobilen kan inte köra Expert Advisors eller Python-botar.**
> Boten måste köra på en Windows-dator (eller VPS) där MT5-desktop är installerat och inloggat.
> Mobilen kan däremot **se kontot** (samma inloggning i MT5-appen) och **styra boten**
> via den lilla webb-app som finns i det här repot.

Så här ser kedjan ut:

```
 📱 Telefonen (webbläsare)          🖥️ Windows-PC / VPS                 🏦 Broker
 ─────────────────────────          ────────────────────                ────────
 Web-app på http://IP:8000  ──►  FastAPI (main.py) + SQLite  ──►  MetaTrader5-Python
 Start / Stopp / Nödstopp        mt5_scalper_bot.py (trading)     ──►  MT5-desktop (inloggad)
 Saldo, positioner, logg         MT5-desktop (Algo Trading PÅ)    ──►  Demo-/livekonto
```

Ingen APK behövs, inget GitHub-bygge behövs. Det är en mobilanpassad webbsida som
kan läggas på hemskärmen och beter sig som en app.

---

## 1. Förbered datorn

1. Windows 10/11 eller Windows VPS.
2. Installera **MetaTrader 5 desktop** och logga in på ett **DEMO-konto**.
3. Slå PÅ **Algo Trading** (grön knapp i verktygsfältet) — annars avvisas ordrar.
4. Installera Python 3.11/3.12 (64-bit) och kryssa i *Add python.exe to PATH*.

## 2. Starta servern

Dubbelklicka `run_server.bat`. Första gången skapas `.venv`, beroenden installeras,
och ett API-token genereras i filen `.api_token`.

Eller manuellt:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

Konfiguration i `.env`:

| Variabel | Betydelse |
|---|---|
| `API_TOKEN` | Lösenordet som mobilen använder. Tomt = genereras automatiskt till `.api_token` |
| `MT5_PATH` | Sökväg till `terminal64.exe` om auto-detektering misslyckas |
| `DRY_RUN` | `true` = inga riktiga ordrar (börja alltid här) |
| `SYMBOLS` | Symboler boten följer, t.ex. `EURUSD,XAUUSD` |
| `DB_PATH` | SQLite-fil för start/stopp-status, inställningar och logg |

På Linux/macOS (utan MT5) kan du starta API:t med `./serve.sh` för att testa
gränssnittet — saldo och positioner visas då som `–` eftersom MT5 saknas.

Starta sedan själva handelsmotorn i ett eget fönster:

```bat
run_bot.bat
```

Båda processerna måste köra på samma maskin — de delar `bot_state.db`, och det är
den filen som gör att **Start/Stopp/Nödstopp på mobilen faktiskt påverkar boten**.

## 3. Koppla mobilen (hemma, samma Wi-Fi)

1. Ta reda på datorns IP: `ipconfig` → *IPv4-adress*, t.ex. `192.168.1.42`.
2. Öppna i mobilens webbläsare: `http://192.168.1.42:8000`
3. Klistra in token från `.api_token` och tryck **Spara token**.
4. Lägg till på hemskärmen:
   - **iPhone/Safari:** Dela → *Lägg till på hemskärmen*
   - **Android/Chrome:** Meny → *Lägg till på startskärmen*

Tillåter Windows-brandväggen inte anslutningen: tillåt Python/port 8000 i
*Windows Defender Firewall → Tillåt en app*.

## 4. Koppla mobilen utifrån (när du inte är hemma)

Öppna **aldrig** port 8000 direkt mot internet utan HTTPS. Enklaste säkra vägarna:

| Lösning | Så gör du | Passar |
|---|---|---|
| **Tailscale** (rekommenderas) | Installera Tailscale på PC + mobil, logga in på samma konto, öppna `http://<tailscale-ip>:8000` | Privat, krypterat, ingen portöppning |
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8000` → får en `https://…trycloudflare.com`-adress | Snabbt test, HTTPS |
| **ngrok** | `ngrok http 8000` → `https://….ngrok-free.app` | Snabbt test, HTTPS |
| Egen VPS + domän | Nginx/Caddy som reverse proxy med TLS | Produktion |

Tips: sätt `CORS_ORIGINS` till din adress i stället för `*` när du är klar.

## 5. Se kontot i MT5-appen

Installera **MetaTrader 5** från App Store / Google Play och logga in med **samma
konto, server och lösenord** som på datorn. Där ser du botens trades, P/L och marginal
i realtid — och kan stänga manuellt vid behov. Själva boten syns inte i appen.

---

## Vad du kan göra från mobilen

| Knapp / vy | Vad den gör |
|---|---|
| **Starta** | Tillåter boten att leta nya setups (skriver `bot_running=1`) |
| **Stoppa** | Stoppar nya ordrar, öppna positioner lämnas orörda |
| **🚨 NÖDSTOPP** | Hård spärr — boten gör inget förrän du startar igen |
| **Positioner** | Lista med P/L och **Stäng** per position |
| **Stäng alla** | Kräver att du skriver `CLOSE ALL` |
| **Signaler** | Senaste EMA/RSI/ATR-läget per symbol |
| **Logg** | Allt som hänt: start/stopp, ordrar, stängningar |
| **Inställningar** | Symboler, tidsram, risk, spread, cooldown, DRY RUN / LIVE |

## API (om du vill bygga egen app senare)

Alla endpoints kräver `X-API-Token`-header (eller `?token=`).

| Metod | Väg | Beskrivning |
|---|---|---|
| GET | `/health` | Publik hälsokoll |
| GET | `/status` | Saldo, equity, P/L, antal positioner, läge |
| GET | `/positions` | Öppna positioner |
| POST | `/positions/{ticket}/close` | Stäng en position |
| POST | `/positions/close-all` | Stäng alla (`{"confirm":"CLOSE ALL"}`) |
| POST | `/bot/start`, `/bot/stop`, `/bot/emergency-stop` | Styrning |
| GET/PUT | `/settings` | Läs/spara inställningar |
| GET | `/signals` | Indikator-/signalöversikt |
| GET | `/history?limit=50` | Logg |
| POST | `/paper-trade` | Demo-order till loggen |

Interaktiv dokumentation: `http://<server>:8000/docs`

---

## Felsökning

| Symptom | Åtgärd |
|---|---|
| `mt5_connected: false` / grå prick | MT5-desktop är inte startat eller inte inloggat på servern |
| `401 Invalid API token` | Token i mobilen ≠ `.api_token` / `API_TOKEN`. Logga ut i appen och klistra in igen |
| Ordrar avvisas (`retcode 10027` m.fl.) | Algo Trading avstängt i MT5, eller för liten volym för symbolen |
| Sidan laddar inte på mobilen | Fel IP, brandvägg, eller servern startad på `127.0.0.1` i stället för `0.0.0.0` |
| Symbolen hittas inte | Många brokers har suffix, t.ex. `EURUSD.m` eller `EURUSDm` — använd exakt det namn som syns i MT5 Market Watch |
| Ingenting händer efter Start | `run_bot.bat` körs inte, eller spread/ATR-filter stoppar alla trades (syns i loggen) |

## Strategin och risken

Boten använder nu fyra setups, ADX-regimfilter, sessions- och spreadfilter samt
R-baserad hantering (delvis vinst, break-even, trailing, circuit breaker).
Allt är mätbart med `python backtest.py --symbol EURUSD --tf 5 --bars 50000`
och förklaras i [RESEARCH.md](RESEARCH.md).

## Få signaler i Telegram eller SMS

Boten kan skanna hela marknaden och skicka **entry, SL, TP och R:R** till dig.

**Telegram (gratis, rekommenderas):**
1. Öppna Telegram, sök **@BotFather**, skriv `/newbot`, följ instruktionerna → du får en token.
2. Skicka ett meddelande till din nya bot.
3. Öppna `https://api.telegram.org/bot<DIN_TOKEN>/getUpdates` i webbläsaren och kopiera siffran efter `"chat":{"id":`.
4. Lägg in i `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```
5. Starta `run_scanner.bat` — den skannar var 5:e minut och skickar nya setups.
6. Testa i mobilen: fliken **Scanner** → **Testa notiser**.

**SMS (Twilio, kostar några ören per meddelande):** skapa konto på twilio.com,
kopiera `Account SID` och `Auth Token`, fyll i `TWILIO_FROM` och `SMS_TO` i `.env`.

Varje setup skickas **en gång** per symbol/tidsram/riktning/ljus — ingen spam.
Boten öppnar inga trades själv i det här läget.

## Säkerhet & ansvar

- Kör **DEMO** tills flödet fungerar. Byt till LIVE först när du förstår alla knappar.
- Dela aldrig `.api_token`, `.env` eller kontouppgifter.
- Sätt en **kill switch**-rutin: NÖDSTOPP + stäng MT5-terminalen.
- Ingen strategi garanterar vinst. Slippage, spread, nyheter och avbrott kan ge förluster.

---

## Alternativ på GitHub (om du hellre vill bygga något färdigt)

| Repo | Vad det är | Varför/varför inte |
|---|---|---|
| [dceoy/mt5api](https://github.com/dceoy/mt5api) | FastAPI-REST-API ovanpå MT5 (Windows) | Bra om du vill ha ett generellt API, inte en färdig mobil-UI |
| [Fangteng-developer/metatrader5-pro](https://github.com/Fangteng-developer/metatrader5-pro) | Android-app (Kotlin/Compose) + FastAPI-brygga | Riktig APK, men du måste bygga den i Android Studio |
| [huy-bui-tech/mt5_api](https://github.com/huy-bui-tech/mt5_api) | Socket-brygga Python (Linux) ↔ MT5-EA (Windows) | Om boten ska köra på Linux i stället för Windows |
| [zolacdo/trader-Pilot](https://github.com/zolacdo/trader-Pilot) | Flutter-app som fjärrkontroll till en brygga | Kräver Flutter SDK + Firebase |
| [Shifrozy/Telegram-to-mt5-bridge](https://github.com/Shifrozy/Telegram-to-mt5-bridge) | Styr MT5 via Telegram-kommandon | Enklast av allt, men mindre överskådligt än en web-app |

Slutsats: att bygga en egen APK är onödigt arbete för det här syftet — den här
web-appen ger samma sak, fungerar på iPhone och Android, och uppdateras direkt när
du ändrar i `web/index.html`.
