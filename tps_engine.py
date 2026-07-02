"""
TPS Long Strategy v2 — Backtest Engine
=======================================
Pure Python/pandas replication of the TPS Long Strategy v2 Pine Script.
Accepts pre-fetched OHLCV DataFrames (from any source) and returns trade results
in the same format as the existing TradingView backtest spreadsheets.

Key design notes:
  - Mirrors process_orders_on_close=true: fills happen at bar CLOSE
  - Multi-TF alignment uses merge_asof (no look-ahead)
  - 15-minute bars are the base; resample to 30m / 78m / 195m internally
  - All timestamps are UTC; market-hour filtering is done before this engine
  - Squeeze formula matches the Makit0 / TTM variant used in Pine Script exactly
"""

import numpy as np
import pandas as pd
import pytz
from datetime import datetime, timedelta, date as date_type

ET = pytz.timezone("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# DEFAULT CONFIG
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # Strategy params — defaults match the V1 TradingView baseline
    "score_threshold": 65.0,
    "sqz_len": 20,
    "min_sqz_bars": 3,
    "max_sqz_bars": 20,
    "release_bars": 3,
    "bb_len": 10,
    "bb_dev": 1.5,
    "atr_len": 10,
    "tp1_atr": 2.0,
    "tp2_atr": 3.0,
    "sl_atr": 2.0,
    "time_exit_bars": 0,       # 0 = no time exit (V1 baseline has none)
    # Runner stop after TP1: "v1" keeps original -SL ATR stop (baseline);
    # "be+1" moves stop to breakeven +1 ATR (v2 behavior)
    "runner_stop_mode": "v1",
    # Chart timeframe in minutes (78 or 195)
    "chart_tf": 78,
    # Trend score weights (sum = 50)
    "trend_pts_d":  20.0,
    "trend_pts_78": 15.0,
    "trend_pts_30": 10.0,
    "trend_pts_15":  5.0,
    # Squeeze score weights (sum = 50; these are per-timeframe totals)
    "sqz_pts_78": 10.0,
    "sqz_pts_30": 15.0,
    "sqz_pts_15": 25.0,
}


# ──────────────────────────────────────────────────────────────────────────────
# BAR RESAMPLING  (15m → 30m / 78m / 195m, aligned to 9:30 AM ET)
# ──────────────────────────────────────────────────────────────────────────────

def filter_market_hours(df_utc: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars that OPEN during regular market hours (9:30–15:45 ET).
    The 15:45 cutoff ensures the last 15m bar of the day is included (opens 15:45,
    covers 15:45–16:00)."""
    idx_et = df_utc.index.tz_convert(ET)
    time_of_day = idx_et.time
    lo = pd.Timestamp("09:30", tz=ET).time()
    hi = pd.Timestamp("15:46", tz=ET).time()   # 15:45 + epsilon
    return df_utc[(time_of_day >= lo) & (time_of_day < hi)]


def resample_ohlcv(df_15m: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    """
    Resample market-hours-filtered 15-minute bars to target_minutes bars,
    anchored at 9:30 AM ET each day.

    Returns DataFrame indexed by bar OPEN TIME (UTC), columns: o, h, l, c, v.
    """
    df_et = df_15m.copy()
    df_et.index = df_et.index.tz_convert(ET)

    rows = []
    for day, day_df in df_et.groupby(df_et.index.date):
        open_dt = ET.localize(datetime.combine(day, datetime.strptime("09:30", "%H:%M").time()))
        close_dt = ET.localize(datetime.combine(day, datetime.strptime("16:00", "%H:%M").time()))
        total_min = int((close_dt - open_dt).total_seconds() / 60)  # 390

        n_bins = total_min // target_minutes
        remainder = total_min % target_minutes

        bin_starts = [open_dt + timedelta(minutes=target_minutes * i) for i in range(n_bins)]
        if remainder > 0:
            bin_starts.append(open_dt + timedelta(minutes=target_minutes * n_bins))

        for i, bs in enumerate(bin_starts):
            be = bin_starts[i + 1] if i + 1 < len(bin_starts) else close_dt + timedelta(seconds=1)
            chunk = day_df[(day_df.index >= bs) & (day_df.index < be)]
            if chunk.empty:
                continue
            rows.append({
                "t": bs.astimezone(pytz.utc),
                "o": chunk["o"].iloc[0],
                "h": chunk["h"].max(),
                "l": chunk["l"].min(),
                "c": chunk["c"].iloc[-1],
                "v": chunk["v"].sum(),
            })

    if not rows:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])

    out = pd.DataFrame(rows).set_index("t")
    out.index = pd.DatetimeIndex(out.index, tz="UTC")
    return out.sort_index()


# ──────────────────────────────────────────────────────────────────────────────
# INDICATOR CALCULATIONS
# ──────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-Cp|, |L-Cp|)."""
    prev_c = df["c"].shift(1)
    return pd.concat([
        df["h"] - df["l"],
        (df["h"] - prev_c).abs(),
        (df["l"] - prev_c).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    return true_range(df).rolling(n).mean()


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
    """Bollinger Bands using SMA (matches Pine Script ta.bb)."""
    basis = sma(df["c"], length)
    dev   = df["c"].rolling(length).std(ddof=1)   # Pine uses population-style but ddof=1 is default
    return basis + dev * mult, basis - dev * mult


def compute_squeeze_states(df: pd.DataFrame, sqz_len: int):
    """
    Reproduce Makit0 squeeze state logic exactly.

    Returns DataFrame with boolean columns:
      sqz_wide, sqz_normal, sqz_narrow, sqz_off_wide, no_sqz, sqz_active, sqz_tight
    """
    ma   = sma(df["c"], sqz_len)
    dev_bb = df["c"].rolling(sqz_len).std(ddof=1)
    dev_kc = true_range(df).rolling(sqz_len).mean()   # SMA of TR (not ATR per se)

    up_bb  = ma + dev_bb * 2.0
    lo_bb  = ma - dev_bb * 2.0

    up_kc_wide   = ma + dev_kc * 2.0
    lo_kc_wide   = ma - dev_kc * 2.0
    up_kc_normal = ma + dev_kc * 1.5
    lo_kc_normal = ma - dev_kc * 1.5
    up_kc_narrow = ma + dev_kc * 1.0
    lo_kc_narrow = ma - dev_kc * 1.0

    sqz_on_wide   = (lo_bb >= lo_kc_wide)   & (up_bb <= up_kc_wide)
    sqz_on_normal = (lo_bb >= lo_kc_normal) & (up_bb <= up_kc_normal)
    # NOTE: Previous implementation incorrectly compared upBB to lowKCNarrow.
    # Pine Script v2 uses (upBB <= upKCNarrow), which is the correct symmetric
    # definition.  Fixed to match Pine Script exactly.
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
    for i in range(1, len(sqz_active)):
        if sqz_active.iloc[i]:
            count.iloc[i] = count.iloc[i - 1] + 1
        else:
            count.iloc[i] = 0
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

    # Recent squeeze: active now OR was active within last `release_bars` bars
    bars_since = d["sqz_active"].apply(lambda x: x).astype(int)
    # Use a rolling window to check if squeeze was active in last N bars
    d["sqz_recent"] = (
        d["sqz_active"].rolling(release_bars + 1, min_periods=1).max().astype(bool)
    )
    # "active_or_recent" = active now OR was active in last release_bars bars
    d["sqz_active_or_recent"] = d["sqz_recent"]

    # Count of consecutive active squeeze bars
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
    For each chart bar, find the most recently COMPLETED lower-TF bar.

    "Completed" means: lower_bar_close_time <= chart_bar_close_time
    bar_close_time = bar_open_time + bar_duration

    Uses merge_asof with direction='backward' on close times.
    """
    chart_close = df_chart.index + pd.Timedelta(minutes=int(df_chart.attrs.get("tf_min", 78)))
    lower_close = df_lower.index + pd.Timedelta(minutes=lower_tf_min)

    # Build temp DataFrame for merge
    left = df_chart.copy()
    left["_close_t"] = chart_close

    cols = [c for c in df_lower.columns if c in
            ("trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2")]
    right = df_lower[cols].copy()
    right.index = lower_close  # index by close time
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
    Join daily trend signals onto chart bars.
    Each chart bar uses the PREVIOUS trading day's completed daily bar
    (TradingView lookahead_off behavior for higher timeframes).
    """
    # Convert chart index to ET dates
    chart_et = df_chart.index.tz_convert(ET)
    chart_dates = pd.Series(chart_et.date, index=df_chart.index)

    # Daily bars indexed by their DATE (ET)
    daily_et = df_daily.index.tz_convert(ET) if df_daily.index.tz else df_daily.index
    df_daily_dated = df_daily.copy()
    df_daily_dated.index = pd.DatetimeIndex([pd.Timestamp(d, tz=ET) for d in daily_et.date])
    df_daily_dated = df_daily_dated[~df_daily_dated.index.duplicated(keep="last")]
    df_daily_dated = df_daily_dated.sort_index()

    # For each chart bar date, look up the most recent daily bar from the PREVIOUS day
    daily_signals = df_daily_dated[["trend_ok"]].copy()
    daily_signals.index = daily_signals.index.date  # pure date objects

    def lookup_daily_trend(bar_date):
        # Find the last daily bar strictly before bar_date
        idx = daily_signals.index
        mask = idx < bar_date
        if not mask.any():
            return False
        return bool(daily_signals.loc[idx[mask][-1], "trend_ok"])

    df_out = df_chart.copy()
    df_out[f"{prefix}_trend_ok"] = chart_dates.map(lookup_daily_trend)
    return df_out


# ──────────────────────────────────────────────────────────────────────────────
# TPS SCORE COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_tps_score(df_chart: pd.DataFrame, config: dict) -> pd.Series:
    """
    Compute the TPS score (0-100) for each bar.
    Assumes df_chart has all signal columns already aligned from each TF:
      d_trend_ok
      c{chart_tf}_trend_ok,  c{chart_tf}_sqz_tight, c{chart_tf}_sqz_active_or_recent,
      c{chart_tf}_sqz_bars_ok, c{chart_tf}_sqz_mom_rise2
      c30_trend_ok,  c30_sqz_tight, c30_sqz_active_or_recent, c30_sqz_bars_ok, c30_sqz_mom_rise2
      c15_trend_ok,  c15_sqz_tight, c15_sqz_active_or_recent, c15_sqz_bars_ok, c15_sqz_mom_rise2

    The chart-TF column prefix is derived from config["chart_tf"]
    (e.g. chart_tf=78 → prefix "c78", chart_tf=195 → prefix "c195").
    """
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

    # Trend score — chart-TF column uses the dynamic prefix
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
    """close > BB Upper (EMA-based, matches Pine Script bbBasis = ta.ema)."""
    bb_basis = ema(df["c"], bb_len)
    bb_std   = df["c"].rolling(bb_len).std(ddof=1)
    bb_upper = bb_basis + bb_std * bb_dev
    return (df["c"] > bb_upper).rename("bb_break")


# ──────────────────────────────────────────────────────────────────────────────
# BACKTEST SIMULATION
# ──────────────────────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, config: dict) -> list[dict]:
    """
    Simulate the TPS Long Strategy v2 on a scored + triggered DataFrame.

    Required columns: o, h, l, c, v, tps_score, bb_break, atr_{n}
    (atr_{n} is the ATR with period=atr_len, already computed)

    Returns list of trade dicts (one per exit event = 2 per signal: TP1/TP2 or stop).

    process_orders_on_close=true equivalent:
      - Entry fires at bar where entry_signal is True → fill at that bar's CLOSE
      - Exits checked each bar: compare CLOSE vs target/stop prices → fill at CLOSE
    """
    score_threshold = float(config["score_threshold"])
    tp1_atr = float(config["tp1_atr"])
    tp2_atr = float(config["tp2_atr"])
    sl_atr  = float(config["sl_atr"])
    time_exit_bars = int(config.get("time_exit_bars", 0) or 0)  # 0 = disabled
    runner_stop_mode = str(config.get("runner_stop_mode", "v1"))

    trades = []

    # State
    in_pos = False
    entry_price = None
    entry_atr   = None
    entry_time  = None
    entry_idx   = None
    tp1_hit     = False
    bars_since_entry = 0
    bars_since_tp1   = 0
    peak_close_since_tp1 = None   # for favorable excursion tracking

    tp1_price = tp2_price = sl_price = adj_stop = None

    # Pre-compute entry signal column
    entry_sig = df["tps_score"] >= score_threshold
    entry_sig = entry_sig & df["bb_break"]

    # Build ATR column name
    atr_col = f"atr_{config['atr_len']}"

    bars = list(df.itertuples())

    for idx, bar in enumerate(bars):
        t       = bar.Index
        c_close = bar.c
        c_high  = bar.h
        c_low   = bar.l
        c_atr   = getattr(bar, atr_col) if hasattr(bar, atr_col) else np.nan

        if not in_pos:
            # ─── Check for new entry ───
            sig = getattr(bar, "tps_score", 0) >= score_threshold and getattr(bar, "bb_break", False)
            if sig and not np.isnan(c_atr):
                in_pos = True
                entry_price = c_close
                entry_atr   = c_atr
                entry_time  = t
                entry_idx   = idx
                tp1_hit     = False
                bars_since_entry = 0
                bars_since_tp1   = 0
                peak_close_since_tp1 = None

                tp1_price = entry_price + entry_atr * tp1_atr
                tp2_price = entry_price + entry_atr * tp2_atr
                sl_price  = entry_price - entry_atr * sl_atr
                # Runner stop after TP1: v1 keeps original stop; be+1 locks profit
                adj_stop  = sl_price if runner_stop_mode == "v1" \
                            else entry_price + entry_atr * 1.0
        else:
            bars_since_entry += 1

            if not tp1_hit:
                # ─── Both contracts still open ───
                # Check time exit first (lowest priority); 0 = disabled
                time_exit = time_exit_bars > 0 and bars_since_entry >= time_exit_bars

                if c_close <= sl_price:
                    # Stop loss hits both
                    trades.append(_make_trade(
                        signal="TP1", exit_type="stop",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=max(0.0, tp1_price - entry_price) if c_close < entry_price else c_close - entry_price,
                        adv_exc=c_close - entry_price,
                    ))
                    trades.append(_make_trade(
                        signal="TP2", exit_type="stop",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=max(0.0, tp2_price - entry_price) if c_close < entry_price else c_close - entry_price,
                        adv_exc=c_close - entry_price,
                    ))
                    in_pos = False

                elif c_close >= tp1_price:
                    # TP1 fills
                    trades.append(_make_trade(
                        signal="TP1", exit_type="tp1",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=c_close - entry_price,
                        adv_exc=min(0.0, sl_price - entry_price),
                    ))
                    tp1_hit = True
                    bars_since_tp1 = 0
                    peak_close_since_tp1 = c_close
                    adj_stop = sl_price if runner_stop_mode == "v1" \
                               else entry_price + entry_atr * 1.0

                elif time_exit:
                    # Time exit — both contracts
                    trades.append(_make_trade(
                        signal="TP1", exit_type="time",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=max(0.0, c_close - entry_price),
                        adv_exc=min(0.0, c_close - entry_price),
                    ))
                    trades.append(_make_trade(
                        signal="TP2", exit_type="time",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=max(0.0, c_close - entry_price),
                        adv_exc=min(0.0, c_close - entry_price),
                    ))
                    in_pos = False

            else:
                # ─── TP1 already hit; second contract still open ───
                bars_since_tp1 += 1
                if peak_close_since_tp1 is not None:
                    peak_close_since_tp1 = max(peak_close_since_tp1, c_close)

                fav_exc_tp2 = (peak_close_since_tp1 - entry_price) if peak_close_since_tp1 else 0.0
                time_exit_2 = time_exit_bars > 0 and bars_since_tp1 >= time_exit_bars

                if c_close <= adj_stop:
                    trades.append(_make_trade(
                        signal="TP2", exit_type="adj_stop",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=fav_exc_tp2,
                        adv_exc=min(0.0, c_close - entry_price),
                    ))
                    in_pos = False

                elif c_close >= tp2_price:
                    trades.append(_make_trade(
                        signal="TP2", exit_type="tp2",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=c_close - entry_price,
                        adv_exc=min(0.0, adj_stop - entry_price),
                    ))
                    in_pos = False

                elif time_exit_2:
                    trades.append(_make_trade(
                        signal="TP2", exit_type="time",
                        entry_time=entry_time, exit_time=t,
                        entry_price=entry_price, exit_price=c_close,
                        fav_exc=fav_exc_tp2,
                        adv_exc=min(0.0, c_close - entry_price),
                    ))
                    in_pos = False

    return trades


def _make_trade(signal, exit_type, entry_time, exit_time,
                entry_price, exit_price, fav_exc, adv_exc):
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    fav_pct  = fav_exc / entry_price * 100
    adv_pct  = adv_exc / entry_price * 100
    duration = (exit_time - entry_time).total_seconds() / 86400

    return {
        "entry_time":   entry_time,
        "exit_time":    exit_time,
        "signal":       signal,       # "TP1" or "TP2"
        "exit_type":    exit_type,    # "tp1", "tp2", "stop", "adj_stop", "time"
        "entry_price":  round(entry_price, 4),
        "exit_price":   round(exit_price, 4),
        "pnl_pct":      round(pnl_pct, 4),
        "fav_exc_pct":  round(fav_pct, 4),
        "adv_exc_pct":  round(adv_pct, 4),
        "duration_days": round(duration, 4),
    }


# ──────────────────────────────────────────────────────────────────────────────
# RESULTS FORMATTING  (matches TradingView export format)
# ──────────────────────────────────────────────────────────────────────────────

def trades_to_dataframe(trades: list[dict], ticker: str) -> pd.DataFrame:
    """
    Convert raw trade list to a DataFrame matching the existing TV spreadsheet format:
      Trade #, Type, Date and time, Signal, Price USD, Size (qty), Net P&L %, etc.
    Each signal (TP1 or TP2) produces TWO rows: entry long + exit long.
    """
    if not trades:
        return pd.DataFrame()

    rows = []
    trade_num = 1

    # Group trades by entry_time (each entry_time has a TP1 and TP2 exit)
    import itertools
    for entry_t, group in itertools.groupby(sorted(trades, key=lambda x: (x["entry_time"], x["signal"])),
                                             key=lambda x: x["entry_time"]):
        group = list(group)
        for t in group:
            # Entry row
            rows.append({
                "Trade #":               trade_num,
                "Type":                  "Entry long",
                "Date and time":         t["entry_time"].tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S"),
                "Signal":                "Long",
                "Price USD":             t["entry_price"],
                "Size (qty)":            1,
                "Size (value)":          t["entry_price"],
                "Total Return":          None,
                "Max Return":            None,
                "Duration":              None,
                "Net P&L USD":           round((t["exit_price"] - t["entry_price"]) * 1, 2),
                "Net P&L %":             t["pnl_pct"],
                "Favorable excursion USD":  round(t["fav_exc_pct"] / 100 * t["entry_price"], 2),
                "Favorable excursion %":   t["fav_exc_pct"],
                "Adverse excursion USD":   round(t["adv_exc_pct"] / 100 * t["entry_price"], 2),
                "Adverse excursion %":     t["adv_exc_pct"],
                "Cumulative P&L USD":      None,
                "Cumulative P&L %":        None,
            })
            # Exit row
            rows.append({
                "Trade #":               trade_num,
                "Type":                  "Exit long",
                "Date and time":         t["exit_time"].tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S"),
                "Signal":                t["signal"],   # "TP1" or "TP2"
                "Price USD":             t["exit_price"],
                "Size (qty)":            1,
                "Size (value)":          t["exit_price"],
                "Total Return":          t["pnl_pct"] / 100,
                "Max Return":            t["fav_exc_pct"] / 100,
                "Duration":              t["duration_days"],
                "Net P&L USD":           round((t["exit_price"] - t["entry_price"]) * 1, 2),
                "Net P&L %":             t["pnl_pct"],
                "Favorable excursion USD":  round(t["fav_exc_pct"] / 100 * t["entry_price"], 2),
                "Favorable excursion %":   t["fav_exc_pct"],
                "Adverse excursion USD":   round(t["adv_exc_pct"] / 100 * t["entry_price"], 2),
                "Adverse excursion %":     t["adv_exc_pct"],
                "Cumulative P&L USD":      None,
                "Cumulative P&L %":        None,
            })
            trade_num += 1

    df = pd.DataFrame(rows)

    # Compute cumulative P&L on exit rows only
    exit_mask = df["Type"] == "Exit long"
    df.loc[exit_mask, "Cumulative P&L USD"] = df.loc[exit_mask, "Net P&L USD"].cumsum()
    df.loc[exit_mask, "Cumulative P&L %"] = (
        df.loc[exit_mask, "Net P&L %"].cumsum()
    )

    return df


def compute_summary(trades_df_by_ticker: dict) -> pd.DataFrame:
    """
    Compute summary stats matching the V2 Summary sheet format.
    """
    rows = {}
    for ticker, df in trades_df_by_ticker.items():
        exits = df[df["Type"] == "Exit long"]
        if exits.empty:
            continue

        pnl = exits["Net P&L %"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        max_ret = exits["Max Return"] * 100

        n = len(exits)
        rows[ticker] = {
            "Count":         n / 2,   # pairs
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
    summary.loc["V2 avgs"] = summary.mean()
    return summary


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR  (called by tps_run_massive.py)
# ──────────────────────────────────────────────────────────────────────────────

def run_ticker(ticker: str, df_15m: pd.DataFrame, df_daily: pd.DataFrame,
               config: dict = None) -> tuple[list[dict], pd.DataFrame]:
    """
    Full pipeline for one ticker:
      1. Filter + resample 15m bars to chart TF, 30m, daily
      2. Compute indicators on all timeframes
      3. Multi-TF alignment
      4. Compute TPS score
      5. Compute entry trigger
      6. Run backtest simulation
      7. Return (raw trades, formatted DataFrame)
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    chart_tf = int(config["chart_tf"])
    sqz_len  = int(config["sqz_len"])

    # ── 1. Resample
    df_mh = filter_market_hours(df_15m)
    df_30  = resample_ohlcv(df_mh, 30)
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

    df_15m_sig  = add_tps_signals(df_mh, **cfg_sqz)
    df_30_sig   = add_tps_signals(df_30, **cfg_sqz)
    df_chart_sig = add_tps_signals(df_chart, **cfg_sqz)
    df_daily_sig = add_tps_signals(df_daily, **cfg_sqz)  # only trend_ok used

    # ── 3. Multi-TF alignment onto chart bars
    merged = df_chart_sig.copy()

    # Rename chart-TF signal columns with prefix c{chart_tf} (e.g. c78, c195)
    tf_name = str(chart_tf)
    for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
        if col in merged.columns:
            merged.rename(columns={col: f"c{tf_name}_{col}"}, inplace=True)

    # Align 30m signals — only if chart TF is higher than 30m to avoid column collision.
    # When chart_tf == 30, the chart IS the 30m data; copy the chart columns under the
    # "c30" alias so that compute_tps_score can find them.
    if chart_tf > 30:
        merged = align_tf(merged, df_30_sig, "c30", 30)
    else:
        # chart_tf <= 30: treat chart columns as the "c30" reference
        for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
            src = f"c{tf_name}_{col}"
            if src in merged.columns:
                merged[f"c30_{col}"] = merged[src]

    # Align 15m signals — only if chart TF is higher than 15m
    if chart_tf > 15:
        merged = align_tf(merged, df_15m_sig, "c15", 15)
    else:
        for col in ["trend_ok", "sqz_tight", "sqz_active_or_recent", "sqz_bars_ok", "sqz_mom_rise2"]:
            src = f"c{tf_name}_{col}"
            if src in merged.columns:
                merged[f"c15_{col}"] = merged[src]

    # Align Daily trend
    merged = align_daily(merged, df_daily_sig, "d")

    # Fill NaN booleans conservatively with False
    bool_cols = [c for c in merged.columns if any(
        c.endswith(s) for s in ["_ok", "_tight", "_recent", "_rise2"])]
    merged[bool_cols] = merged[bool_cols].fillna(False)

    # ── 4. TPS score
    merged["tps_score"] = compute_tps_score(merged, config)

    # ── 5. Entry trigger
    merged["bb_break"] = compute_entry_trigger(
        merged, int(config["bb_len"]), float(config["bb_dev"])
    )

    # ── 6. ATR for position sizing / exits
    atr_col = f"atr_{config['atr_len']}"
    merged[atr_col] = atr(merged, int(config["atr_len"]))

    # ── 7. Run backtest
    raw_trades = run_backtest(merged, config)
    df_trades  = trades_to_dataframe(raw_trades, ticker)

    return raw_trades, df_trades
