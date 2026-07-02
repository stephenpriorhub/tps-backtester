"""
TPS Backtesting Dashboard
=========================
Interactive Streamlit app for tuning and running the TPS Long Strategy v2.
All compute runs locally — no Claude credits used.

Run:
    cd "/Users/stephenprior/Documents/GitHub/brain/Projects/TPS Project"
    streamlit run tps_app.py
"""

import os, sys, json, hashlib, pickle, re, time, tempfile
from pathlib import Path
from datetime import date, datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Project path
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from tps_engine import DEFAULT_CONFIG, run_ticker
from tps_export import export_to_excel
import tps_run_massive as _trm
from tps_run_massive import NDX100, fetch_bars, _cache_path, DATA_DIR

# ── Inject API key from trading-scanner .env (or environment)
def _load_api_key() -> str:
    key = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY", "")
    if not key:
        env_candidates = [
            PROJECT_DIR / ".env",
            Path.home() / "Documents/github/trading-scanner/.env",
        ]
        for p in env_candidates:
            if p.exists():
                for line in p.read_text().splitlines():
                    if "POLYGON_API_KEY=" in line or "MASSIVE_API_KEY=" in line:
                        key = line.split("=", 1)[1].strip()
                        break
            if key:
                break
    return key

_trm.MASSIVE_API_KEY = _load_api_key()

RESULTS_DIR = PROJECT_DIR / "results_cache"
RESULTS_DIR.mkdir(exist_ok=True)

SAVED_TESTS_DIR = Path(os.environ.get("SAVED_TESTS_DIR", str(PROJECT_DIR / "saved_tests")))
SAVED_TESTS_DIR.mkdir(exist_ok=True)

ORIGINAL_9 = ["AAPL", "GOOGL", "NVDA", "TSLA", "AMZN", "MSFT", "AMD", "ORCL", "NFLX"]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cfg_hash(cfg: dict) -> str:
    key = json.dumps(
        {k: v for k, v in sorted(cfg.items()) if k != "end_date"},
        sort_keys=True, default=str
    )
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _results_path(cfg: dict) -> Path:
    return RESULTS_DIR / f"run_{_cfg_hash(cfg)}.pkl"


def load_cached_results(cfg: dict):
    p = _results_path(cfg)
    if p.exists():
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    return None


def save_results(cfg: dict, results: dict):
    p = _results_path(cfg)
    with open(p, "wb") as f:
        pickle.dump(results, f)


def save_test(name: str, cfg: dict, results: dict, notes: str = "") -> str:
    """Save a named test run. Returns the save filename."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:50]
    ts   = datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"{ts}_{slug}.pkl"
    data = {"name": name, "notes": notes, "cfg": cfg, "results": results,
            "saved_at": datetime.now().isoformat()}
    with open(SAVED_TESTS_DIR / fname, "wb") as f:
        pickle.dump(data, f)
    return fname


def list_saved_tests() -> list:
    """Return list of saved tests sorted newest first."""
    tests = []
    for p in sorted(SAVED_TESTS_DIR.glob("*.pkl"), reverse=True):
        try:
            with open(p, "rb") as f:
                d = pickle.load(f)
            tests.append({"fname": p.name, "name": d.get("name", ""),
                          "notes": d.get("notes", ""), "saved_at": d.get("saved_at", ""),
                          "cfg": d.get("cfg", {})})
        except Exception:
            pass
    return tests


def load_saved_test(fname: str) -> dict:
    """Load a saved test by filename."""
    with open(SAVED_TESTS_DIR / fname, "rb") as f:
        return pickle.load(f)


def ticker_stats(ticker: str, df_trades: pd.DataFrame) -> dict:
    empty = dict(ticker=ticker, n=0, wr=0.0, avg_r=0.0,
                 avg_win=0.0, avg_loss=0.0, exp=0.0,
                 avg_dur=0.0, best=0.0, worst=0.0, score=0.0)
    if df_trades is None or df_trades.empty:
        return empty
    exits = df_trades[df_trades["Type"] == "Exit long"]
    if exits.empty:
        return empty
    pnl  = exits["Net P&L %"]
    wins = pnl[pnl > 0]
    loss = pnl[pnl <= 0]
    n    = len(exits)
    wr   = len(wins) / n if n else 0
    avg_r    = pnl.mean()
    avg_win  = wins.mean()  if len(wins) else 0.0
    avg_loss = loss.mean()  if len(loss) else 0.0
    exp      = wr * avg_win + (1 - wr) * abs(avg_loss) * -1 if n else 0.0
    avg_dur  = exits["Duration"].mean() if "Duration" in exits.columns else 0.0
    return dict(ticker=ticker, n=n, wr=wr, avg_r=avg_r, avg_win=avg_win,
                avg_loss=avg_loss, exp=exp, avg_dur=avg_dur,
                best=pnl.max(), worst=pnl.min(), score=wr * avg_r)


def build_stats_df(all_trades: dict) -> pd.DataFrame:
    rows = [ticker_stats(t, df) for t, df in all_trades.items()]
    df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    df.insert(0, "Rank", range(1, len(df) + 1))
    return df


def run_backtest(tickers, cfg, progress_cb=None):
    """Run engine on all tickers, return {ticker: df_trades} dict."""
    all_trades = {}
    start, end = cfg["start_date"], cfg["end_date"]
    for i, ticker in enumerate(tickers):
        if progress_cb:
            progress_cb(i, len(tickers), ticker)
        try:
            df_15m   = fetch_bars(ticker, 15, "minute", start, end)
            df_daily = fetch_bars(ticker, 1,  "day",   start, end)
            if df_15m.empty or df_daily.empty:
                all_trades[ticker] = None
                continue
            _, df_trades = run_ticker(ticker, df_15m, df_daily, cfg)
            all_trades[ticker] = df_trades
        except Exception as e:
            all_trades[ticker] = None
    if progress_cb:
        progress_cb(len(tickers), len(tickers), "Done")
    return all_trades


# ─────────────────────────────────────────────────────────────────────────────
# Page config & CSS
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TPS Backtesting Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state — must initialize before ANY widget reads session_state
if "results"           not in st.session_state: st.session_state.results           = None
if "baseline"          not in st.session_state: st.session_state.baseline          = None
if "cfg_used"          not in st.session_state: st.session_state.cfg_used          = None
if "saved_test_meta"   not in st.session_state: st.session_state.saved_test_meta   = None

# ── Hub-nav integration
st.markdown(
    '<script src="https://oxfordhub.app/hub-nav.js" '
    'data-project-id="cmq8f23bz0000896nlbz411zb" defer></script>',
    unsafe_allow_html=True,
)

st.markdown("""
<style>
  .metric-card {
    background: #1a2332; border-radius: 8px; padding: 16px 20px;
    text-align: center; margin: 4px;
  }
  .metric-val { font-size: 28px; font-weight: 700; color: #4fc3f7; }
  .metric-lbl { font-size: 12px; color: #aaa; margin-top: 4px; }
  .win  { color: #69db7c !important; }
  .loss { color: #ff6b6b !important; }
  .neutral { color: #aaa !important; }
  div[data-testid="stSidebar"] { background: #111b27; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — parameter controls
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ TPS Parameters")

    # ── Universe
    st.subheader("🗂 Universe")
    preset = st.radio("Preset", ["Nasdaq-100 (~94)", "Original 9", "Custom"],
                      horizontal=True, label_visibility="collapsed")
    if preset == "Nasdaq-100 (~94)":
        default_tickers = NDX100
    elif preset == "Original 9":
        default_tickers = ORIGINAL_9
    else:
        default_tickers = ORIGINAL_9

    if preset == "Custom":
        raw = st.text_area("Tickers (comma-separated)",
                           value=", ".join(default_tickers), height=100)
        tickers = [t.strip().upper() for t in raw.replace("\n", ",").split(",") if t.strip()]
    else:
        tickers = default_tickers

    st.caption(f"{len(tickers)} tickers selected")

    # ── Date range
    st.subheader("📅 Date Range")
    _today = date.today()
    _lookback_opts = {"5 Years": 5, "8 Years": 8, "10 Years": 10, "Custom": None}
    _lookback_sel = st.radio("Lookback", list(_lookback_opts.keys()), horizontal=True,
                              index=0, label_visibility="collapsed")
    _yrs = _lookback_opts[_lookback_sel]
    _default_start = date(_today.year - _yrs, _today.month, _today.day) if _yrs else date(2021, 6, 1)
    _min_date = date(2015, 1, 1)
    if _lookback_sel == "Custom":
        col1, col2 = st.columns(2)
        start_date = col1.date_input("Start", value=_default_start,
                                      min_value=_min_date, max_value=_today)
        end_date   = col2.date_input("End",   value=_today,
                                      min_value=_min_date, max_value=_today)
    else:
        start_date = _default_start
        end_date   = _today
        st.caption(f"{start_date.strftime('%b %d, %Y')}  →  {end_date.strftime('%b %d, %Y')}")

    # ── Strategy
    st.subheader("📊 Strategy")
    chart_tf  = st.radio("Chart TF", [78, 195], horizontal=True,
                          format_func=lambda x: f"{x}m")
    score_thr = st.slider("Score Threshold", 50.0, 90.0,
                           float(DEFAULT_CONFIG["score_threshold"]), 1.0,
                           help="Minimum TPS score to trigger an entry")

    # ── Squeeze
    st.subheader("🔩 Squeeze")
    c1, c2 = st.columns(2)
    min_sqz = c1.number_input("Min bars", 1, 20, int(DEFAULT_CONFIG["min_sqz_bars"]))
    max_sqz = c2.number_input("Max bars", 5, 50, int(DEFAULT_CONFIG["max_sqz_bars"]))
    rel_bars = st.slider("Release lookback bars", 1, 10,
                          int(DEFAULT_CONFIG["release_bars"]))
    st.caption("Squeeze point weights (chart TF / 30m / 15m)")
    c1, c2, c3 = st.columns(3)
    wt_78  = c1.number_input("78m", 0.0, 30.0, float(DEFAULT_CONFIG["sqz_pts_78"]),  2.5)
    wt_30  = c2.number_input("30m", 0.0, 30.0, float(DEFAULT_CONFIG["sqz_pts_30"]),  2.5)
    wt_15  = c3.number_input("15m", 0.0, 30.0, float(DEFAULT_CONFIG["sqz_pts_15"]),  2.5)

    # ── Exits
    st.subheader("📍 Exit Parameters")
    c1, c2, c3 = st.columns(3)
    tp1_atr = c1.number_input("TP1 ATR", 0.5, 10.0, float(DEFAULT_CONFIG["tp1_atr"]), 0.5)
    tp2_atr = c2.number_input("TP2 ATR", 0.5, 10.0, float(DEFAULT_CONFIG["tp2_atr"]), 0.5)
    sl_atr  = c3.number_input("SL ATR",  0.5, 10.0, float(DEFAULT_CONFIG["sl_atr"]),  0.5)

    runner_stop = st.selectbox(
        "Runner stop after TP1",
        ["Original stop (v1 baseline)", "Breakeven +1 ATR (v2)"],
        index=0 if DEFAULT_CONFIG.get("runner_stop_mode", "v1") == "v1" else 1,
        help="v1 keeps the -SL ATR stop on the second contract after TP1 fills "
             "(matches the TradingView V1 backtest). v2 raises it to entry +1 ATR.")

    time_exit_on = st.checkbox(
        "Time exit", value=int(DEFAULT_CONFIG["time_exit_bars"]) > 0,
        help="Off = trades only exit at TP1/TP2/stop (matches the V1 baseline)")
    time_exit = st.number_input("Time-exit bars", 5, 500,
                                 max(int(DEFAULT_CONFIG["time_exit_bars"]), 30),
                                 disabled=not time_exit_on)

    # ── BB entry
    with st.expander("BB Entry (advanced)"):
        bb_len = st.number_input("BB Length",  5, 50, int(DEFAULT_CONFIG["bb_len"]))
        bb_dev = st.number_input("BB StdDev",  0.5, 3.0, float(DEFAULT_CONFIG["bb_dev"]), 0.1)
        sqz_len = st.number_input("Sqz Length", 10, 40, int(DEFAULT_CONFIG["sqz_len"]))

    st.divider()
    run_btn  = st.button("▶  Run Backtest", type="primary", use_container_width=True)
    save_btn = st.button("📌 Save as Baseline", use_container_width=True)
    st.divider()

    # ── Saved Tests
    with st.expander("💾 Saved Tests"):
        st.markdown("**Save current results**")
        _save_name  = st.text_input("Test name", placeholder="e.g. 8yr 195m high-score")
        _save_notes = st.text_area("Notes (optional)", height=60)
        _can_save   = st.session_state.results is not None and bool(_save_name.strip())
        if st.button("💾 Save current results", disabled=not _can_save,
                     use_container_width=True):
            _fname = save_test(
                name=_save_name.strip(),
                cfg=st.session_state.cfg_used or cfg,
                results=st.session_state.results,
                notes=_save_notes.strip(),
            )
            st.toast(f"Saved as {_fname}", icon="💾")

        st.markdown("---")
        st.markdown("**Load a saved test**")
        _saved_list = list_saved_tests()
        if not _saved_list:
            st.caption("No saved tests yet.")
        else:
            _saved_options = {
                f"{t['name']}  ({t['saved_at'][:16]})": t["fname"]
                for t in _saved_list
            }
            _sel_label = st.selectbox("Select saved test", list(_saved_options.keys()),
                                      label_visibility="collapsed")
            if st.button("📂 Load", use_container_width=True):
                _loaded = load_saved_test(_saved_options[_sel_label])
                st.session_state.results         = _loaded["results"]
                st.session_state.cfg_used        = _loaded["cfg"]
                st.session_state.saved_test_meta = _loaded
                st.toast(f"Loaded: {_loaded['name']}", icon="📂")
                st.rerun()

    # ── API key status
    if _trm.MASSIVE_API_KEY:
        st.success("API key ✓", icon="🔑")
    else:
        api_key_input = st.text_input("Polygon/Massive API Key", type="password",
                                       help="Or set POLYGON_API_KEY env var")
        if api_key_input:
            _trm.MASSIVE_API_KEY = api_key_input

# ─────────────────────────────────────────────────────────────────────────────
# Build config from sidebar values
# ─────────────────────────────────────────────────────────────────────────────

cfg = {
    **DEFAULT_CONFIG,
    "tickers":         tickers,
    "start_date":      start_date.strftime("%Y-%m-%d"),
    "end_date":        end_date.strftime("%Y-%m-%d"),
    "chart_tf":        chart_tf,
    "score_threshold": score_thr,
    "min_sqz_bars":    min_sqz,
    "max_sqz_bars":    max_sqz,
    "release_bars":    rel_bars,
    "sqz_pts_78":      wt_78,
    "sqz_pts_30":      wt_30,
    "sqz_pts_15":      wt_15,
    "tp1_atr":         tp1_atr,
    "tp2_atr":         tp2_atr,
    "sl_atr":          sl_atr,
    "runner_stop_mode": "v1" if runner_stop.startswith("Original") else "be+1",
    "time_exit_bars":  time_exit if time_exit_on else 0,
    "bb_len":          bb_len,
    "bb_dev":          bb_dev,
    "sqz_len":         sqz_len,
}

cfg_hash    = _cfg_hash(cfg)
cached_data = load_cached_results(cfg)

# ─────────────────────────────────────────────────────────────────────────────
# Session state  (initialized above after set_page_config)
# ─────────────────────────────────────────────────────────────────────────────
if "saved_test_meta"   not in st.session_state: st.session_state.saved_test_meta   = None  # metadata when viewing a saved test

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────

st.title("📈 TPS Long Strategy v2 — Backtesting Dashboard")
cfg_label = (f"{len(tickers)} tickers  ·  {chart_tf}m  ·  "
             f"Score≥{score_thr:.0f}  ·  "
             f"TP1={tp1_atr}×ATR  TP2={tp2_atr}×ATR  SL={sl_atr}×ATR  ·  "
             f"{start_date} → {end_date}")
st.caption(cfg_label)

cache_status = "💾 Cached result available" if cached_data else "🔄 Not yet run with these parameters"
st.caption(f"Config hash: `{cfg_hash}` — {cache_status}")

# ── Saved-test banner
if st.session_state.saved_test_meta is not None:
    _stm = st.session_state.saved_test_meta
    _saved_date_str = _stm.get("saved_at", "")[:10]
    try:
        _saved_date_str = datetime.fromisoformat(_stm["saved_at"]).strftime("%b %d %Y")
    except Exception:
        pass
    _stm_cfg = _stm.get("cfg", {})
    _stm_tickers = _stm_cfg.get("tickers", [])
    _stm_tf = _stm_cfg.get("chart_tf", "?")
    _stm_start = _stm_cfg.get("start_date", "?")
    _stm_end   = _stm_cfg.get("end_date", "?")
    st.info(
        f"📂 Viewing saved test: **\"{_stm['name']}\"** — saved {_saved_date_str}  |  "
        f"{len(_stm_tickers)} tickers  ·  {_stm_tf}m  ·  {_stm_start} → {_stm_end}"
        + (f"  |  _{_stm['notes']}_" if _stm.get("notes") else ""),
        icon=None,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Run backtest
# ─────────────────────────────────────────────────────────────────────────────

if run_btn:
    if cached_data:
        st.session_state.results         = cached_data
        st.session_state.cfg_used        = cfg
        st.session_state.saved_test_meta = None
        st.toast("Loaded from cache — instant!", icon="⚡")
    else:
        if not _trm.MASSIVE_API_KEY:
            st.error("Set your Polygon/Massive API key in the sidebar first.")
            st.stop()

        progress_bar  = st.progress(0.0)
        status_text   = st.empty()
        ticker_log    = st.empty()
        completed_log = []

        def _progress(i, total, label):
            pct = i / total
            progress_bar.progress(pct)
            status_text.markdown(
                f"**Running** {i}/{total} — `{label}` "
                f"({pct*100:.0f}%)"
            )

        all_trades = run_backtest(tickers, cfg, _progress)
        save_results(cfg, all_trades)
        st.session_state.results         = all_trades
        st.session_state.cfg_used        = cfg
        st.session_state.saved_test_meta = None

        progress_bar.empty()
        status_text.empty()
        st.toast(f"Done! {len(tickers)} tickers completed.", icon="✅")

elif cached_data and st.session_state.results is None:
    # Auto-load if cache matches current params
    st.session_state.results  = cached_data
    st.session_state.cfg_used = cfg

if save_btn and st.session_state.results is not None:
    st.session_state.baseline = {
        "all_trades": st.session_state.results,
        "cfg": st.session_state.cfg_used,
        "label": cfg_label,
    }
    st.toast("Saved as baseline!", icon="📌")

# ─────────────────────────────────────────────────────────────────────────────
# Display results
# ─────────────────────────────────────────────────────────────────────────────

results = st.session_state.results

if results is None:
    st.info("Configure parameters in the sidebar and click **▶ Run Backtest**.")
    st.stop()

stats_df = build_stats_df(results)

# ── Excel export button
_cfg_used = st.session_state.cfg_used or cfg
_xl_filename = (
    f"TPS_Backtest_{_cfg_used['chart_tf']}m"
    f"_{_cfg_used['start_date'][:7]}"
    f"_{_cfg_used['end_date'][:7]}.xlsx"
)
with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as _tmp:
    _tmp_path = _tmp.name
export_to_excel(results, _cfg_used, _tmp_path)
with open(_tmp_path, "rb") as _xf:
    st.download_button(
        label="📥 Export to Excel",
        data=_xf.read(),
        file_name=_xl_filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ── Aggregate stat cards
all_exits_list = [
    df[df["Type"] == "Exit long"]
    for df in results.values()
    if df is not None and not df.empty
]
if all_exits_list:
    all_exits = pd.concat(all_exits_list, ignore_index=True)
    agg_n   = len(all_exits)
    agg_wr  = (all_exits["Net P&L %"] > 0).sum() / agg_n * 100 if agg_n else 0
    agg_avg = all_exits["Net P&L %"].mean() if agg_n else 0
    agg_exp = (
        (all_exits[all_exits["Net P&L %"] > 0]["Net P&L %"].mean() *
         (all_exits["Net P&L %"] > 0).mean()) +
        (all_exits[all_exits["Net P&L %"] <= 0]["Net P&L %"].mean() *
         (all_exits["Net P&L %"] <= 0).mean())
    ) if agg_n else 0
    agg_dur = all_exits["Duration"].mean() if "Duration" in all_exits.columns else 0
    active_tickers = (stats_df["n"] > 0).sum()

    cols = st.columns(6)
    metrics = [
        ("Tickers w/ Trades",  f"{active_tickers}/{len(tickers)}", ""),
        ("Total Exits",        f"{agg_n:,}",                      ""),
        ("Win Rate",           f"{agg_wr:.1f}%",                  "win" if agg_wr >= 55 else "loss"),
        ("Avg Return",         f"{agg_avg:+.3f}%",                "win" if agg_avg > 0 else "loss"),
        ("Expectancy",         f"{agg_exp:+.3f}%",                "win" if agg_exp > 0 else "loss"),
        ("Avg Duration",       f"{agg_dur:.1f}d",                 ""),
    ]
    for col, (lbl, val, cls) in zip(cols, metrics):
        col.markdown(
            f'<div class="metric-card">'
            f'<div class="metric-val {cls}">{val}</div>'
            f'<div class="metric-lbl">{lbl}</div></div>',
            unsafe_allow_html=True
        )
    st.markdown("")

# ── Tabs
has_baseline = st.session_state.baseline is not None
tab_labels = ["🏆 Rankings", "📊 Charts", "🔍 Ticker Detail"]
if has_baseline:
    tab_labels.append("⚖️ Compare")
tabs = st.tabs(tab_labels)

# ── Tab 1: Rankings table
with tabs[0]:
    st.subheader("Ranked Results")

    display = stats_df.copy()
    display["Win Rate"]       = (display["wr"] * 100).round(1).astype(str) + "%"
    display["Avg Return %"]   = display["avg_r"].round(3).astype(str) + "%"
    display["Avg Win %"]      = display["avg_win"].round(3).astype(str) + "%"
    display["Avg Loss %"]     = display["avg_loss"].round(3).astype(str) + "%"
    display["Expectancy"]     = display["exp"].round(4)
    display["Avg Duration"]   = display["avg_dur"].round(1).astype(str) + "d"
    display["Trades"]         = (display["n"] // 2).astype(int)

    show_cols = ["Rank", "ticker", "Trades", "Win Rate", "Avg Return %",
                 "Avg Win %", "Avg Loss %", "Expectancy", "Avg Duration"]
    st.dataframe(
        display[show_cols].rename(columns={"ticker": "Ticker"}),
        use_container_width=True,
        height=min(600, 36 * len(display) + 38),
        hide_index=True,
    )

    # Single download: the Excel workbook (summary + per-ticker trades) is the
    # one export, delivered from the main download button above.

# ── Tab 2: Charts
with tabs[1]:
    active = stats_df[stats_df["n"] > 0].copy()
    if active.empty:
        st.warning("No trades generated — adjust parameters.")
    else:
        c1, c2 = st.columns(2)

        with c1:
            fig = px.bar(
                active.head(30), x="ticker", y="avg_r",
                color="wr",
                color_continuous_scale=["#ff4444", "#ffaa00", "#00cc44"],
                range_color=[0.45, 0.70],
                labels={"ticker": "Ticker", "avg_r": "Avg Return %", "wr": "Win Rate"},
                title="Avg Return % by Ticker (top 30, colored by WR)",
            )
            fig.update_layout(
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=400,
                coloraxis_colorbar=dict(tickformat=".0%"),
            )
            fig.update_traces(marker_line_width=0)
            st.plotly_chart(fig, use_container_width=True)

        with c2:
            fig2 = px.scatter(
                active, x="wr", y="avg_r", text="ticker",
                size=active["n"].clip(lower=2),
                color="score",
                color_continuous_scale="RdYlGn",
                labels={"wr": "Win Rate", "avg_r": "Avg Return %", "score": "Score"},
                title="Win Rate vs Avg Return (bubble = trade count)",
            )
            fig2.update_traces(textposition="top center", textfont_size=9)
            fig2.update_layout(
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=400,
                xaxis=dict(tickformat=".0%"),
            )
            st.plotly_chart(fig2, use_container_width=True)

        # Return distribution
        if all_exits_list:
            fig3 = px.histogram(
                all_exits, x="Net P&L %", nbins=80,
                color_discrete_sequence=["#4fc3f7"],
                title="Return Distribution — all exits",
                labels={"Net P&L %": "Net P&L %"},
            )
            fig3.add_vline(x=0, line_color="white", line_dash="dash", opacity=0.5)
            fig3.add_vline(x=agg_avg, line_color="#69db7c", line_dash="dot",
                           annotation_text=f"Mean {agg_avg:+.3f}%",
                           annotation_font_color="#69db7c")
            fig3.update_layout(
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=320,
            )
            st.plotly_chart(fig3, use_container_width=True)

# ── Tab 3: Ticker detail
with tabs[2]:
    col1, col2 = st.columns([1, 3])
    active_tickers_list = sorted(
        [t for t, df in results.items() if df is not None and not df.empty]
    )
    if not active_tickers_list:
        st.warning("No trades generated.")
    else:
        sel_ticker = col1.selectbox("Select ticker", active_tickers_list)
        df_t = results[sel_ticker]
        if df_t is not None and not df_t.empty:
            exits_t = df_t[df_t["Type"] == "Exit long"]
            pnl_t   = exits_t["Net P&L %"]
            wr_t    = (pnl_t > 0).sum() / len(pnl_t) * 100 if len(pnl_t) else 0
            avg_t   = pnl_t.mean() if len(pnl_t) else 0

            col2.markdown(
                f"**{sel_ticker}** — {len(exits_t)//2} signals  |  "
                f"WR {wr_t:.1f}%  |  Avg {avg_t:+.3f}%  |  "
                f"Best {pnl_t.max():+.3f}%  |  Worst {pnl_t.min():+.3f}%"
            )

            # Equity curve
            cum = (1 + pnl_t / 100).cumprod() - 1
            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=list(range(len(cum))), y=cum * 100,
                mode="lines", line=dict(color="#4fc3f7", width=2),
                fill="tozeroy", fillcolor="rgba(79,195,247,0.08)",
                name="Cumulative return %",
            ))
            fig_eq.update_layout(
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=220,
                title=f"{sel_ticker} — cumulative return (TP2 leg exits)",
                xaxis_title="Trade #", yaxis_title="Cumulative %",
                margin=dict(t=40, b=30),
            )
            st.plotly_chart(fig_eq, use_container_width=True)

            # Trade log
            show_df = exits_t[["Trade #", "Date and time", "Signal",
                                "Price USD", "Net P&L %", "Duration"]].copy()
            show_df["P&L"] = show_df["Net P&L %"].apply(
                lambda x: f"{'🟢' if x > 0 else '🔴'} {x:+.3f}%"
            )
            st.dataframe(
                show_df.drop(columns=["Net P&L %"]).rename(columns={"P&L": "Net P&L %"}),
                use_container_width=True,
                hide_index=True,
                height=min(500, 36 * len(show_df) + 38),
            )

# ── Tab 4: Comparison (if baseline set)
if has_baseline and len(tabs) == 4:
    with tabs[3]:
        baseline = st.session_state.baseline
        st.caption(f"**Baseline:** {baseline['label']}")
        st.caption(f"**Current:** {cfg_label}")

        base_stats = build_stats_df(baseline["all_trades"])

        # Merge on ticker
        merged = stats_df.merge(
            base_stats[["ticker", "wr", "avg_r", "n", "exp"]],
            on="ticker", suffixes=("", "_base")
        )
        merged["ΔWR"]       = (merged["wr"] - merged["wr_base"]) * 100
        merged["ΔAvg R%"]   = merged["avg_r"] - merged["avg_r_base"]
        merged["ΔTrades"]   = (merged["n"] // 2) - (merged["n_base"] // 2)
        merged["ΔExp"]      = merged["exp"] - merged["exp_base"]

        def _color_delta(val):
            if isinstance(val, (int, float)):
                return "color: #69db7c" if val > 0 else ("color: #ff6b6b" if val < 0 else "")
            return ""

        delta_show = merged[["ticker", "ΔWR", "ΔAvg R%", "ΔTrades", "ΔExp"]].copy()
        delta_show["ΔWR"]    = delta_show["ΔWR"].round(2).astype(str) + " pp"
        delta_show["ΔAvg R%"] = delta_show["ΔAvg R%"].round(3).astype(str) + "%"
        delta_show["ΔExp"]   = delta_show["ΔExp"].round(4)

        # Aggregate delta
        cur_agg  = stats_df[stats_df["n"] > 0]["avg_r"].mean()
        base_agg = base_stats[base_stats["n"] > 0]["avg_r"].mean()
        cur_wr   = (stats_df[stats_df["n"] > 0]["wr"]).mean() * 100
        base_wr  = (base_stats[base_stats["n"] > 0]["wr"]).mean() * 100

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("WR change",      f"{cur_wr:.1f}%",  f"{cur_wr-base_wr:+.2f} pp")
        c2.metric("Avg Ret change", f"{cur_agg:.3f}%", f"{cur_agg-base_agg:+.3f}%")
        c3.metric("Current config", f"Score≥{score_thr:.0f} TP2={tp2_atr}×ATR")
        c4.metric("Baseline config",
                  f"Score≥{baseline['cfg']['score_threshold']:.0f} "
                  f"TP2={baseline['cfg']['tp2_atr']}×ATR")

        st.markdown("**Per-ticker delta** (current − baseline):")
        st.dataframe(
            delta_show.rename(columns={"ticker": "Ticker"}),
            use_container_width=True, hide_index=True,
            height=min(600, 36 * len(delta_show) + 38),
        )
