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

import re
from pathlib import Path
from typing import Dict

import pandas as pd
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, numbers
from openpyxl.utils import get_column_letter

# ── Reference ticker order (Summary sheet columns B–J) ────────────────────────
REFERENCE_TICKERS = ["AAPL", "GOOGL", "NVDA", "TSLA", "AMZN", "MSFT", "AMD", "ORCL", "NFLX"]

# ── Number formats ─────────────────────────────────────────────────────────────
FMT_PCT          = "0.00%"
FMT_DECIMAL      = "0.00"
FMT_ACCOUNTING   = r'_(* #,##0.00_);_(* \(#,##0.00\);_(* "-"??_);_(@_)'
FMT_PCT3         = "0.000%"
FMT_DATETIME     = r'yyyy\-mm\-dd\ hh:mm'
FMT_GENERAL      = "General"

# ── Fonts ──────────────────────────────────────────────────────────────────────
FONT_NORMAL  = Font(name="Calibri", size=11)
FONT_BOLD    = Font(name="Calibri", size=11, bold=True)


# ══════════════════════════════════════════════════════════════════════════════
# HELPER UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _col_letter(col_idx: int) -> str:
    """1-based column index → Excel letter (A, B, … Z, AA, …)."""
    return get_column_letter(col_idx)


def _set_cell(ws, row: int, col: int, value, fmt: str = None, bold: bool = False):
    cell = ws.cell(row=row, column=col, value=value)
    if fmt:
        cell.number_format = fmt
    if bold:
        cell.font = FONT_BOLD
    else:
        cell.font = FONT_NORMAL
    return cell


def _auto_width(ws, min_width: int = 8, max_width: int = 30, overrides: dict = None):
    """Set column widths based on content; overrides is {col_letter: width}."""
    overrides = overrides or {}
    for col in ws.columns:
        letter = col[0].column_letter
        if letter in overrides:
            ws.column_dimensions[letter].width = overrides[letter]
            continue
        max_len = max((len(str(c.value or "")) for c in col), default=min_width)
        ws.column_dimensions[letter].width = min(max(max_len + 2, min_width), max_width)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY SHEET  ("195min Summary" or "{chart_tf}min Summary")
# ══════════════════════════════════════════════════════════════════════════════

def _write_summary_sheet(wb: Workbook, all_trades: Dict[str, pd.DataFrame],
                          tickers: list[str], chart_tf: int) -> None:
    """
    Build the Summary sheet with Excel formulas referencing individual ticker sheets.
    Layout matches the exact specification: 13 rows × 12 cols (A–L).
    """
    sheet_name = f"{chart_tf}min Summary"
    ws = wb.create_sheet(sheet_name, 0)  # insert at front

    # ── Row 1 headers ──────────────────────────────────────────────────────────
    # Col A: empty; B–K: tickers; K last ticker is "V2 ({chart_tf})avgs"
    # We'll put tickers in B..B+n-1, then the avgs column right after.
    n = len(tickers)
    avgs_col = n + 2  # col index of the "V2 avgs" column (1-based, A=1, B=2)

    # Col A row 1 empty
    _set_cell(ws, 1, 1, None)

    for i, ticker in enumerate(tickers):
        col = i + 2  # B = 2
        _set_cell(ws, 1, col, ticker, bold=True)

    # Avgs column header
    _set_cell(ws, 1, avgs_col, f"V2 ({chart_tf})avgs", bold=True)

    # ── Row 2–11: Metric rows ──────────────────────────────────────────────────
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

    for row_offset, label in enumerate(metric_labels):
        row = row_offset + 2  # rows 2..11
        _set_cell(ws, row, 1, label, bold=True)

    # ── Per-ticker formulas ────────────────────────────────────────────────────
    for i, ticker in enumerate(tickers):
        col = i + 2  # B=2, C=3 …
        cl = _col_letter(col)

        # Sanitize ticker name for sheet references (tickers with . may need quoting)
        # Sheet names with special chars need single-quoting in INDIRECT
        safe = ticker  # sheet name is same as ticker

        # Row 2: Count = MAX(A:A)/2
        _set_cell(ws, 2, col,
                  f"=MAX(INDIRECT(\"'\"&{cl}$1&\"'!A:A\"))/2",
                  fmt=FMT_ACCOUNTING)

        # Row 3: # Wins = COUNTIF(H:H, ">0")
        _set_cell(ws, 3, col,
                  f"=COUNTIF(INDIRECT({cl}$1&\"!H:H\"),\">0\")",
                  fmt=FMT_ACCOUNTING)

        # Row 4: Win Rate = B3/B2
        row2_ref = f"{cl}2"
        row3_ref = f"{cl}3"
        _set_cell(ws, 4, col,
                  f"=IFERROR({row3_ref}/{row2_ref},0)",
                  fmt=FMT_PCT)

        # Row 5: Avg Return %
        _set_cell(ws, 5, col,
                  f"=IFERROR(AVERAGE(INDIRECT({cl}$1&\"!H:H\")),0)",
                  fmt=FMT_PCT)

        # Row 6: Avg Win %
        _set_cell(ws, 6, col,
                  f"=IFERROR(AVERAGEIF(INDIRECT({cl}$1&\"!H:H\"),\">0\"),0)",
                  fmt=FMT_PCT)

        # Row 7: Avg Loss %
        _set_cell(ws, 7, col,
                  f"=IFERROR(AVERAGEIF(INDIRECT({cl}$1&\"!H:H\"),\"<0\"),0)",
                  fmt=FMT_PCT)

        # Row 8: Avg Max Return %
        _set_cell(ws, 8, col,
                  f"=IFERROR(AVERAGE(INDIRECT({cl}$1&\"!I:I\")),0)",
                  fmt=FMT_PCT)

        # Row 9: Win Rate (max)
        _set_cell(ws, 9, col,
                  f"=IFERROR(COUNTIF(INDIRECT({cl}$1&\"!I:I\"),\">0\")/COUNT(INDIRECT({cl}$1&\"!I:I\")),0)",
                  fmt=FMT_PCT)

        # Row 10: Expectancy = (WR * AvgWin) + (1-WR) * AvgLoss
        _set_cell(ws, 10, col,
                  f"=IFERROR(({cl}4*{cl}6+(1-{cl}4)*{cl}7),0)",
                  fmt=FMT_PCT)

        # Row 11: Avg Duration (days)
        _set_cell(ws, 11, col,
                  f"=IFERROR(AVERAGE(INDIRECT({cl}$1&\"!L:L\")),0)",
                  fmt=FMT_ACCOUNTING)

    # ── Avgs column (K in reference, but dynamically placed) ──────────────────
    avgs_cl = _col_letter(avgs_col)
    first_ticker_cl = "B"
    last_ticker_cl  = _col_letter(n + 1)  # last ticker col

    # Row 2: SUM
    _set_cell(ws, 2, avgs_col,
              f"=SUM({first_ticker_cl}2:{last_ticker_cl}2)",
              fmt=FMT_ACCOUNTING)

    # Row 3: SUM
    _set_cell(ws, 3, avgs_col,
              f"=SUM({first_ticker_cl}3:{last_ticker_cl}3)",
              fmt=FMT_ACCOUNTING)

    # Rows 4–10: AVERAGE
    avg_rows = [(4, FMT_PCT), (5, FMT_PCT), (6, FMT_PCT), (7, FMT_PCT),
                (8, FMT_PCT), (9, FMT_PCT), (10, FMT_PCT)]
    for row, fmt in avg_rows:
        _set_cell(ws, row, avgs_col,
                  f"=AVERAGE({first_ticker_cl}{row}:{last_ticker_cl}{row})",
                  fmt=fmt)

    # Row 11: AVERAGE (accounting)
    _set_cell(ws, 11, avgs_col,
              f"=AVERAGE({first_ticker_cl}11:{last_ticker_cl}11)",
              fmt=FMT_ACCOUNTING)

    # ── Row 12: Empty ──────────────────────────────────────────────────────────
    # (nothing to write)

    # ── Row 13: Footer ────────────────────────────────────────────────────────
    # Reference spec: I13="avg days in", J13=K11/2, K13=K10/J13, L13="exp / day"
    # We map those to columns relative to our layout:
    #   "I" = avgs_col - 2, "J" = avgs_col - 1, "K" = avgs_col, "L" = avgs_col + 1
    col_i13 = avgs_col - 2
    col_j13 = avgs_col - 1
    col_k13 = avgs_col
    col_l13 = avgs_col + 1

    _set_cell(ws, 13, col_i13, "avg days in")
    _set_cell(ws, 13, col_j13,
              f"={avgs_cl}11/2",
              fmt=FMT_ACCOUNTING)
    j13_cl = _col_letter(col_j13)
    _set_cell(ws, 13, col_k13,
              f"=IFERROR({avgs_cl}10/{j13_cl}13,0)",
              fmt=FMT_PCT3)
    _set_cell(ws, 13, col_l13, "exp / day")

    # ── Column widths ──────────────────────────────────────────────────────────
    _auto_width(ws, overrides={avgs_cl: 11.2})


# ══════════════════════════════════════════════════════════════════════════════
# TICKER SHEET
# ══════════════════════════════════════════════════════════════════════════════

# Column definitions in reference order (A=1 … T=20)
# Index = col number (1-based)
COL_TRADE_NUM      = 1   # A  Trade #
COL_TYPE           = 2   # B  Type
COL_DATETIME       = 3   # C  Date and time
COL_SIGNAL         = 4   # D  Signal
COL_PRICE          = 5   # E  Price USD
COL_SIZE_QTY       = 6   # F  Size (qty)
COL_SIZE_VAL       = 7   # G  Size (value)
COL_TOTAL_RETURN   = 8   # H  Total Return
COL_MAX_RETURN     = 9   # I  Max Return
COL_STATS_J        = 10  # J  (no header — stats)
COL_STATS_K        = 11  # K  (no header — stats)
COL_DURATION       = 12  # L  Duration
COL_NET_PNL_USD    = 13  # M  Net P&L USD
COL_NET_PNL_PCT    = 14  # N  Net P&L %
COL_FAV_EXC_USD    = 15  # O  Favorable excursion USD
COL_FAV_EXC_PCT    = 16  # P  Favorable excursion %
COL_ADV_EXC_USD    = 17  # Q  Adverse excursion USD
COL_ADV_EXC_PCT    = 18  # R  Adverse excursion %
COL_CUM_PNL_USD    = 19  # S  Cumulative P&L USD
COL_CUM_PNL_PCT    = 20  # T  Cumulative P&L %

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
    COL_STATS_J:      None,   # no header
    COL_STATS_K:      None,   # no header
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
    Write one ticker sheet with the exact 20-column layout from the reference.

    df must be the output of tps_engine.trades_to_dataframe() — which produces
    pairs of (Entry long / Exit long) rows per signal.

    The reference uses 4-row blocks per trade:
        Entry long TP1 (trade N)
        Entry long TP2 (trade N+1)
        Exit long TP1  (trade N, signal TP1)
        Exit long TP2  (trade N+1, signal TP2)

    tps_engine.trades_to_dataframe() already outputs rows in this order
    with sequentially numbered trade IDs. We use it directly.
    """
    ws = wb.create_sheet(ticker)

    # ── Row 1: Headers ─────────────────────────────────────────────────────────
    for col_idx, header in TICKER_HEADERS.items():
        if header is not None:
            _set_cell(ws, 1, col_idx, header, bold=True)

    # ── In-sheet aggregate stats in J2, K2, J4, K4 (no IFERROR, matching reference) ──
    ws.cell(row=2, column=COL_STATS_J).value  = "=COUNTIF(H:H,\">0\")/COUNT(H:H)"
    ws.cell(row=2, column=COL_STATS_J).number_format = FMT_PCT
    ws.cell(row=2, column=COL_STATS_K).value  = "=COUNTIF(I:I,\">0\")/COUNT(I:I)"
    ws.cell(row=2, column=COL_STATS_K).number_format = FMT_PCT
    ws.cell(row=4, column=COL_STATS_J).value  = "=AVERAGE(H:H)"
    ws.cell(row=4, column=COL_STATS_J).number_format = FMT_PCT
    ws.cell(row=4, column=COL_STATS_K).value  = "=AVERAGE(I:I)"
    ws.cell(row=4, column=COL_STATS_K).number_format = FMT_PCT

    if df is None or df.empty:
        ws.freeze_panes = "A2"
        return

    # ── Rebuild df into exact 4-row-block structure ────────────────────────────
    # tps_engine.trades_to_dataframe already produces:
    #   Trade N  Entry long (TP1 leg)
    #   Trade N  Exit long  (TP1 leg)
    #   Trade N+1 Entry long (TP2 leg)
    #   Trade N+1 Exit long  (TP2 leg)
    # BUT the reference wants them reordered as:
    #   Trade N   Entry long TP1
    #   Trade N+1 Entry long TP2
    #   Trade N   Exit long TP1
    #   Trade N+1 Exit long TP2
    # We need to reconstruct this 4-row block order.

    rows_out = _build_4row_blocks(df)

    # ── Write data rows ────────────────────────────────────────────────────────
    for i, row_data in enumerate(rows_out):
        excel_row = i + 2  # row 1 = header

        # Standard columns with simple values
        _set_cell(ws, excel_row, COL_TRADE_NUM, row_data.get("Trade #"))
        _set_cell(ws, excel_row, COL_TYPE,      row_data.get("Type"))

        # Date/time — write as string (already formatted)
        dt_val = row_data.get("Date and time")
        _set_cell(ws, excel_row, COL_DATETIME, dt_val, fmt=FMT_DATETIME)

        _set_cell(ws, excel_row, COL_SIGNAL,   row_data.get("Signal"))
        _set_cell(ws, excel_row, COL_PRICE,    row_data.get("Price USD"))
        _set_cell(ws, excel_row, COL_SIZE_QTY, row_data.get("Size (qty)"))
        _set_cell(ws, excel_row, COL_SIZE_VAL, row_data.get("Size (value)"))

        # H, I, L — reference pattern: every row from row 3 onward gets formulas
        # (row 2 = first entry, never gets formulas)
        # Formula references row n and row n-2 (two rows back)
        if excel_row >= 3:
            n = excel_row
            h_formula = (
                f'=IF(AND(D{n}<>"TP1",B{n}="Exit long",A{n}=A{n-2}),'
                f'AVERAGE(N{n-1}:N{n})/100,"")'
            )
            i_formula = (
                f'=IF(AND(D{n}<>"TP1",B{n}="Exit long",A{n}=A{n-2}),'
                f'((E{n-2}+O{n})/E{n-2})-1,"")'
            )
            l_formula = f'=IF(I{n}<>"",_xlfn.DAYS(C{n},C{n-2}),"")'

            c = ws.cell(row=excel_row, column=COL_TOTAL_RETURN, value=h_formula)
            c.number_format = FMT_PCT
            c = ws.cell(row=excel_row, column=COL_MAX_RETURN, value=i_formula)
            c.number_format = FMT_PCT
            c = ws.cell(row=excel_row, column=COL_DURATION, value=l_formula)
            c.number_format = FMT_DECIMAL

        # M–T: data values
        _set_cell(ws, excel_row, COL_NET_PNL_USD,  row_data.get("Net P&L USD"))
        _set_cell(ws, excel_row, COL_NET_PNL_PCT,  row_data.get("Net P&L %"),     fmt=FMT_DECIMAL)
        _set_cell(ws, excel_row, COL_FAV_EXC_USD,  row_data.get("Favorable excursion USD"))
        _set_cell(ws, excel_row, COL_FAV_EXC_PCT,  row_data.get("Favorable excursion %"),  fmt=FMT_DECIMAL)
        _set_cell(ws, excel_row, COL_ADV_EXC_USD,  row_data.get("Adverse excursion USD"))
        _set_cell(ws, excel_row, COL_ADV_EXC_PCT,  row_data.get("Adverse excursion %"),    fmt=FMT_DECIMAL)
        _set_cell(ws, excel_row, COL_CUM_PNL_USD,  row_data.get("Cumulative P&L USD"))
        _set_cell(ws, excel_row, COL_CUM_PNL_PCT,  row_data.get("Cumulative P&L %"),       fmt=FMT_DECIMAL)

    # ── Format header row datetime column ─────────────────────────────────────
    ws.cell(row=1, column=COL_TOTAL_RETURN).number_format = FMT_PCT

    # ── Freeze header row ─────────────────────────────────────────────────────
    ws.freeze_panes = "A2"

    # ── Column widths ─────────────────────────────────────────────────────────
    _auto_width(ws, min_width=6)


def _build_4row_blocks(df: pd.DataFrame) -> list[dict]:
    """
    Convert tps_engine output (alternating entry/exit pairs per trade leg)
    into reference 4-row blocks:
        [Entry TP1, Entry TP2, Exit TP1, Exit TP2]
    and inject Excel formula strings for H, I, L columns.
    """
    # The engine produces rows in this pattern per trade group:
    #   trade N   Entry long   (TP1 leg)
    #   trade N   Exit long    (TP1 leg)
    #   trade N+1 Entry long   (TP2 leg)
    #   trade N+1 Exit long    (TP2 leg)
    # We need to group by entry_time and reorder.

    # Convert to list of dicts
    records = df.to_dict("records")

    # Group into 4-row blocks by detecting Entry/Exit pairs
    # Walk through and collect (entry_tp1, exit_tp1, entry_tp2, exit_tp2) quads
    blocks = []
    i = 0
    while i < len(records):
        # Expect: entry_tp1, exit_tp1, entry_tp2, exit_tp2
        if i + 3 < len(records):
            e1 = records[i]
            x1 = records[i + 1]
            e2 = records[i + 2]
            x2 = records[i + 3]
            if (e1.get("Type") == "Entry long" and x1.get("Type") == "Exit long" and
                    e2.get("Type") == "Entry long" and x2.get("Type") == "Exit long"):
                blocks.append((e1, x1, e2, x2))
                i += 4
                continue
        # Fallback: take whatever we have
        if i + 1 < len(records):
            e1 = records[i]
            x1 = records[i + 1]
            blocks.append((e1, x1, None, None))
            i += 2
        else:
            break

    # Build output rows — formula injection is handled in _write_ticker_sheet
    # based on excel row number, so just return stripped dicts in order.
    out = []
    for k, block in enumerate(blocks):
        e1, x1, e2, x2 = block

        out.append(_strip_row(e1))

        if e2 is not None:
            out.append(_strip_row(e2))

        out.append(_strip_row(x1))

        if x2 is not None:
            out.append(_strip_row(x2))
        else:
            # Partial block (only TP1 leg available)
            out.append(_strip_row(x1))

    return out


def _strip_row(row: dict) -> dict:
    """Return row dict without Total Return / Max Return / Duration
    (those are replaced by formula strings)."""
    keep = {
        "Trade #", "Type", "Date and time", "Signal",
        "Price USD", "Size (qty)", "Size (value)",
        "Net P&L USD", "Net P&L %",
        "Favorable excursion USD", "Favorable excursion %",
        "Adverse excursion USD", "Adverse excursion %",
        "Cumulative P&L USD", "Cumulative P&L %",
    }
    return {k: v for k, v in row.items() if k in keep}


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def export_to_excel(
    all_trades: dict,   # {ticker: df_trades}  from run_ticker()
    cfg: dict,          # config dict (chart_tf, start_date, end_date, …)
    output_path: str,   # where to save the .xlsx
) -> str:
    """
    Export TPS backtest results to Excel matching the reference V2 format.

    Parameters
    ----------
    all_trades : dict
        Mapping of ticker → DataFrame produced by tps_engine.run_ticker()[1].
        Keys are ticker strings; values are the formatted trade DataFrames.
    cfg : dict
        The config dict used for the backtest run (chart_tf, score_threshold, …).
    output_path : str
        Full path to the output .xlsx file (created/overwritten).

    Returns
    -------
    str
        The resolved absolute path of the written file.
    """
    chart_tf = int(cfg.get("chart_tf", 195))

    # Determine ticker order: reference tickers first (if present), then others
    available = set(all_trades.keys())
    ordered_tickers = [t for t in REFERENCE_TICKERS if t in available]
    extras = [t for t in all_trades if t not in set(REFERENCE_TICKERS)]
    ordered_tickers.extend(sorted(extras))

    wb = Workbook()
    # Remove default empty sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # ── Write individual ticker sheets first ──────────────────────────────────
    for ticker in ordered_tickers:
        df = all_trades.get(ticker)
        _write_ticker_sheet(wb, ticker, df)

    # ── Write summary sheet (inserted at position 0) ─────────────────────────
    _write_summary_sheet(wb, all_trades, ordered_tickers, chart_tf)

    # ── Save ──────────────────────────────────────────────────────────────────
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return str(out)
