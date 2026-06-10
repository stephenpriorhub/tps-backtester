"""
TPS Backtester — Flask backend
Nate Bear's Trend → Pattern → Squeeze system

Data source: Polygon.io (POLYGON_API_KEY)
"""

import os
import json
import time
import math
import pickle
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
BASE_URL    = "https://api.polygon.io"

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

# ── Two-layer cache ─────────────────────────────────────────────────────────────
_mem: dict = {}

def _cache_path(key: str) -> Path:
    h = hashlib.md5(key.encode()).hexdigest()
    return CACHE_DIR / f"{h}.pkl"

def _cache_get(key: str, ttl: int):
    if key in _mem:
        data, ts = _mem[key]
        if time.time() - ts < ttl:
            return data
    disk = _cache_path(key)
    if disk.exists() and (time.time() - disk.stat().st_mtime) < ttl:
        try:
            with open(disk, "rb") as f:
                data = pickle.load(f)
            _mem[key] = (data, time.time())
            return data
        except Exception:
            pass
    return None

def _cache_set(key: str, data):
    _mem[key] = (data, time.time())
    try:
        with open(_cache_path(key), "wb") as f:
            pickle.dump(data, f)
    except Exception:
        pass

# ── Polygon helpers ─────────────────────────────────────────────────────────────
def api_get(path: str, params: Optional[dict] = None, ttl: int = 3600) -> dict:
    if params is None:
        params = {}
    key = f"poly|{path}|{json.dumps(params, sort_keys=True)}"
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached

    p = {**params, "apiKey": POLYGON_KEY}
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE_URL}{path}", params=p, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            data = r.json()
            _cache_set(key, data)
            return data
        except requests.RequestException as exc:
            if attempt == 3:
                log.error("Polygon error %s: %s", path, exc)
                return {}
            time.sleep(1 + attempt)
    return {}

def ts_to_date(ts_ms: int) -> str:
    return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")

def get_bars(ticker: str, from_d: str, to_d: str, timespan: str = "day") -> list[dict]:
    """Fetch OHLCV bars from Polygon."""
    multiplier = 1
    if timespan == "week":
        timespan  = "week"
        multiplier = 1
    path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_d}/{to_d}"
    raw  = api_get(path, {"adjusted": "true", "sort": "asc", "limit": 5000}, ttl=86400)
    bars = []
    for b in raw.get("results", []):
        bars.append({
            "date": ts_to_date(b["t"]),
            "t":    b["t"],
            "o":    b["o"],
            "h":    b["h"],
            "l":    b["l"],
            "c":    b["c"],
            "v":    b.get("v", 0),
        })
    return bars

# ── Technical indicators ────────────────────────────────────────────────────────

def compute_sma(values: list[float], window: int) -> list[Optional[float]]:
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
        else:
            result.append(sum(values[i - window + 1:i + 1]) / window)
    return result

def compute_std(values: list[float], window: int) -> list[Optional[float]]:
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
        else:
            w    = values[i - window + 1:i + 1]
            mean = sum(w) / window
            var  = sum((x - mean) ** 2 for x in w) / window
            result.append(math.sqrt(var))
    return result

def compute_atr(bars: list[dict], window: int = 14) -> list[Optional[float]]:
    """True Range and ATR (Wilder's smoothing)."""
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            prev_c = bars[i - 1]["c"]
            tr = max(b["h"] - b["l"],
                     abs(b["h"] - prev_c),
                     abs(b["l"] - prev_c))
            trs.append(tr)

    # Wilder's smoothing
    atrs: list[Optional[float]] = [None] * len(trs)
    for i in range(len(trs)):
        if i + 1 < window:
            continue
        if atrs[i - 1] is None:
            # Seed with simple average
            atrs[i] = sum(trs[i - window + 1:i + 1]) / window
        else:
            atrs[i] = (atrs[i - 1] * (window - 1) + trs[i]) / window
    return atrs

def compute_bollinger(closes: list[float], window: int = 20,
                      num_std: float = 2.0) -> tuple:
    """Return (upper, lower, mid) as parallel lists."""
    sma = compute_sma(closes, window)
    std = compute_std(closes, window)
    upper, lower, mid = [], [], []
    for i in range(len(closes)):
        if sma[i] is None or std[i] is None:
            upper.append(None); lower.append(None); mid.append(None)
        else:
            upper.append(sma[i] + num_std * std[i])
            lower.append(sma[i] - num_std * std[i])
            mid.append(sma[i])
    return upper, lower, mid

def compute_keltner(bars: list[dict], window: int = 20,
                    mult: float = 1.5) -> tuple:
    """Return (upper, lower, mid) Keltner Channels."""
    closes = [b["c"] for b in bars]
    sma    = compute_sma(closes, window)
    atr    = compute_atr(bars, window)
    upper, lower = [], []
    for i in range(len(bars)):
        if sma[i] is None or atr[i] is None:
            upper.append(None); lower.append(None)
        else:
            upper.append(sma[i] + mult * atr[i])
            lower.append(sma[i] - mult * atr[i])
    return upper, lower

def compute_momentum(closes: list[float], window: int = 12) -> list[Optional[float]]:
    """
    TTM Squeeze momentum oscillator approximation.
    Linear regression of (close - midpoint of BB/KC average) over `window` bars.
    We approximate as: close - ((highest_high + lowest_low) / 2 + sma) / 2
    over `window` bars, then compute the slope via linreg.

    Simpler but effective: use the difference of close from a rolling midpoint,
    then apply a linear regression oscillator (value = linreg forecast at bar i).
    """
    n = len(closes)
    values: list[Optional[float]] = [None] * n

    # Delta = close minus rolling midpoint (same as TTM Squeeze hist)
    # Midpoint = (highest + lowest) / 2 using closes as proxy
    high_low_mid = []
    for i in range(n):
        w = closes[max(0, i - window + 1):i + 1]
        hh = max(w)
        ll = min(w)
        sma_w = sum(w) / len(w)
        # TTM momentum = close minus avg(midpoint, sma)
        mid = (hh + ll) / 2
        avg_mid = (mid + sma_w) / 2
        high_low_mid.append(closes[i] - avg_mid)

    # Smooth with linear regression slope (window bars)
    for i in range(n):
        if i + 1 < window:
            continue
        y = high_low_mid[i - window + 1:i + 1]
        x_mean = (window - 1) / 2
        y_mean = sum(y) / window
        num = sum((j - x_mean) * (y[j] - y_mean) for j in range(window))
        den = sum((j - x_mean) ** 2 for j in range(window))
        if den == 0:
            values[i] = 0.0
        else:
            slope     = num / den
            intercept = y_mean - slope * x_mean
            values[i] = round(slope * (window - 1) + intercept, 6)

    return values

# ── TPS Squeeze detection ───────────────────────────────────────────────────────

def detect_squeeze(bars: list[dict],
                   bb_window: int = 20, bb_std: float = 2.0,
                   kc_window: int = 20, kc_mult: float = 1.5) -> list[dict]:
    """
    For each bar return squeeze state and momentum.
    squeeze_on  = True  → BB fully inside KC (black dot)
    squeeze_off = True  → first bar where BB exits KC after being inside (green/red dot = entry signal)
    momentum    = float → linear regression oscillator value
    momentum_color: 'green_up' | 'green_dn' | 'red_up' | 'red_dn'
    """
    closes = [b["c"] for b in bars]
    bb_up, bb_lo, bb_mid = compute_bollinger(closes, bb_window, bb_std)
    kc_up, kc_lo         = compute_keltner(bars, kc_window, kc_mult)
    momentum             = compute_momentum(closes, bb_window)

    results = []
    prev_squeeze_on = False

    for i, b in enumerate(bars):
        bu = bb_up[i]; bl = bb_lo[i]
        ku = kc_up[i]; kl = kc_lo[i]
        mom = momentum[i]

        squeeze_on = False
        if bu is not None and bl is not None and ku is not None and kl is not None:
            # Squeeze ON = BB fully inside KC
            squeeze_on = (bu <= ku) and (bl >= kl)

        # Squeeze fires = first bar where BB exits KC after being inside
        squeeze_fire = (not squeeze_on) and prev_squeeze_on and (bu is not None)

        # Momentum color (TTM Squeeze style)
        prev_mom = momentum[i - 1] if i > 0 else None
        if mom is None:
            mom_color = "none"
        elif mom >= 0:
            mom_color = "green_up" if (prev_mom is None or mom >= prev_mom) else "green_dn"
        else:
            mom_color = "red_dn" if (prev_mom is None or mom <= prev_mom) else "red_up"

        results.append({
            "date":         b["date"],
            "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "v":            b["v"],
            "bb_upper":     round(bu, 4) if bu is not None else None,
            "bb_lower":     round(bl, 4) if bl is not None else None,
            "bb_mid":       round(bb_mid[i], 4) if bb_mid[i] is not None else None,
            "kc_upper":     round(ku, 4) if ku is not None else None,
            "kc_lower":     round(kl, 4) if kl is not None else None,
            "squeeze_on":   squeeze_on,
            "squeeze_fire": squeeze_fire,
            "momentum":     round(mom, 6) if mom is not None else None,
            "mom_color":    mom_color,
        })
        prev_squeeze_on = squeeze_on

    return results

# ── TPS Trade simulation ────────────────────────────────────────────────────────

def simulate_tps_trades(bars_with_squeeze: list[dict],
                        min_squeeze_bars: int = 5) -> list[dict]:
    """
    Entry rules:
      - Fire bar (squeeze_fire=True) after min_squeeze_bars consecutive squeeze ON
      - Direction: long if momentum > 0 at fire, short if momentum < 0
      - If momentum is None at fire, skip

    Exit rules:
      - 2nd momentum reversal bar (color flip direction)
      - For longs: 2 bars of mom_color starting with 'red' (was green → red)
      - For shorts: 2 bars of mom_color starting with 'green' (was red → green)
      - Also exit at end of data

    Returns list of trade dicts.
    """
    n      = len(bars_with_squeeze)
    trades = []

    # Count consecutive squeeze bars leading into each position
    squeeze_count = 0
    in_trade      = False
    trade_dir     = None
    entry_bar     = None
    entry_price   = None
    reversal_count = 0

    for i, bar in enumerate(bars_with_squeeze):
        if not in_trade:
            # Track squeeze run
            if bar["squeeze_on"]:
                squeeze_count += 1
            elif bar["squeeze_fire"]:
                # Check if we had enough squeeze bars
                if squeeze_count >= min_squeeze_bars and bar["momentum"] is not None:
                    # Determine direction from momentum
                    mom = bar["momentum"]
                    if mom > 0:
                        direction = "long"
                    elif mom < 0:
                        direction = "short"
                    else:
                        squeeze_count = 0
                        continue

                    in_trade      = True
                    trade_dir     = direction
                    entry_bar     = i
                    entry_price   = bar["c"]  # enter on close of fire bar
                    reversal_count = 0
                    log.debug("ENTRY %s %s @ %.2f (squeeze=%d)",
                              direction, bar["date"], entry_price, squeeze_count)
                squeeze_count = 0  # reset after fire regardless
            else:
                squeeze_count = 0  # no squeeze, no fire — reset
        else:
            # We're in a trade — look for 2nd momentum reversal
            mc = bar["mom_color"]
            if trade_dir == "long":
                is_reversal = mc in ("red_dn", "red_up")
            else:
                is_reversal = mc in ("green_up", "green_dn")

            if is_reversal:
                reversal_count += 1
                if reversal_count >= 2:
                    # Exit on close of 2nd reversal bar
                    exit_price = bar["c"]
                    exit_date  = bar["date"]
                    pnl_pct    = ((exit_price - entry_price) / entry_price * 100
                                  if trade_dir == "long"
                                  else (entry_price - exit_price) / entry_price * 100)
                    hold_days  = i - entry_bar
                    trades.append({
                        "entry_date":  bars_with_squeeze[entry_bar]["date"],
                        "exit_date":   exit_date,
                        "direction":   trade_dir,
                        "entry_price": round(entry_price, 2),
                        "exit_price":  round(exit_price, 2),
                        "pnl_pct":     round(pnl_pct, 2),
                        "hold_bars":   hold_days,
                        "win":         pnl_pct > 0,
                        "entry_idx":   entry_bar,
                        "exit_idx":    i,
                        "squeeze_bars_before": squeeze_count,
                    })
                    in_trade       = False
                    trade_dir      = None
                    reversal_count = 0
                    squeeze_count  = 0
            else:
                reversal_count = 0  # reset if reversal stalls

    # Close any open trade at end of data
    if in_trade and entry_bar is not None:
        last      = bars_with_squeeze[-1]
        exit_price = last["c"]
        pnl_pct    = ((exit_price - entry_price) / entry_price * 100
                      if trade_dir == "long"
                      else (entry_price - exit_price) / entry_price * 100)
        trades.append({
            "entry_date":  bars_with_squeeze[entry_bar]["date"],
            "exit_date":   last["date"] + " (open)",
            "direction":   trade_dir,
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "pnl_pct":     round(pnl_pct, 2),
            "hold_bars":   len(bars_with_squeeze) - 1 - entry_bar,
            "win":         pnl_pct > 0,
            "entry_idx":   entry_bar,
            "exit_idx":    len(bars_with_squeeze) - 1,
            "squeeze_bars_before": squeeze_count,
            "open_trade":  True,
        })

    return trades

def compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "avg_gain_pct": 0,
            "avg_loss_pct": 0,
            "avg_hold_bars": 0,
            "total_return_pct": 0,
            "best_trade_pct": 0,
            "worst_trade_pct": 0,
            "longs": 0,
            "shorts": 0,
        }
    closed = [t for t in trades if not t.get("open_trade")]
    winners = [t for t in closed if t["win"]]
    losers  = [t for t in closed if not t["win"]]
    win_rate = len(winners) / len(closed) * 100 if closed else 0
    avg_gain = sum(t["pnl_pct"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl_pct"] for t in losers)  / len(losers)  if losers  else 0
    avg_hold = sum(t["hold_bars"] for t in closed) / len(closed)  if closed  else 0
    # Compound total return
    compound = 1.0
    for t in closed:
        compound *= (1 + t["pnl_pct"] / 100)
    total_return = (compound - 1) * 100
    pnls = [t["pnl_pct"] for t in closed]
    return {
        "total_trades":    len(closed),
        "open_trades":     len(trades) - len(closed),
        "win_rate":        round(win_rate, 1),
        "avg_gain_pct":    round(avg_gain, 2),
        "avg_loss_pct":    round(avg_loss, 2),
        "avg_hold_bars":   round(avg_hold, 1),
        "total_return_pct": round(total_return, 2),
        "best_trade_pct":  round(max(pnls), 2) if pnls else 0,
        "worst_trade_pct": round(min(pnls), 2) if pnls else 0,
        "longs":           len([t for t in closed if t["direction"] == "long"]),
        "shorts":          len([t for t in closed if t["direction"] == "short"]),
    }

# ── Flask routes ────────────────────────────────────────────────────────────────
@app.route("/")
def serve_index():
    return send_from_directory("static", "index.html")

@app.route("/static/<path:p>")
def serve_static(p):
    return send_from_directory("static", p)

@app.route("/api/health")
def health():
    return jsonify({
        "ok":              True,
        "polygon_key_set": bool(POLYGON_KEY),
    })

@app.route("/api/backtest")
def api_backtest():
    ticker     = request.args.get("ticker", "SPY").upper().strip()
    from_d     = request.args.get("from",
                  (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"))
    to_d       = request.args.get("to",
                  datetime.now().strftime("%Y-%m-%d"))
    timespan   = request.args.get("timespan", "day")  # "day" | "week"
    min_sq     = int(request.args.get("min_squeeze", 5))
    bb_window  = int(request.args.get("bb_window", 20))
    bb_std     = float(request.args.get("bb_std", 2.0))
    kc_window  = int(request.args.get("kc_window", 20))
    kc_mult    = float(request.args.get("kc_mult", 1.5))

    if timespan not in ("day", "week"):
        timespan = "day"

    if not POLYGON_KEY:
        return jsonify({"error": "POLYGON_API_KEY not set"}), 500

    # Extra lookback for indicator warmup (need bb_window bars before range start)
    warmup_days = bb_window * 4  # generous padding
    if timespan == "week":
        warmup_days = bb_window * 14  # weeks need more calendar days

    warmup_from = (datetime.strptime(from_d, "%Y-%m-%d") - timedelta(days=warmup_days)).strftime("%Y-%m-%d")

    log.info("TPS backtest: %s %s → %s (%s)", ticker, from_d, to_d, timespan)
    bars = get_bars(ticker, warmup_from, to_d, timespan)

    if not bars:
        return jsonify({"error": f"No data returned for {ticker}"}), 404

    # Run indicators on full dataset (including warmup)
    squeezed = detect_squeeze(bars, bb_window, bb_std, kc_window, kc_mult)

    # Trim to requested range for display and trade simulation
    cutoff   = next((i for i, b in enumerate(squeezed) if b["date"] >= from_d), 0)
    trimmed  = squeezed[cutoff:]

    trades   = simulate_tps_trades(trimmed, min_sq)
    stats    = compute_stats(trades)

    # Build chart-ready series (trim Nones to save bandwidth)
    chart_bars = []
    for b in trimmed:
        chart_bars.append({
            "date":         b["date"],
            "o":            b["o"],
            "h":            b["h"],
            "l":            b["l"],
            "c":            b["c"],
            "v":            b["v"],
            "bb_upper":     b["bb_upper"],
            "bb_lower":     b["bb_lower"],
            "bb_mid":       b["bb_mid"],
            "kc_upper":     b["kc_upper"],
            "kc_lower":     b["kc_lower"],
            "squeeze_on":   b["squeeze_on"],
            "squeeze_fire": b["squeeze_fire"],
            "momentum":     b["momentum"],
            "mom_color":    b["mom_color"],
        })

    return jsonify({
        "ticker":   ticker,
        "from":     from_d,
        "to":       to_d,
        "timespan": timespan,
        "bars":     chart_bars,
        "trades":   trades,
        "stats":    stats,
        "params": {
            "bb_window": bb_window,
            "bb_std":    bb_std,
            "kc_window": kc_window,
            "kc_mult":   kc_mult,
            "min_squeeze_bars": min_sq,
        },
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
