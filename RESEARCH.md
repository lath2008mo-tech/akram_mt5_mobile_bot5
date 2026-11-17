# Vad boten gör nu — och vad den inte kan göra

## Ärligt först

Ingen bot kan leverera "hög vinst och minimal förlust". Det finns ingen strategi
som gör det, och de som påstår det säljer något. Det som **går** att bygga är:

- att bara ta trades där flera oberoende filter säger samma sak
- att varje trade riskerar en liten, känd summa (0,25 % av kontot)
- att förluster begränsas automatiskt (daglig gräns, circuit breaker, stopp som bara dras åt)
- att vinster får löpa (delvis vinst vid +1R, break-even, trailing)
- att allt går att **mäta** i backtest innan pengar står på spel

Det är så professionella deskar jobbar: expectancy och drawdown, inte "träffsäkerhet".

---

## Det som är implementerat

### 1. Fyra setups bakom ett beslut (`strategy.py`)

| Setup | Regel | Används i |
|---|---|---|
| Trendföljd | EMA 9 > 21 > 50 och pris över EMA 50 | trendmarknad |
| Breakout | stängning utanför 20-bars högsta/lägsta | trendmarknad |
| Pullback | pris återvänder till EMA 21 och studsar (inom 0,35 ATR) | trendmarknad |
| Reversion | RSI < 28 vid 20-bars botten / RSI > 72 vid toppen | sidledes marknad |

Varje delfilter ger poäng. Boten tar bara trades som når `MIN_SCORE` (default 4).

### 2. Regimfilter — den viktigaste nyheten

ADX (Wilder-smoothad) avgör om marknaden trendar eller går sidledes:

- `ADX ≥ 22` + staplade EMA → bara trendsetups
- `ADX ≤ 18` → bara reversion
- däremellan → ingen handel

Det här är det filter som oftast saknas i enkla EMA-botar, och det är därför de
bränner kontot i sidledes marknad.

### 3. Filter som stänger av dåliga lägen

- **Session:** handel bara 07–20 UTC (London/NY), fredag valbar
- **Spread:** avvisar trades när spread > 25 % av ATR
- **ATR-golv:** inga trades i död marknad
- **Högre tidsram:** pris måste ligga rätt sida EMA 100
- **Korrelation:** max 2 samtidiga USD-relaterade positioner

### 4. Riskhantering i R, inte i pips (`execution.py`)

| Åtgärd | Nivå |
|---|---|
| Delvis vinst (50 %) | +1,0 R |
| Stop till break-even (+buffert) | +1,0 R |
| ATR-trailing startar | +1,2 R |
| Stop loss / take profit | 1,5 × ATR / 3,0 × ATR |

Plus:

- **Daglig förlustgräns** som ligger kvar i SQLite → en omstart nollställer den inte
- **Circuit breaker:** 3 förluster i rad → 60 min paus, loggas till mobilen
- **Stopp som bara dras åt** — `tighten()` vägrar flytta ett stopp bakåt

### 5. Backtest med samma kod (`backtest.py`)

Samma `decide()` som live-boten används i backtesten. Ingen divergens mellan
"det jag testade" och "det jag kör".

```bash
python backtest.py --symbol EURUSD --tf 5 --bars 50000    # Windows, direkt från MT5
python backtest.py --csv data/EURUSD_M5.csv               # exporterad CSV
python backtest.py --synthetic                            # rök test
```

Utdata: antal trades, träffsäkerhet, **expectancy i R**, profit factor,
max drawdown, sämsta förlustsvit.

Tumregel: en strategi är intressant först när expectancy > 0,1 R efter spread
och profit factor > 1,3 över **flera** symboler och tidsramar.

---

## Var idéerna kommer ifrån

Samma beprövade mönster som i etablerade open source-botar (logik, inte kopierad kod):

| Källa | Vad som inspirerade |
|---|---|
| [fahadkatonvule24/tradingBot](https://github.com/fahadkatonvule24/tradingBot) | tre strategier bakom ett gränssnitt, ATR-normaliserade filter, R-baserad hantering (partial/BE/trail), walk-forward-optimering, journal |
| [kloseddd/trading-bot-mt5](https://github.com/kloseddd/trading-bot-mt5) | risklager före varje order: daglig kill switch, korrelationsgräns, cooldown, nyhetsfilter, "smart exit" |
| [lordgaruda/XAU-60](https://github.com/lordgaruda/XAU-60) | circuit breaker vid N förluster, trailing/partial-konfiguration, signal-kvalitetspoäng |
| [abdlhannan/MT5_Trade_Connector](https://github.com/abdlhannan/MT5_Trade_Connector) | trailing stop i egen tråd, dagliga/månatliga förlustgränser |
| [dceoy/mt5api](https://github.com/dceoy/mt5api) | REST-lager över MT5 (arkitektur för mobil-API:t) |

Känd fallgrop som jag uttryckligen undviker: ADX räknad med rullande summa i
stället för Wilder → ADX blir ~14× för stor och trendfiltret filtrerar aldrig.
Det finns ett test som fångar det (`test_adx_is_not_a_running_sum`).

---

## Så gör du den till "din"

1. Kör backtest per symbol du faktiskt handlar, med din brokers spread.
2. Justera `MIN_SCORE`, `TREND_ADX`, `SL/TP` i `.env` — en variabel i taget.
3. Kör `DRY_RUN=true` i minst 2–4 veckor. Läs loggen på mobilen varje dag.
4. Byt till demo-konto med riktiga ordrar. Jämför mot backtesten.
5. Först därefter, med pengar du har råd att förlora, LIVE.

Villkor som **diskvalificerar** en inställning: drawdown > 15 %, profit factor
< 1,2, eller färre än 100 trades i testet (för litet underlag).

## Tester

```bash
python -m unittest discover -s tests -v
```

18 tester: indikatorer, regim, sessionsfilter, spreadfilter, R-matematik,
circuit breaker, daglig förlustgräns och en backtest-körning.
