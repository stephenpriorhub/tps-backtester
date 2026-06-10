# TPS Backtester — CLAUDE.md

## Purpose
Backtester for Nate Bear's TPS (Trend → Pattern → Squeeze) system. Detects TTM Squeeze setups on any ticker via Polygon.io, simulates entries/exits, and returns chart-ready data with trade stats.

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
