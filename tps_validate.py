"""
TPS Engine Validation
=====================
Loads the Massive data files saved by the MCP tools and runs the engine.
Prints the first 10 trades to compare against TradingView spreadsheet.

Usage:  python3 tps_validate.py
"""

import json
import io
import sys
import os
import pandas as pd
import numpy as np

# Point at the result files written by the MCP query
# Full 15m dataset (Jun 2021 – May 2025, 63k bars)
RESULT_FILES = {
    "15m":   "/Users/stephenprior/.claude/projects/-Users-stephenprior-Downloads-Claude/f55ccd18-d8ca-457c-87fa-bd2ffb72e8ff/tool-results/mcp-Massive_Market_Data-query_data-1779387112692.txt",
    "daily": "/Users/stephenprior/.claude/projects/-Users-stephenprior-Downloads-Claude/f55ccd18-d8ca-457c-87fa-bd2ffb72e8ff/tool-results/mcp-Massive_Market_Data-query_data-1779386307214.txt",
}

sys.path.insert(0, os.path.dirname(__file__))
from tps_engine import run_ticker, DEFAULT_CONFIG


def load_mcp_csv(path: str) -> pd.DataFrame:
    """Parse a MCP tool-result JSON file containing CSV data."""
    with open(path) as f:
        raw = f.read()
    # The file is: {"result":"<csv>"}
    data = json.loads(raw)
    csv_text = data["result"]
    df = pd.read_csv(io.StringIO(csv_text))
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t").sort_index()
    # Rename columns to match engine expectation: o, h, l, c, v
    col_map = {col: col.lower() for col in df.columns}
    df = df.rename(columns=col_map)
    return df[["o", "h", "l", "c", "v"]].astype(float)


def main():
    print("Loading Massive data...")
    df_15m   = load_mcp_csv(RESULT_FILES["15m"])
    df_daily = load_mcp_csv(RESULT_FILES["daily"])

    print(f"  15m bars : {len(df_15m):,}  ({df_15m.index[0].date()} → {df_15m.index[-1].date()})")
    print(f"  Daily bars: {len(df_daily):,} ({df_daily.index[0].date()} → {df_daily.index[-1].date()})")

    cfg = {**DEFAULT_CONFIG, "chart_tf": 78}

    print("\nRunning TPS engine (AAPL, 78m)...")
    raw_trades, df_trades = run_ticker("AAPL", df_15m, df_daily, cfg)

    if not raw_trades:
        print("⚠ No trades generated — may need more data or warm-up period.")
        return

    exits = df_trades[df_trades["Type"] == "Exit long"] if not df_trades.empty else pd.DataFrame()
    wins  = (exits["Net P&L %"] > 0).sum() if not exits.empty else 0
    n     = len(exits)
    wr    = wins / n * 100 if n else 0
    avg_r = exits["Net P&L %"].mean() if n else 0

    print(f"\n── Results Summary ──")
    print(f"  Total signals  : {n // 2}")
    print(f"  Total exits    : {n}")
    print(f"  Win rate       : {wr:.1f}%")
    print(f"  Avg return     : {avg_r:.3f}%")
    print(f"  Avg win        : {exits.loc[exits['Net P&L %'] > 0, 'Net P&L %'].mean():.3f}%")
    print(f"  Avg loss       : {exits.loc[exits['Net P&L %'] <= 0, 'Net P&L %'].mean():.3f}%")
    if n:
        print(f"  Avg duration   : {exits['Duration'].mean():.2f} days")

    print(f"\n── First 10 Exit Trades ──")
    print(df_trades[df_trades["Type"] == "Exit long"][
        ["Trade #", "Date and time", "Signal", "Price USD", "Net P&L %", "Duration"]
    ].head(10).to_string(index=False))

    print("\n── Score distribution check (sample 78m bars) ──")
    # Rerun engine with score output for inspection
    from tps_engine import (filter_market_hours, resample_ohlcv, add_tps_signals,
                             align_tf, align_daily, compute_tps_score, compute_entry_trigger, atr)
    import pytz
    ET = pytz.timezone("America/New_York")

    sqz_cfg = dict(sqz_len=cfg["sqz_len"], release_bars=cfg["release_bars"],
                   min_sqz_bars=cfg["min_sqz_bars"], max_sqz_bars=cfg["max_sqz_bars"])

    df_mh = filter_market_hours(df_15m)
    df_30  = resample_ohlcv(df_mh, 30)
    df_chart = resample_ohlcv(df_mh, 78)
    df_chart.attrs["tf_min"] = 78

    df_15s = add_tps_signals(df_mh, **sqz_cfg)
    df_30s = add_tps_signals(df_30, **sqz_cfg)
    df_cs  = add_tps_signals(df_chart, **sqz_cfg)
    df_ds  = add_tps_signals(df_daily, **sqz_cfg)

    merged = df_cs.copy()
    for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
        if col in merged.columns:
            merged.rename(columns={col: f"c78_{col}"}, inplace=True)

    merged = align_tf(merged, df_30s, "c30", 30)
    merged = align_tf(merged, df_15s, "c15", 15)
    merged = align_daily(merged, df_ds, "d")

    bool_cols = [c for c in merged.columns if any(c.endswith(s) for s in ["_ok", "_tight", "_recent", "_rise2"])]
    merged[bool_cols] = merged[bool_cols].fillna(False)
    merged["tps_score"] = compute_tps_score(merged, cfg)
    merged["bb_break"]  = compute_entry_trigger(merged, cfg["bb_len"], cfg["bb_dev"])
    merged["atr_10"]    = atr(merged, cfg["atr_len"])

    score_nonzero = merged[merged["tps_score"] > 0]
    print(f"  Bars with score > 0 : {len(score_nonzero)}/{len(merged)}")
    print(f"  Max score           : {merged['tps_score'].max():.1f}")
    print(f"  Bars ≥ 65           : {(merged['tps_score'] >= 65).sum()}")
    print(f"  Bars ≥ 65 + BB break: {((merged['tps_score'] >= 65) & merged['bb_break']).sum()}")

    print("\n  Score distribution:")
    bins = [0, 10, 20, 30, 40, 50, 60, 65, 70, 80, 90, 100]
    hist, edges = np.histogram(merged["tps_score"].dropna(), bins=bins)
    for i, cnt in enumerate(hist):
        print(f"    {edges[i]:>3.0f}–{edges[i+1]:>3.0f} : {'█' * min(cnt // 5, 40)} {cnt}")


if __name__ == "__main__":
    main()
