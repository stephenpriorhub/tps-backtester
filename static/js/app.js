/* TPS Backtester — frontend */

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  data: null,
  loading: false,
};

// ── Boot ───────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  bindUI();
  await checkHealth();
  // Auto-run with defaults on load
  runBacktest();
  document.documentElement.classList.add("ready");
});

// ── Health ─────────────────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const h = await apiFetch("/api/health");
    const el = document.getElementById("api-status");
    if (!h.polygon_key_set) {
      el.innerHTML = '<span class="status-dot status-err"></span> No API Key';
    } else {
      el.innerHTML = '<span class="status-dot status-ok"></span> Polygon Connected';
    }
  } catch {
    document.getElementById("api-status").innerHTML =
      '<span class="status-dot status-err"></span> Server Offline';
  }
}

// ── UI Bindings ────────────────────────────────────────────────────────────────
function bindUI() {
  document.getElementById("run-btn").addEventListener("click", runBacktest);
  document.getElementById("ticker-input").addEventListener("keydown", e => {
    if (e.key === "Enter") runBacktest();
  });
  document.getElementById("advanced-toggle").addEventListener("click", () => {
    const el = document.getElementById("advanced-params");
    el.classList.toggle("open");
    document.getElementById("advanced-toggle").textContent =
      el.classList.contains("open") ? "Hide Advanced" : "Show Advanced";
  });
}

// ── API fetch helper ───────────────────────────────────────────────────────────
async function apiFetch(path) {
  const r = await fetch(path);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: r.statusText }));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

// ── Run backtest ───────────────────────────────────────────────────────────────
async function runBacktest() {
  if (state.loading) return;

  const ticker   = (document.getElementById("ticker-input").value || "SPY").toUpperCase().trim();
  const fromDate = document.getElementById("from-date").value;
  const toDate   = document.getElementById("to-date").value;
  const timespan = document.getElementById("timespan-select").value;
  const minSq    = document.getElementById("min-squeeze").value || 5;
  const bbWindow = document.getElementById("bb-window").value || 20;
  const bbStd    = document.getElementById("bb-std").value || 2.0;
  const kcWindow = document.getElementById("kc-window").value || 20;
  const kcMult   = document.getElementById("kc-mult").value || 1.5;

  state.loading = true;
  showLoading(true);
  clearError();

  const params = new URLSearchParams({
    ticker,
    from:        fromDate,
    to:          toDate,
    timespan,
    min_squeeze: minSq,
    bb_window:   bbWindow,
    bb_std:      bbStd,
    kc_window:   kcWindow,
    kc_mult:     kcMult,
  });

  try {
    const data = await apiFetch(`/api/backtest?${params}`);
    state.data  = data;
    renderAll(data);
  } catch (err) {
    showError(err.message || "Backtest failed");
  } finally {
    state.loading = false;
    showLoading(false);
  }
}

// ── Render ─────────────────────────────────────────────────────────────────────
function renderAll(data) {
  renderStats(data.stats, data.ticker);
  renderCharts(data);
  renderTradesTable(data.trades);
}

function renderStats(stats, ticker) {
  const el = document.getElementById("stats-grid");
  if (!stats || stats.total_trades === 0) {
    el.innerHTML = `<div class="stat-card"><div class="stat-value yellow">0</div><div class="stat-label">No Signals Found</div></div>`;
    return;
  }

  const winClass    = stats.win_rate >= 50 ? "green" : "red";
  const returnClass = stats.total_return_pct >= 0 ? "green" : "red";
  const gainClass   = "green";
  const lossClass   = "red";

  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-value ${winClass}">${stats.win_rate}%</div>
      <div class="stat-label">Win Rate</div>
    </div>
    <div class="stat-card">
      <div class="stat-value cyan">${stats.total_trades}</div>
      <div class="stat-label">Closed Trades</div>
    </div>
    <div class="stat-card">
      <div class="stat-value ${returnClass}">${stats.total_return_pct > 0 ? "+" : ""}${stats.total_return_pct}%</div>
      <div class="stat-label">Total Return</div>
    </div>
    <div class="stat-card">
      <div class="stat-value ${gainClass}">+${stats.avg_gain_pct}%</div>
      <div class="stat-label">Avg Gain</div>
    </div>
    <div class="stat-card">
      <div class="stat-value ${lossClass}">${stats.avg_loss_pct}%</div>
      <div class="stat-label">Avg Loss</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.avg_hold_bars}</div>
      <div class="stat-label">Avg Hold (bars)</div>
    </div>
    <div class="stat-card">
      <div class="stat-value green">+${stats.best_trade_pct}%</div>
      <div class="stat-label">Best Trade</div>
    </div>
    <div class="stat-card">
      <div class="stat-value red">${stats.worst_trade_pct}%</div>
      <div class="stat-label">Worst Trade</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.longs}L / ${stats.shorts}S</div>
      <div class="stat-label">Long / Short</div>
    </div>
  `;
}

// ── Charts ─────────────────────────────────────────────────────────────────────
function renderCharts(data) {
  const bars   = data.bars;
  const trades = data.trades;
  const dates  = bars.map(b => b.date);

  // ── Price + BBands + KC + Trade markers ────────────────────────────────────
  const candleTrace = {
    type: "candlestick",
    x:    dates,
    open:  bars.map(b => b.o),
    high:  bars.map(b => b.h),
    low:   bars.map(b => b.l),
    close: bars.map(b => b.c),
    name:  data.ticker,
    increasing: { line: { color: "#2ecc71" } },
    decreasing: { line: { color: "#e74c3c" } },
  };

  const bbUpTrace = {
    type: "scatter", mode: "lines",
    x: dates, y: bars.map(b => b.bb_upper),
    name: "BB Upper", line: { color: "rgba(79,158,255,0.5)", width: 1, dash: "dot" },
    showlegend: true,
  };
  const bbLoTrace = {
    type: "scatter", mode: "lines",
    x: dates, y: bars.map(b => b.bb_lower),
    name: "BB Lower", line: { color: "rgba(79,158,255,0.5)", width: 1, dash: "dot" },
    fill: "tonexty", fillcolor: "rgba(79,158,255,0.04)",
    showlegend: false,
  };
  const kcUpTrace = {
    type: "scatter", mode: "lines",
    x: dates, y: bars.map(b => b.kc_upper),
    name: "KC Upper", line: { color: "rgba(124,92,252,0.5)", width: 1 },
    showlegend: true,
  };
  const kcLoTrace = {
    type: "scatter", mode: "lines",
    x: dates, y: bars.map(b => b.kc_lower),
    name: "KC Lower", line: { color: "rgba(124,92,252,0.5)", width: 1 },
    fill: "tonexty", fillcolor: "rgba(124,92,252,0.04)",
    showlegend: false,
  };

  // Entry/exit markers
  const entryLong = { type: "scatter", mode: "markers", name: "Long Entry",
    x: [], y: [], marker: { color: "#2ecc71", size: 12, symbol: "triangle-up" } };
  const entryShort = { type: "scatter", mode: "markers", name: "Short Entry",
    x: [], y: [], marker: { color: "#e74c3c", size: 12, symbol: "triangle-down" } };
  const exitTrace  = { type: "scatter", mode: "markers", name: "Exit",
    x: [], y: [], marker: { color: "#f39c12", size: 9, symbol: "x" } };

  for (const t of trades) {
    const entryBar = bars[t.entry_idx];
    const exitBar  = bars[t.exit_idx];
    if (!entryBar) continue;
    if (t.direction === "long") {
      entryLong.x.push(entryBar.date);
      entryLong.y.push(entryBar.l * 0.995);
    } else {
      entryShort.x.push(entryBar.date);
      entryShort.y.push(entryBar.h * 1.005);
    }
    if (exitBar) {
      exitTrace.x.push(exitBar.date);
      exitTrace.y.push(exitBar.c);
    }
  }

  const priceLayout = {
    paper_bgcolor: "transparent",
    plot_bgcolor:  "transparent",
    font: { color: "#e0e4f0", size: 11 },
    margin: { t: 10, r: 20, b: 40, l: 60 },
    xaxis: {
      gridcolor: "#1e2330", zeroline: false,
      rangeslider: { visible: false },
      type: "category",
    },
    yaxis: { gridcolor: "#1e2330", zeroline: false },
    legend: { orientation: "h", y: 1.05, bgcolor: "transparent" },
    hovermode: "x unified",
  };

  Plotly.newPlot("chart-price",
    [candleTrace, bbLoTrace, bbUpTrace, kcLoTrace, kcUpTrace, entryLong, entryShort, exitTrace],
    priceLayout,
    { responsive: true, displayModeBar: false }
  );

  // ── Squeeze dots chart ─────────────────────────────────────────────────────
  const sqOnDates  = dates.filter((_, i) => bars[i].squeeze_on);
  const sqOffDates = dates.filter((_, i) => !bars[i].squeeze_on && !bars[i].squeeze_fire);
  const sqFireDates= dates.filter((_, i) => bars[i].squeeze_fire);

  const sqOnTrace = {
    type: "scatter", mode: "markers",
    x: sqOnDates, y: sqOnDates.map(() => 0),
    name: "Squeeze ON", marker: { color: "#333a4d", size: 8, symbol: "circle" },
  };
  const sqOffTrace = {
    type: "scatter", mode: "markers",
    x: sqOffDates, y: sqOffDates.map(() => 0),
    name: "No Squeeze", marker: { color: "#2ecc71", size: 8, symbol: "circle" },
  };
  const sqFireTrace = {
    type: "scatter", mode: "markers",
    x: sqFireDates, y: sqFireDates.map(() => 0),
    name: "Squeeze FIRE", marker: { color: "#ffeb3b", size: 12, symbol: "star" },
  };

  Plotly.newPlot("chart-squeeze",
    [sqOffTrace, sqOnTrace, sqFireTrace],
    {
      paper_bgcolor: "transparent", plot_bgcolor: "transparent",
      font: { color: "#e0e4f0", size: 10 },
      margin: { t: 4, r: 20, b: 30, l: 60 },
      xaxis: { gridcolor: "#1e2330", zeroline: false, type: "category" },
      yaxis: { gridcolor: "#1e2330", zeroline: false, showticklabels: false,
               range: [-0.5, 0.5], fixedrange: true },
      showlegend: true,
      legend: { orientation: "h", y: 1.2, bgcolor: "transparent", font: { size: 10 } },
      title: { text: "TTM Squeeze Dots", font: { size: 11, color: "#8892a4" }, x: 0.01 },
    },
    { responsive: true, displayModeBar: false }
  );

  // ── Momentum histogram ─────────────────────────────────────────────────────
  const momColors = bars.map(b => {
    const c = b.mom_color;
    if (c === "green_up") return "#2ecc71";
    if (c === "green_dn") return "#1a7a44";
    if (c === "red_dn")   return "#e74c3c";
    if (c === "red_up")   return "#7a2020";
    return "#444";
  });

  const momTrace = {
    type: "bar",
    x: dates,
    y: bars.map(b => b.momentum),
    name: "Momentum",
    marker: { color: momColors },
  };

  Plotly.newPlot("chart-momentum",
    [momTrace],
    {
      paper_bgcolor: "transparent", plot_bgcolor: "transparent",
      font: { color: "#e0e4f0", size: 10 },
      margin: { t: 4, r: 20, b: 30, l: 60 },
      xaxis: { gridcolor: "#1e2330", zeroline: false, type: "category" },
      yaxis: { gridcolor: "#1e2330", zerolinecolor: "#3a4155", zerolinewidth: 1 },
      bargap: 0.1,
      title: { text: "Momentum Oscillator", font: { size: 11, color: "#8892a4" }, x: 0.01 },
    },
    { responsive: true, displayModeBar: false }
  );
}

// ── Trades table ───────────────────────────────────────────────────────────────
function renderTradesTable(trades) {
  const wrap = document.getElementById("trades-wrap");
  if (!trades || trades.length === 0) {
    wrap.innerHTML = `<div class="section-title">Trade Log</div>
      <div class="no-signals">No trades found. Try adjusting parameters or expanding the date range.</div>`;
    return;
  }

  let rows = "";
  for (const t of [...trades].reverse()) {
    const dirBadge  = t.direction === "long"
      ? '<span class="badge badge-long">LONG</span>'
      : '<span class="badge badge-short">SHORT</span>';
    const winBadge  = t.win
      ? '<span class="badge badge-win">WIN</span>'
      : '<span class="badge badge-loss">LOSS</span>';
    const pnlClass  = t.pnl_pct >= 0 ? "green" : "red";
    const pnlSign   = t.pnl_pct >= 0 ? "+" : "";
    const openMark  = t.open_trade ? " *" : "";

    rows += `<tr>
      <td>${t.entry_date}</td>
      <td>${t.exit_date}${openMark}</td>
      <td>${dirBadge}</td>
      <td>$${t.entry_price.toFixed(2)}</td>
      <td>$${t.exit_price.toFixed(2)}</td>
      <td class="${pnlClass}">${pnlSign}${t.pnl_pct}%</td>
      <td>${t.hold_bars}</td>
      <td>${winBadge}</td>
    </tr>`;
  }

  wrap.innerHTML = `
    <div class="section-title">Trade Log (${trades.length} trades, newest first)</div>
    <table>
      <thead>
        <tr>
          <th>Entry</th><th>Exit</th><th>Dir</th>
          <th>Entry $</th><th>Exit $</th>
          <th>P&amp;L %</th><th>Bars</th><th>Result</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function showLoading(on) {
  document.getElementById("loading").classList.toggle("hidden", !on);
  document.getElementById("results").classList.toggle("hidden", on);
}

function showError(msg) {
  const el = document.getElementById("error-box");
  el.textContent = "Error: " + msg;
  el.classList.remove("hidden");
}

function clearError() {
  document.getElementById("error-box").classList.add("hidden");
}
