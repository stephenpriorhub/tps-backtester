"""
tps_export.py
=============
Export TPS backtest results to Excel exactly matching the reference
"TPS Score Strategy Backtesting V2" spreadsheet format.

Public API
----------
    export_to_excel(all_trades, cfg, output_path) -> str
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

# ── Reference ticker order (Summary sheet columns B–J) ────────────────────────
REFERENCE_TICKERS = ["AAPL", "GOOGL", "NVDA", "TSLA", "AMZN", "MSFT", "AMD", "ORCL", "NFLX"]

# ── Number formats ─────────────────────────────────────────────────────────────
FMT_PCT       = "0.00%"
FMT_PCT3      = "0.000%"
FMT_DECIMAL   = "0.00"
FMT_DATETIME  = r'yyyy\-mm\-dd\ hh:mm'
FMT_NUMBER    = "#,##0.00"

# ── Fonts ──────────────────────────────────────────────────────────────────────
FONT_NORMAL = Font(name="Calibri", size=11)
FONT_BOLD   = Font(name="Calibri", size=11, bold=True)


# ══════════════════════════════════════════════════════════════════════════════
# HELPER UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _col_letter(col_idx: int) -> str:
    return get_column_letter(col_idx)


def _set_cell(ws, row: int, col: int, value, fmt: str = None, bold: bool = False):
    cell = ws.cell(row=row, column=col, value=value)
    if fmt:
        cell.number_format = fmt
    cell.font = FONT_BOLD if bold else FONT_NORMAL
    return cell


def _auto_width(ws, min_width: int = 8, max_width: int = 30, overrides: dict = None):
    overrides = overrides or {}
    for col in ws.columns:
        letter = col[0].column_letter
        if letter in overrides:
            ws.column_dimensions[letter].width = overrides[letter]
            continue
        max_len = max((len(str(c.value or "")) for c in col), default=min_width)
        ws.column_dimensions[letter].width = min(max(max_len + 2, min_width), max_width)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY SHEET
# ══════════════════════════════════════════════════════════════════════════════

def _write_summary_sheet(wb: Workbook, tickers: list, chart_tf: int,
                         stats_by_ticker: dict) -> None:
    """
    Build the Summary sheet with COMPUTED VALUES (no formulas) so it renders
    identically in Excel, Numbers, Quick Look, and any parser — matching the
    reference workbook, whose summary holds cached values.

    Layout: 13 rows × (2 + n_tickers + 1) columns
    Row 1: blank A1, ticker names in B..., "V2 (tf)avgs" in last col
    Rows 2–11: metric labels in A, values per ticker, SUM/AVERAGE in avgs col
    Row 13: footer with avg-days-in and exp/day calculations
    """
    sheet_name = f"{chart_tf}min Summary"
    ws = wb.create_sheet(sheet_name, 0)

    n = len(tickers)
    avgs_col = n + 2  # 1-based; A=1, tickers in cols 2..n+1, avgs in col n+2

    # ── Row 1: headers
    for i, ticker in enumerate(tickers):
        _set_cell(ws, 1, i + 2, ticker, bold=True)
    _set_cell(ws, 1, avgs_col, f"V2 ({chart_tf})avgs", bold=True)

    # ── Row 2–11: metric labels
    metric_labels = [
        "Count",
        "# Wins",
        "Win Rate",
        "Avg Return %",
        "Avg Win %",
        "Avg Loss %",
        "Avg Max Return %",
        "Win Rate (max)",
        "Expectancy:",
        "Avg Duration (days)",
    ]
    for offset, label in enumerate(metric_labels):
        _set_cell(ws, offset + 2, 1, label, bold=True)

    # (row, stats key, number format, aggregate fn for the avgs column)
    metric_rows = [
        (2,  "count",        FMT_NUMBER, "sum"),
        (3,  "wins",         FMT_NUMBER, "sum"),
        (4,  "win_rate",     FMT_PCT,    "avg"),
        (5,  "avg_ret",      FMT_PCT,    "avg"),
        (6,  "avg_win",      FMT_PCT,    "avg"),
        (7,  "avg_loss",     FMT_PCT,    "avg"),
        (8,  "avg_max_ret",  FMT_PCT,    "avg"),
        (9,  "win_rate_max", FMT_PCT,    "avg"),
        (10, "expectancy",   FMT_PCT,    "avg"),
        (11, "avg_dur",      FMT_NUMBER, "avg"),
    ]

    # ── Per-ticker computed values
    for i, ticker in enumerate(tickers):
        col = i + 2
        stats = stats_by_ticker.get(ticker) or {}
        for row, key, fmt, _agg in metric_rows:
            _set_cell(ws, row, col, stats.get(key, 0), fmt=fmt)

    # ── Avgs column (SUM for counts, AVERAGE for rates/returns)
    for row, key, fmt, agg in metric_rows:
        vals = [(stats_by_ticker.get(t) or {}).get(key, 0) for t in tickers]
        if agg == "sum":
            out = sum(vals)
        else:
            out = sum(vals) / len(vals) if vals else 0
        _set_cell(ws, row, avgs_col, out, fmt=fmt)

    # ── Row 13: footer — avg days in (per-leg) and expectancy per day
    all_avg_dur = [(stats_by_ticker.get(t) or {}).get("avg_dur", 0) for t in tickers]
    all_expect  = [(stats_by_ticker.get(t) or {}).get("expectancy", 0) for t in tickers]
    avg_dur_all = sum(all_avg_dur) / len(all_avg_dur) if all_avg_dur else 0
    expect_all  = sum(all_expect) / len(all_expect) if all_expect else 0
    avg_days_in = avg_dur_all / 2
    exp_per_day = (expect_all / avg_days_in) if avg_days_in else 0

    col_i = avgs_col - 2
    col_j = avgs_col - 1
    col_k = avgs_col
    col_l = avgs_col + 1

    _set_cell(ws, 13, col_i, "avg days in")
    _set_cell(ws, 13, col_j, avg_days_in, fmt=FMT_NUMBER)
    _set_cell(ws, 13, col_k, exp_per_day, fmt=FMT_PCT3)
    _set_cell(ws, 13, col_l, "exp / day")

    # ── Column widths
    _auto_width(ws, overrides={"A": 18})


# ══════════════════════════════════════════════════════════════════════════════
# TICKER SHEET
# ══════════════════════════════════════════════════════════════════════════════

# Column indices (1-based)
COL_TRADE_NUM    = 1   # A  Trade #
COL_TYPE         = 2   # B  Type
COL_DATETIME     = 3   # C  Date and time
COL_SIGNAL       = 4   # D  Signal
COL_PRICE        = 5   # E  Price USD
COL_SIZE_QTY     = 6   # F  Size (qty)
COL_SIZE_VAL     = 7   # G  Size (value)
COL_TOTAL_RETURN = 8   # H  Total Return
COL_MAX_RETURN   = 9   # I  Max Return
COL_STATS_J      = 10  # J  (no header)
COL_STATS_K      = 11  # K  (no header)
COL_DURATION     = 12  # L  Duration
COL_NET_PNL_USD  = 13  # M  Net P&L USD
COL_NET_PNL_PCT  = 14  # N  Net P&L %
COL_FAV_EXC_USD  = 15  # O  Favorable excursion USD
COL_FAV_EXC_PCT  = 16  # P  Favorable excursion %
COL_ADV_EXC_USD  = 17  # Q  Adverse excursion USD
COL_ADV_EXC_PCT  = 18  # R  Adverse excursion %
COL_CUM_PNL_USD  = 19  # S  Cumulative P&L USD
COL_CUM_PNL_PCT  = 20  # T  Cumulative P&L %

TICKER_HEADERS = {
    COL_TRADE_NUM:    "Trade #",
    COL_TYPE:         "Type",
    COL_DATETIME:     "Date and time",
    COL_SIGNAL:       "Signal",
    COL_PRICE:        "Price USD",
    COL_SIZE_QTY:     "Size (qty)",
    COL_SIZE_VAL:     "Size (value)",
    COL_TOTAL_RETURN: "Total Return",
    COL_MAX_RETURN:   "Max Return",
    COL_STATS_J:      None,
    COL_STATS_K:      None,
    COL_DURATION:     "Duration",
    COL_NET_PNL_USD:  "Net P&L USD",
    COL_NET_PNL_PCT:  "Net P&L %",
    COL_FAV_EXC_USD:  "Favorable excursion USD",
    COL_FAV_EXC_PCT:  "Favorable excursion %",
    COL_ADV_EXC_USD:  "Adverse excursion USD",
    COL_ADV_EXC_PCT:  "Adverse excursion %",
    COL_CUM_PNL_USD:  "Cumulative P&L USD",
    COL_CUM_PNL_PCT:  "Cumulative P&L %",
}


def _write_ticker_sheet(wb: Workbook, ticker: str, df: pd.DataFrame) -> None:
    """
    Write one ticker sheet with the exact 20-column reference layout.

    Reference 4-row block per signal:
        Row 1: Trade N   | Entry long | entry_date | Long  | entry_price | ...
        Row 2: Trade N+1 | Entry long | entry_date | Long  | entry_price | ...
        Row 3: Trade N   | Exit long  | exit_date  | TP1   | exit_price  | ... (H blank)
        Row 4: Trade N+1 | Exit long  | exit_date  | TP2   | exit_price  | ... (H formula fires)

    Column H fires on row 4 (TP2 exit):
        =IF(AND(D4<>"TP1", B4="Exit long", A4=A2), AVERAGE(N3:N4)/100, "")
    Column I fires on row 4:
        =IF(AND(D4<>"TP1", B4="Exit long", A4=A2), ((E2+O4)/E2)-1, "")
    Column L fires on row 4:
        =IF(I4<>"", _xlfn.DAYS(C4, C2), "")

    In-sheet aggregate formulas (no header columns J, K):
        J2 = COUNTIF(H:H,">0")/COUNT(H:H)   — win rate by Total Return
        K2 = COUNTIF(I:I,">0")/COUNT(I:I)   — win rate by Max Return
        J4 = AVERAGE(H:H)
        K4 = AVERAGE(I:I)
    """
    ws = wb.create_sheet(ticker)

    # ── Row 1: Headers
    for col_idx, header in TICKER_HEADERS.items():
        if header is not None:
            _set_cell(ws, 1, col_idx, header, bold=True)

    # Per-signal aggregates collected while writing (H / I / L values)
    h_vals: list = []   # Total Return (fraction, e.g. 0.0231)
    i_vals: list = []   # Max Return (fraction)
    l_vals: list = []   # Duration (whole days)

    if df is None or df.empty:
        ws.freeze_panes = "A2"
        return _ticker_stats(h_vals, i_vals, l_vals)

    # ── Build 4-row blocks from engine output
    blocks = _build_signal_blocks(df)

    # ── Write rows
    cum_pnl_usd = 0.0
    cum_pnl_pct = 0.0

    for signal_idx, block in enumerate(blocks):
        # signal_idx 0-based; trade numbers for TP1 = 2*signal_idx+1, TP2 = 2*signal_idx+2
        tp1_trade_num = 2 * signal_idx + 1
        tp2_trade_num = 2 * signal_idx + 2

        # Excel row numbers (1-based; row 1 = header)
        # Block occupies 4 rows: entry_tp1, entry_tp2, exit_tp1, exit_tp2
        base_row = 2 + signal_idx * 4  # row of Entry TP1

        entry_tp1_row = base_row
        entry_tp2_row = base_row + 1
        exit_tp1_row  = base_row + 2
        exit_tp2_row  = base_row + 3

        entry = block["entry"]
        exit_tp1 = block["exit_tp1"]
        exit_tp2 = block["exit_tp2"]

        entry_price   = entry["entry_price"]
        exit_tp1_price = exit_tp1["exit_price"]
        exit_tp2_price = exit_tp2["exit_price"]

        tp1_pnl_pct = (exit_tp1_price - entry_price) / entry_price * 100
        tp2_pnl_pct = (exit_tp2_price - entry_price) / entry_price * 100
        tp1_pnl_usd = round(exit_tp1_price - entry_price, 2)
        tp2_pnl_usd = round(exit_tp2_price - entry_price, 2)

        tp1_fav_usd = round(exit_tp1["fav_exc_pct"] / 100 * entry_price, 2)
        tp1_fav_pct = exit_tp1["fav_exc_pct"]
        tp1_adv_usd = round(exit_tp1["adv_exc_pct"] / 100 * entry_price, 2)
        tp1_adv_pct = exit_tp1["adv_exc_pct"]

        tp2_fav_usd = round(exit_tp2["fav_exc_pct"] / 100 * entry_price, 2)
        tp2_fav_pct = exit_tp2["fav_exc_pct"]
        tp2_adv_usd = round(exit_tp2["adv_exc_pct"] / 100 * entry_price, 2)
        tp2_adv_pct = exit_tp2["adv_exc_pct"]

        cum_pnl_usd += tp1_pnl_usd + tp2_pnl_usd
        cum_pnl_pct += tp1_pnl_pct + tp2_pnl_pct

        # Format datetimes
        entry_dt_str = entry["entry_time_str"]
        exit_tp1_dt_str = exit_tp1["exit_time_str"]
        exit_tp2_dt_str = exit_tp2["exit_time_str"]

        # Determine exit signal labels
        tp1_signal = _exit_signal_label(exit_tp1["exit_type"], "TP1")
        tp2_signal = _exit_signal_label(exit_tp2["exit_type"], "TP2")

        # ── Row: Entry TP1 (trade N)
        _write_data_row(ws, entry_tp1_row,
                        trade_num=tp1_trade_num,
                        row_type="Entry long",
                        dt_str=entry_dt_str,
                        signal="Long",
                        price=entry_price,
                        size_qty=1,
                        size_val=entry_price,
                        net_pnl_usd=tp1_pnl_usd,
                        net_pnl_pct=tp1_pnl_pct,
                        fav_exc_usd=tp1_fav_usd,
                        fav_exc_pct=tp1_fav_pct,
                        adv_exc_usd=tp1_adv_usd,
                        adv_exc_pct=tp1_adv_pct,
                        cum_pnl_usd=None,
                        cum_pnl_pct=None)

        # ── Row: Entry TP2 (trade N+1)
        _write_data_row(ws, entry_tp2_row,
                        trade_num=tp2_trade_num,
                        row_type="Entry long",
                        dt_str=entry_dt_str,
                        signal="Long",
                        price=entry_price,
                        size_qty=1,
                        size_val=entry_price,
                        net_pnl_usd=tp2_pnl_usd,
                        net_pnl_pct=tp2_pnl_pct,
                        fav_exc_usd=tp2_fav_usd,
                        fav_exc_pct=tp2_fav_pct,
                        adv_exc_usd=tp2_adv_usd,
                        adv_exc_pct=tp2_adv_pct,
                        cum_pnl_usd=None,
                        cum_pnl_pct=None)

        # ── Row: Exit TP1 (trade N)
        # H is blank on TP1 exit rows (the IF condition D<>"TP1" fails)
        # But we still write the formula — it evaluates to "" because D="TP1"
        _write_data_row(ws, exit_tp1_row,
                        trade_num=tp1_trade_num,
                        row_type="Exit long",
                        dt_str=exit_tp1_dt_str,
                        signal=tp1_signal,
                        price=exit_tp1_price,
                        size_qty=1,
                        size_val=entry_price,
                        net_pnl_usd=tp1_pnl_usd,
                        net_pnl_pct=tp1_pnl_pct,
                        fav_exc_usd=tp1_fav_usd,
                        fav_exc_pct=tp1_fav_pct,
                        adv_exc_usd=tp1_adv_usd,
                        adv_exc_pct=tp1_adv_pct,
                        cum_pnl_usd=round(cum_pnl_usd - tp2_pnl_usd, 2),
                        cum_pnl_pct=round(cum_pnl_pct - tp2_pnl_pct, 4))

        # H/I/L stay blank on the TP1 exit row (reference fires them on TP2 row only)

        # ── Row: Exit TP2 (trade N+1)
        # H fires here because D<>"TP1" (D="TP2" or "Close entry(s) order Long")
        _write_data_row(ws, exit_tp2_row,
                        trade_num=tp2_trade_num,
                        row_type="Exit long",
                        dt_str=exit_tp2_dt_str,
                        signal=tp2_signal,
                        price=exit_tp2_price,
                        size_qty=1,
                        size_val=entry_price,
                        net_pnl_usd=tp2_pnl_usd,
                        net_pnl_pct=tp2_pnl_pct,
                        fav_exc_usd=tp2_fav_usd,
                        fav_exc_pct=tp2_fav_pct,
                        adv_exc_usd=tp2_adv_usd,
                        adv_exc_pct=tp2_adv_pct,
                        cum_pnl_usd=round(cum_pnl_usd, 2),
                        cum_pnl_pct=round(cum_pnl_pct, 4))

        # ── H/I/L computed VALUES on the TP2 exit row (reference semantics):
        #    H = AVERAGE(N_tp1, N_tp2)/100          (per-signal total return, fraction)
        #    I = ((entry + fav_exc_usd_tp2)/entry)-1 (per-signal max return, fraction)
        #    L = DAYS(exit_tp2_date, entry_date)     (whole-day date difference)
        n = exit_tp2_row
        h_val = (tp1_pnl_pct + tp2_pnl_pct) / 2.0 / 100.0
        i_val = ((entry_price + tp2_fav_usd) / entry_price) - 1 if entry_price else 0.0
        entry_dt   = _parse_dt(entry_dt_str)
        exit_tp2_dt = _parse_dt(exit_tp2_dt_str)
        if hasattr(entry_dt, "date") and hasattr(exit_tp2_dt, "date"):
            l_val = (exit_tp2_dt.date() - entry_dt.date()).days
        else:
            l_val = None

        c = ws.cell(row=n, column=COL_TOTAL_RETURN, value=round(h_val, 6))
        c.number_format = FMT_PCT
        c = ws.cell(row=n, column=COL_MAX_RETURN, value=round(i_val, 6))
        c.number_format = FMT_PCT
        if l_val is not None:
            c = ws.cell(row=n, column=COL_DURATION, value=l_val)
            c.number_format = FMT_DECIMAL

        h_vals.append(h_val)
        i_vals.append(i_val)
        if l_val is not None:
            l_vals.append(l_val)

    # ── In-sheet aggregate stats (computed values): J2, K2, J4, K4
    if h_vals:
        _set_cell(ws, 2, COL_STATS_J,
                  sum(1 for v in h_vals if v > 0) / len(h_vals), fmt=FMT_PCT)
        _set_cell(ws, 4, COL_STATS_J, sum(h_vals) / len(h_vals), fmt=FMT_PCT)
    if i_vals:
        _set_cell(ws, 2, COL_STATS_K,
                  sum(1 for v in i_vals if v > 0) / len(i_vals), fmt=FMT_PCT)
        _set_cell(ws, 4, COL_STATS_K, sum(i_vals) / len(i_vals), fmt=FMT_PCT)

    ws.freeze_panes = "A2"
    _auto_width(ws, min_width=6, overrides={"C": 18, "O": 22, "Q": 20})
    return _ticker_stats(h_vals, i_vals, l_vals)


def _signal_values(df: pd.DataFrame):
    """
    Reduce a ticker's raw trade DataFrame to per-SIGNAL value lists, using the
    exact same math the Excel export writes into columns H / I / L:
        h = average of the two legs' net return (fraction)   — "Total Return"
        i = ((entry + TP2-leg favorable excursion $)/entry)-1 — "Max Return"
        l = whole-day span from entry to the TP2 exit         — "Duration (days)"
    This is the single source of truth for both the workbook summary and the
    on-screen dashboard summary, so they can never disagree.
    """
    h_vals, i_vals, l_vals = [], [], []
    if df is None or df.empty:
        return h_vals, i_vals, l_vals
    for block in _build_signal_blocks(df):
        entry_price    = block["entry"]["entry_price"]
        if not entry_price:
            continue
        exit_tp1_price = block["exit_tp1"]["exit_price"]
        exit_tp2_price = block["exit_tp2"]["exit_price"]
        tp1_pnl_pct = (exit_tp1_price - entry_price) / entry_price * 100
        tp2_pnl_pct = (exit_tp2_price - entry_price) / entry_price * 100
        tp2_fav_usd = round(block["exit_tp2"]["fav_exc_pct"] / 100 * entry_price, 2)

        h_vals.append((tp1_pnl_pct + tp2_pnl_pct) / 2.0 / 100.0)
        i_vals.append(((entry_price + tp2_fav_usd) / entry_price) - 1)

        entry_dt    = _parse_dt(block["entry"]["entry_time_str"])
        exit_tp2_dt = _parse_dt(block["exit_tp2"]["exit_time_str"])
        if hasattr(entry_dt, "date") and hasattr(exit_tp2_dt, "date"):
            l_vals.append((exit_tp2_dt.date() - entry_dt.date()).days)
    return h_vals, i_vals, l_vals


def compute_signal_stats(df: pd.DataFrame) -> dict:
    """Per-ticker summary metrics (per-signal) — matches the workbook summary tab."""
    return _ticker_stats(*_signal_values(df))


def _ticker_stats(h_vals: list, i_vals: list, l_vals: list) -> dict:
    """Compute the ten summary metrics for one ticker from per-signal values."""
    n = len(h_vals)
    if n == 0:
        return {k: 0 for k in ("count", "wins", "win_rate", "avg_ret", "avg_win",
                                "avg_loss", "avg_max_ret", "win_rate_max",
                                "expectancy", "avg_dur")}
    wins     = sum(1 for v in h_vals if v > 0)
    win_rate = wins / n
    pos      = [v for v in h_vals if v > 0]
    neg      = [v for v in h_vals if v < 0]
    avg_win  = sum(pos) / len(pos) if pos else 0.0
    avg_loss = sum(neg) / len(neg) if neg else 0.0
    return {
        "count":        n,
        "wins":         wins,
        "win_rate":     win_rate,
        "avg_ret":      sum(h_vals) / n,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
        "avg_max_ret":  sum(i_vals) / len(i_vals) if i_vals else 0.0,
        "win_rate_max": (sum(1 for v in i_vals if v > 0) / len(i_vals)) if i_vals else 0.0,
        "expectancy":   win_rate * avg_win + (1 - win_rate) * avg_loss,
        "avg_dur":      sum(l_vals) / len(l_vals) if l_vals else 0.0,
    }


def _write_data_row(ws, row: int, trade_num, row_type, dt_str, signal,
                    price, size_qty, size_val,
                    net_pnl_usd, net_pnl_pct,
                    fav_exc_usd, fav_exc_pct,
                    adv_exc_usd, adv_exc_pct,
                    cum_pnl_usd, cum_pnl_pct):
    """Write all data columns (not H/I/L — those get formula strings separately)."""
    _set_cell(ws, row, COL_TRADE_NUM, trade_num)
    _set_cell(ws, row, COL_TYPE, row_type)

    # Write datetime — try to parse to Python datetime for proper Excel date handling
    dt_val = _parse_dt(dt_str)
    cell = ws.cell(row=row, column=COL_DATETIME, value=dt_val)
    cell.number_format = FMT_DATETIME
    cell.font = FONT_NORMAL

    _set_cell(ws, row, COL_SIGNAL,  signal)
    _set_cell(ws, row, COL_PRICE,   price)
    _set_cell(ws, row, COL_SIZE_QTY, size_qty)
    _set_cell(ws, row, COL_SIZE_VAL, round(size_val, 4))

    # H, I, L are formula-only — skip here (written by caller)

    _set_cell(ws, row, COL_NET_PNL_USD, round(net_pnl_usd, 2))
    _set_cell(ws, row, COL_NET_PNL_PCT, round(net_pnl_pct, 4), fmt=FMT_DECIMAL)
    _set_cell(ws, row, COL_FAV_EXC_USD, round(fav_exc_usd, 2))
    _set_cell(ws, row, COL_FAV_EXC_PCT, round(fav_exc_pct, 4), fmt=FMT_DECIMAL)
    _set_cell(ws, row, COL_ADV_EXC_USD, round(adv_exc_usd, 2))
    _set_cell(ws, row, COL_ADV_EXC_PCT, round(adv_exc_pct, 4), fmt=FMT_DECIMAL)

    if cum_pnl_usd is not None:
        _set_cell(ws, row, COL_CUM_PNL_USD, cum_pnl_usd)
    if cum_pnl_pct is not None:
        _set_cell(ws, row, COL_CUM_PNL_PCT, round(cum_pnl_pct, 4), fmt=FMT_DECIMAL)


def _parse_dt(dt_str):
    """Parse datetime string to Python datetime for proper Excel serial date."""
    if dt_str is None:
        return None
    import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(dt_str), fmt)
        except ValueError:
            pass
    return dt_str  # fallback: leave as string


def _exit_signal_label(exit_type: str, leg: str) -> str:
    """
    Convert engine exit_type to reference signal label.
    The V1 reference workbook labels EVERY exit with its order ID ("TP1"/"TP2"),
    including stop-outs and time exits — match that exactly.
    """
    return leg


def _build_signal_blocks(df: pd.DataFrame) -> list:
    """
    Convert tps_engine.trades_to_dataframe() output into signal blocks.

    The engine produces rows grouped by entry_time, in signal order (TP1 then TP2):
        Entry long  | TP1 leg
        Exit long   | TP1 leg
        Entry long  | TP2 leg
        Exit long   | TP2 leg

    We need to regroup into 4-row blocks:
        Entry TP1, Entry TP2, Exit TP1, Exit TP2

    Each block dict contains: entry, exit_tp1, exit_tp2
    where entry has: entry_price, entry_time_str
    and exits have: exit_price, exit_time_str, exit_type, fav_exc_pct, adv_exc_pct
    """
    records = df.to_dict("records")

    # Walk records in groups of 4: [entry_tp1, exit_tp1, entry_tp2, exit_tp2]
    blocks = []
    i = 0
    while i < len(records):
        remaining = len(records) - i
        if remaining >= 4:
            r0 = records[i]
            r1 = records[i + 1]
            r2 = records[i + 2]
            r3 = records[i + 3]
            if (r0.get("Type") == "Entry long" and r1.get("Type") == "Exit long" and
                    r2.get("Type") == "Entry long" and r3.get("Type") == "Exit long"):
                blocks.append(_make_block(r0, r1, r2, r3))
                i += 4
                continue
        if remaining >= 2:
            r0 = records[i]
            r1 = records[i + 1]
            if r0.get("Type") == "Entry long" and r1.get("Type") == "Exit long":
                # Partial: duplicate leg as both TP1 and TP2
                blocks.append(_make_block(r0, r1, r0, r1))
                i += 2
                continue
        i += 1  # skip malformed row

    return blocks


def _make_block(entry_r0, exit_r1, entry_r2, exit_r3) -> dict:
    """
    Create a block dict from the 4 raw rows.
    entry_r0 / entry_r2 are Entry long rows (TP1 and TP2 legs)
    exit_r1  / exit_r3  are Exit long rows (TP1 and TP2 legs)
    """
    def _exit_type_from_row(row):
        sig = row.get("Signal", "")
        if sig == "TP1":
            return "tp1"
        if sig == "TP2":
            return "tp2"
        return "time"  # Close entry(s) order Long or other

    entry_price = entry_r0.get("Price USD", 0)

    return {
        "entry": {
            "entry_price":    entry_price,
            "entry_time_str": entry_r0.get("Date and time", ""),
        },
        "exit_tp1": {
            "exit_price":     exit_r1.get("Price USD", entry_price),
            "exit_time_str":  exit_r1.get("Date and time", ""),
            "exit_type":      _exit_type_from_row(exit_r1),
            "fav_exc_pct":    exit_r1.get("Favorable excursion %", 0) or 0,
            "adv_exc_pct":    exit_r1.get("Adverse excursion %", 0) or 0,
        },
        "exit_tp2": {
            "exit_price":     exit_r3.get("Price USD", entry_price),
            "exit_time_str":  exit_r3.get("Date and time", ""),
            "exit_type":      _exit_type_from_row(exit_r3),
            "fav_exc_pct":    exit_r3.get("Favorable excursion %", 0) or 0,
            "adv_exc_pct":    exit_r3.get("Adverse excursion %", 0) or 0,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def export_to_excel(
    all_trades: dict,   # {ticker: df_trades} from run_ticker()
    cfg: dict,          # config dict (chart_tf, start_date, end_date, ...)
    output_path: str,   # where to save the .xlsx
) -> str:
    """
    Export TPS backtest results to Excel matching the reference V2 format.

    Parameters
    ----------
    all_trades : dict
        Mapping ticker -> DataFrame from tps_engine.run_ticker()[1].
    cfg : dict
        Config dict used for the backtest run (chart_tf, score_threshold, ...).
    output_path : str
        Full path to the output .xlsx file (created/overwritten).

    Returns
    -------
    str
        The resolved absolute path of the written file.
    """
    chart_tf = int(cfg.get("chart_tf", 195))

    # Determine ticker order: reference order first, then extras alphabetically
    available = set(all_trades.keys())
    ordered_tickers = [t for t in REFERENCE_TICKERS if t in available]
    extras = sorted(t for t in all_trades if t not in set(REFERENCE_TICKERS))
    ordered_tickers.extend(extras)

    wb = Workbook()
    # Remove default empty sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Write ticker sheets first, collecting computed stats for the summary
    stats_by_ticker = {}
    for ticker in ordered_tickers:
        df = all_trades.get(ticker)
        stats_by_ticker[ticker] = _write_ticker_sheet(wb, ticker, df)

    # Write summary sheet with computed values (inserted at position 0)
    _write_summary_sheet(wb, ordered_tickers, chart_tf, stats_by_ticker)

    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return str(out)
