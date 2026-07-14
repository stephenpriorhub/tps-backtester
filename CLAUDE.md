# TPS Backtester — CLAUDE.md

## Purpose
Backtester for Nate Bear's TPS (Trend → Pattern → Squeeze) system. Detects TTM Squeeze setups on any ticker via Polygon.io/Massive, simulates entries/exits, and returns chart-ready data with trade stats.

## Strategy model — v3 (current)
Implements the "TPS Long Strategy v3 (78m, RSI momentum exit)" Pine Script. See
`tps_engine.py` for the pure-Python replication.
- **Scoring / entry are unchanged from v2:** Trend (D/78/30/15 = 20/15/10/5) +
  Squeeze (78/30/15 = 10/15/25) = 100. Entry when `score ≥ 65` AND `close > BB
  upper (EMA 10, ×1.5)` AND flat.
- **v3 exit model (the only logic that changed from v2):** a SINGLE contract per
  signal — no TP1/TP2, no chandelier trail, no runner/time exit. Exits are:
  1. **Hard stop** = entry − `sl_atr` × ATR (ATR fixed at the entry bar; fills
     intrabar at the stop level, or at the open on a gap-down).
  2. **RSI momentum-exhaustion exit** — once the trade is in profit, close on the
     first bar where `RSI(14) < rsi_exit_threshold` (default 60); fills on close.
  A position still open at the end of data is marked-to-market as signal `Open`.
- **Data granularity:** the engine runs on **1-MINUTE base bars** (the TradingView
  backtests were run on 1-min). 78m/30m/15m bars are built from 1-min so the
  78-minute grid — which is NOT a multiple of 15 — is exact. `run_ticker(ticker,
  df_1m, df_daily, cfg)` expects 1-min bars; `tps_run_massive.py` / `tps_app.py`
  fetch `(1, "minute")`.
- **Pine-exact indicators:** `ta.stdev` → population std (ddof=0); `ta.atr` /
  `ta.rsi` → Wilder RMA; squeeze KC width → SMA of TR.
- Validated vs the reference TradingView export (`TPS Score Strategy Backtesting
  V2 7.9.26.xlsx`): AAPL 128 vs 122 trades, 66.4% vs 68.9% win rate; NVDA 121 vs
  107, 62.0% vs 59.8%. Residual gaps are data-vendor + RSI-near-threshold timing.

## App
The live app is a **Streamlit** dashboard (`tps_app.py`), not Flask. Excel export
lives in `tps_export.py`; the batch NDX-100 runner in `tps_run_massive.py`.

## Tech Stack
- Backend: Python/Flask (`app.py`)
- Frontend: Static HTML/JS (`static/index.html`, `static/js/app.js`)
- Data: Polygon.io API (`POLYGON_API_KEY` env var)
- Charts: Plotly.js
- Deployment: Railway via Nixpacks (Python)

## Environment Variables
| Variable | Required | Notes |
|---|---|---|
| `POLYGON_API_KEY` | Yes | Polygon.io API key — same key as trading-scanner |

## Local Development
```bash
cd ~/Documents/GitHub/tps-backtester
pip install -r requirements.txt
export POLYGON_API_KEY=your_key
python app.py  # runs on localhost:5050
```

## Hub Integration (Static HTML Pattern)
```html
<!-- In <head> (first item): -->
<style>html{visibility:hidden}</style>
<!-- In <body> (first element): -->
<script src="https://oxfordhub.app/hub-nav.js" data-project-id="cmq8f23bz0000896nlbz411zb" id="hub-nav"></script>
```
**Hub Project cuid: `cmq8f23bz0000896nlbz411zb`** — already registered in hub DB; `static/index.html` line 13 is updated.

## Railway Deployment
- **Service name:** tps-backtester
- **Live URL:** https://tps.oxfordhub.app
- **Builder:** Nixpacks (auto-detected Python from requirements.txt + Procfile)
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` (from Procfile / railway.toml)
- **Health check:** `/api/health`
- **Env vars set in Railway:** `POLYGON_API_KEY`

## Manual Steps Remaining
1. In Railway dashboard: create new service → link GitHub repo `stephenpriorhub/tps-backtester`
2. Set env var `POLYGON_API_KEY` in Railway service environment (same value as trading-scanner)
3. Add custom domain `tps.oxfordhub.app` in Railway service settings
4. Update DNS: add CNAME `tps` → Railway-provided domain at your DNS provider

## Important Notes
- `.cache/` directory stores Polygon API response cache — must stay in `.gitignore`, never commit
- `data-project-id` in hub-nav script tag must be a real cuid (not placeholder) or non-admin users will be locked out
- Two-layer cache (memory + disk pickle) — ephemeral on Railway; cache rebuilds on each deploy/restart
