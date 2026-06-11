#!/bin/bash
# Launch the TPS Backtesting Dashboard locally
cd "$(dirname "$0")"

# Load API key from .env or trading-scanner
if [ -f ".env" ]; then
  export $(grep -E "POLYGON_API_KEY|MASSIVE_API_KEY" .env | xargs)
elif [ -f "$HOME/Documents/GitHub/trading-scanner/.env" ]; then
  export $(grep -E "POLYGON_API_KEY|MASSIVE_API_KEY" "$HOME/Documents/GitHub/trading-scanner/.env" | xargs)
fi

echo "Starting TPS Backtesting Dashboard..."
echo "Open in browser: http://localhost:8501"
echo "Press Ctrl+C to stop."
echo ""

python3 -m streamlit run tps_app.py \
  --server.port 8501 \
  --server.headless false \
  --browser.gatherUsageStats false
