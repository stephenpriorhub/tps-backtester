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
<script src="https://oxfordhub.app/hub-nav.js" data-project-id="REPLACE_WITH_REAL_CUID" id="hub-nav"></script>
```
**Hub Project cuid: `REPLACE_WITH_REAL_CUID`** — register at oxfordhub.app/admin/projects, then update `static/index.html` line 13 and this file.

## Railway Deployment
- **Service name:** tps-backtester (to be created)
- **Live URL:** https://tps.oxfordhub.app (custom domain — to be configured)
- **Builder:** Nixpacks (auto-detected Python from requirements.txt + Procfile)
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` (from Procfile)
- **Env vars to set in Railway:** `POLYGON_API_KEY`

## Manual Steps Required
1. Create Railway service: link GitHub repo `stephenpriorhub/tps-backtester`
2. Set `POLYGON_API_KEY` in Railway service environment
3. Add custom domain `tps.oxfordhub.app` in Railway service settings
4. Register project in OxfordHub admin at oxfordhub.app/admin/projects:
   - Name: TPS Backtester
   - URL: https://tps.oxfordhub.app
5. Copy the generated cuid and update `static/index.html` line 13 `data-project-id`
6. Update this CLAUDE.md with the real cuid and live URL
7. Commit and push

## Important Notes
- `.cache/` directory stores Polygon API response cache — must stay in `.gitignore`, never commit
- `data-project-id` in hub-nav script tag must be a real cuid (not placeholder) or non-admin users will be locked out
- Two-layer cache (memory + disk pickle) — ephemeral on Railway; cache rebuilds on each deploy/restart
