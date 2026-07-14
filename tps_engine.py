"""
TPS Long Strategy v3 — Backtest Engine
=======================================
Pure Python/pandas replication of the "TPS Long Strategy v3 (78m, RSI momentum
exit)" Pine Script.  Accepts pre-fetched OHLCV DataFrames (from any source) and
returns trade results in the same format as the TradingView backtest export.

What changed from v2 → v3 (exit model only; scoring/entry are UNCHANGED):
  - SINGLE contract per signal (pyramiding=0, qty=1). No more TP1/TP2 dual legs.
  - Exits are ONLY:
      1. Hard stop at entry − slATR × ATR(atr_len), with ATR fixed at the entry
         bar. Fills intrabar at the stop level (or at the open on a gap-down).
      2. RSI(14) momentum-exhaustion exit: once the trade is in profit
         (close > entry), close the moment RSI(rsi_length) drops below
         rsi_exit_threshold (default 60). Fills on the bar CLOSE.
  - The old fixed 2.5/3.5-ATR targets, the chandelier trail, and the runner /
    time-exit logic are all removed.

Key design notes:
  - Base data is 1-MINUTE bars (the TradingView backtests were run on 1-min
    resolution). 78m / 30m / 15m bars are built from the 1-min base so the
    78-minute grid (which is NOT a multiple of 15m) is exact.
  - Mirrors process_orders_on_close=true: market fills (entry, RSI exit) happen
    at bar CLOSE. Stop orders fill intrabar at the stop price.
  - Multi-TF alignment uses merge_asof on bar CLOSE times (no look-ahead).
  - All timestamps are UTC; market-hour filtering is done before this engine.
  - Pine-exact indicator math:
      * ta.stdev  → population std (ddof=0)  — used by BB and the squeeze.
      * ta.atr    → Wilder's RMA of True Range (NOT a simple average).
      * ta.rsi    → Wilder's RMA of gains / losses.
      * squeeze KC width  → ta.sma(ta.tr, sqzLen)  (explicit SMA per Makit0).
"""

import numpy as np
import pandas as pd
import pytz
from datetime import datetime, timedelta

ET = pytz.timezone("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# DEFAULT CONFIG  (matches the v3 Pine Script inputs)
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # ── Strategy params
    "score_threshold": 65.0,
    "sqz_len": 20,
    "min_sqz_bars": 3,
    "max_sqz_bars": 20,
    "release_bars": 3,
    "bb_len": 10,
    "bb_dev": 1.5,
    "atr_len": 10,

    # ── v3 exits
    "sl_atr": 2.0,                 # hard stop = entry − sl_atr × ATR(entry bar)
    "rsi_length": 14,              # RSI period for the momentum-exhaustion exit
    "rsi_exit_threshold": 60.0,    # exit (when in profit) once RSI drops below this

    # ── Data / timeframe
    "base_tf_min": 1,              # base bars fed to the engine are 1-minute
    "chart_tf": 78,                # chart timeframe in minutes (78 or 195)

    # ── Fill semantics (v3 / TradingView defaults)
    #   Entry + RSI exit are market orders → fill on the bar CLOSE.
    #   The hard stop is a stop order → fills intrabar at the stop level.
    "stop_trigger": "intrabar",    # "intrabar" | "close"

    # ── Trend score weights (sum = 50)
    "trend_pts_d":  20.0,
    "trend_pts_78": 15.0,
    "trend_pts_30": 10.0,
    "trend_pts_15":  5.0,
    # ── Squeeze score weights (sum = 50; per-timeframe totals; Daily removed)
    "sqz_pts_78": 10.0,
    "sqz_pts_30": 15.0,
    "sqz_pts_15": 25.0,
}


# ──────────────────────────────────────────────────────────────────────────────
# BAR RESAMPLING  (1m → 15m / 30m / 78m / 195m, aligned to 9:30 AM ET)
# ──────────────────────────────────────────────────────────────────────────────

def filter_market_hours(df_utc: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars that OPEN during regular market hours (9:30–16:00 ET).

    With 1-minute base data this keeps every RTH minute (9:30 … 15:59). The
    16:00 upper bound is exclusive so the (nonexistent) 16:00 open is dropped.
    """
    idx_et = df_utc.index.tz_convert(ET)
    time_of_day = idx_et.time
    lo = pd.Timestamp("09:30", tz=ET).time()
    hi = pd.Timestamp("16:00", tz=ET).time()
    return df_utc[(time_of_day >= lo) & (time_of_day < hi)]


def resample_ohlcv(df_base: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    """
    Resample market-hours-filtered base bars (typically 1-minute) to
    target_minutes bars, anchored at 9:30 AM ET each day.

    Bars are labeled by their OPEN time (UTC), matching TradingView. Because
    binning is by minutes-since-open, the 78-minute grid (9:30, 10:48, 12:06,
    13:24, 14:42) is exact even though 78 is not a multiple of 15.

    Returns DataFrame indexed by bar OPEN TIME (UTC), columns: o, h, l, c, v.
    """
    if df_base.empty:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])

    df_et = df_base.copy()
    df_et.index = df_et.index.tz_convert(ET)

    idx = df_et.index
    minutes_since_open = idx.hour * 60 + idx.minute - (9 * 60 + 30)
    # Guard: only bars inside the 390-minute RTH window (defensive; caller filters)
    mask = (minutes_since_open >= 0) & (minutes_since_open < 390)
    df_et = df_et[mask]
    if df_et.empty:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
    idx = df_et.index
    minutes_since_open = idx.hour * 60 + idx.minute - (9 * 60 + 30)

    bin_index = (minutes_since_open // target_minutes).astype(int)
    bin_open_min = (9 * 60 + 30) + bin_index * target_minutes

    # Build bin-open timestamps with WALL-CLOCK arithmetic (drop tz → add minutes
    # → re-localize) so DST-transition days aren't shifted by an hour. RTH times
    # (9:30–16:00) never fall in the 2–3 AM DST gap, so localization is unambiguous.
    naive_midnight = idx.tz_localize(None).normalize()
    naive_bin_open = naive_midnight + pd.to_timedelta(bin_open_min, unit="m")
    bin_open = pd.DatetimeIndex(naive_bin_open).tz_localize(ET)

    grp = df_et.groupby(bin_open)
    out = pd.DataFrame({
        "o": grp["o"].first(),
        "h": grp["h"].max(),
        "l": grp["l"].min(),
        "c": grp["c"].last(),
        "v": grp["v"].sum(),
    })
    out.index = out.index.tz_convert(pytz.utc)
    out.index.name = "t"
    return out.sort_index()


# ──────────────────────────────────────────────────────────────────────────────
# INDICATOR CALCULATIONS
# ──────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def stdev_pop(series: pd.Series, n: int) -> pd.Series:
    """Population standard deviation (ddof=0), matching Pine's ta.stdev default."""
    return series.rolling(n).std(ddof=0)


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-Cp|, |L-Cp|). First bar = H-L (no prev close)."""
    prev_c = df["c"].shift(1)
    return pd.concat([
        df["h"] - df["l"],
        (df["h"] - prev_c).abs(),
        (df["l"] - prev_c).abs(),
    ], axis=1).max(axis=1)


def wilder_rma(values, n: int) -> np.ndarray:
    """
    Wilder's RMA (ta.rma): seed with the SMA of the first n finite values, then
    rma[i] = (rma[i-1] * (n-1) + x[i]) / n. Leading NaNs (e.g. from a diff/shift)
    are skipped when finding the seed, matching Pine's na-handling.
    """
    v = np.asarray(values, dtype=float)
    out = np.full(v.shape, np.nan)
    finite = np.where(np.isfinite(v))[0]
    if len(finite) < n:
        return out
    start = finite[0]
    seed_idx = start + n - 1
    if seed_idx >= len(v):
        return out
    out[seed_idx] = np.mean(v[start:seed_idx + 1])
    for i in range(seed_idx + 1, len(v)):
        out[i] = (out[i - 1] * (n - 1) + v[i]) / n
    return out


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder ATR = ta.rma(true_range, n), matching Pine's ta.atr."""
    tr = true_range(df)
    return pd.Series(wilder_rma(tr.to_numpy(), n), index=df.index)


def rsi(series: pd.Series, n: int) -> pd.Series:
    """Wilder RSI (ta.rsi): RMA of gains / RMA of losses."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_rma(gain.to_numpy(), n)
    avg_loss = wilder_rma(loss.to_numpy(), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 → RSI = 100 (no losses in the window)
    out = np.where((avg_loss == 0) & np.isfinite(avg_gain), 100.0, out)
    return pd.Series(out, index=series.index)


def linreg_last(y_arr: np.ndarray, length: int) -> float:
    """Linear regression value at the last point (bar 0 = offset 0 in Pine)."""
    x = np.arange(length, dtype=float)
    coeffs = np.polyfit(x, y_arr, 1)
    return float(np.polyval(coeffs, length - 1))


def rolling_linreg(series: pd.Series, length: int) -> pd.Series:
    """Rolling linear regression value at each bar's last point."""
    return series.rolling(length).apply(
        lambda y: linreg_last(y, length), raw=True
    )


def compute_bb(df: pd.DataFrame, length: int, mult: float):
    """Bollinger Bands using SMA basis + population stdev (Pine ta.bb)."""
    basis = sma(df["c"], length)
    dev = stdev_pop(df["c"], length)
    return basis + dev * mult, basis - dev * mult


def compute_squeeze_states(df: pd.DataFrame, sqz_len: int):
    """
    Reproduce Makit0 squeeze state logic exactly.

    BB uses population stdev (ta.stdev); KC width uses ta.sma(ta.tr, sqzLen).

    Returns DataFrame with boolean columns:
      sqz_wide, sqz_normal, sqz_narrow, sqz_off, no_sqz, sqz_active, sqz_tight
    """
    ma = sma(df["c"], sqz_len)
    dev_bb = stdev_pop(df["c"], sqz_len)
    dev_kc = true_range(df).rolling(sqz_len).mean()   # SMA of TR (Makit0)

    up_bb = ma + dev_bb * 2.0
    lo_bb = ma - dev_bb * 2.0

    up_kc_wide   = ma + dev_kc * 2.0
    lo_kc_wide   = ma - dev_kc * 2.0
    up_kc_normal = ma + dev_kc * 1.5
    lo_kc_normal = ma - dev_kc * 1.5
    up_kc_narrow = ma + dev_kc * 1.0
    lo_kc_narrow = ma - dev_kc * 1.0

    sqz_on_wide   = (lo_bb >= lo_kc_wide)   & (up_bb <= up_kc_wide)
    sqz_on_normal = (lo_bb >= lo_kc_normal) & (up_bb <= up_kc_normal)
    sqz_on_narrow = (lo_bb >= lo_kc_narrow) & (up_bb <= up_kc_narrow)
    sqz_off_wide  = (lo_bb < lo_kc_wide)    & (up_bb > up_kc_wide)
    no_sqz        = ~sqz_on_wide & ~sqz_off_wide

    sqz_active = sqz_on_wide | sqz_on_normal | sqz_on_narrow
    sqz_tight  = sqz_on_normal | sqz_on_narrow

    return pd.DataFrame({
        "sqz_wide":   sqz_on_wide,
        "sqz_normal": sqz_on_normal,
        "sqz_narrow": sqz_on_narrow,
        "sqz_off":    sqz_off_wide,
        "no_sqz":     no_sqz,
        "sqz_active": sqz_active,
        "sqz_tight":  sqz_tight,
    }, index=df.index)


def compute_sqz_momentum(df: pd.DataFrame, sqz_len: int) -> pd.Series:
    """
    Makit0 momentum oscillator: linreg(close - d, sqzLen, 0)
    where d = avg(avg(highest_high, lowest_low), sma(close, sqzLen))
    """
    highest_high = df["h"].rolling(sqz_len).max()
    lowest_low   = df["l"].rolling(sqz_len).min()
    c_sma        = sma(df["c"], sqz_len)
    d            = ((highest_high + lowest_low) / 2 + c_sma) / 2
    return rolling_linreg(df["c"] - d, sqz_len)


def compute_sqz_count(sqz_active: pd.Series) -> pd.Series:
    """Consecutive bars in active squeeze (resets to 0 on inactive bar)."""
    count = pd.Series(0, index=sqz_active.index, dtype=int)
    active = sqz_active.to_numpy()
    vals = np.zeros(len(active), dtype=int)
    for i in range(1, len(active)):
        vals[i] = vals[i - 1] + 1 if active[i] else 0
    count[:] = vals
    return count


def add_tps_signals(df: pd.DataFrame, sqz_len: int, release_bars: int,
                    min_sqz_bars: int, max_sqz_bars: int) -> pd.DataFrame:
    """
    Adds all signal columns needed for TPS scoring to a DataFrame:
      trend_ok, sqz_tight, sqz_active_or_recent, sqz_bars_ok, sqz_mom_rise2
    """
    d = df.copy()

    # Trend: EMA 8 > EMA 21
    d["trend_ok"] = ema(d["c"], 8) > ema(d["c"], 21)

    # Squeeze states
    sqz_df = compute_squeeze_states(d, sqz_len)
    d["sqz_active"] = sqz_df["sqz_active"]
    d["sqz_tight"]  = sqz_df["sqz_tight"]

    # "active_or_recent" = active now OR was active within the last release_bars bars
    d["sqz_active_or_recent"] = (
        d["sqz_active"].rolling(release_bars + 1, min_periods=1).max().astype(bool)
    )

    # Count of consecutive active squeeze bars, gated to [min, max]
    sqz_count = compute_sqz_count(d["sqz_active"])
    d["sqz_bars_ok"] = (sqz_count >= min_sqz_bars) & (sqz_count <= max_sqz_bars)

    # Momentum rising 2 bars (mom > mom[1] and mom[1] > mom[2])
    mom = compute_sqz_momentum(d, sqz_len)
    d["sqz_mom_rise2"] = (mom > mom.shift(1)) & (mom.shift(1) > mom.shift(2))

    return d


# ──────────────────────────────────────────────────────────────────────────────
# MULTI-TIMEFRAME ALIGNMENT
# ──────────────────────────────────────────────────────────────────────────────

def align_tf(df_chart: pd.DataFrame, df_lower: pd.DataFrame,
             prefix: str, lower_tf_min: int) -> pd.DataFrame:
    """
    Left-join lower-TF signal columns onto the chart DataFrame.
    For each chart bar, use the most recently COMPLETED lower-TF bar.

    "Completed" means: lower_bar_close_time <= chart_bar_close_time
    bar_close_time = bar_open_time + bar_duration.  Uses merge_asof (backward).
    """
    chart_close = df_chart.index + pd.Timedelta(minutes=int(df_chart.attrs.get("tf_min", 78)))
    lower_close = df_lower.index + pd.Timedelta(minutes=lower_tf_min)

    left = df_chart.copy()
    left["_close_t"] = chart_close

    cols = [c for c in df_lower.columns if c in
            ("trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2")]
    right = df_lower[cols].copy()
    right.index = lower_close
    right = right.reset_index().rename(columns={"index": "_lower_close_t", "t": "_lower_close_t"})
    right.columns = [f"{prefix}_{c}" if c != "_lower_close_t" else c for c in right.columns]

    merged = pd.merge_asof(
        left.reset_index().rename(columns={"t": "_t"}).sort_values("_close_t"),
        right.sort_values("_lower_close_t"),
        left_on="_close_t",
        right_on="_lower_close_t",
        direction="backward",
    ).set_index("_t")
    merged.index.name = "t"
    merged = merged.drop(columns=["_close_t", "_lower_close_t"], errors="ignore")
    return merged


def align_daily(df_chart: pd.DataFrame, df_daily: pd.DataFrame,
                prefix: str = "d") -> pd.DataFrame:
    """
    Join daily trend signals onto chart bars. Each chart bar uses the PREVIOUS
    trading day's completed daily bar (lookahead_off behavior for the daily TF).
    """
    chart_et = df_chart.index.tz_convert(ET)
    chart_dates = pd.Series(chart_et.date, index=df_chart.index)

    daily_et = df_daily.index.tz_convert(ET) if df_daily.index.tz else df_daily.index
    df_daily_dated = df_daily.copy()
    df_daily_dated.index = pd.DatetimeIndex([pd.Timestamp(d, tz=ET) for d in daily_et.date])
    df_daily_dated = df_daily_dated[~df_daily_dated.index.duplicated(keep="last")]
    df_daily_dated = df_daily_dated.sort_index()

    daily_signals = df_daily_dated[["trend_ok"]].copy()
    daily_signals.index = daily_signals.index.date

    def lookup_daily_trend(bar_date):
        idx = daily_signals.index
        mask = idx < bar_date
        if not mask.any():
            return False
        return bool(daily_signals.loc[idx[mask][-1], "trend_ok"])

    df_out = df_chart.copy()
    df_out[f"{prefix}_trend_ok"] = chart_dates.map(lookup_daily_trend)
    return df_out


# ──────────────────────────────────────────────────────────────────────────────
# TPS SCORE COMPUTATION  (unchanged from v2 — scoring is identical in v3)
# ──────────────────────────────────────────────────────────────────────────────

def compute_tps_score(df_chart: pd.DataFrame, config: dict) -> pd.Series:
    """Compute the TPS score (0-100) for each bar."""
    chart_tf  = int(config["chart_tf"])
    chart_pfx = f"c{chart_tf}"

    p_d   = float(config["trend_pts_d"])
    p_78  = float(config["trend_pts_78"])
    p_30  = float(config["trend_pts_30"])
    p_15  = float(config["trend_pts_15"])
    sp_78 = float(config["sqz_pts_78"])
    sp_30 = float(config["sqz_pts_30"])
    sp_15 = float(config["sqz_pts_15"])

    c = df_chart

    trend_score = (
        c["d_trend_ok"].astype(float)                    * p_d  +
        c[f"{chart_pfx}_trend_ok"].astype(float)         * p_78 +
        c["c30_trend_ok"].astype(float)                  * p_30 +
        c["c15_trend_ok"].astype(float)                  * p_15
    )

    def sqz_score_for_tf(prefix, pts):
        part = pts / 3.0
        ar = c[f"{prefix}_sqz_active_or_recent"]
        return (
            c[f"{prefix}_sqz_tight"].astype(float) * part +
            (ar & c[f"{prefix}_sqz_bars_ok"]).astype(float) * part +
            (ar & c[f"{prefix}_sqz_mom_rise2"]).astype(float) * part
        )

    sqz_score = (
        sqz_score_for_tf(chart_pfx, sp_78) +
        sqz_score_for_tf("c30", sp_30) +
        sqz_score_for_tf("c15", sp_15)
    )

    return (trend_score + sqz_score).rename("tps_score")


# ──────────────────────────────────────────────────────────────────────────────
# BB ENTRY TRIGGER (on chart TF)
# ──────────────────────────────────────────────────────────────────────────────

def compute_entry_trigger(df: pd.DataFrame, bb_len: int, bb_dev: float) -> pd.Series:
    """close > BB Upper (EMA basis + population stdev, matching the Pine entry)."""
    bb_basis = ema(df["c"], bb_len)
    bb_std   = stdev_pop(df["c"], bb_len)
    bb_upper = bb_basis + bb_std * bb_dev
    return (df["c"] > bb_upper).rename("bb_break")


# ──────────────────────────────────────────────────────────────────────────────
# BACKTEST SIMULATION  (v3: single contract, hard stop + RSI momentum exit)
# ──────────────────────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, config: dict) -> list:
    """
    Simulate the TPS Long Strategy v3 on a scored + triggered DataFrame.

    Required columns: o, h, l, c, v, tps_score, bb_break, atr_{n}, rsi_{n}

    Semantics (process_orders_on_close=true):
      - Entry fires on the bar where score ≥ threshold AND close > BB upper AND
        flat → fill at that bar's CLOSE. entry_atr is fixed at the entry bar.
      - From the NEXT bar onward, each bar checks (in order):
          1. Hard stop: if low ≤ stop → exit at stop level ("Stop").
             (gap-down: if the bar OPENS below the stop, fill at the open.)
          2. RSI exit: if in profit (close > entry) and RSI < threshold → exit at
             CLOSE ("RSI Exit").
      - A position still open at the end of data is closed at the last bar's
        CLOSE and labeled "Open" (matches TradingView's open-trade row).

    Returns a list of trade dicts (one per signal).
    """
    score_threshold = float(config["score_threshold"])
    sl_atr  = float(config["sl_atr"])
    rsi_exit_threshold = float(config["rsi_exit_threshold"])
    stop_trig = str(config.get("stop_trigger", "intrabar"))

    atr_col = f"atr_{config['atr_len']}"
    rsi_col = f"rsi_{config['rsi_length']}"

    trades = []

    in_pos      = False
    entry_price = None
    entry_atr   = None
    entry_time  = None
    stop_price  = None
    run_high    = None   # intrabar favorable excursion tracker
    run_low     = None   # intrabar adverse excursion tracker

    bars = list(df.itertuples())
    n = len(bars)

    for idx in range(n):
        bar = bars[idx]
        t       = bar.Index
        c_open  = bar.o
        c_high  = bar.h
        c_low   = bar.l
        c_close = bar.c
        c_atr   = getattr(bar, atr_col, np.nan)
        c_rsi   = getattr(bar, rsi_col, np.nan)

        if not in_pos:
            sig = (getattr(bar, "tps_score", 0) >= score_threshold
                   and getattr(bar, "bb_break", False))
            if sig and not np.isnan(c_atr):
                in_pos      = True
                entry_price = c_close
                entry_atr   = c_atr
                entry_time  = t
                stop_price  = entry_price - sl_atr * entry_atr
                run_high    = entry_price
                run_low     = entry_price
            continue

        # ── In a position: update excursion trackers (intrabar high/low) ──
        run_high = max(run_high, c_high)
        run_low  = min(run_low,  c_low)
        fav = run_high - entry_price   # favorable excursion USD (>= 0)
        adv = run_low  - entry_price   # adverse   excursion USD (<= 0)

        # 1. Hard stop (intrabar). Gap-down fills at the open.
        hit_stop = (c_low <= stop_price) if stop_trig == "intrabar" else (c_close <= stop_price)
        if hit_stop:
            fill = min(c_open, stop_price) if stop_trig == "intrabar" else c_close
            trades.append(_make_trade(
                exit_type="Stop", entry_time=entry_time, exit_time=t,
                entry_price=entry_price, exit_price=fill,
                fav_exc=fav, adv_exc=adv))
            in_pos = False
            continue

        # 2. RSI momentum-exhaustion exit (on close, only when in profit).
        in_profit = c_close > entry_price
        if in_profit and not np.isnan(c_rsi) and c_rsi < rsi_exit_threshold:
            trades.append(_make_trade(
                exit_type="RSI Exit", entry_time=entry_time, exit_time=t,
                entry_price=entry_price, exit_price=c_close,
                fav_exc=fav, adv_exc=adv))
            in_pos = False
            continue

    # ── Position still open at the end of data → mark-to-market as "Open"
    if in_pos:
        last = bars[-1]
        run_high = max(run_high, last.h)
        run_low  = min(run_low,  last.l)
        trades.append(_make_trade(
            exit_type="Open", entry_time=entry_time, exit_time=last.Index,
            entry_price=entry_price, exit_price=last.c,
            fav_exc=run_high - entry_price, adv_exc=run_low - entry_price))

    return trades


def _make_trade(exit_type, entry_time, exit_time,
                entry_price, exit_price, fav_exc, adv_exc):
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    fav_pct  = fav_exc / entry_price * 100
    adv_pct  = adv_exc / entry_price * 100
    duration = (exit_time - entry_time).total_seconds() / 86400

    return {
        "entry_time":   entry_time,
        "exit_time":    exit_time,
        "exit_type":    exit_type,       # "RSI Exit" | "Stop" | "Open"
        "entry_price":  round(entry_price, 4),
        "exit_price":   round(exit_price, 4),
        "pnl_pct":      round(pnl_pct, 4),
        "fav_exc_pct":  round(fav_pct, 4),
        "adv_exc_pct":  round(adv_pct, 4),
        "duration_days": round(duration, 4),
    }


# ──────────────────────────────────────────────────────────────────────────────
# RESULTS FORMATTING  (v3: one entry row + one exit row per signal)
# ──────────────────────────────────────────────────────────────────────────────

def trades_to_dataframe(trades: list, ticker: str) -> pd.DataFrame:
    """
    Convert raw trade list to a DataFrame. Each signal produces TWO rows:
        Entry long  | Signal="Long"
        Exit long   | Signal="RSI Exit" | "Stop" | "Open"
    Column names are kept stable for the app / exporter consumers.
    """
    if not trades:
        return pd.DataFrame()

    trades = sorted(trades, key=lambda x: x["entry_time"])
    rows = []
    cum_usd = 0.0
    cum_pct = 0.0

    for i, t in enumerate(trades, start=1):
        net_usd = round((t["exit_price"] - t["entry_price"]) * 1, 2)
        cum_usd += net_usd
        cum_pct += t["pnl_pct"]
        fav_usd = round(t["fav_exc_pct"] / 100 * t["entry_price"], 2)
        adv_usd = round(t["adv_exc_pct"] / 100 * t["entry_price"], 2)

        # Entry row
        rows.append({
            "Trade #":               i,
            "Type":                  "Entry long",
            "Date and time":         t["entry_time"].tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S"),
            "Signal":                "Long",
            "Price USD":             t["entry_price"],
            "Size (qty)":            1,
            "Size (value)":          t["entry_price"],
            "Total Return":          None,
            "Max Return":            None,
            "Duration":              None,
            "Net P&L USD":           net_usd,
            "Net P&L %":             t["pnl_pct"],
            "Favorable excursion USD": fav_usd,
            "Favorable excursion %":   t["fav_exc_pct"],
            "Adverse excursion USD":   adv_usd,
            "Adverse excursion %":     t["adv_exc_pct"],
            "Cumulative P&L USD":      None,
            "Cumulative P&L %":        None,
        })
        # Exit row
        rows.append({
            "Trade #":               i,
            "Type":                  "Exit long",
            "Date and time":         t["exit_time"].tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S"),
            "Signal":                t["exit_type"],
            "Price USD":             t["exit_price"],
            "Size (qty)":            1,
            "Size (value)":          t["entry_price"],
            "Total Return":          t["pnl_pct"] / 100,
            "Max Return":            t["fav_exc_pct"] / 100,
            "Duration":              t["duration_days"],
            "Net P&L USD":           net_usd,
            "Net P&L %":             t["pnl_pct"],
            "Favorable excursion USD": fav_usd,
            "Favorable excursion %":   t["fav_exc_pct"],
            "Adverse excursion USD":   adv_usd,
            "Adverse excursion %":     t["adv_exc_pct"],
            "Cumulative P&L USD":      round(cum_usd, 2),
            "Cumulative P&L %":        round(cum_pct, 4),
        })

    return pd.DataFrame(rows)


def compute_summary(trades_df_by_ticker: dict) -> pd.DataFrame:
    """
    Compute summary stats matching the Summary sheet format (v3, per-signal).
    """
    rows = {}
    for ticker, df in trades_df_by_ticker.items():
        if df is None or df.empty:
            continue
        exits = df[df["Type"] == "Exit long"]
        if exits.empty:
            continue

        pnl = exits["Net P&L %"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        max_ret = exits["Max Return"] * 100

        n = len(exits)
        rows[ticker] = {
            "Count":         n,
            "# Wins":        len(wins),
            "Win Rate":      len(wins) / n if n else 0,
            "Avg Return %":  pnl.mean() / 100,
            "Avg Win %":     wins.mean() / 100 if len(wins) else 0,
            "Avg Loss %":    losses.mean() / 100 if len(losses) else 0,
            "Avg Max Return %": max_ret.mean() / 100 if not max_ret.empty else 0,
            "Win Rate (max)":   (max_ret > 0).sum() / n if n else 0,
            "Expectancy":    pnl.mean() / 100,
            "Avg Duration (days)": exits["Duration"].mean(),
        }

    summary = pd.DataFrame(rows).T
    if not summary.empty:
        summary.loc["V3 avgs"] = summary.mean()
    return summary


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR  (called by tps_run_massive.py / tps_app.py)
# ──────────────────────────────────────────────────────────────────────────────

def run_ticker(ticker: str, df_base: pd.DataFrame, df_daily: pd.DataFrame,
               config: dict = None) -> tuple:
    """
    Full pipeline for one ticker:
      1. Filter + resample 1-min base bars to chart TF, 30m, 15m
      2. Compute indicators on all timeframes
      3. Multi-TF alignment
      4. Compute TPS score
      5. Compute entry trigger
      6. Compute chart-TF ATR (Wilder) + RSI (Wilder)
      7. Run the v3 backtest simulation
      8. Return (raw trades, formatted DataFrame)

    `df_base` should be 1-MINUTE bars for an exact 78-minute grid. (15-minute
    base still runs, but the 78m bars will be approximate.)
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    chart_tf = int(config["chart_tf"])
    sqz_len  = int(config["sqz_len"])

    # ── 1. Resample
    df_mh    = filter_market_hours(df_base)
    df_15    = resample_ohlcv(df_mh, 15)
    df_30    = resample_ohlcv(df_mh, 30)
    df_chart = resample_ohlcv(df_mh, chart_tf)
    df_chart.attrs["tf_min"] = chart_tf

    if df_chart.empty or len(df_chart) < max(sqz_len, 30):
        return [], pd.DataFrame()

    # ── 2. Compute indicators on each TF
    cfg_sqz = dict(
        sqz_len=sqz_len,
        release_bars=int(config["release_bars"]),
        min_sqz_bars=int(config["min_sqz_bars"]),
        max_sqz_bars=int(config["max_sqz_bars"]),
    )

    df_15_sig    = add_tps_signals(df_15, **cfg_sqz)
    df_30_sig    = add_tps_signals(df_30, **cfg_sqz)
    df_chart_sig = add_tps_signals(df_chart, **cfg_sqz)
    df_daily_sig = add_tps_signals(df_daily, **cfg_sqz)  # only trend_ok used

    # ── 3. Multi-TF alignment onto chart bars
    merged = df_chart_sig.copy()
    merged.attrs["tf_min"] = chart_tf

    tf_name = str(chart_tf)
    for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
        if col in merged.columns:
            merged.rename(columns={col: f"c{tf_name}_{col}"}, inplace=True)
    merged.attrs["tf_min"] = chart_tf

    if chart_tf > 30:
        merged = align_tf(merged, df_30_sig, "c30", 30)
    else:
        for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
            src = f"c{tf_name}_{col}"
            if src in merged.columns:
                merged[f"c30_{col}"] = merged[src]

    if chart_tf > 15:
        merged = align_tf(merged, df_15_sig, "c15", 15)
    else:
        for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
            src = f"c{tf_name}_{col}"
            if src in merged.columns:
                merged[f"c15_{col}"] = merged[src]

    merged = align_daily(merged, df_daily_sig, "d")

    bool_cols = [c for c in merged.columns if any(
        c.endswith(s) for s in ["_ok", "_tight", "_recent", "_rise2"])]
    merged[bool_cols] = merged[bool_cols].fillna(False)

    # ── 4. TPS score
    merged["tps_score"] = compute_tps_score(merged, config)

    # ── 5. Entry trigger
    merged["bb_break"] = compute_entry_trigger(
        merged, int(config["bb_len"]), float(config["bb_dev"])
    )

    # ── 6. Chart-TF ATR (Wilder) + RSI (Wilder) for the v3 exits
    atr_col = f"atr_{config['atr_len']}"
    rsi_col = f"rsi_{config['rsi_length']}"
    merged[atr_col] = atr(merged, int(config["atr_len"]))
    merged[rsi_col] = rsi(merged["c"], int(config["rsi_length"]))

    # ── 7. Run backtest
    raw_trades = run_backtest(merged, config)
    df_trades  = trades_to_dataframe(raw_trades, ticker)

    return raw_trades, df_trades
