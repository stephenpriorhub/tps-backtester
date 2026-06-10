# TPS Backtester

Nate Bear's Trend → Pattern → Squeeze backtesting tool, built for Monument Traders Alliance / OxfordHub.

## What is TPS?

The TPS system trades market compression events:

1. **Trend** — overall market direction
2. **Pattern** — price compression (squeeze)
3. **Squeeze** — TTM Squeeze indicator: Bollinger Bands fully inside Keltner Channels = squeeze ON (black dot). First bar BB exits KC = squeeze FIRE (green/yellow dot = entry signal)

Entry requires minimum 5 consecutive squeeze bars before the fire. Direction is determined by the momentum oscillator at the fire bar (positive = long, negative = short). Exit on the 2nd momentum reversal bar.

## Stack

- Backend: Python / Flask
- Frontend: Vanilla JS + Plotly.js
- Data: Polygon.io

## Setup

```bash
cd tps-backtester

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your API key
cp .env.example .env
# Edit .env: set POLYGON_API_KEY=your_key

# Run locally
python app.py
# App at http://localhost:5050
```

## Deploy (Railway)

Set env vars on Railway:
- `POLYGON_API_KEY`
- `PORT` (Railway sets this automatically)

Uses `Procfile` for gunicorn startup.

## Indicators

| Indicator | Default Params |
|-----------|---------------|
| Bollinger Bands | 20-period, 2.0 std dev |
| Keltner Channels | 20-period, 1.5x ATR (Wilder's) |
| Momentum | Linear regression oscillator, 12-period looback on TTM midpoint delta |

## Signal Logic

- **Squeeze ON**: BB upper <= KC upper AND BB lower >= KC lower
- **Squeeze FIRE**: First bar where BB exits KC after being inside
- **Valid entry**: Fire bar must follow >= 5 consecutive squeeze bars
- **Long entry**: Momentum > 0 at fire bar
- **Short entry**: Momentum < 0 at fire bar
- **Exit**: 2nd consecutive bar of opposite momentum color

## OxfordHub Integration

The hub nav bar is loaded from `https://oxfordhub.app/hub-nav.js`. The `data-project-id` attribute controls which project entry is checked for user access.
