const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const number   = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });

let activePeriod      = "today";
let rejectionsFilter  = false;
let activeStrategy = "all";
let latestState    = {
  summary: {}, risk: {}, balance: {}, performance: [], positions: [],
  signals: [], history: [], analytics: { equity_curve: [], status_counts: {}, symbol_exposure: [] },
};

const equityState2 = { points: [], pad: null, width: 0, height: 0 };

let pnlRange = "1D";
const pnlChartState          = { data: null };
const signalActivityState    = { data: null };

const SIGNAL_DATA = [
  { t: "09:30", passed: 4, blocked: 0 },
  { t: "10:00", passed: 7, blocked: 1 },
  { t: "10:30", passed: 3, blocked: 2 },
  { t: "11:00", passed: 9, blocked: 0 },
  { t: "11:30", passed: 5, blocked: 1 },
  { t: "12:00", passed: 2, blocked: 3 },
  { t: "12:30", passed: 6, blocked: 0 },
  { t: "13:00", passed: 8, blocked: 1 },
  { t: "13:30", passed: 4, blocked: 2 },
  { t: "14:00", passed: 3, blocked: 0 },
  { t: "14:30", passed: 1, blocked: 3 },
];

const PNL_DATA = {
  "1D": [
    { t: "09:30", pnl: 0 },    { t: "09:45", pnl: 420 },
    { t: "10:00", pnl: 890 },  { t: "10:15", pnl: 1350 },
    { t: "10:30", pnl: 1820 }, { t: "10:45", pnl: 2100 },
    { t: "11:00", pnl: 1650 }, { t: "11:15", pnl: 1200 },
    { t: "11:30", pnl: 650 },  { t: "11:45", pnl: 100 },
    { t: "12:00", pnl: -450 }, { t: "12:15", pnl: -1200 },
    { t: "12:30", pnl: -2100 },{ t: "12:45", pnl: -3100 },
    { t: "13:00", pnl: -4200 },{ t: "13:15", pnl: -5100 },
    { t: "13:30", pnl: -5900 },{ t: "13:45", pnl: -6600 },
    { t: "14:00", pnl: -7100 },{ t: "14:15", pnl: -7500 },
    { t: "14:30", pnl: -7820 },
  ],
  "5D": [
    { t: "Mon", pnl: 3200 }, { t: "Tue", pnl: 1800 },
    { t: "Wed", pnl: -2100 }, { t: "Thu", pnl: -4600 }, { t: "Fri", pnl: -7820 },
  ],
  "MTD": [
    { t: "Jun 2", pnl: 1200 },  { t: "Jun 3", pnl: 2800 },
    { t: "Jun 4", pnl: 1900 },  { t: "Jun 5", pnl: 3500 },
    { t: "Jun 6", pnl: 2100 },  { t: "Jun 9", pnl: 4200 },
    { t: "Jun 10", pnl: 3100 }, { t: "Jun 11", pnl: 1500 },
    { t: "Jun 12", pnl: -800 }, { t: "Jun 13", pnl: -3200 },
    { t: "Jun 16", pnl: -7820 },
  ],
};

// ─── Helpers ────────────────────────────────────────────────────────────────

function utc(str) {
  if (!str) return null;
  return str.endsWith("Z") || str.includes("+") ? str : str + "Z";
}

function fmtDate(v) {
  if (!v) return "—";
  return new Date(utc(v)).toLocaleString(undefined, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function pnlClass(v) { return v > 0 ? "positive" : v < 0 ? "negative" : "neutral"; }

function statusBadge(status) {
  const ok  = ["enabled", "open", "executed", "closed", "accepted"];
  const bad = ["rejected", "failed"];
  return `<span class="badge ${ok.includes(status) ? "ok" : bad.includes(status) ? "bad" : "warn"}">${status}</span>`;
}

function emptyRow(cols, label) {
  return `<tr><td colspan="${cols}" class="empty">${label}</td></tr>`;
}

function escapeAttr(v) {
  return String(v).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
}

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

function cumulativeCurve(points) {
  let t = 0;
  return points.map(p => { t += p.net_result; return { ...p, cumulative_net: t }; });
}

function timeAgo(dateStr) {
  if (!dateStr) return "Never";
  const s = Math.floor((Date.now() - new Date(utc(dateStr))) / 1000);
  if (s >= 86400) return new Date(utc(dateStr)).toLocaleDateString();
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h ${m}m ${sec}s`;
}

function duration(start, end) {
  const s = Math.max(0, Math.floor((new Date(end) - new Date(start)) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Request failed: ${url}`);
  return r.json();
}

async function patchJson(url, body) {
  const r = await fetch(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`Update failed: ${url}`);
  return r.json();
}

// ─── Navigation ──────────────────────────────────────────────────────────────

function navigate(pageId) {
  document.querySelectorAll(".nav-item").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.page === pageId);
  });
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("active", p.id === `page-${pageId}`);
  });
  document.querySelector(".main-content").scrollTo(0, 0);
  // Re-draw canvases that were hidden during last render
  if (pageId !== "trading-bots") {
    renderAll();
  }
  if (pageId === "trading-bots") {
    refreshBots();
    wireBotForm();
  }
}

document.querySelectorAll(".nav-item[data-page]").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

// nav-link-btn (inline "go to page" links inside content)
document.querySelectorAll(".nav-link-btn[data-page]").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

// ─── Render: Summary cards ───────────────────────────────────────────────────

function renderSummary() {
  const { summary, risk, balance, signals } = latestState;

  const modeEl   = document.querySelector("#liveMode");
  modeEl.textContent = (summary.execution_mode || "paper").toUpperCase();
  modeEl.className   = `live-badge ${summary.execution_mode === "live" ? "live" : "paper"}`;

  const stopEl   = document.querySelector("#stopLabel");
  stopEl.textContent = summary.emergency_stop ? "EMERGENCY STOP" : "controls active";
  stopEl.className   = `stop-label ${summary.emergency_stop ? "danger" : ""}`;

  const dailyPnl  = risk.today_account_net || 0;
  const maxExp    = risk.max_account_exposure || 0;
  const curExp    = risk.current_account_exposure || 0;
  const lossLimit = risk.account_daily_loss_limit || 0;
  const marginPct = maxExp ? Math.round(curExp / maxExp * 100) : null;
  const marginCls = marginPct === null ? "neutral" : marginPct > 80 ? "negative" : "positive";
  const lossUsedPct = lossLimit ? Math.round(Math.abs(Math.min(0, dailyPnl)) / lossLimit * 100) : null;

  const lastSig    = signals[0]?.created_at ?? null;
  const lastSigAge = lastSig ? Math.floor((Date.now() - new Date(utc(lastSig))) / 1000) : Infinity;
  const sigCls     = lastSigAge < 300 ? "positive" : lastSigAge < 1800 ? "negative" : "neutral";

  const isLive = balance?.mode === "live";
  const equity = isLive ? (balance?.equity ?? null) : 12450.00;
  const avail  = isLive ? (balance?.available ?? null) : 8320.00;
  const upl    = isLive ? (balance?.unrealized_pnl ?? null) : 230.50;
  const balVal     = equity !== null ? currency.format(equity) : "—";
  const balSub     = avail !== null
    ? `${currency.format(avail)} avail · UPL ${upl !== null ? (upl >= 0 ? "+" : "") + currency.format(upl) : "—"}${!isLive ? " (demo)" : ""}`
    : "failed to fetch";
  const gaugeRatio = (equity && avail != null && equity > 0) ? Math.min(avail / equity, 1) : 0;
  const gaugeClr   = gaugeRatio < 0.25 ? "var(--red)" : gaugeRatio < 0.5 ? "var(--yellow)" : "var(--green)";

  document.querySelector("#summary").innerHTML = `
    <div class="metrics-highlights" style="grid-template-columns:repeat(3,1fr)">
      <div class="metric highlight">
        <span class="metric-label">Account Balance</span>
        <span class="metric-value neutral">${balVal}</span>
        <span class="metric-sub">${balSub}</span>
      </div>
      <div class="metric highlight" style="flex-direction:row;align-items:center;gap:14px;padding:20px 22px">
        <div style="flex-shrink:0;width:72px;height:72px">
          <canvas id="balanceGauge" height="72" style="height:72px;width:72px"></canvas>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;min-width:0">
          <span class="metric-label">Available</span>
          <span style="font-size:19px;font-weight:700;font-family:monospace;color:${gaugeClr}">${avail !== null ? currency.format(avail) : "—"}</span>
          <span style="font-size:11px;color:var(--muted)">of ${balVal}${!isLive ? " · demo" : ""}</span>
        </div>
      </div>
      <div class="metric highlight">
        <span class="metric-label">Daily P&amp;L</span>
        <span class="metric-value ${pnlClass(dailyPnl)}">${currency.format(dailyPnl)}</span>
        <span class="metric-sub">${lossUsedPct !== null ? lossUsedPct + "% of limit" : "no limit set"}</span>
      </div>
    </div>
    <div class="metrics">
      <div class="metric">
        <span class="metric-label">Open Positions</span>
        <span class="metric-value ${(summary.open_positions ?? 0) > 0 ? "positive" : "neutral"}">${summary.open_positions ?? 0}</span>
        <span class="metric-sub">${summary.signals_logged ?? 0} signals received</span>
      </div>
      <div class="metric">
        <span class="metric-label">Signals Blocked</span>
        <span class="metric-value ${(summary.signals_rejected ?? 0) > 0 ? "negative" : "neutral"}">${summary.signals_rejected ?? 0}</span>
        <span class="metric-sub">of ${summary.signals_logged ?? 0} total</span>
      </div>
      <div class="metric">
        <span class="metric-label">Account Exposure</span>
        <span class="metric-value ${marginCls}">${marginPct !== null ? marginPct + "%" : number.format(curExp)}</span>
        <span class="metric-sub">${maxExp ? "limit " + number.format(maxExp) : "no limit set"}</span>
      </div>
      <div class="metric">
        <span class="metric-label">Last Signal</span>
        <span class="metric-value ${sigCls}">${timeAgo(lastSig)}</span>
        <span class="metric-sub">${lastSig ? new Date(lastSig).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "no signals yet"}</span>
      </div>
    </div>`;
  drawBalanceGauge(equity, avail);
}

// ─── Balance Gauge ───────────────────────────────────────────────────────────

function drawBalanceGauge(equity, available) {
  const canvas = document.querySelector("#balanceGauge");
  const setup  = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);

  const cx        = width / 2;
  const cy        = height / 2 + 3;
  const r         = Math.min(width, height) * 0.35;
  const lw        = 6;
  const ratio     = (equity && available != null && equity > 0) ? Math.min(available / equity, 1) : 0;
  const hexColor  = ratio < 0.25 ? "#ff3d5a" : ratio < 0.5 ? "#ffc107" : "#00e676";
  const startA    = 5 * Math.PI / 6;   // 150° — 8 o'clock position
  const totalSpan = 4 * Math.PI / 3;   // 240° sweep clockwise through top to 4 o'clock

  // Background track
  ctx.beginPath();
  ctx.arc(cx, cy, r, startA, startA + totalSpan, false);
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth   = lw;
  ctx.lineCap     = "round";
  ctx.stroke();

  // Filled arc — proportion of available/equity
  if (ratio > 0.005) {
    ctx.beginPath();
    ctx.arc(cx, cy, r, startA, startA + ratio * totalSpan, false);
    ctx.strokeStyle = hexColor;
    ctx.lineWidth   = lw;
    ctx.lineCap     = "round";
    ctx.stroke();
  }

  // Center percentage label
  ctx.fillStyle    = hexColor;
  ctx.font         = "700 12px monospace";
  ctx.textAlign    = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(Math.round(ratio * 100) + "%", cx, cy);
}

// ─── Render: Risk controls ───────────────────────────────────────────────────

function renderRisk(risk) {
  // Sidebar emergency stop widget
  const widget = document.querySelector("#sidebarEmergencyStop");
  const stateEl = document.querySelector("#esWidgetState");
  if (widget)  widget.classList.toggle("active", Boolean(risk.runtime_emergency_stop));
  if (stateEl) stateEl.textContent = risk.runtime_emergency_stop
    ? "ACTIVE — click to deactivate"
    : "INACTIVE — click to activate";

  // Risk page toggles (IDs changed to avoid collisions)
  const esToggle  = document.querySelector("#emergencyStopToggle");
  const dupToggle = document.querySelector("#duplicateBlockingToggle");
  if (esToggle)  setSwitch(esToggle, risk.runtime_emergency_stop);
  if (dupToggle) setSwitch(dupToggle, risk.duplicate_blocking);

  const lossEl = document.querySelector("#accountLossLimit");
  const expEl  = document.querySelector("#accountExposureLimit");
  if (lossEl) lossEl.value = risk.account_daily_loss_limit || "";
  if (expEl)  expEl.value  = risk.max_account_exposure     || "";

  const todayNetEl = document.querySelector("#todayNet");
  if (todayNetEl) {
    todayNetEl.textContent = currency.format(risk.today_account_net || 0);
    todayNetEl.className   = pnlClass(risk.today_account_net || 0);
  }
  const curExpEl = document.querySelector("#currentExposure");
  if (curExpEl) curExpEl.textContent = number.format(risk.current_account_exposure || 0);

  const lossLimit = Math.abs(risk.account_daily_loss_limit || 0);
  const lossUsed  = lossLimit ? clamp(Math.abs(Math.min(0, risk.today_account_net || 0)) / lossLimit, 0, 1) : 0;
  const expLimit  = risk.max_account_exposure || 0;
  const expUsed   = expLimit ? clamp((risk.current_account_exposure || 0) / expLimit, 0, 1) : 0;
  setMeter(document.querySelector("#lossMeter"),    lossUsed);
  setMeter(document.querySelector("#exposureMeter"), expUsed);
}

function renderRiskStatus() {
  const { risk } = latestState;
  const esActive  = risk.runtime_emergency_stop || false;
  const dailyPnl  = risk.today_account_net || 0;
  const lossLimit = Math.abs(risk.account_daily_loss_limit || 0);
  const curExp    = risk.current_account_exposure || 0;
  const maxExp    = risk.max_account_exposure || 0;
  const lossUsed  = lossLimit ? clamp(Math.abs(Math.min(0, dailyPnl)) / lossLimit, 0, 1) : 0;
  const expUsed   = maxExp    ? clamp(curExp / maxExp, 0, 1) : 0;

  const status = esActive ? "stopped"
    : (lossUsed > 0.85 || expUsed > 0.85) ? "warning"
    : "operational";

  const bannerEl = document.querySelector("#riskStatusBanner");
  if (bannerEl) {
    bannerEl.className = `status-banner ${status === "stopped" ? "stopped" : status === "warning" ? "warning" : ""}`;
    document.querySelector("#riskStatusText").textContent =
      status === "stopped"     ? "EMERGENCY STOP ACTIVE" :
      status === "warning"     ? "APPROACHING LIMITS"    :
      "ALL SYSTEMS OPERATIONAL";
    document.querySelector("#riskStatusSub").textContent =
      status === "stopped"     ? "All signal execution is halted" :
      status === "warning"     ? "One or more limits above 85%" :
      "No limits breached · Emergency stop inactive";
    document.querySelector("#riskStatusIcon").textContent =
      status === "stopped" ? "⊗" : status === "warning" ? "⚠" : "●";
  }

  const fields = [
    ["#rsDailyLossVal",   currency.format(Math.abs(Math.min(0, dailyPnl)))],
    ["#rsDailyLossSub",   lossLimit ? `limit ${currency.format(lossLimit)} · ${Math.round(lossUsed * 100)}% used` : "no limit set"],
    ["#rsExposureVal",    number.format(curExp)],
    ["#rsExposureSub",    maxExp ? `limit ${number.format(maxExp)} · ${Math.round(expUsed * 100)}% used` : "no limit set"],
    ["#rsTodayNet",       currency.format(dailyPnl)],
    ["#rsTodayNetSub",    `today's closed P&L`],
  ];
  fields.forEach(([sel, val]) => { const el = document.querySelector(sel); if (el) el.textContent = val; });

  const dupEl = document.querySelector("#rsDupBlocking");
  if (dupEl) {
    dupEl.textContent = risk.duplicate_blocking ? "ON" : "OFF";
    dupEl.className   = `badge ${risk.duplicate_blocking ? "ok" : "bad"}`;
  }

  setMeter(document.querySelector("#rsLossMeter"), lossUsed);
  setMeter(document.querySelector("#rsExpMeter"),  expUsed);
}

function setSwitch(btn, on) {
  if (!btn) return;
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.classList.toggle("on", Boolean(on));
}

function setMeter(el, ratio) {
  if (!el) return;
  el.style.width = `${Math.round(ratio * 100)}%`;
  el.className   = ratio > 0.85 ? "danger" : ratio > 0.6 ? "warning" : "";
}

// ─── Render: Signal tables ───────────────────────────────────────────────────

function signalRow(s, cols) {
  if (cols === 6) return `
    <tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${s.strategy_name}</td>
      <td>${s.symbol}</td>
      <td>${s.action}${s.direction ? " " + s.direction : ""}</td>
      <td>${statusBadge(s.status)}</td>
      <td class="neutral" style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${s.rejection_reason || "—"}</td>
    </tr>`;
  return `
    <tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${s.strategy_name}</td>
      <td>${s.symbol}</td>
      <td>${s.action}${s.direction ? " " + s.direction : ""}</td>
      <td class="neutral" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${s.rejection_reason || "—"}</td>
    </tr>`;
}

function renderSignals(rows) {
  const el = document.querySelector("#signals");
  if (!el) return;
  const filtered = rejectionsFilter
    ? rows.filter(s => s.status === "rejected" || s.status === "failed")
    : rows;
  el.innerHTML = filtered.length
    ? filtered.map(s => signalRow(s, 6)).join("")
    : emptyRow(6, rejectionsFilter ? "No rejections." : "No signals received.");
  const countEl = document.querySelector("#signalCount");
  if (countEl) {
    const rejCount = rows.filter(s => s.status === "rejected" || s.status === "failed").length;
    countEl.textContent = rejectionsFilter ? `${rejCount} rejected` : `${rows.length} signals`;
  }
}


// ─── Render: Performance table ───────────────────────────────────────────────

function renderPerformance(rows) {
  const el = document.querySelector("#performance");
  if (!rows.length) { if (el) el.innerHTML = emptyRow(8, "No strategies have sent signals yet."); return; }
  if (el) el.innerHTML = rows.map(r => `
    <tr class="clickable ${activeStrategy === r.strategy_name ? "selected" : ""}" data-strategy="${escapeAttr(r.strategy_name)}">
      <td>${r.strategy_name}</td>
      <td>${statusBadge(r.enabled ? "enabled" : "disabled")}</td>
      <td class="${pnlClass(r.net_profit_after_fees)}">${currency.format(r.net_profit_after_fees)}</td>
      <td>${r.number_of_trades}</td>
      <td>${number.format(r.win_rate)}%</td>
      <td class="${pnlClass(r.average_win)}">${currency.format(r.average_win)}</td>
      <td class="${pnlClass(r.average_loss)}">${currency.format(r.average_loss)}</td>
      <td>${r.open_positions}</td>
    </tr>`).join("");
  if (el) el.querySelectorAll("[data-strategy]").forEach(row => {
    row.addEventListener("click", () => {
      activeStrategy = activeStrategy === row.dataset.strategy ? "all" : row.dataset.strategy;
      renderAll();
    });
  });
}

function renderStrategyRisk(rows) {
  const el = document.querySelector("#strategyRisk");
  if (!el) return;
  if (!rows.length) { el.innerHTML = emptyRow(6, "No strategies available."); return; }
  el.innerHTML = rows.map(r => `
    <tr>
      <td>${r.strategy_name}</td>
      <td>
        <button class="mini-switch ${r.enabled ? "on" : ""}" data-strategy-id="${r.strategy_id}" data-field="enabled" data-value="${r.enabled ? "false" : "true"}">
          ${r.enabled ? "Enabled" : "Paused"}
        </button>
      </td>
      <td><input class="inline-input" type="number" min="0" step="0.0001" value="${r.max_position_size || ""}" data-strategy-id="${r.strategy_id}" data-field="max_position_size"/></td>
      <td><input class="inline-input" type="number" min="0" step="0.01" value="${r.daily_loss_limit || ""}" data-strategy-id="${r.strategy_id}" data-field="daily_loss_limit"/></td>
      <td>${r.open_positions}</td>
      <td class="${pnlClass(r.net_profit_after_fees)}">${currency.format(r.net_profit_after_fees)}</td>
    </tr>`).join("");
  el.querySelectorAll(".mini-switch").forEach(btn => {
    btn.addEventListener("click", async () => {
      await patchJson(`/api/strategies/${btn.dataset.strategyId}`, { [btn.dataset.field]: btn.dataset.value === "true" });
      await refresh();
    });
  });
  el.querySelectorAll(".inline-input").forEach(inp => {
    inp.addEventListener("change", async () => {
      await patchJson(`/api/strategies/${inp.dataset.strategyId}`, { [inp.dataset.field]: Number(inp.value || 0) });
      await refresh();
    });
  });
}

// ─── Render: Positions / History ─────────────────────────────────────────────

function renderPositions(rows) {
  const el = document.querySelector("#positions");
  if (!el) return;
  if (!rows.length) { el.innerHTML = emptyRow(9, "No open positions."); return; }
  el.innerHTML = rows.map(t => `
    <tr>
      <td>${t.strategy_name}</td><td>${t.symbol}</td><td>${t.direction}</td>
      <td>${number.format(t.entry_price)}</td><td>${number.format(t.size)}</td>
      <td>${number.format(t.leverage)}x</td>
      <td class="neutral" data-upl="${t.symbol}_${t.direction}">—</td>
      <td>${statusBadge(t.status)}</td>
      <td>${fmtDate(t.opened_at)}</td>
    </tr>`).join("");
  // Fetch live unrealized P&L from Bitget
  getJson("/api/unrealized-pnl").then(upl => {
    el.querySelectorAll("[data-upl]").forEach(cell => {
      const key = cell.dataset.upl;
      const val = upl[key] ?? upl[key.split("_")[0]];
      if (val !== undefined) {
        cell.textContent = (val >= 0 ? "+" : "") + currency.format(val);
        cell.className   = pnlClass(val);
      }
    });
  }).catch(() => {});
}

function renderHistory(rows) {
  const el = document.querySelector("#history");
  if (!el) return;
  if (!rows.length) { el.innerHTML = emptyRow(11, "No trades recorded."); return; }
  el.innerHTML = rows.map(t => `
    <tr>
      <td>${t.strategy_name}</td><td>${t.symbol}</td><td>${t.direction}</td>
      <td>${number.format(t.entry_price)}</td>
      <td>${t.exit_price == null ? "—" : number.format(t.exit_price)}</td>
      <td>${number.format(t.size)}</td>
      <td class="${pnlClass(t.profit_loss)}">${currency.format(t.profit_loss)}</td>
      <td class="${pnlClass(t.net_result)}">${currency.format(t.net_result)}</td>
      <td>${fmtDate(t.opened_at)}</td>
      <td>${fmtDate(t.closed_at)}</td>
      <td>${t.closed_at ? duration(t.opened_at, t.closed_at) : "—"}</td>
    </tr>`).join("");
}


// ─── Refresh / Render ────────────────────────────────────────────────────────

async function refresh() {
  const [summary, risk, balance, performance, positions, signals, history, analytics] = await Promise.all([
    getJson("/api/summary"),
    getJson("/api/risk"),
    getJson("/api/account-balance"),
    getJson(`/api/performance?period=${activePeriod}`),
    getJson("/api/open-positions"),
    getJson("/api/signals?limit=200"),
    getJson("/api/trade-history?limit=100"),
    getJson(`/api/analytics?period=${activePeriod}`),
  ]);
  latestState = { summary, risk, balance, performance, positions, signals, history, analytics };
  renderAll();
}

function renderAll() {
  const { risk, performance, positions, signals, history, analytics } = latestState;
  const focused = activeStrategy === "all" ? performance : performance.filter(r => r.strategy_name === activeStrategy);

  // Header badges
  const el = (id) => document.querySelector(id);
  if (el("#positionCount"))  el("#positionCount").textContent  = `${positions.length} open`;
  if (el("#historyCount"))   el("#historyCount").textContent   = `${history.length} trades`;

  const fl = document.querySelector("#focusLabel");
  if (fl) fl.textContent = activeStrategy === "all" ? "All strategies" : `Focused on ${activeStrategy}`;

  const rawCurve = activeStrategy === "all"
    ? analytics.equity_curve
    : analytics.equity_curve.filter(p => p.strategy_name === activeStrategy);
  const curve = cumulativeCurve(rawCurve);

  renderSummary();
  renderRisk(risk);
  renderRiskStatus();
  renderCharts(curve, focused, analytics, signals);
  renderPerformance(focused);
  renderStrategyRisk(performance);
  renderPositions(positions);
  renderSignals(signals);
  renderHistory(history);
}

function topRoundRect(ctx, x, y, w, h, r) {
  const rad = Math.min(r, w / 2, h);
  ctx.beginPath();
  ctx.moveTo(x, y + h);
  ctx.lineTo(x, y + rad);
  ctx.arcTo(x, y, x + rad, y, rad);
  ctx.lineTo(x + w - rad, y);
  ctx.arcTo(x + w, y, x + w, y + rad, rad);
  ctx.lineTo(x + w, y + h);
  ctx.closePath();
}

// ─── P&L Chart ───────────────────────────────────────────────────────────────

function renderPnlChart() {
  const data = PNL_DATA[pnlRange];
  const lastPnl = data[data.length - 1].pnl;
  const isPos = lastPnl >= 0;
  const pnlEl = document.querySelector("#pnlHeadline");
  if (pnlEl) {
    pnlEl.textContent = (isPos ? "+" : "") + "$" + Math.abs(lastPnl).toLocaleString();
    pnlEl.style.color = isPos ? "#22c55e" : "#ef4444";
  }
  drawPnlChart(document.querySelector("#pnlChart"), data, null);
}

function drawPnlChart(canvas, data, hoverX) {
  pnlChartState.data = data;
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);

  if (!data || !data.length) { drawEmpty(ctx, width, height, "No data"); return; }

  const pad = { top: 12, right: 8, bottom: 26, left: 40 };
  const cW = width - pad.left - pad.right;
  const cH = height - pad.top - pad.bottom;

  const values = data.map(p => p.pnl);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;

  const xFor = i => pad.left + (data.length === 1 ? cW / 2 : (i / (data.length - 1)) * cW);
  const yFor = v => pad.top + cH - ((v - min) / span) * cH;
  const zeroY = yFor(0);
  const lastV = values[values.length - 1];
  const isPos = lastV >= 0;
  const lineClr = isPos ? "#22c55e" : "#ef4444";

  // Horizontal grid — dashed, 5% opacity
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (cH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Y-axis labels ($Xk)
  ctx.fillStyle = "#64748b"; ctx.font = "10px monospace"; ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = max - (span / 4) * i;
    ctx.fillText(`$${(v / 1000).toFixed(0)}k`, pad.left - 4, pad.top + (cH / 4) * i + 4);
  }

  // Zero reference line — white 15% opacity, dashed
  ctx.strokeStyle = "rgba(255,255,255,0.15)";
  ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(width - pad.right, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // Gradient fill — 25% at top → transparent
  const grad = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
  grad.addColorStop(0.05, isPos ? "rgba(34,197,94,0.25)" : "rgba(239,68,68,0.25)");
  grad.addColorStop(0.95, isPos ? "rgba(34,197,94,0)"    : "rgba(239,68,68,0)");

  ctx.beginPath();
  data.forEach((p, i) => { const x = xFor(i), y = yFor(p.pnl); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.lineTo(xFor(data.length - 1), height - pad.bottom);
  ctx.lineTo(xFor(0), height - pad.bottom);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  // Area line
  ctx.beginPath();
  data.forEach((p, i) => { const x = xFor(i), y = yFor(p.pnl); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.strokeStyle = lineClr; ctx.lineWidth = 1.5; ctx.stroke();

  // X-axis labels — every 4th on 1D, every tick on 5D/MTD
  const interval = pnlRange === "1D" ? 4 : 1;
  ctx.fillStyle = "#64748b"; ctx.font = "10px monospace"; ctx.textAlign = "center";
  data.forEach((p, i) => { if (i % interval === 0) ctx.fillText(p.t, xFor(i), height - 4); });

  // Hover crosshair + tooltip
  if (hoverX !== null && hoverX !== undefined) {
    let best = 0, bestD = Infinity;
    data.forEach((_, i) => { const d = Math.abs(xFor(i) - hoverX); if (d < bestD) { bestD = d; best = i; } });
    const pt = data[best];
    const cx = xFor(best), cy = yFor(pt.pnl);

    ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, pad.top); ctx.lineTo(cx, height - pad.bottom); ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = lineClr;
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#111116";
    ctx.beginPath(); ctx.arc(cx, cy, 1.5, 0, Math.PI * 2); ctx.fill();

    const tW = 128, tH = 46;
    const tx = cx + 10 + tW > width - 8 ? cx - tW - 10 : cx + 10;
    const ty = Math.max(pad.top, Math.min(cy - tH / 2, height - pad.bottom - tH));
    ctx.fillStyle = "#111318"; ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1;
    roundRect(ctx, tx, ty, tW, tH, 6); ctx.fill(); ctx.stroke();

    ctx.fillStyle = "#94a3b8"; ctx.font = "10px monospace"; ctx.textAlign = "left";
    ctx.fillText(pt.t, tx + 10, ty + 16);
    ctx.fillStyle = lineClr; ctx.font = "700 12px monospace";
    ctx.fillText("$" + pt.pnl.toLocaleString(), tx + 10, ty + 33);
  }
}

// ─── Signal Activity Chart ────────────────────────────────────────────────────

function drawSignalActivityChart(canvas, data, hoverX) {
  signalActivityState.data = data;
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);

  if (!data || !data.length) { drawEmpty(ctx, width, height, "No data"); return; }

  const pad = { top: 12, right: 8, bottom: 26, left: 28 };
  const cW = width - pad.left - pad.right;
  const cH = height - pad.top - pad.bottom;
  const n   = data.length;

  const rawMax = Math.max(1, ...data.map(d => Math.max(d.passed, d.blocked)));
  const yMax   = Math.ceil(rawMax / 5) * 5 || 10;

  const groupW  = cW / n;
  const barW    = Math.max(2, groupW * 0.36);
  const barGap  = 1;
  const totalBW = barW * 2 + barGap;

  // Find hovered bucket
  let hoverIdx = -1;
  if (hoverX !== null && hoverX !== undefined) {
    let bestD = Infinity;
    data.forEach((_, i) => {
      const d = Math.abs((pad.left + i * groupW + groupW / 2) - hoverX);
      if (d < bestD) { bestD = d; hoverIdx = i; }
    });
  }

  // Horizontal grid
  ctx.strokeStyle = "rgba(255,255,255,0.05)"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (cH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Y-axis labels (whole numbers)
  ctx.fillStyle = "#64748b"; ctx.font = "10px monospace"; ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    ctx.fillText(String(Math.round(yMax * (1 - i / 4))), pad.left - 4, pad.top + (cH / 4) * i + 4);
  }

  // Column hover highlight (drawn before bars so bars sit on top)
  if (hoverIdx >= 0) {
    ctx.fillStyle = "rgba(255,255,255,0.03)";
    ctx.fillRect(pad.left + hoverIdx * groupW, pad.top, groupW, cH);
  }

  // Side-by-side bars
  data.forEach((d, i) => {
    const cx = pad.left + i * groupW + groupW / 2;
    const x0 = cx - totalBW / 2;
    const x1 = x0 + barW + barGap;

    const ph = (d.passed  / yMax) * cH;
    const bh = (d.blocked / yMax) * cH;

    if (ph > 0) {
      topRoundRect(ctx, x0, pad.top + cH - ph, barW, ph, 2);
      ctx.fillStyle = "rgba(59,130,246,0.7)"; ctx.fill();
    }
    if (bh > 0) {
      topRoundRect(ctx, x1, pad.top + cH - bh, barW, bh, 2);
      ctx.fillStyle = "rgba(239,68,68,0.8)"; ctx.fill();
    }
  });

  // X-axis labels — every 3rd bucket (matches interval={2} in Recharts)
  ctx.fillStyle = "#64748b"; ctx.font = "9px monospace"; ctx.textAlign = "center";
  data.forEach((d, i) => {
    if (i % 3 === 0) ctx.fillText(d.t, pad.left + i * groupW + groupW / 2, height - 4);
  });

  // Hover tooltip
  if (hoverIdx >= 0) {
    const pt = data[hoverIdx];
    const cx = pad.left + hoverIdx * groupW + groupW / 2;
    const tW = 118, tH = 58;
    const tx = cx + 10 + tW > width - 8 ? cx - tW - 10 : cx + 10;
    const ty = Math.max(pad.top, Math.min(height / 2 - tH / 2, height - pad.bottom - tH));
    ctx.fillStyle = "#111318"; ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1;
    roundRect(ctx, tx, ty, tW, tH, 6); ctx.fill(); ctx.stroke();

    ctx.fillStyle = "#94a3b8"; ctx.font = "10px monospace"; ctx.textAlign = "left";
    ctx.fillText(pt.t, tx + 10, ty + 16);
    ctx.fillStyle = "#3b82f6"; ctx.font = "700 11px monospace";
    ctx.fillText(`passed   ${pt.passed}`,  tx + 10, ty + 33);
    ctx.fillStyle = "#ef4444";
    ctx.fillText(`blocked  ${pt.blocked}`, tx + 10, ty + 49);
  }
}

// ─── Charts ──────────────────────────────────────────────────────────────────

function renderCharts(curve, performance, analytics, signals) {
  // Strategies performance equity chart
  const lastVal = curve.length ? curve[curve.length - 1].cumulative_net : 0;
  const cv2El = document.querySelector("#curveValue2");
  if (cv2El) { cv2El.textContent = currency.format(lastVal); cv2El.className = `chart-value ${pnlClass(lastVal)}`; }

  renderPnlChart();
  drawEquityChart(document.querySelector("#equityChart2"), curve, equityState2, null);
  drawSignalActivityChart(document.querySelector("#activityChart"), SIGNAL_DATA, null);
  drawActivityChart(document.querySelector("#activityChart2"), signals);
  drawStatusChart(document.querySelector("#statusChart"), analytics.status_counts);
}

// ─── Canvas utilities ─────────────────────────────────────────────────────────

function setupCanvas(canvas) {
  if (!canvas) return null;
  const rect  = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  // Snapshot the intended CSS height once — setting canvas.height updates the attribute,
  // so reading it again on the next call (hover, resize) would multiply by ratio again.
  if (!canvas._logicalH) canvas._logicalH = Number(canvas.getAttribute("height")) || 150;
  const logicalH = canvas._logicalH;
  canvas.width  = Math.max(1, Math.floor(rect.width  * ratio));
  canvas.height = Math.max(1, Math.floor(logicalH    * ratio));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: logicalH };
}

function clearCanvas(ctx, width, height) {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#111116";
  ctx.fillRect(0, 0, width, height);
}

function drawEmpty(ctx, width, height, label) {
  ctx.fillStyle = "#52526e";
  ctx.font = "600 13px Inter, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(label, width / 2, height / 2);
}

function roundRect(ctx, x, y, w, h, r) {
  const rad = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
  ctx.beginPath();
  ctx.moveTo(x + rad, y);
  ctx.arcTo(x + w, y, x + w, y + h, rad);
  ctx.arcTo(x + w, y + h, x, y + h, rad);
  ctx.arcTo(x, y + h, x, y, rad);
  ctx.arcTo(x, y, x + w, y, rad);
  ctx.closePath();
}

// ─── Equity Chart ─────────────────────────────────────────────────────────────

function drawEquityChart(canvas, points, state, hoverX) {
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);
  const pad = { top: 24, right: 18, bottom: 34, left: 62 };
  const cW = width - pad.left - pad.right;
  const cH = height - pad.top  - pad.bottom;

  if (state) { state.points = points; state.pad = pad; state.width = width; state.height = height; }

  if (!points.length) { drawEmpty(ctx, width, height, "No closed trades yet"); return; }

  const values  = points.map(p => p.cumulative_net);
  const min     = Math.min(0, ...values);
  const max     = Math.max(0, ...values);
  const span    = max - min || 1;
  const xFor    = i => pad.left + (points.length === 1 ? cW : (i / (points.length - 1)) * cW);
  const yFor    = v => pad.top + cH - ((v - min) / span) * cH;
  const zeroY   = yFor(0);
  const lastV   = values[values.length - 1];
  const lineClr = lastV >= 0 ? "#00e676" : "#ff3d5a";
  const fillClr = lastV >= 0 ? "rgba(0,230,118," : "rgba(255,61,90,";

  ctx.strokeStyle = "#1e1e2a"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (cH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
  }

  ctx.strokeStyle = "#2e2e42";
  ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(width - pad.right, zeroY); ctx.stroke();

  const grad = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
  grad.addColorStop(0, fillClr + "0.18)");
  grad.addColorStop(1, fillClr + "0.02)");
  ctx.beginPath();
  points.forEach((p, i) => { const x = xFor(i), y = yFor(p.cumulative_net); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.lineTo(xFor(points.length - 1), height - pad.bottom);
  ctx.lineTo(xFor(0), height - pad.bottom);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  ctx.beginPath();
  points.forEach((p, i) => { const x = xFor(i), y = yFor(p.cumulative_net); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.strokeStyle = lineClr; ctx.lineWidth = 2; ctx.stroke();

  ctx.fillStyle = "#52526e"; ctx.font = "600 11px monospace"; ctx.textAlign = "right";
  ctx.fillText(currency.format(max), pad.left - 6, pad.top + 4);
  ctx.fillText(currency.format(min), pad.left - 6, height - pad.bottom);

  if (points.length > 1) {
    ctx.font = "600 10px monospace"; ctx.textAlign = "left";
    const fmt = t => new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    ctx.fillText(fmt(points[0].time), pad.left, height - 6);
    ctx.textAlign = "right";
    ctx.fillText(fmt(points[points.length - 1].time), width - pad.right, height - 6);
  }

  if (hoverX !== null && hoverX !== undefined) {
    let best = 0, bestD = Infinity;
    points.forEach((_, i) => {
      const x = pad.left + (points.length === 1 ? cW : (i / (points.length - 1)) * cW);
      const d = Math.abs(x - hoverX);
      if (d < bestD) { bestD = d; best = i; }
    });
    const pt = points[best];
    const cx = xFor(best), cy = yFor(pt.cumulative_net);

    ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, pad.top); ctx.lineTo(cx, height - pad.bottom); ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = lineClr;
    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#111116";
    ctx.beginPath(); ctx.arc(cx, cy, 2.5, 0, Math.PI * 2); ctx.fill();

    const tW = 155, tH = 52;
    const tx = cx + 12 + tW > width - 12 ? cx - tW - 12 : cx + 12;
    const ty = Math.max(pad.top, Math.min(cy - tH / 2, height - pad.bottom - tH));
    ctx.fillStyle = "rgba(22,22,30,0.96)"; ctx.strokeStyle = "#2e2e42"; ctx.lineWidth = 1;
    roundRect(ctx, tx, ty, tW, tH, 6); ctx.fill(); ctx.stroke();

    ctx.fillStyle = "#52526e"; ctx.font = "600 11px monospace"; ctx.textAlign = "left";
    ctx.fillText(new Date(pt.time).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }), tx + 12, ty + 18);
    ctx.fillStyle = pt.cumulative_net >= 0 ? "#00e676" : "#ffc107";
    ctx.font = "700 13px monospace";
    ctx.fillText(`P&L : ${currency.format(pt.cumulative_net)}`, tx + 12, ty + 37);
  }
}

// ─── Activity Chart ───────────────────────────────────────────────────────────

function drawActivityChart(canvas, signals) {
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);
  const pad = { top: 20, right: 18, bottom: 36, left: 16 };
  const numBuckets = 12;
  const now   = Date.now();
  const range = activePeriod === "today" ? 8 * 3600e3 : activePeriod === "week" ? 5 * 86400e3 : 30 * 86400e3;
  const start = now - range;
  const bucketMs = range / numBuckets;
  const buckets = Array.from({ length: numBuckets }, (_, i) => ({ t: start + i * bucketMs, passed: 0, blocked: 0 }));

  signals.forEach(s => {
    const t = new Date(s.created_at).getTime();
    if (t < start) return;
    const idx = Math.min(numBuckets - 1, Math.floor((t - start) / bucketMs));
    if (s.status === "rejected" || s.status === "failed") buckets[idx].blocked++;
    else buckets[idx].passed++;
  });

  const maxCount = Math.max(1, ...buckets.map(b => b.passed + b.blocked));
  const cW = width - pad.left - pad.right;
  const cH = height - pad.top  - pad.bottom;
  const gap = 4;
  const bW  = (cW - gap * (numBuckets - 1)) / numBuckets;

  ctx.strokeStyle = "#1e1e2a"; ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.top + (cH / 3) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
  }

  buckets.forEach((b, i) => {
    const x = pad.left + i * (bW + gap);
    const tot = b.passed + b.blocked;
    const totH = (tot / maxCount) * cH;
    const blkH = tot ? (b.blocked / tot) * totH : 0;
    const pasH = totH - blkH;
    if (pasH > 0) { roundRect(ctx, x, pad.top + cH - totH, bW, pasH, 3); ctx.fillStyle = "#4488ff"; ctx.fill(); }
    if (blkH > 0) { roundRect(ctx, x, pad.top + cH - blkH, bW, blkH, 3); ctx.fillStyle = "#ff3d5a"; ctx.fill(); }
    if (i % 3 === 0) {
      ctx.fillStyle = "#52526e"; ctx.font = "600 10px monospace"; ctx.textAlign = "center";
      ctx.fillText(new Date(b.t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }), x + bW / 2, height - 8);
    }
  });

  if (!signals.length) drawEmpty(ctx, width, height, "No signal data yet");
}

// ─── Status Chart ─────────────────────────────────────────────────────────────

function drawStatusChart(canvas, counts) {
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);
  const entries = Object.entries(counts);
  const total   = entries.reduce((s, [, n]) => s + n, 0);
  if (!total) { drawEmpty(ctx, width, height, "No signals yet"); return; }

  const colors = { accepted: "#4488ff", executed: "#00e676", closed: "#52526e", rejected: "#ff3d5a", failed: "#ff3d5a", received: "#ffc107" };
  const cx = width / 2, cy = 88, radius = 60;
  let start = -Math.PI / 2;
  entries.forEach(([status, count]) => {
    const end = start + (count / total) * Math.PI * 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, radius, start, end); ctx.closePath();
    ctx.fillStyle = colors[status] || "#52526e"; ctx.fill(); start = end;
  });
  ctx.beginPath(); ctx.arc(cx, cy, 32, 0, Math.PI * 2); ctx.fillStyle = "#111116"; ctx.fill();
  ctx.fillStyle = "#e0e0ee"; ctx.font = "700 20px Inter, sans-serif"; ctx.textAlign = "center";
  ctx.fillText(total, cx, cy + 7);

  entries.slice(0, 6).forEach(([status, count], i) => {
    const col = 16 + (i % 2) * Math.max(120, width / 2);
    const row = 172 + Math.floor(i / 2) * 24;
    ctx.fillStyle = colors[status] || "#52526e"; ctx.fillRect(col, row - 8, 8, 8);
    ctx.fillStyle = "#52526e"; ctx.font = "600 11px Inter, sans-serif"; ctx.textAlign = "left";
    ctx.fillText(`${status}  ${count}`, col + 13, row);
  });
}

// ─── Event wiring ─────────────────────────────────────────────────────────────

// Chart crosshairs
(function () {
  function wire(canvasId, state) {
    const canvas = document.querySelector(canvasId);
    if (!canvas) return;
    canvas.addEventListener("mousemove", e => {
      if (!state.points.length) return;
      drawEquityChart(canvas, state.points, state, e.clientX - canvas.getBoundingClientRect().left);
    });
    canvas.addEventListener("mouseleave", () => {
      if (state.points.length) drawEquityChart(canvas, state.points, state, null);
    });
  }
  wire("#equityChart2", equityState2);

  const pnlCanvas = document.querySelector("#pnlChart");
  if (pnlCanvas) {
    pnlCanvas.addEventListener("mousemove", e => {
      if (!pnlChartState.data) return;
      drawPnlChart(pnlCanvas, pnlChartState.data, e.clientX - pnlCanvas.getBoundingClientRect().left);
    });
    pnlCanvas.addEventListener("mouseleave", () => {
      if (pnlChartState.data) drawPnlChart(pnlCanvas, pnlChartState.data, null);
    });
  }

  const sigCanvas = document.querySelector("#activityChart");
  if (sigCanvas) {
    sigCanvas.addEventListener("mousemove", e => {
      if (!signalActivityState.data) return;
      drawSignalActivityChart(sigCanvas, signalActivityState.data, e.clientX - sigCanvas.getBoundingClientRect().left);
    });
    sigCanvas.addEventListener("mouseleave", () => {
      if (signalActivityState.data) drawSignalActivityChart(sigCanvas, signalActivityState.data, null);
    });
  }
})();

// P&L range switcher
document.querySelectorAll(".pnl-range-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".pnl-range-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    pnlRange = btn.dataset.pnlRange;
    renderPnlChart();
  });
});

// Period tabs — Dashboard (1D / 5D / MTD / ALL)
document.querySelectorAll("[data-period]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-period]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activePeriod = btn.dataset.period;
    document.querySelectorAll("[data-period-table]").forEach(b => {
      b.classList.toggle("active", b.dataset.periodTable === activePeriod);
    });
    refresh();
  });
});

// Period tabs — Strategies Ranking (Today / Week / Month / All)
document.querySelectorAll("[data-period-table]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-period-table]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activePeriod = btn.dataset.periodTable;
    document.querySelectorAll("[data-period]").forEach(b => {
      b.classList.toggle("active", b.dataset.period === activePeriod);
    });
    refresh();
  });
});

// Emergency Stop — sidebar widget
document.querySelector("#sidebarEmergencyStop")?.addEventListener("click", async () => {
  await patchJson("/api/risk", { emergency_stop: !latestState.risk.runtime_emergency_stop });
  await refresh();
});

// Emergency Stop — Risk Controls page toggle
document.querySelector("#emergencyStopToggle")?.addEventListener("click", async () => {
  await patchJson("/api/risk", { emergency_stop: !latestState.risk.runtime_emergency_stop });
  await refresh();
});

// Duplicate blocking
document.querySelector("#duplicateBlockingToggle")?.addEventListener("click", async () => {
  await patchJson("/api/risk", { duplicate_blocking: !latestState.risk.duplicate_blocking });
  await refresh();
});

// Loss / exposure inputs
document.querySelector("#accountLossLimit")?.addEventListener("change", async e => {
  await patchJson("/api/risk", { account_daily_loss_limit: Number(e.target.value || 0) });
  await refresh();
});

document.querySelector("#accountExposureLimit")?.addEventListener("change", async e => {
  await patchJson("/api/risk", { max_account_exposure: Number(e.target.value || 0) });
  await refresh();
});

// Webhook URL display + copy (multiple instances)
(function () {
  const url = window.location.origin + "/webhook";
  const urlEl = document.querySelector("#webhookUrl");
  const btnEl = document.querySelector("#copyWebhook");
  if (urlEl) urlEl.textContent = url;
  if (btnEl) btnEl.addEventListener("click", () => {
    navigator.clipboard.writeText(url).then(() => {
      btnEl.textContent = "Copied!";
      setTimeout(() => { btnEl.textContent = "Copy"; }, 2000);
    });
  });

  const payloadEl  = document.querySelector("#payloadDisplay");
  const payloadBtn = document.querySelector("#copyPayload");
  if (payloadBtn && payloadEl) payloadBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(payloadEl.textContent).then(() => {
      payloadBtn.textContent = "Copied!";
      setTimeout(() => { payloadBtn.textContent = "Copy"; }, 2000);
    });
  });
})();

document.querySelector("#rejectionsFilterBtn")?.addEventListener("click", function() {
  rejectionsFilter = !rejectionsFilter;
  this.classList.toggle("active", rejectionsFilter);
  renderAll();
});

window.addEventListener("resize", renderAll);

// ─── Signal Bots ─────────────────────────────────────────────────────────────

function botTemplate(name) {
  return JSON.stringify({
    secret: _webhookSecret,
    strategy: name,
    symbol: "{{ticker}}",
    action: "{{strategy.order.action}}",
    market_position: "{{strategy.market_position}}",
    price: "{{close}}",
  }, null, 2);
}

function loadBotTemplate(name) {
  const el = document.querySelector("#botTemplateDisplay");
  if (el) el.textContent = botTemplate(name);
}

async function refreshBots() {
  const [bots, uplData] = await Promise.all([
    getJson("/api/signal-bots").catch(() => []),
    getJson("/api/unrealized-pnl").catch(() => ({})),
  ]);
  const el = document.querySelector("#signalBots");
  if (!el) return;

  if (!bots.length) {
    el.innerHTML = emptyRow(6, "No signal bots yet — click + New Bot to create one.");
    return;
  }

  const thStyle = "padding:10px 16px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--panel)";
  const tdStyle = "padding:12px 16px;border-bottom:1px solid var(--border)";

  el.innerHTML = bots.map(b => {
    // Aggregate unrealized P&L — per symbol for single-pair, across all positions for multi-pair
    const upl = (() => {
      if (b.symbol) {
        const syms = b.symbol.split(",").map(s => s.trim().toUpperCase());
        return syms.reduce((sum, sym) => sum + (uplData[`${sym}_long`] ?? uplData[`${sym}_short`] ?? uplData[sym] ?? 0), 0);
      }
      return (latestState.positions || [])
        .filter(p => p.strategy_name === b.name)
        .reduce((sum, p) => sum + (uplData[`${p.symbol}_${p.direction}`] ?? uplData[p.symbol] ?? 0), 0);
    })();
    const total  = b.session_pnl + upl;
    const pnlCls = total > 0 ? "positive" : total < 0 ? "negative" : "neutral";
    const pnlPct = b.size > 0 ? ((total / b.size) * 100).toFixed(1) : "0.0";
    const cycPct = b.max_cycles ? Math.min(b.cycles_completed / b.max_cycles * 100, 100).toFixed(0) : 0;
    const cycBar = b.max_cycles ? `<div style="height:3px;background:var(--border);border-radius:999px;margin-top:4px"><div style="height:100%;width:${cycPct}%;background:var(--blue);border-radius:999px"></div></div>` : "";
    const tpsl   = `${b.tp_pct ? "+" + b.tp_pct + "%" : "—"} / ${b.sl_pct ? "-" + b.sl_pct + "%" : "—"}`;

    return `
    <tr data-expand="${b.id}" style="cursor:pointer">
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="chev-${b.id}" style="font-size:9px;color:var(--muted);transition:transform 150ms;display:inline-block">▶</span>
          <span class="bot-dot ${b.enabled ? "active" : "paused"}"></span>
          <span style="font-weight:600">${b.name}</span>
        </div>
      </td>
      <td style="font-family:monospace;font-size:12px">${tpsl}</td>
      <td>
        <div class="${pnlCls}" style="font-family:monospace;font-weight:700">${total >= 0 ? "+" : ""}${currency.format(total)}</div>
        <div style="font-size:10px;color:var(--muted)">${pnlPct}%${upl !== 0 ? ` · <span style="font-style:italic">${upl >= 0 ? "+" : ""}${currency.format(upl)} unrlzd</span>` : ""}</div>
      </td>
      <td>
        <div style="font-size:12px">${b.cycles_completed}${b.max_cycles ? "/" + b.max_cycles : ""}</div>
        ${cycBar}
      </td>
      <td>
        <button class="mini-switch ${b.hedge_mode ? "on" : ""}" data-bot-id="${b.id}" data-hedge="${b.hedge_mode}">
          ${b.hedge_mode ? "On" : "Off"}
        </button>
      </td>
      <td>
        <button class="mini-switch ${b.enabled ? "on" : ""}" data-bot-id="${b.id}" data-enabled="${b.enabled}">
          ${b.enabled ? "Active" : "Paused"}
        </button>
      </td>
      <td>
        <button class="mini-switch" data-delete-bot="${b.id}" style="background:var(--red-dim);color:var(--red)">Delete</button>
      </td>
    </tr>
    <tr id="detail-${b.id}" style="display:none">
      <td colspan="7" style="padding:0;border-bottom:2px solid var(--border)">
        <table style="width:100%;border-collapse:collapse;background:var(--panel-2)">
          <thead><tr>
            <th style="${thStyle}">Bot</th>
            <th style="${thStyle}">Size · Lev</th>
            <th style="${thStyle}">TP / SL</th>
            <th style="${thStyle}">Cycles</th>
            <th style="${thStyle}">Session P&amp;L</th>
            <th style="${thStyle}">Hedge</th>
            <th style="${thStyle}">Status</th>
          </tr></thead>
          <tbody><tr>
            <td style="${tdStyle}">
              <div style="font-weight:600;margin-bottom:6px">${b.name}</div>
              <button class="nav-link-btn" data-toggle-tpl="${b.id}" style="font-size:10px;padding:2px 8px">TradingView Alert ▾</button>
              <div id="bot-tpl-${b.id}" style="display:none;margin-top:8px">
                <div class="url-row">
                  <code class="url-display dim" style="font-size:10px;white-space:pre;overflow-x:auto;line-height:1.6">${escapeAttr(botTemplate(b.name))}</code>
                  <button class="copy-btn" data-copy-tpl="${escapeAttr(b.name)}" style="align-self:stretch;font-size:11px">Copy</button>
                </div>
              </div>
            </td>
            <td style="${tdStyle}">
              <div style="display:flex;align-items:center;gap:4px">
                <input class="inline-input" type="number" step="1" min="1" value="${b.size}" data-bot-id="${b.id}" data-field="size" style="width:64px" />
                <span style="color:var(--muted);font-size:11px">×</span>
                <input class="inline-input" type="number" step="1" min="1" value="${b.leverage}" data-bot-id="${b.id}" data-field="leverage" style="width:46px" />
              </div>
            </td>
            <td style="${tdStyle}">
              <div style="display:flex;align-items:center;gap:4px">
                <input class="inline-input" type="number" step="0.1" min="0" value="${b.tp_pct ?? ""}" placeholder="TP" data-bot-id="${b.id}" data-field="tp_pct" style="width:52px" />
                <span style="color:var(--muted);font-size:10px">/</span>
                <input class="inline-input" type="number" step="0.1" min="0" value="${b.sl_pct ?? ""}" placeholder="SL" data-bot-id="${b.id}" data-field="sl_pct" style="width:52px" />
              </div>
            </td>
            <td style="${tdStyle}">
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <input class="inline-input" type="number" step="1" min="1" value="${b.max_cycles ?? ""}" placeholder="∞" data-bot-id="${b.id}" data-field="max_cycles" style="width:52px" />
                <span style="font-size:11px;color:var(--muted)">${b.cycles_completed}${b.max_cycles ? "/" + b.max_cycles : ""}</span>
              </div>
              ${cycBar}
            </td>
            <td style="${tdStyle}">
              <div class="${pnlCls}" style="font-family:monospace;font-weight:700">${total >= 0 ? "+" : ""}${currency.format(total)}</div>
              <div style="font-size:10px;color:var(--muted)">${pnlPct}%</div>
            </td>
            <td style="${tdStyle}">
              <button class="mini-switch ${b.hedge_mode ? "on" : ""}" data-bot-id="${b.id}" data-hedge="${b.hedge_mode}">
                ${b.hedge_mode ? "On" : "Off"}
              </button>
            </td>
            <td style="${tdStyle}">
              <button class="mini-switch ${b.enabled ? "on" : ""}" data-bot-id="${b.id}" data-enabled="${b.enabled}">
                ${b.enabled ? "Active" : "Paused"}
              </button>
            </td>
          </tr></tbody>
        </table>
      </td>
    </tr>`;
  }).join("");

  // Expand / collapse on summary row click
  el.querySelectorAll("[data-expand]").forEach(row => {
    row.addEventListener("click", e => {
      if (e.target.closest("button, input")) return;
      const id     = row.dataset.expand;
      const detail = document.querySelector(`#detail-${id}`);
      const chev   = document.querySelector(`#chev-${id}`);
      if (!detail) return;
      const open = detail.style.display !== "none";
      detail.style.display = open ? "none" : "table-row";
      if (chev) chev.style.transform = open ? "" : "rotate(90deg)";
    });
  });

  el.querySelectorAll("[data-toggle-tpl]").forEach(btn => {
    btn.addEventListener("click", () => {
      const tpl = document.querySelector(`#bot-tpl-${btn.dataset.toggleTpl}`);
      if (!tpl) return;
      const open = tpl.style.display !== "none";
      tpl.style.display = open ? "none" : "block";
      btn.textContent = open ? "TradingView Alert ▾" : "TradingView Alert ▲";
    });
  });

  el.querySelectorAll("[data-copy-tpl]").forEach(btn => {
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(botTemplate(btn.dataset.copyTpl)).then(() => {
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = "Copy"; }, 2000);
      });
    });
  });

  el.querySelectorAll(".inline-input").forEach(inp => {
    inp.addEventListener("change", async () => {
      const nullableFields = ["tp_pct", "sl_pct", "max_cycles"];
      const val = inp.value === "" && nullableFields.includes(inp.dataset.field)
        ? null : Number(inp.value);
      await patchJson(`/api/signal-bots/${inp.dataset.botId}`, { [inp.dataset.field]: val });
      await refreshBots();
    });
  });

  el.querySelectorAll("[data-hedge]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const current = btn.dataset.hedge === "true";
      await patchJson(`/api/signal-bots/${btn.dataset.botId}`, { hedge_mode: !current });
      await refreshBots();
    });
  });

  el.querySelectorAll("[data-enabled]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const current = btn.dataset.enabled === "true";
      await patchJson(`/api/signal-bots/${btn.dataset.botId}`, { enabled: !current });
      await refreshBots();
    });
  });

  el.querySelectorAll("[data-delete-bot]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Delete this bot?`)) return;
      await fetch(`/api/signal-bots/${btn.dataset.deleteBot}`, { method: "DELETE" });
      await refreshBots();
    });
  });
}

let _webhookSecret = "YOUR_SECRET";

let _botFormWired = false;
function wireBotForm() {
  if (_botFormWired) return;
  _botFormWired = true;

  // Load real webhook secret for template
  getJson("/api/config").then(cfg => {
    if (cfg.webhook_secret) _webhookSecret = cfg.webhook_secret;
  }).catch(() => {});


  document.querySelector("#newBotToggle")?.addEventListener("click", () => {
    const form = document.querySelector("#newBotForm");
    if (form) form.style.display = form.style.display === "none" ? "block" : "none";
  });

  document.querySelector("#generateMsgBtn")?.addEventListener("click", () => {
    const name = (document.querySelector("#botName")?.value || "").trim();
    const errEl = document.querySelector("#botError");
    if (!name) {
      if (errEl) { errEl.textContent = "Enter a strategy name first."; errEl.style.display = "block"; }
      return;
    }
    if (errEl) errEl.style.display = "none";
    loadBotTemplate(name);
    const preview = document.querySelector("#botTemplatePreview");
    if (preview) preview.style.display = "block";
  });

  document.querySelector("#createBotBtn")?.addEventListener("click", async () => {
    const name     = (document.querySelector("#botName")?.value || "").trim();
    const symbol   = "";
    const size     = parseFloat(document.querySelector("#botSize")?.value || "0");
    const leverage = parseFloat(document.querySelector("#botLeverage")?.value || "1");
    const errEl    = document.querySelector("#botError");

    if (!name || size <= 0) {
      if (errEl) { errEl.textContent = "Strategy name and size are required."; errEl.style.display = "block"; }
      return;
    }
    if (errEl) errEl.style.display = "none";

    const hedge_mode  = document.querySelector("#botHedge")?.checked || false;
    const tp_raw      = parseFloat(document.querySelector("#botTp")?.value || "");
    const sl_raw      = parseFloat(document.querySelector("#botSl")?.value || "");
    const cycles_raw  = parseInt(document.querySelector("#botCycles")?.value || "");
    const body = { name, size, leverage, hedge_mode };
    if (symbol) body.symbol = symbol.toUpperCase();
    if (!isNaN(tp_raw) && tp_raw > 0)    body.tp_pct     = tp_raw;
    if (!isNaN(sl_raw) && sl_raw > 0)    body.sl_pct     = sl_raw;
    if (!isNaN(cycles_raw) && cycles_raw > 0) body.max_cycles = cycles_raw;
    const res = await fetch("/api/signal-bots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const data = await res.json();
      if (errEl) { errEl.textContent = data.detail || "Failed to create bot."; errEl.style.display = "block"; }
      return;
    }
    ["#botName","#botSymbol","#botSize","#botLeverage"].forEach(sel => {
      const el = document.querySelector(sel); if (el) el.value = "";
    });
    await refreshBots();
    // Close form and reset template preview
    const form = document.querySelector("#newBotForm");
    if (form) form.style.display = "none";
    const preview = document.querySelector("#botTemplatePreview");
    if (preview) preview.style.display = "none";
    ["#botName","#botSymbol","#botSize","#botLeverage","#botTp","#botSl","#botCycles"].forEach(sel => {
      const el = document.querySelector(sel); if (el) el.value = "";
    });
    const hedge = document.querySelector("#botHedge");
    if (hedge) hedge.checked = false;
  });
}

(function() {
  const webhookUrl = window.location.origin + "/webhook";

  const wuEl  = document.querySelector("#botWebhookUrl");
  const wuBtn = document.querySelector("#copyBotWebhook");
  if (wuEl) wuEl.textContent = webhookUrl;
  if (wuBtn) wuBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(webhookUrl).then(() => {
      wuBtn.textContent = "Copied!";
      setTimeout(() => { wuBtn.textContent = "Copy"; }, 2000);
    });
  });

  getJson("/api/config").then(cfg => {
    if (cfg.webhook_secret) _webhookSecret = cfg.webhook_secret;
  }).catch(() => {});

  const tplBtn = document.querySelector("#copyBotTemplate");
  if (tplBtn) tplBtn.addEventListener("click", () => {
    const tplEl = document.querySelector("#botTemplateDisplay");
    if (!tplEl) return;
    navigator.clipboard.writeText(tplEl.textContent).then(() => {
      tplBtn.textContent = "Copied!";
      setTimeout(() => { tplBtn.textContent = "Copy"; }, 2000);
    });
  });
})();

refresh();
setInterval(refresh, 10000);
