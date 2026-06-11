"""
TPS Cache Builder — converts MCP query_data files to parquet cache
===================================================================
Run this after fetching data via MCP to pre-populate the disk cache
so tps_run_massive.py can load it without hitting the API.

Usage:  python3 tps_cache_from_mcp.py

Reads the MCP result files and writes them into the ./data/ directory
in the same parquet format that tps_run_massive.py expects.
"""

import json
import io
import hashlib
import sys
import os
from pathlib import Path
import pandas as pd
from datetime import datetime

PROJECT_DIR = Path(__file__).parent
DATA_DIR    = PROJECT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── MCP result files to load
# key = (ticker, multiplier, timespan)  →  MCP result file path
MCP_FILES = {
    # AAPL 15m  — combined Jun 2021 – May 2025
    ("AAPL", 15, "minute"): (
        "/Users/stephenprior/.claude/projects/"
        "-Users-stephenprior-Downloads-Claude/"
        "f55ccd18-d8ca-457c-87fa-bd2ffb72e8ff/"
        "tool-results/mcp-Massive_Market_Data-query_data-1779387112692.txt"
    ),
    # AAPL daily — May 2021 – May 2025
    ("AAPL", 1, "day"): (
        "/Users/stephenprior/.claude/projects/"
        "-Users-stephenprior-Downloads-Claude/"
        "f55ccd18-d8ca-457c-87fa-bd2ffb72e8ff/"
        "tool-results/mcp-Massive_Market_Data-query_data-1779386307214.txt"
    ),
}

START = "2021-06-01"
END   = datetime.today().strftime("%Y-%m-%d")


def _cache_path(ticker, multiplier, timespan):
    """Reproduce tps_run_massive.py's cache naming scheme."""
    key = f"{ticker}_{multiplier}_{timespan}_{START}_{END}"
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    return DATA_DIR / f"{ticker}_{multiplier}{timespan}_{START[:7]}_{END[:7]}_{h}.parquet"


def load_mcp_csv(path: str) -> pd.DataFrame:
    """Parse a MCP tool-result JSON file containing CSV data."""
    with open(path) as f:
        raw = f.read()
    data = json.loads(raw)
    csv_text = data["result"]
    df = pd.read_csv(io.StringIO(csv_text))
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t").sort_index()
    col_map = {col: col.lower() for col in df.columns}
    df = df.rename(columns=col_map)
    available = [c for c in ["o", "h", "l", "c", "v"] if c in df.columns]
    return df[available].astype(float)


def main():
    print("Building parquet cache from MCP result files...\n")
    for (ticker, mult, span), mcp_path in MCP_FILES.items():
        cache = _cache_path(ticker, mult, span)
        if cache.exists():
            print(f"  [skip]  {ticker} {mult}{span} — already cached at {cache.name}")
            continue
        if not os.path.exists(mcp_path):
            print(f"  [MISS]  {ticker} {mult}{span} — file not found: {mcp_path}")
            continue
        print(f"  [load]  {ticker} {mult}{span} ...", end="", flush=True)
        df = load_mcp_csv(mcp_path)
        df.to_parquet(cache)
        print(f"  → {len(df):,} bars saved to {cache.name}")

    print("\nCache ready. Run tps_run_massive.py to execute the backtest.")
    print("(You still need MASSIVE_API_KEY set to fetch data for other tickers.)")


if __name__ == "__main__":
    main()
