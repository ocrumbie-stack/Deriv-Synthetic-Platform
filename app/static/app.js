const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const number   = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });

let activePeriod      = "today";
let rejectionsFilter  = false;
let activeStrategy = "all";
let historySymbolFilter = "all";
let performanceSymbolFilter = "all";
// Declared here (not near their first use further down) because a cold
// load landing directly on a non-overview page (e.g. a refresh on
// #trading-bots) calls navigate() - and transitively wireBotForm() - before
// the script has executed past wherever these would otherwise be declared,
// which throws a "Cannot access before initialization" TDZ error for a
// `let` declared later in the file.
// Templates on screen always show this placeholder so the secret is never
// displayed. Copy buttons swap in the real one (withRealSecret) when the
// server hands it over - only behind Google sign-in.
let _webhookSecret = "YOUR_SECRET";
let _realWebhookSecret = null;
let _botFormWired = false;
// Latest /api/signal-bots rows by id, so a bot's pair table can tell which
// pair settings differ from the bot's defaults.
let botsById = {};
let latestState    = {
  summary: {}, risk: {}, balance: {}, performance: [], positions: [],
  signals: [], history: [], analytics: { equity_curve: [], status_counts: {}, symbol_exposure: [] },
  symbolLeverage: [],
};
// Last summed unrealized P&L, shown while the next Deriv fetch is in flight
// so the Open Positions Running P/L card doesn't flash "—" on every refresh.
let lastRunningPnl = null;
// Last /api/app-settings response, so the Settings page can redraw its
// balance-check row on each refresh without refetching.
let lastAppSettings = null;

const equityState2 = { points: [], pad: null, width: 0, height: 0 };

let pnlRange = "1D";
const pnlChartState          = { data: null };
const signalActivityState    = { data: null };
const symbolPerfState        = { rows: null };

function equityCurveToPnlSeries(curve, period, history, balanceNow) {
  // No balance history is stored, so walk back from Deriv's current balance:
  // undo every buy (-stake) and close (+stake + net) that happened after each
  // point. Deposits or withdrawals since then would shift the older points.
  const moves = [];
  (history || []).forEach(t => {
    if (t.execution_status === "failed") return;
    moves.push({ at: new Date(utc(t.opened_at)).getTime(), cash: -t.size });
    if (t.closed_at) moves.push({ at: new Date(utc(t.closed_at)).getTime(), cash: t.size + t.net_result });
  });
  const balanceAt = at => balanceNow == null ? null
    : moves.reduce((bal, m) => m.at > at ? bal - m.cash : bal, balanceNow);

  return (curve || []).map(p => {
    const at = new Date(utc(p.time)).getTime();
    return {
      t: new Date(at).toLocaleString(undefined,
        period === "today" ? { hour: "2-digit", minute: "2-digit" } : { month: "numeric", day: "numeric" }),
      pnl: p.cumulative_net,
      balance: balanceAt(at),
    };
  });
}

function buildActivityBuckets(signals, period) {
  const numBuckets = 12;
  const now     = Date.now();
  const range   = period === "today" ? 8 * 3600e3 : period === "week" ? 5 * 86400e3 : 30 * 86400e3;
  const start   = now - range;
  const bucketMs = range / numBuckets;
  const buckets = Array.from({ length: numBuckets }, (_, i) => ({ tRaw: start + i * bucketMs, passed: 0, blocked: 0 }));

  (signals || []).forEach(s => {
    const t = new Date(utc(s.created_at)).getTime();
    if (t < start) return;
    const idx = Math.min(numBuckets - 1, Math.floor((t - start) / bucketMs));
    if (s.status === "rejected" || s.status === "failed") buckets[idx].blocked++;
    else buckets[idx].passed++;
  });

  return buckets.map(b => ({
    t: new Date(b.tRaw).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }),
    passed: b.passed,
    blocked: b.blocked,
  }));
}

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

function modeBadge(mode) {
  return `<span class="live-badge ${mode === "live" ? "live" : "paper"}">${(mode || "demo").toUpperCase()}</span>`;
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

// Every dashboard call goes through fetch, so one wrapper sends the user back
// to Google sign-in whenever their session has expired.
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await _fetch(...args);
  if (r.status === 401) window.location.href = "/login";
  return r;
};

// Fetched up front rather than on click: a clipboard write after an await can
// lose the click's user activation and be refused by the browser.
function loadWebhookSecret() {
  getJson("/api/webhook-secret").then(d => { _realWebhookSecret = d.secret || null; }).catch(() => {});
}

function withRealSecret(text) {
  if (!_realWebhookSecret) return text;
  // A function replacement, so a "$" in the secret isn't read as a pattern.
  return text.replace(`"${_webhookSecret}"`, () => JSON.stringify(_realWebhookSecret));
}

async function getJson(url, timeoutMs = 12000) {
  // A single slow/hanging endpoint must never block the whole dashboard's
  // refresh cycle indefinitely (fetch has no default timeout).
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: controller.signal });
    if (!r.ok) throw new Error(`Request failed: ${url}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

async function patchJson(url, body) {
  const r = await fetch(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    const detail = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("; ") : data.detail;
    throw new Error(detail || `Update failed: ${url}`);
  }
  return r.json();
}

// ─── Navigation ──────────────────────────────────────────────────────────────

const pageIds = new Set([
  "home",
  "dashboard-overview",
  "trading-bots",
  "trading-positions",
  "trading-history",
  "signals-journal",
  "strategies-ranking",
  "strategies-settings",
  "strategies-leverage",
  "cfd-overview",
  "cfd-bots",
  "cfd-positions",
  "cfd-history",
  "forex-overview",
  "forex-bots",
  "forex-positions",
  "forex-history",
  "crypto-overview",
  "crypto-bots",
  "crypto-positions",
  "crypto-history",
  "system-risk",
  "system-settings",
]);

// Pages shown as tabs of another page, mapped to the sidebar item that stays
// highlighted while they're open.
const tabParent = {
  "strategies-ranking":  "trading-history",
  "signals-journal":     "trading-history",
  "strategies-settings": "system-risk",
};

// The top bar's market each page belongs to. The sidebar only lists the
// selected market's pages.
const marketPages = {
  home:      ["home"],
  synthetic: ["dashboard-overview", "trading-bots", "trading-positions", "trading-history", "signals-journal", "strategies-ranking",
              "strategies-leverage", "cfd-overview", "cfd-bots", "cfd-positions", "cfd-history"],
  forex:     ["forex-overview", "forex-bots", "forex-positions", "forex-history"],
  crypto:    ["crypto-overview", "crypto-bots", "crypto-positions", "crypto-history"],
  system:    ["system-risk", "system-settings", "strategies-settings"],
};
const pageMarket = Object.fromEntries(Object.entries(marketPages).flatMap(([m, pages]) => pages.map(p => [p, m])));

// Synthetic's sidebar switches between its two accounts' pages: Multipliers
// (Deriv) and CFDs (cTrader). Only the chosen group is listed.
const SUB_HOME = { multipliers: "dashboard-overview", cfds: "cfd-overview" };
function syntheticSub(pageId) { return pageId.startsWith("cfd-") ? "cfds" : "multipliers"; }
// Old links from before the CFDs Overview existed.
const pageAliases = { "cfd-account": "cfd-overview" };

// Declared here because navigate() runs during this script's first pass.
const lastMarketPage = {};
const lastSubPage = {};
const forexView = { data: null, timer: null, refetchAt: 0, loading: false };
const cryptoView = { data: null, market: null, timer: null, loading: false };
const homeView = { timer: null, loading: false };

function navigate(pageId, updateUrl = true) {
  pageId = pageAliases[pageId] || pageId;
  if (!pageIds.has(pageId)) pageId = "home";
  const navId = tabParent[pageId] || pageId;
  const market = pageMarket[pageId] || "home";
  lastMarketPage[market] = pageId;
  document.body.dataset.market = market;
  document.querySelectorAll(".nav-item").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.page === navId);
  });
  document.querySelectorAll(".market-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.market === market);
  });
  document.querySelectorAll(".nav-section[data-market]").forEach(section => {
    section.hidden = section.dataset.market !== market;
  });
  if (market === "synthetic") {
    const sub = syntheticSub(pageId);
    lastSubPage[sub] = pageId;
    document.querySelectorAll(".nav-group[data-sub]").forEach(group => { group.hidden = group.dataset.sub !== sub; });
    document.querySelectorAll(".nav-switch-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.sub === sub);
      btn.setAttribute("aria-selected", btn.dataset.sub === sub);
    });
  }
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("active", p.id === `page-${pageId}`);
  });
  document.querySelector(".main-content").scrollTo(0, 0);
  // Re-draw canvases that were hidden during last render
  if (pageId !== "trading-bots") {
    renderAll();
  }
  if (pageId === "trading-bots" || pageId === "cfd-bots" || pageId === "cfd-overview") {
    refreshBots();
    wireBotForm();
  }
  if (pageId === "system-settings") loadAppSettings();
  if (pageId === "forex-overview") startForexView();
  else stopForexView();
  if (pageId === "forex-bots") loadForexBots();
  if (market === "crypto") startCryptoView();
  else stopCryptoView();
  if (pageId === "home") startHomeView();
  else stopHomeView();
  if (updateUrl && window.location.hash !== `#${pageId}`) {
    history.pushState(null, "", `#${pageId}`);
  }
}

window.addEventListener("hashchange", () => {
  navigate(window.location.hash.slice(1), false);
});

window.addEventListener("popstate", () => {
  navigate(window.location.hash.slice(1), false);
});

document.querySelectorAll(".nav-item[data-page]").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

// A market tab reopens the page last used in that market.
document.querySelectorAll(".market-tab[data-market]").forEach(tab => {
  tab.addEventListener("click", () => {
    const market = tab.dataset.market;
    navigate(lastMarketPage[market] || marketPages[market][0]);
  });
});

document.querySelectorAll(".nav-switch-btn").forEach(btn => {
  btn.addEventListener("click", () => navigate(lastSubPage[btn.dataset.sub] || SUB_HOME[btn.dataset.sub]));
});

// The CFDs Overview's figures open their pages.
document.querySelectorAll(".cfd-stats [data-page]").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

// nav-link-btn (inline "go to page" links inside content) and page tabs
document.querySelectorAll(".nav-link-btn[data-page], .page-tab[data-page]").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

navigate(window.location.hash.slice(1) || "home", false);

// ─── Home page ───────────────────────────────────────────────────────────────

function startHomeView() {
  loadHome();
  if (!homeView.timer) homeView.timer = setInterval(loadHome, 15000);
}

function stopHomeView() {
  clearInterval(homeView.timer);
  homeView.timer = null;
}

async function loadHome() {
  if (homeView.loading) return;
  homeView.loading = true;
  let message = "";
  try {
    renderHome(await getJson("/api/home"));
  } catch {
    message = "Couldn't load the home page. Retrying…";
  } finally {
    document.querySelector("#homeError").textContent = message;
    homeView.loading = false;
  }
}

// Synthetic is in USD and Crypto in USDT, so each card keeps its own
// currency and there's no combined total.
function homeMoney(v, unit) {
  if (v == null || v === "") return "—";
  const text = currency.format(Number(v));
  return unit === "USDT" ? `${text.replace("$", "")} USDT` : text;
}

function homeCard(m) {
  const status = !m.connected ? ["warn", "Not connected"] : m.error ? ["bad", "Unavailable"] : ["ok", "Connected"];
  const b = m.balance || {};
  const t = m.today;
  const winRate = t && t.trades ? `${Math.round((t.wins / t.trades) * 100)}%` : "—";
  const stat = (label, value, cls = "") => `<div><dt>${label}</dt><dd class="${cls}">${value}</dd></div>`;
  return `<div class="home-card${m.connected ? "" : " offline"}">
    <div class="home-card-head">
      <span class="home-card-name">${escapeAttr(m.label)}</span>
      <span class="badge ${status[0]}"${m.error ? ` title="${escapeAttr(m.error)}"` : ""}>${status[1]}</span>
    </div>
    <p class="panel-label">Equity</p>
    <p class="home-card-equity">${homeMoney(b.equity, m.currency)}</p>
    <dl class="home-card-stats">
      ${stat("Available", homeMoney(b.available, m.currency))}
      ${stat("Unrealized P/L", homeMoney(b.unrealized_pnl, m.currency), pnlClass(Number(b.unrealized_pnl || 0)))}
      ${stat("Open positions", m.open_positions ?? "—")}
      ${stat("Today", t ? homeMoney(t.net, m.currency) : "—", t ? pnlClass(t.net) : "")}
      ${stat("Trades today", t ? `${t.trades} · ${winRate} won` : "—")}
      ${stat("Last signal", m.last_signal_at ? timeAgo(m.last_signal_at) : "—")}
    </dl>
    <button type="button" class="nav-link-btn" data-home-page="${m.key === "synthetic" ? "dashboard-overview" : `${m.key}-overview`}">Open ${escapeAttr(m.label)} →</button>
  </div>`;
}

// Fill a usage bar: amount used against a limit (0 = no limit set).
function homeMeter(bar, used, limit) {
  const pct = limit > 0 ? clamp((used / limit) * 100, 0, 100) : 0;
  bar.style.width = `${pct}%`;
  bar.className = pct >= 80 ? "danger" : pct >= 50 ? "warn" : "";
}

function renderHome(data) {
  const live = data.mode === "live";
  const badge = document.querySelector("#homeMode");
  badge.textContent = live ? "LIVE" : "DEMO";
  badge.className = `live-badge ${live ? "live" : "paper"}`;

  document.querySelector("#homeMarkets").innerHTML = data.markets.map(homeCard).join("");

  const r = data.risk;
  const stop = document.querySelector("#homeStop");
  stop.textContent = r.emergency_stop ? "ACTIVE" : "Inactive";
  stop.className = `panel-title ${r.emergency_stop ? "negative" : "positive"}`;
  const loss = Math.max(0, -r.today_net);
  document.querySelector("#homeLoss").textContent = r.daily_loss_limit > 0
    ? `${currency.format(loss)} of ${currency.format(r.daily_loss_limit)}`
    : `${currency.format(loss)} · no limit`;
  homeMeter(document.querySelector("#homeLossBar"), loss, r.daily_loss_limit);
  document.querySelector("#homeExposure").textContent = r.max_exposure > 0
    ? `${currency.format(r.exposure)} of ${currency.format(r.max_exposure)}`
    : `${currency.format(r.exposure)} · no limit`;
  homeMeter(document.querySelector("#homeExposureBar"), r.exposure, r.max_exposure);

  const marketName = { synthetic: "Synthetic", crypto: "Crypto", forex: "Forex" };
  document.querySelector("#homeActivity").innerHTML = data.activity.length
    ? data.activity.map(s => `<tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${marketName[s.market] || escapeAttr(s.market)}</td>
      <td>${escapeAttr(s.strategy_name)}</td>
      <td>${s.market === "crypto" ? cryptoPair(s.symbol) : escapeAttr(s.symbol)}</td>
      <td>${escapeAttr(s.action)}${s.direction ? ` ${escapeAttr(s.direction)}` : ""}</td>
      <td>${statusBadge(s.status)}</td>
      <td class="subtle wrap-cell">${escapeAttr(s.reason || "")}</td>
    </tr>`).join("")
    : emptyRow(7, "No signals yet");
}

document.querySelector("#homeMarkets").addEventListener("click", e => {
  const btn = e.target.closest("[data-home-page]");
  if (btn) navigate(btn.dataset.homePage);
});

// ─── Forex page ──────────────────────────────────────────────────────────────

function startForexView() {
  loadForex();
  if (!forexView.timer) forexView.timer = setInterval(tickForex, 1000);
}

function stopForexView() {
  clearInterval(forexView.timer);
  forexView.timer = null;
}

async function loadForex() {
  if (forexView.loading) return;
  forexView.loading = true;
  const error = document.querySelector("#forexError");
  // Refetch when the next open or close passes, but never in a tight loop if
  // this device's clock runs ahead of the server's.
  forexView.refetchAt = Date.now() + 15000;
  try {
    const data = await getJson("/api/forex/overview");
    forexView.data = data;
    const changes = [data.day_end, data.market.next_change, data.rollover.start, data.rollover.end,
      ...data.overlaps.flatMap(o => [o.start, o.end]), ...data.sessions.map(s => s.next_change), ...data.pairs.map(p => p.next_change)]
      .filter(Boolean).map(v => new Date(v).getTime()).filter(t => t > Date.now());
    forexView.refetchAt = Math.max(Math.min(...changes), Date.now() + 15000);
    error.textContent = "";
    renderForex();
  } catch {
    error.textContent = "Couldn't load forex sessions. Retrying…";
  } finally {
    forexView.loading = false;
  }
}

function forexClockTime(zone, seconds = false) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined }).format(new Date());
}

function forexCountdown(iso) {
  if (!iso) return "—";
  const minutes = Math.max(0, Math.ceil((new Date(iso).getTime() - Date.now()) / 60000));
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days) return `${days}d ${hours}h`;
  return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}

function forexDayPosition(iso) {
  const start = new Date(forexView.data.day_start).getTime();
  const end = new Date(forexView.data.day_end).getTime();
  return clamp((new Date(iso).getTime() - start) / (end - start) * 100, 0, 100);
}

// The session clock: one ring per session, outermost first, over the display
// time zone's day with midnight at the top.
const FX_RINGS = { sydney: 120, tokyo: 104, london: 88, new_york: 72 };

function forexTime(iso, zone) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
}

function forexZoneAbbr(zone) {
  const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(new Date())
    .find(p => p.type === "timeZoneName");
  return part ? part.value : zone;
}

function forexDialArcs(key, periods, live) {
  const r = FX_RINGS[key];
  const circumference = 2 * Math.PI * r;
  return periods.map(p => {
    const start = forexDayPosition(p.start) / 100;
    const length = (forexDayPosition(p.end) / 100 - start) * circumference;
    return `<circle class="fx-arc ${key}${live ? " live" : ""}" cx="150" cy="150" r="${r}"
      stroke-dasharray="${length.toFixed(2)} ${circumference.toFixed(2)}" stroke-dashoffset="${(-start * circumference).toFixed(2)}"/>`;
  }).join("");
}

function renderForexDial(data) {
  const rings = Object.values(FX_RINGS).map(r => `<circle class="fx-ring" cx="150" cy="150" r="${r}"/>`).join("");
  const arcs = data.sessions.map(s => forexDialArcs(s.key, s.periods, s.open)).join("");
  const labels = [["00", 150, 13, "middle"], ["06", 293, 154, "end"], ["12", 150, 295, "middle"], ["18", 7, 154, "start"]]
    .map(([t, x, y, anchor]) => `<text class="fx-hour" x="${x}" y="${y}" text-anchor="${anchor}">${t}</text>`).join("");
  document.querySelector("#forexDial").innerHTML = `
    <g transform="rotate(-90 150 150)">${rings}${arcs}</g>${labels}
    <line class="fx-needle" id="forexNeedle" x1="150" y1="150" x2="150" y2="24"/>
    <circle class="fx-hub" cx="150" cy="150" r="46"/>
    <text class="fx-hub-time" id="forexDialTime" x="150" y="148" text-anchor="middle"></text>
    <text class="fx-hub-zone" x="150" y="166" text-anchor="middle">${escapeAttr(forexZoneAbbr(data.timezone))}</text>`;
}

function renderForexSessions(data) {
  const order = [...data.sessions].sort((a, b) => (b.open - a.open) || (new Date(a.next_change) - new Date(b.next_change)));
  document.querySelector("#forexSessions").innerHTML = order.map(s => `<div class="fx-session ${s.open ? "live" : ""}">
    <div class="fx-session-name"><i class="fx-dot ${s.key}"></i>${escapeAttr(s.name)}<span class="sub" data-fx-clock="${escapeAttr(s.zone)}"></span></div>
    <div class="fx-progress"><b class="${s.key}" ${s.open ? `data-fx-from="${escapeAttr(s.opened_at)}" data-fx-to="${escapeAttr(s.next_change)}"` : ""}></b></div>
    <div class="fx-session-next">${s.open ? '<span class="fx-live">LIVE</span>' : ""}<span data-fx-countdown="${escapeAttr(s.next_change || "")}" data-fx-verb="${s.open ? "closes" : "opens"}"></span></div>
  </div>`).join("");

  // London + New York overlap: tightest spreads on the majors.
  const callout = document.querySelector("#forexCallout");
  const overlap = data.overlaps.find(o => new Date(o.end) > Date.now());
  callout.hidden = !overlap || !data.market.open;
  if (overlap) {
    const live = new Date(overlap.start) <= Date.now();
    callout.classList.toggle("live", live);
    callout.innerHTML = `<b>Peak liquidity</b> · London + New York overlap ${forexTime(overlap.start, data.timezone)}–${forexTime(overlap.end, data.timezone)},
      <span data-fx-countdown="${escapeAttr(live ? overlap.end : overlap.start)}" data-fx-verb="${live ? "live now, ends" : "starts"}"></span>. Majors usually have their tightest spreads then.`;
  }
  document.querySelector("#forexRollover").innerHTML =
    `Rollover pause <b>${forexTime(data.rollover.start, data.timezone)}–${forexTime(data.rollover.end, data.timezone)}</b>`;
}

// A pair's open periods today: its sessions' periods joined where they touch.
function forexPairPeriods(data, keys) {
  const periods = data.sessions.filter(s => keys.includes(s.key)).flatMap(s => s.periods)
    .map(p => [new Date(p.start).getTime(), new Date(p.end).getTime()]).sort((a, b) => a[0] - b[0]);
  const merged = [];
  periods.forEach(([start, end]) => {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  });
  return merged.map(([start, end]) => ({ start: new Date(start).toISOString(), end: new Date(end).toISOString() }));
}

function forexCurrencies(label) {
  return label.split("/").map(c => `<span class="fx-ccy c-${escapeAttr(c)}">${escapeAttr(c)}</span>`).join("");
}

function renderForex() {
  const data = forexView.data;
  const market = document.querySelector("#forexMarket");
  market.textContent = data.market.open ? "Market open" : "Market closed";
  market.className = `badge ${data.market.open ? "ok" : "warn"}`;

  renderForexDial(data);
  renderForexSessions(data);
  const tradable = data.pairs.filter(p => p.open).length;
  document.querySelector("#forexTradable").innerHTML = `${tradable} <span>/ ${data.pairs.length}</span>`;
  document.querySelector("#forexTradableBar").style.width = `${tradable / data.pairs.length * 100}%`;

  // Open pairs first, then by when they next change.
  const pairs = [...data.pairs].sort((a, b) => (b.open - a.open) || (new Date(a.next_change) - new Date(b.next_change)));
  document.querySelector("#forexPairs").innerHTML = pairs.map(p => `<tr class="${p.open ? "fx-open" : ""}">
    <td><div class="fx-pair">${forexCurrencies(p.label)}<strong>${escapeAttr(p.label)}</strong></div></td>
    <td class="fx-mono">${p.pip}</td>
    <td>
      <div class="fx-strip ${p.open ? "on" : ""}">${forexPairPeriods(data, p.sessions).map(period => {
        const left = forexDayPosition(period.start);
        return `<i style="left:${left}%;width:${forexDayPosition(period.end) - left}%"></i>`;
      }).join("")}<span class="fx-now"></span></div>
      <div class="fx-chips">${data.sessions.map(s => `<button type="button" class="fx-chip ${s.key} ${p.sessions.includes(s.key) ? "on" : ""}"
        data-symbol="${escapeAttr(p.symbol)}" data-session="${escapeAttr(s.key)}" aria-pressed="${p.sessions.includes(s.key)}">${escapeAttr(s.name)}</button>`).join("")}</div>
    </td>
    <td><span class="fx-status ${p.open ? "open" : ""}"><i></i>${p.open ? "Open" : "Closed"}</span>${p.reason ? `<span class="fx-reason">${escapeAttr(p.reason.replace(/^.*?closed: /, ""))}</span>` : ""}</td>
    <td class="fx-mono"><span data-fx-countdown="${escapeAttr(p.next_change || "")}" data-fx-verb="${p.open ? "Closes" : "Opens"}"></span></td>
  </tr>`).join("");
  tickForex();
}

function tickForex() {
  const data = forexView.data;
  if (!data) return;
  if (Date.now() >= forexView.refetchAt) loadForex();
  document.querySelector("#forexClock").textContent = `${forexClockTime(data.timezone, true)} ${data.timezone}`;
  document.querySelector("#forexDialTime").textContent = forexClockTime(data.timezone);
  document.querySelector("#forexMarketNext").innerHTML =
    `Week ${data.market.open ? "closes" : "opens"} in <b>${forexCountdown(data.market.next_change)}</b>`;
  document.querySelectorAll("[data-fx-clock]").forEach(el => { el.textContent = forexClockTime(el.dataset.fxClock); });
  document.querySelectorAll("[data-fx-countdown]").forEach(el => {
    const at = el.dataset.fxCountdown;
    el.textContent = at ? `${el.dataset.fxVerb} in ${forexCountdown(at)}` : "—";
  });
  document.querySelectorAll("[data-fx-from]").forEach(el => {
    const from = new Date(el.dataset.fxFrom).getTime(), to = new Date(el.dataset.fxTo).getTime();
    el.style.width = `${clamp((Date.now() - from) / (to - from) * 100, 0, 100)}%`;
  });
  const nowPct = forexDayPosition(new Date().toISOString());
  document.querySelector("#forexNeedle").setAttribute("transform", `rotate(${nowPct * 3.6} 150 150)`);
  document.querySelectorAll(".fx-now").forEach(el => { el.style.left = `${nowPct}%`; });
}

document.querySelector("#forexPairs").addEventListener("click", async event => {
  const chip = event.target.closest(".fx-chip");
  if (!chip) return;
  const pair = forexView.data.pairs.find(p => p.symbol === chip.dataset.symbol);
  const key = chip.dataset.session;
  const sessions = pair.sessions.includes(key) ? pair.sessions.filter(s => s !== key) : [...pair.sessions, key];
  if (!sessions.length) {
    showNotice("Trading window", `${pair.label} needs at least one session.`);
    return;
  }
  try {
    await patchJson(`/api/forex/pairs/${pair.symbol}`, { sessions });
    await loadForex();
  } catch (e) {
    showNotice("Trading window", e.message);
  }
});

// ─── Crypto page ─────────────────────────────────────────────────────────────

function startCryptoView() {
  document.querySelector("#cryptoWebhookUrl").textContent = `${window.location.origin}/webhook/blofin`;
  loadCrypto();
  if (!cryptoView.timer) cryptoView.timer = setInterval(loadCrypto, 10000);
}

function stopCryptoView() {
  clearInterval(cryptoView.timer);
  cryptoView.timer = null;
}

function cryptoMoney(v) {
  return v == null || v === "" ? "—" : currency.format(Number(v));
}

async function loadCrypto() {
  if (cryptoView.loading) return;
  cryptoView.loading = true;
  let message = "";
  try {
    const data = await getJson("/api/crypto/overview");
    message = data.error ? `BloFin: ${data.error}` : "";
    cryptoView.data = data;
    renderCrypto(data);
    loadCryptoMarkets(data);
  } catch {
    message = "Couldn't load crypto data. Retrying…";
  } finally {
    document.querySelectorAll(".crypto-error").forEach(el => { el.textContent = message; });
    cryptoView.loading = false;
  }
}

// Majors shown alongside the pairs being traded, so the strip is never
// empty before the first bot or position.
const CRYPTO_DEFAULT_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"];
const CRYPTO_MARKET_MIN = 4, CRYPTO_MARKET_MAX = 8;
const compactMoney = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });

// Open positions first, then the pairs bots are limited to, then majors.
function cryptoMarketPairs(data) {
  const pairs = [...data.positions.map(p => p.symbol), ...data.bots.flatMap(b => b.symbols)];
  for (const pair of CRYPTO_DEFAULT_PAIRS) if (new Set(pairs).size < CRYPTO_MARKET_MIN) pairs.push(pair);
  return [...new Set(pairs)].slice(0, CRYPTO_MARKET_MAX);
}

// Separate from the overview so slow market data never holds up the account.
async function loadCryptoMarkets(data) {
  try {
    const market = await getJson(`/api/crypto/market?symbols=${encodeURIComponent(cryptoMarketPairs(data).join(","))}`);
    cryptoView.market = market;
    renderCryptoMarkets(market, data);
  } catch {
    if (!cryptoView.market) document.querySelector("#cryptoMarkets").innerHTML = "";
  }
}

function cryptoSparkline(closes, tone) {
  if (closes.length < 2) return "";
  const lo = Math.min(...closes), hi = Math.max(...closes), span = hi - lo || 1;
  const points = closes.map((c, i) => `${(i / (closes.length - 1) * 100).toFixed(1)},${(26 - (c - lo) / span * 24).toFixed(1)}`).join(" ");
  return `<svg class="crypto-spark ${tone}" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline points="${points}" /></svg>`;
}

function cryptoCountdown(ms) {
  const m = Math.max(0, Math.round((ms - Date.now()) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

function cryptoFunding(f) {
  if (!f || f.rate == null) return `<span class="crypto-funding">Funding —</span>`;
  const pct = f.rate * 100;
  const who = f.rate > 0 ? "Longs pay shorts" : f.rate < 0 ? "Shorts pay longs" : "No funding";
  const every = f.interval_hours ? ` every ${f.interval_hours}h` : "";
  return `<span class="crypto-funding ${f.rate > 0 ? "pay-long" : f.rate < 0 ? "pay-short" : ""}" title="${who}${every}">` +
    `Funding <b>${pct > 0 ? "+" : ""}${pct.toFixed(4)}%</b>${f.next_time ? ` · ${cryptoCountdown(f.next_time)}` : ""}</span>`;
}

function renderCryptoMarkets(market, data) {
  const open = new Set(data.positions.map(p => p.symbol));
  document.querySelector("#cryptoMarkets").innerHTML = market.pairs.map(m => {
    const change = m.change_24h;
    const tone = change == null ? "neutral" : pnlClass(change);
    const range = m.high_24h && m.low_24h && m.high_24h > m.low_24h ? clamp((m.last - m.low_24h) / (m.high_24h - m.low_24h), 0, 1) : null;
    return `<div class="crypto-market${open.has(m.symbol) ? " has-position" : ""}">
      <div class="crypto-market-head">
        <strong>${cryptoPair(m.symbol)}</strong>
        ${open.has(m.symbol) ? `<span class="crypto-market-open" title="You have an open position">OPEN</span>` : ""}
        <span class="crypto-market-change ${tone}">${change == null ? "—" : `${change > 0 ? "+" : ""}${(change * 100).toFixed(2)}%`}</span>
      </div>
      <div class="crypto-market-price">${cryptoPrice(m.last)}</div>
      ${cryptoSparkline(m.sparkline || [], tone)}
      <div class="crypto-market-range" title="Where the price sits in today's range">
        <span>${cryptoPrice(m.low_24h)}</span>
        <div class="crypto-range-track">${range == null ? "" : `<i style="left:${range * 100}%"></i>`}</div>
        <span>${cryptoPrice(m.high_24h)}</span>
      </div>
      <div class="crypto-market-foot">
        ${cryptoFunding(m.funding)}
        <span title="24h volume">Vol ${m.volume_24h == null ? "—" : compactMoney.format(m.volume_24h)}</span>
      </div>
    </div>`;
  }).join("");
}

// What the connected key can do, and how long it has left. A key without an
// IP lock expires after 90 days on BloFin.
function renderCryptoKeyBadges(key) {
  const el = document.querySelector("#cryptoKeyBadges");
  if (!key) { el.innerHTML = ""; return; }
  const badges = [
    key.read_only
      ? `<span class="badge bad" title="This key can't place orders, so alerts will fail">Read-only key</span>`
      : `<span class="badge ok" title="This key can read the account and place orders">Trade access</span>`,
    key.ip_locked
      ? `<span class="badge ok" title="Only whitelisted IPs can use this key">IP-locked</span>`
      : `<span class="badge warn" title="Any IP can use this key, and BloFin expires unlocked keys after 90 days">No IP lock</span>`,
  ];
  if (key.expires_at) {
    const days = Math.floor((key.expires_at - Date.now()) / 86400000);
    const tone = days < 3 ? "bad" : days < 14 ? "warn" : "ok";
    badges.push(`<span class="badge ${tone}" title="Key expires ${new Date(key.expires_at).toLocaleDateString()}">${days < 1 ? "Expires today" : `Key expires in ${days}d`}</span>`);
  }
  el.innerHTML = badges.join("");
}

// BloFin's BTC-USDT as the BTCUSDT a TradingView alert sends (and Toobit's
// old BTC-SWAP-USDT, still in signals logged before the switch).
function cryptoPairText(symbol) {
  return String(symbol).replace("-SWAP-", "").replace("-", "");
}

function cryptoPair(symbol) {
  return escapeAttr(cryptoPairText(symbol));
}

function cryptoSignalAction(s) {
  return `${escapeAttr(s.action)}${s.direction ? ` ${escapeAttr(s.direction)}` : ""}`;
}

function cryptoPrice(v) {
  return v == null ? "—" : number.format(v);
}

function cryptoSince(dateStr) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(utc(dateStr))) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// How much of the room between entry and liquidation is left at the mark
// price (100% at entry, 0% at liquidation), and the price move that would
// liquidate the position from here.
function cryptoLiquidation(p) {
  const { entry_price: entry, mark_price: mark, liquidation_price: liq } = p;
  if (!entry || !mark || !liq) return null;
  const long = p.side === "LONG";
  const room = long ? entry - liq : liq - entry;
  if (room <= 0) return null;
  const left = long ? mark - liq : liq - mark;
  return { buffer: clamp(left / room, 0, 1), move: Math.max(0, left) / mark };
}

function renderCryptoOverview(data) {
  const setup = document.querySelector("#cryptoSetup");
  setup.hidden = data.connected;
  document.querySelector("#cryptoAccount").hidden = !data.connected;
  if (!data.connected) {
    document.querySelector("#cryptoSetupTitle").textContent = `Connect BloFin ${data.mode === "live" ? "live" : "demo"}`;
    document.querySelector("#cryptoSetupText").innerHTML =
      `Set ${data.missing.map(m => `<code>${escapeAttr(m)}</code>`).join(", ")} on the server, then restart it. ` +
      `Until then, crypto alerts in ${escapeAttr(data.mode)} mode are refused.`;
  }

  const b = data.balance || {};
  const equity = b.equity == null ? null : Number(b.equity);
  const available = b.available == null ? null : Number(b.available);
  document.querySelector("#cryptoBalance").textContent = cryptoMoney(equity);
  document.querySelector("#cryptoAvailable").textContent = cryptoMoney(available);

  const unrealized = document.querySelector("#cryptoUnrealized");
  const upl = b.unrealized_pnl == null ? null : Number(b.unrealized_pnl);
  unrealized.textContent = upl == null ? "—" : `${upl > 0 ? "+" : ""}${cryptoMoney(upl)}`;
  unrealized.className = `crypto-figure-value ${upl == null ? "" : pnlClass(upl)}`;
  document.querySelector("#cryptoUnrealizedPct").textContent =
    upl != null && equity ? `${upl > 0 ? "+" : ""}${(upl / equity * 100).toFixed(2)}% of equity` : "";

  const used = equity != null && available != null ? Math.max(0, equity - available) : null;
  const share = used != null && equity > 0 ? clamp(used / equity, 0, 1) : null;
  document.querySelector("#cryptoMarginUsed").innerHTML = used == null ? "—"
    : `${cryptoMoney(used)}${share == null ? "" : ` <span class="crypto-figure-sub">${Math.round(share * 100)}%</span>`}`;
  const meter = document.querySelector("#cryptoMarginMeter");
  meter.style.width = `${(share || 0) * 100}%`;
  meter.className = share >= 0.8 ? "danger" : share >= 0.5 ? "warn" : "";

  document.querySelector("#cryptoOpenTitle").textContent = `Open Positions (${data.positions.length})`;
  // Entry value on each side, so a lopsided book stands out.
  const exposure = document.querySelector("#cryptoExposure");
  const sides = { LONG: 0, SHORT: 0 };
  for (const p of data.positions) sides[p.side] += Number(p.value || 0);
  const total = sides.LONG + sides.SHORT;
  exposure.hidden = !total;
  exposure.innerHTML = total ? `
    <span class="positive">Long ${cryptoMoney(sides.LONG)}</span>
    <div class="crypto-exposure-bar" title="Long vs short value at entry"><b style="width:${sides.LONG / total * 100}%"></b></div>
    <span class="crypto-exposure-short">Short ${cryptoMoney(sides.SHORT)}</span>` : "";
  document.querySelector("#cryptoOverviewPositions").innerHTML = data.positions.length
    ? data.positions.map(p => {
      const liq = cryptoLiquidation(p);
      const pnl = Number(p.unrealized_pnl || 0);
      const tone = !liq ? "" : liq.buffer < 0.25 ? "danger" : liq.buffer < 0.5 ? "warn" : "";
      return `<div class="crypto-position">
        <div class="crypto-position-head">
          <strong>${cryptoPair(p.symbol)}</strong>
          <span class="badge ${p.side === "LONG" ? "ok" : "bad"}">${escapeAttr(p.side)}${p.leverage == null ? "" : ` ${escapeAttr(p.leverage)}x`}</span>
          <span class="crypto-position-prices">${cryptoPrice(p.entry_price)} → ${cryptoPrice(p.mark_price)}</span>
          <span class="crypto-position-pnl ${pnlClass(pnl)}">${pnl > 0 ? "+" : ""}${cryptoMoney(p.unrealized_pnl)}</span>
          <button type="button" class="mini-switch" style="background:var(--red-dim);color:var(--red)" data-crypto-close="${escapeAttr(p.symbol)}" data-side="${escapeAttr(p.side)}">Close</button>
        </div>
        <div class="crypto-liq">
          <span>Liq ${p.liquidation_price ? cryptoPrice(p.liquidation_price) : "—"}</span>
          <div class="home-meter" title="Room left before liquidation"><b class="${tone}" style="width:${liq ? liq.buffer * 100 : 0}%"></b></div>
          <span>${liq ? `${(liq.move * 100).toFixed(1)}% away` : ""}</span>
        </div>
      </div>`;
    }).join("")
    : `<p class="empty">${data.connected ? "No open positions" : "Connect BloFin to see positions"}</p>`;

  const lastSignal = {};
  for (const s of data.signals) if (!(s.strategy_name in lastSignal)) lastSignal[s.strategy_name] = s;
  const activeBots = data.bots.filter(bot => bot.enabled).length;
  document.querySelector("#cryptoBotsTitle").textContent = `Bots (${activeBots} active)`;
  document.querySelector("#cryptoOverviewBots").innerHTML = data.bots.length
    ? data.bots.map(bot => {
      const last = lastSignal[bot.name];
      return `<div class="crypto-bot${bot.enabled ? "" : " paused"}">
        <span class="crypto-bot-dot" title="${bot.enabled ? "Active" : "Paused"}"></span>
        <strong>${escapeAttr(bot.name)}</strong>
        <span class="crypto-bot-size">${bot.enabled ? `${cryptoMoney(bot.margin)} · ${bot.leverage}x` : "paused"}</span>
        <span class="crypto-bot-last">${last ? `${escapeAttr(last.action)} ${cryptoSince(last.created_at)}` : "no recent signals"}</span>
      </div>`;
    }).join("")
    : `<p class="empty">No crypto bots yet. <button type="button" class="nav-link-btn" data-page="crypto-bots">Create one →</button></p>`;

  document.querySelector("#cryptoRecent").innerHTML = data.signals.length
    ? data.signals.slice(0, 8).map(s => `<tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${escapeAttr(s.strategy_name)}</td>
      <td>${cryptoPair(s.symbol)}</td>
      <td>${cryptoSignalAction(s)}</td>
      <td>${statusBadge(s.status)}</td>
      <td class="subtle wrap-cell">${escapeAttr(s.reason || "")}</td>
    </tr>`).join("")
    : emptyRow(6, "No crypto signals yet");
}

function renderCrypto(data) {
  const badge = document.querySelector("#cryptoConnection");
  badge.textContent = data.connected ? (data.error ? "BloFin error" : "Connected") : "Not connected";
  badge.className = `badge ${data.connected && !data.error ? "ok" : "warn"}`;
  const live = data.mode === "live";
  document.querySelectorAll(".crypto-mode").forEach(el => {
    el.textContent = live ? "LIVE" : "DEMO";
    el.className = `live-badge crypto-mode ${live ? "live" : "paper"}`;
  });
  document.querySelector("#cryptoPositionsSub").textContent = `${live ? "Live" : "Demo"} on BloFin`;
  renderCryptoKeyBadges(data.connected ? data.api_key : null);

  renderCryptoOverview(data);

  document.querySelector("#cryptoPositionCount").textContent = `${data.positions.length} open`;
  document.querySelector("#cryptoPositions").innerHTML = data.positions.length
    ? data.positions.map(p => `<tr>
      <td><strong>${cryptoPair(p.symbol)}</strong></td>
      <td><span class="badge ${p.side === "LONG" ? "ok" : "bad"}">${escapeAttr(p.side)}</span></td>
      <td>${cryptoMoney(p.margin)}</td>
      <td>${p.leverage == null ? "—" : `${escapeAttr(p.leverage)}x`}</td>
      <td>${cryptoMoney(p.value)}</td>
      <td>${p.entry_price == null ? "—" : number.format(p.entry_price)}</td>
      <td>${p.mark_price == null ? "—" : number.format(p.mark_price)}</td>
      <td>${p.liquidation_price ? number.format(p.liquidation_price) : "—"}</td>
      <td class="${pnlClass(Number(p.unrealized_pnl || 0))}">${cryptoMoney(p.unrealized_pnl)}</td>
      <td><button type="button" class="mini-switch" style="background:var(--red-dim);color:var(--red)" data-crypto-close="${escapeAttr(p.symbol)}" data-side="${escapeAttr(p.side)}">Close</button></td>
    </tr>`).join("")
    : emptyRow(10, data.connected ? "No open positions" : `Set ${data.missing.map(escapeAttr).join(", ")} to connect BloFin ${escapeAttr(data.mode)}`);

  document.querySelector("#cryptoHistoryCount").textContent = `${data.signals.length} signal${data.signals.length === 1 ? "" : "s"}`;
  document.querySelector("#cryptoSignals").innerHTML = data.signals.length
    ? data.signals.map(s => `<tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${escapeAttr(s.strategy_name)}</td>
      <td>${cryptoPair(s.symbol)}</td>
      <td>${cryptoSignalAction(s)}</td>
      <td>${s.action === "entry" && s.margin ? cryptoMoney(s.margin) : "—"}</td>
      <td>${s.action === "entry" && s.margin ? `${s.leverage}x` : "—"}</td>
      <td>${s.take_profit || s.stop_loss ? `${s.take_profit ? number.format(s.take_profit) : "—"} / ${s.stop_loss ? number.format(s.stop_loss) : "—"}` : "—"}</td>
      <td>${statusBadge(s.status)}</td>
      <td class="subtle wrap-cell">${escapeAttr(s.reason || "")}</td>
    </tr>`).join("")
    : emptyRow(9, "No crypto signals yet");

  document.querySelector("#cryptoBots").innerHTML = data.bots.length
    ? data.bots.map(bot => `<tr>
      <td><strong>${escapeAttr(bot.name)}</strong></td>
      <td>${cryptoMoney(bot.margin)}</td>
      <td>${bot.leverage}x</td>
      <td>${cryptoMoney(bot.position_value)}</td>
      <td>${bot.symbols.length ? bot.symbols.map(cryptoPair).join(", ") : '<span class="subtle">Any</span>'}</td>
      <td>${statusBadge(bot.enabled ? "enabled" : "paused")}</td>
      <td class="row-actions">
        <button type="button" class="mini-switch neutral" data-bot-action="toggle" data-bot-id="${bot.id}">${bot.enabled ? "Pause" : "Resume"}</button>
        <button type="button" class="mini-switch neutral" data-bot-action="edit" data-bot-id="${bot.id}">Edit</button>
        <button type="button" class="mini-switch" data-bot-action="delete" data-bot-id="${bot.id}">Delete</button>
      </td>
    </tr>`).join("")
    : emptyRow(7, "No crypto bots yet. Create one for each strategy that should trade on BloFin.");
}

function cryptoPositionHint(margin, leverage) {
  const value = Number(margin) * Number(leverage);
  return value > 0
    ? `Each entry opens about a ${currency.format(value)} position: ${currency.format(Number(margin))} margin at ${Number(leverage)}x.`
    : "";
}

async function sendJson(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    const detail = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("; ") : data.detail;
    throw new Error(detail || "Request failed.");
  }
  return r.json();
}

document.querySelector("#cryptoBotNew").addEventListener("click", () => {
  const form = document.querySelector("#cryptoBotForm");
  form.hidden = !form.hidden;
  if (!form.hidden) form.elements.name.focus();
  form.dispatchEvent(new Event("input"));
});

document.querySelector("#cryptoBotForm").addEventListener("input", event => {
  const form = event.currentTarget;
  document.querySelector("#cryptoBotPreview").textContent = cryptoPositionHint(form.elements.margin.value, form.elements.leverage.value);
});

document.querySelector("#cryptoBotForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  const error = document.querySelector("#cryptoBotError");
  try {
    await sendJson("POST", "/api/crypto/bots", {
      name: form.elements.name.value.trim(),
      margin: Number(form.elements.margin.value),
      leverage: Number(form.elements.leverage.value),
      symbols: form.elements.symbols.value.trim() || null,
    });
    form.reset();
    form.hidden = true;
    error.textContent = "";
    loadCrypto();
  } catch (e) {
    error.textContent = e.message;
  }
});

document.querySelector("#cryptoBots").addEventListener("click", async event => {
  const button = event.target.closest("[data-bot-action]");
  if (!button) return;
  const bot = cryptoView.data.bots.find(b => b.id === Number(button.dataset.botId));
  const url = `/api/crypto/bots/${bot.id}`;
  const action = button.dataset.botAction;
  if (action === "toggle") {
    try {
      await sendJson("PATCH", url, { enabled: !bot.enabled });
      loadCrypto();
    } catch (e) {
      showNotice("Crypto bot", e.message);
    }
  } else if (action === "edit") {
    const saved = await openDialog({
      title: `Edit ${bot.name}`,
      fields: [
        { name: "name", label: "Strategy name", value: bot.name },
        [{ name: "margin", label: "Margin", value: bot.margin, prefix: "$" },
         { name: "leverage", label: "Leverage", value: bot.leverage }],
        { name: "symbols", label: "Allowed pairs", value: bot.symbols.map(cryptoPairText).join(", "), placeholder: "Any", hint: "Leave empty to allow any USDT pair." },
      ],
      note: "Position value is margin × leverage, rounded down to BloFin's lot size.",
      confirmLabel: "Save",
      onSubmit: values => sendJson("PATCH", url, {
        name: values.name, margin: Number(values.margin), leverage: Number(values.leverage), symbols: values.symbols,
      }),
    });
    if (saved) loadCrypto();
  } else if (action === "delete") {
    const deleted = await openDialog({
      title: "Delete crypto bot",
      message: `Delete ${bot.name}? Its entries will be refused until a bot with this name exists again. Open positions stay open.`,
      confirmLabel: "Delete", tone: "danger",
      onSubmit: () => sendJson("DELETE", url),
    });
    if (deleted) loadCrypto();
  }
});

document.querySelectorAll("#cryptoPositions, #cryptoOverviewPositions").forEach(list => list.addEventListener("click", async event => {
  const button = event.target.closest("[data-crypto-close]");
  if (!button) return;
  const { cryptoClose: symbol, side } = button.dataset;
  const closed = await openDialog({
    title: "Close position",
    message: `Close the ${side.toLowerCase()} ${cryptoPairText(symbol)} position at market on your ${cryptoView.data.mode} BloFin account?`,
    confirmLabel: "Close", tone: "danger",
    onSubmit: async () => {
      const r = await fetch(`/api/crypto/positions/close?symbol=${encodeURIComponent(symbol)}&side=${encodeURIComponent(side)}`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Close failed.");
    },
  });
  if (closed) loadCrypto();
}));

// The bots list is re-rendered, so its "Create one" link is wired here.
document.querySelector("#cryptoOverviewBots").addEventListener("click", event => {
  const link = event.target.closest("[data-page]");
  if (link) navigate(link.dataset.page);
});

document.querySelectorAll("[data-crypto-copy]").forEach(btn => btn.addEventListener("click", () => {
  const text = document.getElementById(btn.dataset.cryptoCopy).textContent;
  navigator.clipboard.writeText(withRealSecret(text)).then(() => {
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  });
}));

// ─── Forex bots ──────────────────────────────────────────────────────────────

let forexBots = [];

function fxPair(symbol) { return escapeAttr(`${symbol.slice(0, 3)}/${symbol.slice(3)}`); }

function fxPips(v) { return v == null ? '<span class="subtle">—</span>' : `${number.format(v)} pips`; }

function fxLots(lots) {
  return `${number.format(lots)} lot${lots === 1 ? "" : "s"}`;
}

// Blank pip fields mean "no stop", sent as null.
function fxBotBody(get) {
  const pips = name => (get(name) === "" ? null : Number(get(name)));
  return {
    name: get("name"),
    lots: Number(get("lots")),
    stop_loss_pips: pips("stop_loss_pips"),
    take_profit_pips: pips("take_profit_pips"),
    trailing_stop_pips: pips("trailing_stop_pips"),
    symbols: get("symbols") || null,
  };
}

async function loadForexBots() {
  try {
    forexBots = await getJson("/api/forex/bots");
  } catch {
    return;
  }
  document.querySelector("#forexBots").innerHTML = forexBots.length
    ? forexBots.map(bot => `<tr>
      <td><strong>${escapeAttr(bot.name)}</strong></td>
      <td>${fxLots(bot.lots)}<span class="sub-line">${number.format(bot.units)} units</span></td>
      <td>${fxPips(bot.stop_loss_pips)}</td>
      <td>${fxPips(bot.take_profit_pips)}</td>
      <td>${fxPips(bot.trailing_stop_pips)}</td>
      <td>${bot.symbols.length ? bot.symbols.map(fxPair).join(", ") : '<span class="subtle">All pairs</span>'}</td>
      <td>${statusBadge(bot.enabled ? "enabled" : "paused")}</td>
      <td class="row-actions">
        <button type="button" class="mini-switch neutral" data-bot-action="toggle" data-bot-id="${bot.id}">${bot.enabled ? "Pause" : "Resume"}</button>
        <button type="button" class="mini-switch neutral" data-bot-action="edit" data-bot-id="${bot.id}">Edit</button>
        <button type="button" class="mini-switch" data-bot-action="delete" data-bot-id="${bot.id}">Delete</button>
      </td>
    </tr>`).join("")
    : emptyRow(8, "No forex bots yet. Create one for each strategy that should trade forex on cTrader.");
}

document.querySelector("#forexBotNew").addEventListener("click", () => {
  const form = document.querySelector("#forexBotForm");
  form.hidden = !form.hidden;
  if (!form.hidden) form.elements.name.focus();
  form.dispatchEvent(new Event("input"));
});

document.querySelector("#forexBotForm").addEventListener("input", event => {
  const lots = Number(event.currentTarget.elements.lots.value);
  document.querySelector("#forexBotPreview").textContent = lots > 0
    ? `Each entry trades ${fxLots(lots)} = ${number.format(Math.round(lots * 100000))} units. On EUR/USD that's about $${number.format(lots * 10)} per pip.`
    : "";
});

document.querySelector("#forexBotForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  const error = document.querySelector("#forexBotError");
  try {
    await sendJson("POST", "/api/forex/bots", fxBotBody(name => form.elements[name].value.trim()));
    form.reset();
    form.hidden = true;
    error.textContent = "";
    loadForexBots();
  } catch (e) {
    error.textContent = e.message;
  }
});

document.querySelector("#forexBots").addEventListener("click", async event => {
  const button = event.target.closest("[data-bot-action]");
  if (!button) return;
  const bot = forexBots.find(b => b.id === Number(button.dataset.botId));
  const url = `/api/forex/bots/${bot.id}`;
  const action = button.dataset.botAction;
  if (action === "toggle") {
    try {
      await sendJson("PATCH", url, { enabled: !bot.enabled });
      loadForexBots();
    } catch (e) {
      showNotice("Forex bot", e.message);
    }
  } else if (action === "edit") {
    const saved = await openDialog({
      title: `Edit ${bot.name}`,
      fields: [
        [{ name: "name", label: "Strategy name", value: bot.name },
         { name: "lots", label: "Size (lots)", value: bot.lots }],
        [{ name: "stop_loss_pips", label: "Stop loss (pips)", value: bot.stop_loss_pips, placeholder: "none" },
         { name: "take_profit_pips", label: "Take profit (pips)", value: bot.take_profit_pips, placeholder: "none" }],
        [{ name: "trailing_stop_pips", label: "Trailing stop (pips)", value: bot.trailing_stop_pips, placeholder: "none" },
         { name: "symbols", label: "Allowed pairs", value: bot.symbols.join(", "), placeholder: "All pairs" }],
      ],
      note: "Leave a pip field empty for no stop. 1 lot = 100,000 units.",
      confirmLabel: "Save",
      onSubmit: values => sendJson("PATCH", url, fxBotBody(name => values[name])),
    });
    if (saved) loadForexBots();
  } else if (action === "delete") {
    const deleted = await openDialog({
      title: "Delete forex bot",
      message: `Delete ${bot.name}? Its entries will be refused until a bot with this name exists again.`,
      confirmLabel: "Delete", tone: "danger",
      onSubmit: () => sendJson("DELETE", url),
    });
    if (deleted) loadForexBots();
  }
});

// ─── Render: Summary cards ───────────────────────────────────────────────────

function renderSummary() {
  const { summary, risk, balance, signals } = latestState;

  const modeEl   = document.querySelector("#liveMode");
  modeEl.textContent = (summary.execution_mode || "demo").toUpperCase();
  modeEl.className   = `live-badge ${summary.execution_mode === "live" ? "live" : "paper"}`;

  const modeBrandEl = document.querySelector("#liveModeBrand");
  if (modeBrandEl) {
    modeBrandEl.textContent = modeEl.textContent;
    modeBrandEl.className   = `live-badge ${summary.execution_mode === "live" ? "live" : "paper"}`;
  }

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

  const isDemo = balance?.mode === "demo";
  const equity = balance?.equity ?? null;
  const avail  = balance?.available ?? null;
  const upl    = balance?.unrealized_pnl ?? null;
  // Running = today's closed P&L + live unrealized on open positions.
  const runningPnl = upl !== null ? dailyPnl + upl : null;
  const balVal     = equity !== null ? currency.format(equity) : "—";
  const balSub     = avail !== null
    ? `${currency.format(avail)} avail · UPL ${upl !== null ? (upl >= 0 ? "+" : "") + currency.format(upl) : "—"}${isDemo ? " (demo)" : ""}`
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
          <span style="font-size:11px;color:var(--muted)">of ${balVal}${isDemo ? " · demo" : ""}</span>
        </div>
      </div>
      <div class="metric highlight">
        <span class="metric-label">Daily P&amp;L</span>
        <span class="metric-value ${pnlClass(dailyPnl)}">${currency.format(dailyPnl)}</span>
        <span class="metric-sub">closed · ${lossUsedPct !== null ? lossUsedPct + "% of limit" : "no limit set"}</span>
        <span class="metric-sub">Running <b style="font-family:monospace;color:var(--blue)">${runningPnl !== null ? currency.format(runningPnl) : "—"}</b>${upl !== null ? ` · open <b class="${pnlClass(upl)}" style="font-family:monospace">${(upl >= 0 ? "+" : "") + currency.format(upl)}</b>` : ""}</span>
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

  const isLiveMode = risk.execution_mode === "live";
  const modeToggle = document.querySelector("#executionModeToggle");
  const modeHint   = document.querySelector("#executionModeHint");
  if (modeToggle) setSwitch(modeToggle, isLiveMode);
  if (modeHint) modeHint.textContent = isLiveMode
    ? "Live mode — orders are sent to your real Deriv account (real money)."
    : "Demo mode — orders are sent to your Deriv demo account (virtual money).";

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

const EXIT_SOURCE_LABELS = {
  strategy_entry: "Strategy entry",
  strategy_exit: "Strategy exit",
  reversal:      "Reversal",
  webhook_exit:  "Webhook exit",
  manual_close:  "Manual close",
  price_exit:    "Price exit",
  trailing_stop: "Trailing stop",
  close_all_target: "Close-all target",
  deriv_close:   "Closed on Deriv (TP/SL/stop-out)",
};

function tradeReason(t) {
  if (t.execution_status === "failed") return "Order failed";
  if (t.status === "open") return "Open";
  return EXIT_SOURCE_LABELS[t.close_reason] || "—";
}

function signalReason(s) {
  return s.rejection_reason || EXIT_SOURCE_LABELS[s.source] || "—";
}

function signalRow(s, cols) {
  if (cols === 6) return `
    <tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${s.strategy_name}</td>
      <td>${s.symbol}</td>
      <td>${s.action}${s.direction ? " " + s.direction : ""}</td>
      <td>${statusBadge(s.status)}</td>
      <td>${modeBadge(s.execution_mode)}</td>
      <td class="neutral" style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${signalReason(s)}</td>
    </tr>`;
  return `
    <tr>
      <td>${fmtDate(s.created_at)}</td>
      <td>${s.strategy_name}</td>
      <td>${s.symbol}</td>
      <td>${s.action}${s.direction ? " " + s.direction : ""}</td>
      <td class="neutral" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${signalReason(s)}</td>
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
    : emptyRow(7, rejectionsFilter ? "No rejections." : "No signals received.");
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

function renderPerformanceSymbolFilter(history) {
  const filterEl = document.querySelector("#performanceSymbolFilter");
  if (!filterEl) return;
  // Same rebuild-only-on-change rule as the Trade History filter, so the
  // selection survives the refresh cycle.
  const symbols = [...new Set(history.map(t => t.symbol))].sort();
  if (performanceSymbolFilter !== "all" && !symbols.includes(performanceSymbolFilter)) symbols.push(performanceSymbolFilter);
  const existing = [...filterEl.options].slice(1).map(o => o.value);
  if (existing.join(",") !== symbols.join(",")) {
    filterEl.innerHTML = `<option value="all">All symbols</option>` +
      symbols.map(s => `<option value="${escapeAttr(s)}">${s}</option>`).join("");
  }
  filterEl.value = performanceSymbolFilter;
  if (!filterEl.dataset.wired) {
    // The strategy table is filtered server-side, so a change refetches.
    filterEl.addEventListener("change", () => {
      performanceSymbolFilter = filterEl.value;
      refresh();
    });
    filterEl.dataset.wired = "1";
  }
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

function renderSymbolLeverage(rows) {
  const el = document.querySelector("#symbolLeverage");
  if (!el) return;
  if (!rows.length) { el.innerHTML = emptyRow(3, "No synthetic index leverage data available."); return; }
  el.innerHTML = rows.map(r => `
    <tr>
      <td>${r.display_name} <span style="color:var(--muted)">(${r.symbol})</span></td>
      <td style="color:var(--muted);font-size:12px">${r.allowed_multipliers.join(", ")}</td>
      <td>
        <select class="inline-input" data-symbol="${r.symbol}" style="width:90px">
          ${r.allowed_multipliers.map(v => `<option value="${v}" ${v === r.leverage ? "selected" : ""}>${v}x</option>`).join("")}
        </select>
      </td>
    </tr>`).join("");
  el.querySelectorAll(".inline-input").forEach(sel => {
    sel.addEventListener("change", async () => {
      await patchJson(`/api/symbol-leverage/${sel.dataset.symbol}`, { leverage: Number(sel.value) });
      await refresh();
    });
  });
}

// ─── Render: Positions / History ─────────────────────────────────────────────

function renderPositionsSummary(rows) {
  const box = document.querySelector("#positionsSummary");
  if (!box) return;
  const { risk, balance } = latestState;

  const isDemo = balance?.mode === "demo";
  const equity = balance?.equity ?? null;
  const avail  = balance?.available ?? null;

  const longs  = rows.filter(t => t.direction === "long").length;
  const shorts = rows.length - longs;

  const maxExp    = risk?.max_account_exposure || 0;
  const curExp    = risk?.current_account_exposure || 0;
  const expPct    = maxExp ? Math.round(curExp / maxExp * 100) : null;
  const expCls    = expPct === null ? "neutral" : expPct > 80 ? "negative" : "positive";

  if (!rows.length) lastRunningPnl = null;
  const target = risk?.close_all_target;

  box.innerHTML = `
    <div class="metric highlight">
      <span class="metric-label">Account Balance</span>
      <span class="metric-value neutral">${equity !== null ? currency.format(equity) : "—"}</span>
      <span class="metric-sub">${avail !== null ? currency.format(avail) + " available" : "failed to fetch"}${isDemo ? " · demo" : ""}</span>
    </div>
    <div class="metric highlight">
      <span class="metric-label">Open Positions</span>
      <span class="metric-value ${rows.length ? "positive" : "neutral"}">${rows.length}</span>
      <span class="metric-sub">${rows.length ? `${longs} long · ${shorts} short` : "no trades running"}</span>
    </div>
    <div class="metric highlight">
      <span class="metric-label">Account Exposure</span>
      <span class="metric-value ${expCls}">${expPct !== null ? expPct + "%" : currency.format(curExp)}</span>
      <span class="metric-sub">${maxExp ? `${currency.format(curExp)} of ${currency.format(maxExp)} limit` : "no limit set"}</span>
    </div>
    <div class="metric highlight">
      <span class="metric-label">Running P/L</span>
      <span class="metric-value ${lastRunningPnl === null ? "neutral" : pnlClass(lastRunningPnl)}" id="runningPnl">${lastRunningPnl === null ? (rows.length ? "—" : currency.format(0)) : signedCurrency(lastRunningPnl)}</span>
      <span class="metric-sub">${target ? `closes all at ${closeAllTargetText(target)}` : "unrealized, all open positions"}</span>
    </div>`;
}

// "+$50.00 or −$20.00" - whichever sides of the close-all target are set,
// plus "· repeats" when it stays armed after firing.
function closeAllTargetText(target) {
  const levels = [
    target.take_profit != null ? `+${currency.format(target.take_profit)}` : null,
    target.stop_loss   != null ? `−${currency.format(target.stop_loss)}`   : null,
  ].filter(Boolean).join(" or ");
  return target.repeat ? `${levels} · repeats` : levels;
}

function signedCurrency(v) { return (v >= 0 ? "+" : "") + currency.format(v); }

function renderPositions(rows) {
  const el = document.querySelector("#positions");
  if (!el) return;
  renderPositionsSummary(rows);
  if (!rows.length) { el.innerHTML = emptyRow(11, "No open positions."); return; }
  el.innerHTML = rows.map(t => `
    <tr>
      <td>${t.strategy_name}</td><td>${t.symbol}</td><td>${t.direction}</td>
      <td>${number.format(t.entry_price)}</td><td>${number.format(t.size)}</td>
      <td>${number.format(t.leverage)}x</td>
      <td class="neutral" data-upl="${t.symbol}_${t.direction}">—</td>
      <td>${statusBadge(t.status)}${t.close_at_price != null ? `<div style="color:var(--muted);font-size:12px;white-space:nowrap">Closes at ${number.format(t.close_at_price)}</div>` : ""}${t.trail_distance != null ? `<div style="color:var(--muted);font-size:12px;white-space:nowrap">Trails $${number.format(t.trail_distance)}</div>` : ""}</td>
      <td>${modeBadge(t.execution_mode)}</td>
      <td>${fmtDate(t.opened_at)}</td>
      <td>
        <select class="inline-input exit-select" data-trade-id="${t.id}" data-strategy="${escapeAttr(t.strategy_name)}" data-symbol="${t.symbol}" style="background:var(--red-dim);color:var(--red)">
          <option value="" selected disabled>Exit ▾</option>
          <option value="instant">Close instantly</option>
          <option value="price">${t.close_at_price != null ? "Change close price…" : "Close at price…"}</option>
          ${t.close_at_price != null ? `<option value="cancel-price">Cancel close at ${number.format(t.close_at_price)}</option>` : ""}
          <option value="tpsl">Set TP/SL…</option>
          <option value="webhook">Get webhook payload</option>
        </select>
      </td>
    </tr>`).join("");
  // Fetch live unrealized P&L from Deriv
  getJson("/api/unrealized-pnl").then(upl => {
    let total = 0, priced = 0;
    el.querySelectorAll("[data-upl]").forEach(cell => {
      const key = cell.dataset.upl;
      const val = upl[key] ?? upl[key.split("_")[0]];
      if (val !== undefined) {
        cell.textContent = (val >= 0 ? "+" : "") + currency.format(val);
        cell.className   = pnlClass(val);
        total += val;
        priced++;
      }
    });
    const runningEl = document.querySelector("#runningPnl");
    if (runningEl && priced) {
      lastRunningPnl = total;
      runningEl.textContent = signedCurrency(total);
      runningEl.className   = `metric-value ${pnlClass(total)}`;
    }
  }).catch(() => {});
  const byId = new Map(rows.map(t => [String(t.id), t]));
  el.querySelectorAll(".exit-select").forEach(sel => {
    sel.addEventListener("change", async () => {
      const action   = sel.value;
      const tradeId  = sel.dataset.tradeId;
      const trade    = byId.get(tradeId);
      const row      = sel.closest("tr");
      const label    = row ? `${row.children[1].textContent} ${row.children[2].textContent}` : "this position";
      const details  = positionDetails(trade, row);
      sel.value = ""; // reset to placeholder - each choice is a one-shot action, not a persisted setting

      if (action === "instant") {
        await openDialog({
          title: `Close ${label}`,
          message: "Sells the contract on Deriv now, at the current market price.",
          details,
          confirmLabel: "Close position",
          tone: "danger",
          onSubmit: () => closePositionNow(tradeId),
        });
      } else if (action === "price") {
        await openDialog({
          title: `Close ${label} at price`,
          details,
          fields: [{
            name: "price",
            label: "Close when price reaches",
            value: trade?.close_at_price,
            hint: "Triggers when the market touches this level, from above or below. The platform watches it, so it only fires while the server is running.",
          }],
          confirmLabel: "Set close price",
          onSubmit: ({ price }) => setCloseAtPrice(tradeId, parsePositive(price, "Enter the price to close at.")),
        });
      } else if (action === "cancel-price") {
        await setCloseAtPrice(tradeId, null).catch(err => showNotice("Couldn't cancel close price", err.message));
      } else if (action === "tpsl") {
        let current = {};
        try { current = await getJson(`/api/open-positions/${tradeId}/tpsl`); } catch { /* fields just start empty */ }
        const convert = tpslPriceConverter(current);
        const pricePlaceholder = convert ? "None" : "Unavailable";
        await openDialog({
          title: `TP/SL for ${label}`,
          details,
          fields: [
            [
              { name: "take_profit",       label: "Take profit", prefix: "$", value: current.take_profit,       placeholder: "None" },
              { name: "take_profit_price", label: "at price",                  value: current.take_profit_price, placeholder: pricePlaceholder },
            ],
            [
              { name: "stop_loss",       label: "Stop loss", prefix: "$", value: current.stop_loss,       placeholder: "None" },
              { name: "stop_loss_price", label: "at price",                value: current.stop_loss_price, placeholder: pricePlaceholder },
            ],
          ],
          note: "Type a dollar amount or a price - the other fills in. Deriv stores the dollar amount. Leave a side blank to remove it. Deriv enforces these, even if the server is down.",
          confirmLabel: "Save TP/SL",
          onReady: form => wireTpslPrices(form, convert),
          onSubmit: ({ take_profit, stop_loss }) => setPositionTpSl(tradeId, {
            take_profit: take_profit === "" ? null : parsePositive(take_profit, `Take profit must be a positive dollar amount (a price ${current.direction === "short" ? "below" : "above"} the entry).`),
            stop_loss:   stop_loss   === "" ? null : parsePositive(stop_loss, `Stop loss must be a positive dollar amount (a price ${current.direction === "short" ? "above" : "below"} the entry).`),
          }),
        });
      } else if (action === "webhook") {
        const url = window.location.origin + "/webhook/exit";
        const payload = JSON.stringify({ secret: _webhookSecret, strategy: sel.dataset.strategy, symbol: sel.dataset.symbol, price: "{{close}}" }, null, 2);
        showExitWebhookModal(label, url, payload);
      }
    });
  });
}

let _exitWebhookModalWired = false;
function showExitWebhookModal(label, url, payload) {
  const modal = document.querySelector("#exitWebhookModal");
  if (!modal) return;
  document.querySelector("#exitWebhookModalTitle").textContent = `Manual exit webhook — ${label}`;
  document.querySelector("#exitWebhookModalUrl").textContent = url;
  document.querySelector("#exitWebhookModalPayload").textContent = payload;
  modal.style.display = "flex";

  const copy = (text, btn) => {
    navigator.clipboard?.writeText(text).then(() => {
      const original = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = original; }, 1500);
    }).catch(() => {});
  };
  const hide = () => { modal.style.display = "none"; };

  if (_exitWebhookModalWired) return; // listeners only need wiring once - content is re-read live on each click
  _exitWebhookModalWired = true;

  document.querySelector("#exitWebhookModalCopyUrl").addEventListener("click", function () {
    copy(document.querySelector("#exitWebhookModalUrl").textContent, this);
  });
  document.querySelector("#exitWebhookModalCopyPayload").addEventListener("click", function () {
    copy(withRealSecret(document.querySelector("#exitWebhookModalPayload").textContent), this);
  });
  document.querySelector("#exitWebhookModalDismiss").addEventListener("click", hide);
  document.querySelector("#exitWebhookModalX").addEventListener("click", hide);
  modal.addEventListener("click", e => { if (e.target === modal) hide(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && modal.style.display !== "none") hide(); });
}

// Position actions throw on failure so the dialog that started them can show
// the reason in place. A response without a JSON detail (e.g. a server crash)
// still names its status instead of a bare "failed".
async function sendPositionAction(url, options, failure) {
  const r = await fetch(url, options);
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `${failure} (server error ${r.status}).`);
  }
  refresh();
}

// Arms (or, with price null, cancels) a pending close - the server sells the
// contract once the live spot reaches the price.
function setCloseAtPrice(tradeId, price) {
  return price == null
    ? sendPositionAction(`/api/open-positions/${tradeId}/close-at`, { method: "DELETE" }, "Couldn't cancel the close price")
    : sendPositionAction(`/api/open-positions/${tradeId}/close-at?price=${encodeURIComponent(price)}`, { method: "POST" }, "Couldn't set the close price");
}

// Converts a contract's TP/SL between Deriv's dollar amounts and prices -
// the same sum chart_tpsl_amounts does in deriv.py: P/L at a price is
// stake × multiplier × move ÷ entry, less Deriv's commission (so TP pays it
// out of its gain and SL adds it to its loss). null when Deriv didn't report
// the figures, e.g. before the contract has filled.
function tpslPriceConverter(c) {
  if (!(c.entry_spot > 0 && c.stake > 0 && c.multiplier > 0 && c.direction)) return null;
  const perPoint = c.stake * c.multiplier / c.entry_spot;
  const sign = c.direction === "long" ? 1 : -1;
  const fee = c.commission || 0;
  // Show prices to the same decimals Deriv quotes this market in.
  const decimals = Math.max(1, ...[c.entry_spot, c.take_profit_price, c.stop_loss_price]
    .filter(v => v != null).map(v => (String(v).split(".")[1] || "").length));
  // Deriv quotes a level rounded to the next tick away from the entry
  // (TP 7377.63 shows as 7377.7, SL 7335.12 as 7335.1) - match it.
  const scale = 10 ** decimals;
  const awayFromEntry = (level, up) => (up ? Math.ceil(level * scale - 1e-6) : Math.floor(level * scale + 1e-6)) / scale;
  return {
    amount: (side, price) => (side === "take_profit"
      ? sign * (price - c.entry_spot) * perPoint - fee
      : sign * (c.entry_spot - price) * perPoint + fee).toFixed(2),
    price: (side, amount) => (side === "take_profit"
      ? awayFromEntry(c.entry_spot + sign * (amount + fee) / perPoint, sign > 0)
      : awayFromEntry(c.entry_spot - sign * (amount - fee) / perPoint, sign < 0)).toFixed(decimals),
  };
}

// Typing in a TP/SL dollar box fills its price box, and the other way round.
function wireTpslPrices(form, convert) {
  for (const side of ["take_profit", "stop_loss"]) {
    const amount = form.elements[side];
    const price = form.elements[`${side}_price`];
    if (!convert) { price.readOnly = true; continue; }
    const fill = (from, to, fn) => from.addEventListener("input", () => {
      const n = parseFloat(from.value);
      to.value = from.value.trim() === "" || !Number.isFinite(n) ? "" : fn(side, n);
    });
    fill(amount, price, convert.price);
    fill(price, amount, convert.amount);
  }
}

// Dollar TP/SL on one open contract, set on Deriv itself. null removes that side.
function setPositionTpSl(tradeId, tpsl) {
  return sendPositionAction(`/api/open-positions/${tradeId}/tpsl`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tpsl),
  }, "Couldn't update the TP/SL");
}

function closePositionNow(tradeId) {
  return sendPositionAction(`/api/open-positions/${tradeId}/close`, { method: "POST" }, "Couldn't close the position");
}

// Closes every open position in the current mode. The server keeps going
// past a failure, so a partial result is reported rather than thrown.
async function closeAllPositions() {
  const r = await fetch("/api/open-positions/close-all", { method: "POST" });
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `Couldn't close the positions (server error ${r.status}).`);
  }
  const result = await r.json();
  refresh();
  if (result.failed.length) {
    openDialog({
      title: "Some positions are still open",
      message: `Closed ${result.closed}, but ${result.failed.length} couldn't be closed on Deriv and are still open.`,
      details: result.failed.map(f => [f.symbol, f.error]),
      confirmLabel: "OK",
      cancelLabel: null,
    });
  }
}

document.querySelector("#closeAllPositions")?.addEventListener("click", () => {
  const positions = latestState.positions || [];
  if (!positions.length) return;
  const mode = (positions[0].execution_mode || "").toUpperCase();
  openDialog({
    title: `Close all ${positions.length} open position${positions.length === 1 ? "" : "s"}`,
    message: `Sells every ${mode} contract on Deriv now, at the current market price. This can't be undone.`,
    details: positions.map(p => [p.strategy_name, `${p.symbol} ${p.direction}`]),
    confirmLabel: "Close all",
    tone: "danger",
    onSubmit: closeAllPositions,
  });
});

// Close-all target: the server closes every open position once their summed
// unrealized P/L reaches it. Both fields blank removes it.
document.querySelector("#closeAllTarget")?.addEventListener("click", () => {
  const { risk, positions = [] } = latestState;
  const current = risk?.close_all_target || {};
  const mode = (risk?.execution_mode || "").toUpperCase();
  openDialog({
    title: "Close-all target",
    message: `Closes every ${mode} position at market once the running P/L of all of them together reaches either level.`,
    details: [
      ["Open positions", String(positions.length)],
      ["Running P/L now", lastRunningPnl === null ? "—" : signedCurrency(lastRunningPnl), lastRunningPnl === null ? "" : pnlClass(lastRunningPnl)],
    ],
    fields: [
      { name: "take_profit", label: "Close all when profit reaches", prefix: "$", value: current.take_profit, placeholder: "None" },
      { name: "stop_loss",   label: "Close all when loss reaches",   prefix: "$", value: current.stop_loss,   placeholder: "None" },
      { name: "repeat", type: "checkbox", label: "Run until changed", value: current.repeat,
        hint: "Keeps the target after it fires, so it applies to every new set of positions. Unticked, it fires once and clears." },
    ],
    note: "Leave both blank to remove the target. The platform watches it, so it only fires while the server is running.",
    confirmLabel: "Save target",
    onSubmit: async ({ take_profit, stop_loss, repeat }) => {
      const body = {
        repeat,
        take_profit: take_profit === "" ? null : parsePositive(take_profit, "Profit target must be a positive dollar amount."),
        stop_loss:   stop_loss   === "" ? null : parsePositive(stop_loss, "Loss limit must be a positive dollar amount."),
      };
      const cleared = body.take_profit === null && body.stop_loss === null;
      const r = await fetch("/api/open-positions/close-all-target", cleared
        ? { method: "DELETE" }
        : { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))).detail;
        throw new Error(typeof detail === "string" ? detail : `Couldn't save the target (server error ${r.status}).`);
      }
      refresh();
    },
  });
});

function parsePositive(raw, message) {
  const value = Number(raw);
  if (raw === "" || !Number.isFinite(value) || value <= 0) throw new Error(message);
  return value;
}

function positionDetails(trade, row) {
  if (!trade) return [];
  const upl = row?.querySelector("[data-upl]");
  return [
    ["Entry", number.format(trade.entry_price)],
    ["Stake", currency.format(trade.size)],
    ["Multiplier", `${number.format(trade.leverage)}x`],
    ["Unrealized P/L", upl?.textContent || "—", upl?.className],
  ];
}

// ─── In-app dialog (instead of the browser's confirm/prompt/alert) ─────────

// Resolves true once onSubmit succeeds, false if dismissed. onSubmit receives
// the trimmed field values by name; if it throws, the message is shown inside
// the dialog and it stays open so the input can be corrected.
// `fields` entries can also be arrays of fields, shown side by side on one
// row. `onReady(form)` runs once the dialog is on screen, for wiring fields
// that update each other.
function openDialog({ title, message, details = [], fields = [], note, confirmLabel = "Confirm", tone = "primary", cancelLabel = "Cancel", onSubmit, onReady }) {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const fieldHtml = f => f.type === "checkbox" ? `
          <label class="dialog-check">
            <input type="checkbox" name="${escapeAttr(f.name)}"${f.value ? " checked" : ""} />
            <span>${escapeAttr(f.label)}${f.hint ? `<span class="dialog-hint">${escapeAttr(f.hint)}</span>` : ""}</span>
          </label>` : `
          <label class="dialog-field">
            <span class="webhook-label">${escapeAttr(f.label)}</span>
            <span class="dialog-input${f.prefix ? " has-prefix" : ""}">
              ${f.prefix ? `<span class="dialog-prefix">${escapeAttr(f.prefix)}</span>` : ""}
              <input type="text" inputmode="decimal" autocomplete="off" name="${escapeAttr(f.name)}"
                value="${f.value != null ? escapeAttr(f.value) : ""}" placeholder="${escapeAttr(f.placeholder || "")}" />
            </span>
            ${f.hint ? `<span class="dialog-hint">${escapeAttr(f.hint)}</span>` : ""}
          </label>`;
    overlay.innerHTML = `
      <form class="modal-box dialog-box" role="dialog" aria-modal="true" novalidate>
        <div class="modal-header">
          <span class="modal-title">${escapeAttr(title)}</span>
          <button type="button" class="modal-close-x" data-cancel aria-label="Close">&times;</button>
        </div>
        ${message ? `<p class="dialog-message">${escapeAttr(message)}</p>` : ""}
        ${details.length ? `<dl class="dialog-details">${details.map(([k, v, cls]) => `
          <div><dt>${escapeAttr(k)}</dt><dd class="${escapeAttr(cls || "")}">${escapeAttr(v)}</dd></div>`).join("")}</dl>` : ""}
        ${fields.map(f => Array.isArray(f) ? `<div class="dialog-row">${f.map(fieldHtml).join("")}</div>` : fieldHtml(f)).join("")}
        ${note ? `<p class="dialog-hint">${escapeAttr(note)}</p>` : ""}
        <p class="dialog-error" role="alert" hidden></p>
        <div class="modal-footer">
          ${cancelLabel ? `<button type="button" class="dialog-btn secondary" data-cancel>${escapeAttr(cancelLabel)}</button>` : ""}
          <button type="submit" class="dialog-btn ${tone}">${escapeAttr(confirmLabel)}</button>
        </div>
      </form>`;
    document.body.appendChild(overlay);

    const form = overlay.querySelector("form");
    const error = overlay.querySelector(".dialog-error");
    const submit = form.querySelector("button[type=submit]");
    let busy = false;
    const close = ok => {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(ok);
    };
    const onKey = e => { if (e.key === "Escape" && !busy) close(false); };
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("click", e => { if (!busy && (e.target === overlay || e.target.closest("[data-cancel]"))) close(false); });

    form.addEventListener("submit", async e => {
      e.preventDefault();
      if (busy) return;
      const values = Object.fromEntries(fields.flat().map(f => {
        const input = form.elements[f.name];
        return [f.name, f.type === "checkbox" ? input.checked : input.value.trim()];
      }));
      busy = true;
      error.hidden = true;
      form.querySelectorAll("button, input").forEach(n => { n.disabled = true; });
      submit.textContent = "Working…";
      try {
        if (onSubmit) await onSubmit(values);
        close(true);
      } catch (err) {
        busy = false;
        form.querySelectorAll("button, input").forEach(n => { n.disabled = false; });
        submit.textContent = confirmLabel;
        error.textContent = err.message || "Something went wrong.";
        error.hidden = false;
        (form.querySelector("input") || submit).focus();
      }
    });

    onReady?.(form);
    const firstInput = form.querySelector("input");
    (firstInput || submit).focus();
    firstInput?.select();
  });
}

function showNotice(title, message) {
  return openDialog({ title, message, confirmLabel: "OK", cancelLabel: null });
}

function symbolPerfRows(rows) {
  const bySymbol = new Map();
  rows.filter(t => t.closed_at).forEach(t => {
    if (!bySymbol.has(t.symbol)) bySymbol.set(t.symbol, []);
    bySymbol.get(t.symbol).push(t);
  });
  const out = [];
  bySymbol.forEach((trades, symbol) => {
    if (!trades.length) return; // 0 executed trades for this symbol - nothing to plot
    const wins = trades.filter(t => t.net_result > 0);
    const losses = trades.filter(t => t.net_result < 0);
    out.push({
      symbol,
      trades: trades.length,
      net: trades.reduce((s, t) => s + t.net_result, 0),
      winRate: trades.length ? wins.length / trades.length * 100 : 0,
      avgWin: wins.length ? wins.reduce((s, t) => s + t.net_result, 0) / wins.length : 0,
      avgLoss: losses.length ? losses.reduce((s, t) => s + t.net_result, 0) / losses.length : 0,
    });
  });
  out.sort((a, b) => b.net - a.net);
  return out;
}

// A bar rounded only on its data-end (4px), square at the zero baseline -
// never symmetric, so the baseline reads flush against the axis.
function drawDivergingBar(ctx, x, y, w, h, r, roundRight) {
  const rad = Math.max(0, Math.min(r, w, h / 2));
  ctx.beginPath();
  if (roundRight) {
    ctx.moveTo(x, y);
    ctx.lineTo(x + w - rad, y);
    ctx.arcTo(x + w, y, x + w, y + rad, rad);
    ctx.lineTo(x + w, y + h - rad);
    ctx.arcTo(x + w, y + h, x + w - rad, y + h, rad);
    ctx.lineTo(x, y + h);
  } else {
    ctx.moveTo(x + rad, y);
    ctx.lineTo(x + w, y);
    ctx.lineTo(x + w, y + h);
    ctx.lineTo(x + rad, y + h);
    ctx.arcTo(x, y + h, x, y + h - rad, rad);
    ctx.lineTo(x, y + rad);
    ctx.arcTo(x, y, x + rad, y, rad);
  }
  ctx.closePath();
}

function drawSymbolPerfChart(canvas, rows, hoverY) {
  symbolPerfState.rows = rows;
  const setup = setupCanvas(canvas);
  if (!setup) return;
  const { ctx, width, height } = setup;
  clearCanvas(ctx, width, height);

  const captionEl = document.querySelector("#symbolPerfCaption");
  if (captionEl) captionEl.textContent = `${rows.length} symbol${rows.length === 1 ? "" : "s"} with closed trades`;

  if (!rows.length) { drawEmpty(ctx, width, height, "No closed trades yet"); return; }

  // Padding scales with the canvas's actual rendered width (it's 100% of
  // its container, so this varies a lot) instead of fixed pixels - on a
  // narrow viewport, fixed 172+64px of padding would leave almost no room
  // for bars at all.
  const narrow = width < 420;
  const pad = { top: 10, right: narrow ? 34 : 64, bottom: 6, left: narrow ? 92 : 172 };
  const maxLabelChars = narrow ? 11 : 22;
  const barAreaX0 = pad.left, barAreaX1 = width - pad.right;
  const barAreaWidth = Math.max(1, barAreaX1 - barAreaX0);
  const rowH = Math.max(16, (height - pad.top - pad.bottom) / rows.length);
  const barH = Math.min(24, rowH - 4);

  const maxPos = Math.max(0, ...rows.map(r => r.net));
  const maxNeg = Math.max(0, ...rows.map(r => -r.net));
  const span = maxPos + maxNeg || 1;
  const zeroX = barAreaX0 + barAreaWidth * (maxNeg / span);

  // Zero baseline - recessive, hairline, solid (never dashed per spec).
  ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(zeroX, pad.top); ctx.lineTo(zeroX, height - pad.bottom); ctx.stroke();

  ctx.font = "600 11px Inter, sans-serif"; ctx.textBaseline = "middle";
  let hoverRow = null;

  rows.forEach((r, i) => {
    const rowY = pad.top + i * rowH;
    const barY = rowY + (rowH - barH) / 2;
    const isPos = r.net >= 0;
    const barLen = Math.abs(r.net) / span * barAreaWidth;
    const barX = isPos ? zeroX : zeroX - barLen;
    const color = isPos ? "#22c55e" : "#ef4444";

    if (hoverY !== null && hoverY >= rowY && hoverY < rowY + rowH) hoverRow = r;

    // Symbol label - text token color, never the series color (spec).
    ctx.fillStyle = "#94a3b8"; ctx.textAlign = "right"; ctx.font = "600 11px Inter, sans-serif";
    const label = r.symbol.length > maxLabelChars ? r.symbol.slice(0, maxLabelChars - 1) + "…" : r.symbol;
    ctx.fillText(label, pad.left - 10, rowY + rowH / 2);

    ctx.fillStyle = color;
    drawDivergingBar(ctx, barX, barY, Math.max(barLen, 1), barH, 4, isPos);
    ctx.fill();

    // Direct value label at the tip - measured first, placed outside the
    // bar if it fits, otherwise pulled inside against the fill.
    const valueText = (r.net >= 0 ? "+" : "") + currency.format(r.net);
    ctx.font = "700 11px monospace";
    const textW = ctx.measureText(valueText).width;
    const outsideX = isPos ? barX + barLen + 6 : barX - 6;
    // Left boundary is where the symbol-label column ends (pad.left), not
    // the canvas edge - a long negative bar's tip sits right next to that
    // column, and checking against the canvas edge let the value label land
    // on top of the symbol name instead of falling back inside the bar.
    const fitsOutside = isPos ? outsideX + textW <= width - 4 : outsideX - textW >= pad.left + 4;
    if (fitsOutside) {
      ctx.fillStyle = "#e2e8f0";
      ctx.textAlign = isPos ? "left" : "right";
      ctx.fillText(valueText, outsideX, rowY + rowH / 2);
    } else if (barLen > textW + 12) {
      ctx.fillStyle = "#0b0b0f";
      ctx.textAlign = isPos ? "right" : "left";
      ctx.fillText(valueText, isPos ? barX + barLen - 6 : barX + 6, rowY + rowH / 2);
    }
  });

  if (hoverRow) {
    const idx = rows.indexOf(hoverRow);
    const rowY = pad.top + idx * rowH;
    ctx.fillStyle = "rgba(255,255,255,0.04)";
    ctx.fillRect(barAreaX0 - 2, rowY, barAreaX1 - barAreaX0 + 2, rowH);

    const lines = [
      hoverRow.symbol,
      `${hoverRow.trades} trades · ${hoverRow.winRate.toFixed(1)}% win rate`,
      `avg win ${currency.format(hoverRow.avgWin)} · avg loss ${currency.format(hoverRow.avgLoss)}`,
    ];
    ctx.font = "600 11px Inter, sans-serif";
    const tW = Math.max(...lines.map(l => ctx.measureText(l).width)) + 24;
    const tH = lines.length * 16 + 12;
    let tx = zeroX + 10;
    if (tx + tW > width - 4) tx = width - 4 - tW;
    let ty = rowY + rowH / 2 - tH / 2;
    ty = Math.max(pad.top, Math.min(ty, height - pad.bottom - tH));

    ctx.fillStyle = "#111318"; ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1;
    roundRect(ctx, tx, ty, tW, tH, 6); ctx.fill(); ctx.stroke();
    ctx.textAlign = "left";
    lines.forEach((line, i) => {
      ctx.fillStyle = i === 0 ? "#e2e8f0" : "#94a3b8";
      ctx.font = i === 0 ? "700 11px Inter, sans-serif" : "11px Inter, sans-serif";
      ctx.fillText(line, tx + 12, ty + 14 + i * 16);
    });
  }
}

function historyStats(rows) {
  const closed = rows.filter(t => t.closed_at);
  const wins = closed.filter(t => t.net_result > 0);
  const losses = closed.filter(t => t.net_result < 0);
  const netTotal = closed.reduce((s, t) => s + t.net_result, 0);
  return {
    count: closed.length,
    winRate: closed.length ? (wins.length / closed.length * 100) : 0,
    netTotal,
    avgWin: wins.length ? wins.reduce((s, t) => s + t.net_result, 0) / wins.length : 0,
    avgLoss: losses.length ? losses.reduce((s, t) => s + t.net_result, 0) / losses.length : 0,
  };
}

// The frozen Symbol column sits right after Strategy, whose width depends on
// the bot names - re-measure whenever it changes, including when the page
// first becomes visible (a hidden table measures 0).
const historyFirstCol = document.querySelector("#historyTable th");
if (historyFirstCol && window.ResizeObserver) {
  new ResizeObserver(() => {
    historyFirstCol.closest("table").style.setProperty("--frozen-left", `${historyFirstCol.offsetWidth}px`);
  }).observe(historyFirstCol);
}

function renderHistory(rows) {
  const el = document.querySelector("#history");
  const filterEl = document.querySelector("#historySymbolFilter");
  const statsEl = document.querySelector("#historyStats");
  if (!el) return;

  // Always the full unfiltered set, independent of historySymbolFilter -
  // comparing across symbols is the whole point of this chart.
  drawSymbolPerfChart(document.querySelector("#symbolPerfChart"), symbolPerfRows(rows), null);

  if (filterEl) {
    // Only rebuild the option list when the underlying symbol set actually
    // changes, so picking a symbol survives the 10s refresh cycle instead
    // of silently resetting to "All symbols" every time.
    const symbols = [...new Set(rows.map(t => t.symbol))].sort();
    const existing = [...filterEl.options].slice(1).map(o => o.value);
    if (existing.join(",") !== symbols.join(",")) {
      const current = filterEl.value || historySymbolFilter;
      filterEl.innerHTML = `<option value="all">All symbols</option>` +
        symbols.map(s => `<option value="${s}">${s}</option>`).join("");
      filterEl.value = symbols.includes(current) ? current : "all";
      historySymbolFilter = filterEl.value;
    }
    if (!filterEl.dataset.wired) {
      filterEl.addEventListener("change", () => {
        historySymbolFilter = filterEl.value;
        renderHistory(latestState.history || []);
      });
      filterEl.dataset.wired = "1";
    }
  }

  const filtered = rows
    .filter(t => historySymbolFilter === "all" || t.symbol === historySymbolFilter);

  if (statsEl) {
    const s = historyStats(filtered);
    statsEl.innerHTML = `
      <div class="metric">
        <span class="metric-label">Trades</span>
        <span class="metric-value neutral">${s.count}</span>
        <span class="metric-sub">${historySymbolFilter === "all" ? "all symbols, closed" : historySymbolFilter + ", closed"}</span>
      </div>
      <div class="metric">
        <span class="metric-label">Win rate</span>
        <span class="metric-value ${s.winRate >= 50 ? "positive" : "negative"}">${s.count ? s.winRate.toFixed(1) : "0.0"}%</span>
        <span class="metric-sub">of closed trades</span>
      </div>
      <div class="metric">
        <span class="metric-label">Net P&amp;L</span>
        <span class="metric-value ${pnlClass(s.netTotal)}">${currency.format(s.netTotal)}</span>
        <span class="metric-sub">closed trades total</span>
      </div>
      <div class="metric">
        <span class="metric-label">Avg win / loss</span>
        <span class="metric-value neutral">${currency.format(s.avgWin)} / ${currency.format(s.avgLoss)}</span>
        <span class="metric-sub">per closed trade</span>
      </div>`;
  }

  if (!filtered.length) { el.innerHTML = emptyRow(14, "No trades recorded."); return; }
  el.innerHTML = filtered.map(t => `
    <tr>
      <td>${t.strategy_name}</td><td>${t.symbol}</td><td>${t.direction}</td>
      <td>${number.format(t.entry_price)}</td>
      <td>${t.exit_price == null ? "—" : number.format(t.exit_price)}</td>
      <td>${number.format(t.size)}</td>
      <td>${number.format(t.leverage)}x</td>
      <td class="${pnlClass(t.profit_loss)}">${currency.format(t.profit_loss)}</td>
      <td class="${pnlClass(t.net_result)}">${currency.format(t.net_result)}</td>
      <td>${tradeReason(t)}</td>
      <td>${modeBadge(t.execution_mode)}</td>
      <td>${fmtDate(t.opened_at)}</td>
      <td>${fmtDate(t.closed_at)}</td>
      <td>${t.closed_at ? duration(t.opened_at, t.closed_at) : "—"}</td>
    </tr>`).join("");
}


// ─── Refresh / Render ────────────────────────────────────────────────────────

async function refresh() {
  const [summary, risk, balance, performance, symbolPerformance, positions, signals, history, analytics, symbolLeverage] = await Promise.all([
    getJson("/api/summary"),
    getJson("/api/risk"),
    getJson("/api/account-balance").catch(() => ({})),
    getJson(`/api/performance?period=${activePeriod}`),
    // Strategy Limits keeps using the unfiltered rows; only the Performance
    // page follows the symbol picker.
    performanceSymbolFilter === "all"
      ? null
      : getJson(`/api/performance?period=${activePeriod}&symbol=${encodeURIComponent(performanceSymbolFilter)}`),
    getJson("/api/open-positions"),
    getJson("/api/signals?limit=200"),
    getJson("/api/trade-history?limit=1000"),
    getJson(`/api/analytics?period=${activePeriod}`),
    getJson("/api/symbol-leverage").catch(() => []),
  ]);
  latestState = { summary, risk, balance, performance, symbolPerformance, positions, signals, history, analytics, symbolLeverage };
  renderAll();
}

// Which Synthetic account a bot or trade belongs to: Multipliers ("options")
// or CFDs on cTrader ("ctrader").
function isCfd(row) { return row.account === "ctrader"; }

function renderAll() {
  const { risk, performance, signals, analytics, symbolLeverage } = latestState;
  const positions = latestState.positions.filter(t => !isCfd(t));
  const history = latestState.history.filter(t => !isCfd(t));
  const tableRows = (performanceSymbolFilter !== "all" && latestState.symbolPerformance) || performance;
  const focused = activeStrategy === "all" ? tableRows : tableRows.filter(r => r.strategy_name === activeStrategy);

  // Header badges
  const el = (id) => document.querySelector(id);
  if (el("#positionCount"))  el("#positionCount").textContent  = `${positions.length} open`;
  // visibility, not display, so the header keeps its height and the page doesn't jump.
  if (el("#closeAllPositions")) el("#closeAllPositions").style.visibility = positions.length ? "visible" : "hidden";
  if (el("#closeAllTarget")) {
    const target = risk?.close_all_target;
    el("#closeAllTarget").textContent = target ? `Target: ${closeAllTargetText(target)}` : "Close-all target";
  }
  if (el("#historyCount"))   el("#historyCount").textContent   = `${history.length} trades`;

  const fl = document.querySelector("#focusLabel");
  if (fl) {
    const strategyText = activeStrategy === "all" ? "All strategies" : `Focused on ${activeStrategy}`;
    fl.textContent = performanceSymbolFilter === "all" ? strategyText : `${strategyText} · ${performanceSymbolFilter}`;
  }
  renderPerformanceSymbolFilter(history);

  const rawCurve = analytics.equity_curve
    .filter(p => activeStrategy === "all" || p.strategy_name === activeStrategy)
    .filter(p => performanceSymbolFilter === "all" || p.symbol === performanceSymbolFilter);
  const curve = cumulativeCurve(rawCurve);

  renderSummary();
  renderRisk(risk);
  renderRiskStatus();
  renderCharts(curve, focused, analytics, signals);
  renderPerformance(focused);
  renderStrategyRisk(performance);
  renderSymbolLeverage(symbolLeverage);
  if (lastAppSettings && document.querySelector("#page-system-settings.active")) renderAppSettings(lastAppSettings);
  renderPositions(positions);
  renderSignals(signals);
  renderHistory(history);
  renderCfdTrades(latestState.positions.filter(isCfd), latestState.history.filter(isCfd));
}

function renderCfdTrades(open, closed) {
  const pending = "cTrader orders aren't switched on yet";
  document.querySelector("#cfdPositionCount").textContent = `${open.length} open`;
  document.querySelector("#cfdPositions").innerHTML = open.length
    ? open.map(t => `<tr>
      <td>${escapeAttr(t.strategy_name)}</td><td>${escapeAttr(t.symbol)}</td><td>${escapeAttr(t.direction)}</td>
      <td>${number.format(t.size)}</td>
      <td>${number.format(t.entry_price)}</td>
      <td>${modeBadge(t.execution_mode)}</td>
      <td>${fmtDate(t.opened_at)}</td>
    </tr>`).join("")
    : emptyRow(7, `No open CFD positions. ${pending}.`);
  document.querySelector("#cfdStatOpen").textContent = open.length;
  document.querySelector("#cfdStatClosed").textContent = closed.length;
  const net = closed.reduce((sum, t) => sum + t.net_result, 0);
  const pnl = document.querySelector("#cfdStatPnl");
  pnl.textContent = closed.length ? `${net > 0 ? "+" : ""}${currency.format(net)}` : "—";
  pnl.className = `crypto-figure-value ${closed.length ? pnlClass(net) : ""}`;
  document.querySelector("#cfdHistoryCount").textContent = `${closed.length} trade${closed.length === 1 ? "" : "s"}`;
  document.querySelector("#cfdHistory").innerHTML = closed.length
    ? closed.map(t => `<tr>
      <td>${escapeAttr(t.strategy_name)}</td><td>${escapeAttr(t.symbol)}</td><td>${escapeAttr(t.direction)}</td>
      <td>${number.format(t.size)}</td>
      <td>${number.format(t.entry_price)}</td>
      <td>${t.exit_price == null ? "—" : number.format(t.exit_price)}</td>
      <td class="${pnlClass(t.net_result)}">${currency.format(t.net_result)}</td>
      <td>${tradeReason(t)}</td>
      <td>${modeBadge(t.execution_mode)}</td>
      <td>${fmtDate(t.opened_at)}</td>
      <td>${fmtDate(t.closed_at)}</td>
      <td>${t.closed_at ? duration(t.opened_at, t.closed_at) : "—"}</td>
    </tr>`).join("")
    : emptyRow(12, `No closed CFD trades yet. ${pending}.`);
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

// Axis label that stays readable at any scale: "$1.2k" for big ranges,
// "$12" or "$1.5" for the small ones a $10-stake account actually has.
function compactUsd(v, span) {
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (span >= 1000) return `${sign}$${(a / 1000).toFixed(span >= 10000 ? 0 : 1)}k`;
  return `${sign}$${a.toFixed(span >= 20 ? 0 : 1)}`;
}

function renderPnlChart() {
  const balanceNow = latestState.balance?.equity ?? null;
  const data = equityCurveToPnlSeries(latestState.analytics.equity_curve, activePeriod, latestState.history, balanceNow);
  const lastPnl = data.length ? data[data.length - 1].pnl : 0;
  const isPos = lastPnl >= 0;
  const pnlEl = document.querySelector("#pnlHeadline");
  if (pnlEl) {
    pnlEl.textContent = (isPos ? "+" : "-") + currency.format(Math.abs(lastPnl));
    pnlEl.style.color = isPos ? "#22c55e" : "#ef4444";
  }
  const balEl = document.querySelector("#pnlBalance");
  if (balEl) balEl.textContent = balanceNow == null ? "" : `Balance ${currency.format(balanceNow)}`;
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
    ctx.fillText(compactUsd(v, span), pad.left - 4, pad.top + (cH / 4) * i + 4);
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

    const hasBal = pt.balance != null;
    const tW = 140, tH = hasBal ? 62 : 46;
    const tx = cx + 10 + tW > width - 8 ? cx - tW - 10 : cx + 10;
    const ty = Math.max(pad.top, Math.min(cy - tH / 2, height - pad.bottom - tH));
    ctx.fillStyle = "#111318"; ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1;
    roundRect(ctx, tx, ty, tW, tH, 6); ctx.fill(); ctx.stroke();

    ctx.fillStyle = "#94a3b8"; ctx.font = "10px monospace"; ctx.textAlign = "left";
    ctx.fillText(pt.t, tx + 10, ty + 16);
    ctx.fillStyle = lineClr; ctx.font = "700 12px monospace";
    ctx.fillText((pt.pnl < 0 ? "-" : "+") + currency.format(Math.abs(pt.pnl)), tx + 10, ty + 33);
    if (hasBal) {
      ctx.fillStyle = "#94a3b8"; ctx.font = "10px monospace";
      ctx.fillText(`Bal ${currency.format(pt.balance)}`, tx + 10, ty + 51);
    }
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
  drawSignalActivityChart(document.querySelector("#activityChart"), buildActivityBuckets(signals, activePeriod), null);
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

  const perfCanvas = document.querySelector("#symbolPerfChart");
  if (perfCanvas) {
    perfCanvas.addEventListener("mousemove", e => {
      if (!symbolPerfState.rows) return;
      drawSymbolPerfChart(perfCanvas, symbolPerfState.rows, e.clientY - perfCanvas.getBoundingClientRect().top);
    });
    perfCanvas.addEventListener("mouseleave", () => {
      if (symbolPerfState.rows) drawSymbolPerfChart(perfCanvas, symbolPerfState.rows, null);
    });
  }
})();

function periodToPnlRange(period) {
  return period === "today" ? "1D" : period === "week" ? "5D" : "MTD";
}
function pnlRangeToPeriod(range) {
  return range === "1D" ? "today" : range === "5D" ? "week" : "month";
}
function syncPeriodControls() {
  document.querySelectorAll(".pnl-range-btn").forEach(b => b.classList.toggle("active", b.dataset.pnlRange === pnlRange));
  document.querySelectorAll("[data-period]").forEach(b => b.classList.toggle("active", b.dataset.period === activePeriod));
  document.querySelectorAll("[data-period-table]").forEach(b => b.classList.toggle("active", b.dataset.periodTable === activePeriod));
}

// P&L range switcher
document.querySelectorAll(".pnl-range-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    pnlRange = btn.dataset.pnlRange;
    activePeriod = pnlRangeToPeriod(pnlRange);
    syncPeriodControls();
    refresh();
  });
});

// Period tabs — Dashboard (1D / 5D / MTD / ALL)
document.querySelectorAll("[data-period]").forEach(btn => {
  btn.addEventListener("click", () => {
    activePeriod = btn.dataset.period;
    pnlRange = periodToPnlRange(activePeriod);
    syncPeriodControls();
    refresh();
  });
});

// Period tabs — Strategies Ranking (Today / Week / Month / All)
document.querySelectorAll("[data-period-table]").forEach(btn => {
  btn.addEventListener("click", () => {
    activePeriod = btn.dataset.periodTable;
    pnlRange = periodToPnlRange(activePeriod);
    syncPeriodControls();
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

// Execution mode — demo/live toggle
document.querySelector("#executionModeToggle")?.addEventListener("click", async () => {
  const goingLive = latestState.risk.execution_mode !== "live";
  const switchMode = async () => {
    const r = await fetch("/api/risk", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ execution_mode: goingLive ? "live" : "demo" }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `Failed to switch execution mode (server error ${r.status}).`);
    }
    await refresh();
  };
  if (goingLive) {
    await openDialog({
      title: "Switch to live trading?",
      message: "Real orders will be sent to your real Deriv account with real money.",
      confirmLabel: "Switch to live",
      tone: "danger",
      onSubmit: switchMode,
    });
  } else {
    await switchMode().catch(err => showNotice("Couldn't switch to demo", err.message));
  }
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

// ─── Settings page (whole platform) ─────────────────────────────────────────

async function loadAppSettings() {
  try {
    renderAppSettings(await getJson("/api/app-settings"));
  } catch (err) {
    document.querySelector("#capError").textContent = `Couldn't load settings: ${err.message}`;
  }
}

function renderAppSettings(s) {
  lastAppSettings = s;
  // Cap: the input holds only a value saved here; blank shows the Railway one.
  const capInput = document.querySelector("#multiplierCap");
  if (document.activeElement !== capInput) capInput.value = s.max_multiplier_floor_saved ?? "";
  capInput.placeholder = `${s.env_max_multiplier_floor} (Railway)`;
  const capSrc = s.max_multiplier_floor_saved != null ? "saved here" : "from Railway";
  const noCap = s.max_multiplier_floor >= 10000;
  document.querySelector("#capStatus").innerHTML =
    `In use: <strong>${s.max_multiplier_floor}x</strong> (${capSrc})` +
    (noCap ? ` · <span class="warn-text">effectively no cap — every symbol is allowed</span>` : "");
  document.querySelector("#capPanel").classList.toggle("critical", noCap);

  // Time zone: "" = the Railway value.
  const sel = document.querySelector("#timezoneSelect");
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = []; }
  for (const z of [s.env_timezone, s.timezone_saved, "UTC"]) if (z && !zones.includes(z)) zones.unshift(z);
  if (sel.options.length !== zones.length + 1) {
    sel.innerHTML = `<option value="">Railway value (${escapeAttr(s.env_timezone)})</option>` +
      zones.map(z => `<option value="${escapeAttr(z)}">${escapeAttr(z.replace(/_/g, " "))}</option>`).join("");
  }
  sel.value = s.timezone_saved ?? "";
  document.querySelector("#tzStatus").innerHTML =
    `In use: <strong>${escapeAttr(s.timezone)}</strong> (${s.timezone_saved ? "saved here" : "from Railway"})`;
  const device = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const useDevice = document.querySelector("#useDeviceZone");
  useDevice.hidden = !device || device === s.timezone;
  useDevice.textContent = `Use this device's zone (${device})`;
  useDevice.dataset.zone = device || "";

  // Connections - set/missing only.
  const row = (label, sub, badge, cls) =>
    `<div class="settings-row"><div>${label}${sub ? `<span class="sub">${sub}</span>` : ""}</div><span class="badge ${cls}">${badge}</span></div>`;
  const deriv = s.deriv.map(d => {
    const active = d.mode === s.execution_mode;
    const sub = [d.account_id ? escapeAttr(d.account_id) : "no account id", active ? "in use now" : ""].filter(Boolean).join(" · ");
    return d.missing.length
      ? row(`Deriv ${d.mode}`, `Missing ${d.missing.map(escapeAttr).join(", ")}`, "Missing", "bad")
      : row(`Deriv ${d.mode}`, sub, "Ready", "ok");
  }).join("");
  // From the dashboard's own balance fetch; redrawn on each refresh (renderAll).
  const bal = latestState.balance || {};
  const balanceRow = !("equity" in bal)
    ? row("Balance check", `${escapeAttr(s.execution_mode)} account`, "Checking…", "")
    : bal.equity == null
      ? row("Balance check", `${escapeAttr(s.execution_mode)} account: ${escapeAttr(bal.error || "no answer")}`, "Failing", "bad")
      : row("Balance check", `${escapeAttr(s.execution_mode)} account answering`, "OK", "ok");
  const blofin = s.blofin.map(b => {
    const sub = [b.mode === "live" ? "crypto futures · real money" : "crypto futures · demo trading", b.mode === s.execution_mode ? "in use now" : ""].filter(Boolean).join(" · ");
    return b.missing.length
      ? row(`BloFin ${b.mode}`, `Missing ${b.missing.map(escapeAttr).join(", ")}`, "Not set", "warn")
      : row(`BloFin ${b.mode}`, sub, "Ready", "ok");
  }).join("");
  document.querySelector("#connectionRows").innerHTML = deriv + balanceRow + blofin +
    row("Webhook secret", s.webhook_secret_set ? "set in Railway" : "still the default — alerts can be forged", s.webhook_secret_set ? "Set" : "Default", s.webhook_secret_set ? "ok" : "bad") +
    row("Dashboard sign-in", s.sign_in_enabled ? `Google · ${s.allowed_email_count} allowed email${s.allowed_email_count === 1 ? "" : "s"}` : "anyone with the URL can open the dashboard", s.sign_in_enabled ? "On" : "Off", s.sign_in_enabled ? "ok" : "warn");

  // Telegram alerts - status plus one switch per event.
  const tg = s.telegram;
  document.querySelector("#telegramStatus").innerHTML = tg.configured
    ? row("Telegram bot", "token and chat id set in Railway", "Ready", "ok")
    : row("Telegram bot", `Missing ${tg.missing.map(escapeAttr).join(", ")}`, "Off", "warn");
  document.querySelector("#telegramEvents").innerHTML = tg.events.map(e => `
    <div class="switch-row">
      <span>${escapeAttr(e.label)}</span>
      <button class="switch telegram-switch${e.enabled ? " on" : ""}" type="button" data-telegram-event="${escapeAttr(e.key)}"
        aria-pressed="${e.enabled}" aria-label="${escapeAttr(e.label)} alerts"><span></span></button>
    </div>`).join("");
  document.querySelector("#telegramTest").disabled = !tg.configured;
}

async function saveAppSetting(body, errorSel) {
  const errEl = document.querySelector(errorSel);
  errEl.textContent = "";
  try {
    renderAppSettings(await patchJson("/api/app-settings", body));
    await refresh();  // "today" may have moved
  } catch (err) {
    errEl.textContent = err.message;
  }
}

document.querySelector("#multiplierCap")?.addEventListener("change", e => {
  const v = e.target.value.trim();
  saveAppSetting({ max_multiplier_floor: v === "" ? null : Number(v) }, "#capError");
});
document.querySelector("#timezoneSelect")?.addEventListener("change", e => {
  saveAppSetting({ timezone: e.target.value || null }, "#tzError");
});
document.querySelector("#useDeviceZone")?.addEventListener("click", e => {
  saveAppSetting({ timezone: e.currentTarget.dataset.zone }, "#tzError");
});

document.querySelector("#telegramEvents")?.addEventListener("click", e => {
  const btn = e.target.closest("[data-telegram-event]");
  if (!btn) return;
  const on = btn.getAttribute("aria-pressed") !== "true";
  saveAppSetting({ telegram_events: { [btn.dataset.telegramEvent]: on } }, "#telegramError");
});
document.querySelector("#telegramTest")?.addEventListener("click", async e => {
  const btn = e.currentTarget;
  const statusEl = document.querySelector("#telegramTestStatus");
  const errEl = document.querySelector("#telegramError");
  statusEl.textContent = errEl.textContent = "";
  btn.disabled = true;
  try {
    const r = await fetch("/api/app-settings/telegram-test", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || "Couldn't send the test message.");
    statusEl.textContent = "Sent - check Telegram.";
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = !lastAppSettings?.telegram?.configured;
  }
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
    navigator.clipboard.writeText(withRealSecret(payloadEl.textContent)).then(() => {
      payloadBtn.textContent = "Copied!";
      setTimeout(() => { payloadBtn.textContent = "Copy"; }, 2000);
    });
  });

  const priceAlertPayloadEl  = document.querySelector("#priceAlertPayloadDisplay");
  const priceAlertPayloadBtn = document.querySelector("#copyPriceAlertPayload");
  if (priceAlertPayloadBtn && priceAlertPayloadEl) priceAlertPayloadBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(withRealSecret(priceAlertPayloadEl.textContent)).then(() => {
      priceAlertPayloadBtn.textContent = "Copied!";
      setTimeout(() => { priceAlertPayloadBtn.textContent = "Copy"; }, 2000);
    });
  });

  const exitUrl = window.location.origin + "/webhook/exit";
  const exitUrlEl = document.querySelector("#exitWebhookUrl");
  const exitBtnEl = document.querySelector("#copyExitWebhook");
  if (exitUrlEl) exitUrlEl.textContent = exitUrl;
  if (exitBtnEl) exitBtnEl.addEventListener("click", () => {
    navigator.clipboard.writeText(exitUrl).then(() => {
      exitBtnEl.textContent = "Copied!";
      setTimeout(() => { exitBtnEl.textContent = "Copy"; }, 2000);
    });
  });

  const exitPayloadEl  = document.querySelector("#exitPayloadDisplay");
  const exitPayloadBtn = document.querySelector("#copyExitPayload");
  if (exitPayloadBtn && exitPayloadEl) exitPayloadBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(withRealSecret(exitPayloadEl.textContent)).then(() => {
      exitPayloadBtn.textContent = "Copied!";
      setTimeout(() => { exitPayloadBtn.textContent = "Copy"; }, 2000);
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

// Same shape as the Overview's price alert entry payload. Built by hand so
// {{close}} stays unquoted - TradingView substitutes a bare number.
function priceAlertTemplate(name, direction) {
  return `{"secret":${JSON.stringify(_webhookSecret)},"strategy":${JSON.stringify(name)},"symbol":"{{ticker}}","action":"entry","direction":"${direction}","price":{{close}}}`;
}

function loadBotTemplate(name) {
  const el = document.querySelector("#botTemplateDisplay");
  if (el) el.textContent = botTemplate(name);
}

async function loadBotPairs(botId, botName) {
  const tbody = document.querySelector(`#pairs-${botId}`);
  if (!tbody) return;

  const [pairs, uplData] = await Promise.all([
    getJson(`/api/signal-bots/${botId}/pairs`).catch(() => []),
    getJson("/api/unrealized-pnl").catch(() => ({})),
  ]);

  const openPositions = (latestState.positions || []).filter(p => p.strategy_name === botName);
  const openSymbols = new Set(openPositions.map(p => p.symbol));
  const bot = botsById[botId] || {};
  const hasOverrides = p => p.tp_pct != bot.default_pair_tp_pct
    || p.sl_pct != bot.default_pair_sl_pct
    || p.max_cycles != bot.default_pair_max_cycles;
  const needsAttention = p => openSymbols.has(p.symbol) || !p.enabled || hasOverrides(p);
  const allSymbols = [...new Set([
    ...openPositions.map(p => p.symbol),
    ...pairs.filter(needsAttention).map(p => p.symbol),
  ])].sort();

  const tdS = "padding:10px 16px;border-bottom:1px solid var(--border)";

  if (!allSymbols.length) {
    const msg = pairs.length
      ? "No pairs need attention — open positions, paused pairs, or pairs with custom TP/SL/Cycles will appear here. See Open Positions and Trade History for full activity."
      : "No pairs traded yet — pairs appear here automatically when signals arrive.";
    tbody.innerHTML = `<tr><td colspan="5" style="${tdS}color:var(--muted);font-size:12px">${msg}</td></tr>`;
    return;
  }

  tbody.innerHTML = allSymbols.map(sym => {
    const pos  = openPositions.find(p => p.symbol === sym);
    const pair = pairs.find(p => p.symbol === sym);
    const upl  = uplData[`${sym}_${pos?.direction}`] ?? uplData[sym] ?? null;
    const uplCls = upl === null ? "neutral" : upl > 0 ? "positive" : upl < 0 ? "negative" : "neutral";
    const pnlCls = (pair?.session_pnl || 0) > 0 ? "positive" : (pair?.session_pnl || 0) < 0 ? "negative" : "neutral";
    const cycPct = pair?.max_cycles ? Math.min(pair.cycles_completed / pair.max_cycles * 100, 100).toFixed(0) : 0;
    const cycBar = pair?.max_cycles ? `<div style="height:3px;background:var(--border);border-radius:999px;margin-top:4px"><div style="height:100%;width:${cycPct}%;background:var(--blue);border-radius:999px"></div></div>` : "";

    return `
    <tr>
      <td style="${tdS}">
        <div style="display:flex;align-items:center;gap:7px">
          <span style="font-family:monospace;font-weight:600">${sym}</span>
          ${pos ? `<span class="badge ${pos.direction === "long" ? "ok" : "warn"}" style="font-size:9px">${pos.direction}</span>` : ""}
        </div>
        ${pos ? `<div style="font-size:11px;color:var(--muted);margin-top:3px">Entry ${number.format(pos.entry_price)} · ${number.format(pos.size)} contracts</div>` : ""}
      </td>
      <td style="${tdS}">
        <div style="display:flex;align-items:center;gap:4px">
          <input class="inline-input pair-input" type="number" step="0.1" min="0" value="${pair?.tp_pct ?? ""}" placeholder="TP"
            data-bot-id="${botId}" data-symbol="${sym}" data-field="tp_pct" style="width:52px" />
          <span style="color:var(--muted);font-size:10px">/</span>
          <input class="inline-input pair-input" type="number" step="0.1" min="0" value="${pair?.sl_pct ?? ""}" placeholder="SL"
            data-bot-id="${botId}" data-symbol="${sym}" data-field="sl_pct" style="width:52px" />
        </div>
      </td>
      <td style="${tdS}">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <input class="inline-input pair-input" type="number" step="1" min="1" value="${pair?.max_cycles ?? ""}" placeholder="∞"
            data-bot-id="${botId}" data-symbol="${sym}" data-field="max_cycles" style="width:52px" />
          <span style="font-size:11px;color:var(--muted)">${pair?.cycles_completed ?? 0}${pair?.max_cycles ? "/" + pair.max_cycles : ""}</span>
        </div>
        ${cycBar}
      </td>
      <td style="${tdS}">
        ${pos && upl !== null
          ? `<div class="${uplCls}" style="font-family:monospace;font-weight:700">${upl >= 0 ? "+" : ""}${currency.format(upl)}</div>
             <div style="font-size:10px;color:var(--muted)">unrealized</div>`
          : `<div class="${pnlCls}" style="font-family:monospace;font-weight:700">${currency.format(pair?.session_pnl || 0)}</div>
             <div style="font-size:10px;color:var(--muted)">session</div>`}
      </td>
      <td style="${tdS}">
        ${pos
          ? `<span class="badge ok" style="font-size:10px">In position</span>`
          : `<button class="mini-switch ${pair?.enabled ? "on" : ""}" data-pair-bot="${botId}" data-pair-sym="${sym}" data-pair-enabled="${pair?.enabled}">
               ${pair?.enabled ? "Pause" : "Resume"}
             </button>`}
      </td>
    </tr>`;
  }).join("");

  // Wire pair inputs
  tbody.querySelectorAll(".pair-input").forEach(inp => {
    inp.addEventListener("change", async () => {
      const nullables = ["tp_pct", "sl_pct", "max_cycles"];
      const val = inp.value === "" && nullables.includes(inp.dataset.field) ? null : Number(inp.value);
      try {
        await patchJson(`/api/signal-bots/${inp.dataset.botId}/pairs/${inp.dataset.symbol}`, { [inp.dataset.field]: val });
      } catch (err) {
        showNotice(`Couldn't save ${inp.dataset.symbol} setting`, err.message);
      }
      await loadBotPairs(inp.dataset.botId, botName);
    });
  });

  // Wire pair status toggles
  tbody.querySelectorAll("[data-pair-bot]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const current = btn.dataset.pairEnabled === "true";
      await patchJson(`/api/signal-bots/${btn.dataset.pairBot}/pairs/${btn.dataset.pairSym}`, { enabled: !current });
      await loadBotPairs(btn.dataset.pairBot, botName);
    });
  });
}

async function refreshBots() {
  const [allBots, uplData] = await Promise.all([
    getJson("/api/signal-bots").catch(() => []),
    getJson("/api/unrealized-pnl").catch(() => ({})),
  ]);
  botsById = Object.fromEntries(allBots.map(b => [b.id, b]));
  renderCfdBots(allBots.filter(isCfd));
  const bots = allBots.filter(b => !isCfd(b));
  const el = document.querySelector("#signalBots");
  if (!el) return;

  if (!bots.length) {
    el.innerHTML = emptyRow(6, "No signal bots yet — click + New Bot to create one.");
    return;
  }

  const thStyle = "padding:10px 16px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--panel)";
  const tdStyle = "padding:12px 16px;border-bottom:1px solid var(--border)";

  // Aggregate unrealized P&L — per symbol for single-pair, across all positions for multi-pair
  const botUpl = b => {
    if (b.symbol) {
      const syms = b.symbol.split(",").map(s => s.trim().toUpperCase());
      return syms.reduce((sum, sym) => sum + (uplData[`${sym}_long`] ?? uplData[`${sym}_short`] ?? uplData[sym] ?? 0), 0);
    }
    return (latestState.positions || [])
      .filter(p => p.strategy_name === b.name)
      .reduce((sum, p) => sum + (uplData[`${p.symbol}_${p.direction}`] ?? uplData[p.symbol] ?? 0), 0);
  };
  // Highest Session P&L (closed + unrealized) first.
  const rows = bots
    .map(b => { const upl = botUpl(b); return { b, upl, total: b.session_pnl + upl }; })
    .sort((x, y) => y.total - x.total);

  el.innerHTML = rows.map(({ b, upl, total }) => {
    const pnlCls = total > 0 ? "positive" : total < 0 ? "negative" : "neutral";
    const pnlPct = b.size > 0 ? ((total / b.size) * 100).toFixed(1) : "0.0";
    const openPairs = (latestState.positions || []).filter(p => p.strategy_name === b.name);

    // Name any manual exits in place alongside the bot's % TP/SL (no values).
    const exitLabels = [
      (b.trail_distance_pct > 0 || openPairs.some(p => p.trail_distance != null)) && "Trailing",
      openPairs.some(p => p.close_at_price != null) && "Close at price",
      openPairs.some(p => p.manual_tpsl) && "Manual TP/SL",
    ].filter(Boolean);
    const pctTpsl = b.default_pair_tp_pct || b.default_pair_sl_pct
      ? `${b.default_pair_tp_pct ? "+" + b.default_pair_tp_pct + "%" : "—"} / ${b.default_pair_sl_pct ? "-" + b.default_pair_sl_pct + "%" : "—"}`
      : "";
    const labelHtml = exitLabels.map(l => `<div style="white-space:nowrap">${l}</div>`).join("");
    const tpsl = pctTpsl || exitLabels.length ? `${pctTpsl}${labelHtml}` : "— / —";
    const pairBadges = openPairs.map(p => {
      const pUpl = uplData[`${p.symbol}_${p.direction}`] ?? uplData[p.symbol] ?? null;
      const uplStr = pUpl !== null ? ` ${pUpl >= 0 ? "+" : ""}${currency.format(pUpl)}` : "";
      const uplCls = pUpl > 0 ? "ok" : pUpl < 0 ? "warn" : "";
      return `<span class="badge ${uplCls}" style="font-size:9px;margin-right:3px">${p.symbol}${uplStr}</span>`;
    }).join("");

    return `
    <tr data-expand="${b.id}" data-bot-name="${escapeAttr(b.name)}" style="cursor:pointer">
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="chev-${b.id}" style="font-size:9px;color:var(--muted);transition:transform 150ms;display:inline-block">▶</span>
          <span class="bot-dot ${b.enabled ? "active" : "paused"}"></span>
          <span style="font-weight:600">${b.name}</span>
        </div>
        ${pairBadges ? `<div style="margin-top:5px;padding-left:22px">${pairBadges}</div>` : ""}
      </td>
      <td style="font-family:monospace;font-size:12px">${tpsl}</td>
      <td>
        <div class="${pnlCls}" style="font-family:monospace;font-weight:700">${total >= 0 ? "+" : ""}${currency.format(total)}</div>
        <div style="font-size:10px;color:var(--muted)">${pnlPct}%${upl !== 0 ? ` · <span style="font-style:italic">${upl >= 0 ? "+" : ""}${currency.format(upl)} unrlzd</span>` : ""}</div>
      </td>
      <td>
        <div style="font-size:12px">${b.cycles_completed} total${b.default_pair_max_cycles ? ` <span style="color:var(--muted)">· ${b.default_pair_max_cycles}/coin cap</span>` : ""}</div>
      </td>
      <td>
        <button class="mini-switch ${b.hedge_mode ? "on" : ""}" data-bot-id="${b.id}" data-hedge="${b.hedge_mode}">
          ${b.hedge_mode ? "On" : "Off"}
        </button>
      </td>
      <td>
        <button class="mini-switch ${b.enabled ? "on" : ""}" data-bot-id="${b.id}" data-enabled="${b.enabled}">
          ${b.enabled ? "Pause" : "Resume"}
        </button>
      </td>
      <td>
        <button class="mini-switch" data-delete-bot="${b.id}" data-bot-name="${escapeAttr(b.name)}" style="background:var(--red-dim);color:var(--red)">Delete</button>
      </td>
    </tr>
    <tr id="detail-${b.id}" style="display:none">
      <td colspan="7" style="padding:0;border-bottom:2px solid var(--border)">
        <div style="background:var(--panel-2);padding:18px 20px">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:18px">
            <div style="display:flex;align-items:center;gap:10px">
              <span style="font-weight:700;font-size:15px">${b.name}</span>
              <button class="nav-link-btn" data-toggle-tpl="${b.id}" style="font-size:10px;padding:2px 8px">TradingView Alert ▾</button>
              <button class="nav-link-btn" data-toggle-price-alert="${b.id}" style="font-size:10px;padding:2px 8px">Price Alert Entry ▾</button>
            </div>
            <div style="display:flex;align-items:center;gap:20px">
              <div style="display:flex;align-items:center;gap:8px">
                <span class="bot-field-label" style="margin-bottom:0">Hedge</span>
                <button class="mini-switch ${b.hedge_mode ? "on" : ""}" data-bot-id="${b.id}" data-hedge="${b.hedge_mode}">
                  ${b.hedge_mode ? "On" : "Off"}
                </button>
              </div>
              <div style="display:flex;align-items:center;gap:8px">
                <span class="bot-field-label" style="margin-bottom:0">Status</span>
                <span class="badge ${b.enabled ? "ok" : "bad"}" style="font-size:10px">${b.enabled ? "Active" : "Paused"}</span>
                <button class="mini-switch ${b.enabled ? "on" : ""}" data-bot-id="${b.id}" data-enabled="${b.enabled}">
                  ${b.enabled ? "Pause" : "Resume"}
                </button>
              </div>
            </div>
          </div>
          <div id="bot-tpl-${b.id}" style="display:none;margin-bottom:18px">
            <div class="url-row">
              <code class="url-display dim" style="font-size:10px;white-space:pre;overflow-x:auto;line-height:1.6">${escapeAttr(botTemplate(b.name))}</code>
              <button class="copy-btn" data-copy-tpl="${escapeAttr(b.name)}" style="align-self:stretch;font-size:11px">Copy</button>
            </div>
          </div>
          <div id="bot-price-alert-${b.id}" style="display:none;margin-bottom:18px">
            <span class="webhook-label" style="display:block;margin-bottom:8px">Plain price / trendline alerts on the TradingView webhook URL</span>
            ${["long", "short"].map(dir => `
            <div class="url-row" style="margin-bottom:6px">
              <span class="bot-field-label" style="margin-bottom:0;min-width:42px;align-self:center">${dir === "long" ? "Long" : "Short"}</span>
              <code class="url-display dim" style="font-size:10px;white-space:pre;overflow-x:auto">${escapeAttr(priceAlertTemplate(b.name, dir))}</code>
              <button class="copy-btn" data-copy-price-alert="${escapeAttr(b.name)}" data-direction="${dir}" style="align-self:stretch;font-size:11px">Copy</button>
            </div>`).join("")}
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:18px 28px">
            <div>
              <label class="bot-field-label">Size (USDT)</label>
              <input class="inline-input" type="number" step="1" min="1" value="${b.size}" data-bot-id="${b.id}" data-field="size" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">Leverage (blank = auto)</label>
              <input class="inline-input" type="number" step="1" min="0" value="${b.leverage > 0 ? b.leverage : ""}" placeholder="Auto" data-bot-id="${b.id}" data-field="leverage" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">TP % of stake (per trade)</label>
              <input class="inline-input" type="number" step="0.1" min="0" value="${b.default_pair_tp_pct ?? ""}" placeholder="—" data-bot-id="${b.id}" data-field="default_pair_tp_pct" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">SL % of stake (per trade)</label>
              <input class="inline-input" type="number" step="0.1" min="0" value="${b.default_pair_sl_pct ?? ""}" placeholder="—" data-bot-id="${b.id}" data-field="default_pair_sl_pct" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">Trail start % of stake</label>
              <input class="inline-input" type="number" step="0.1" min="0" value="${b.trail_start_pct ?? ""}" placeholder="= distance" data-bot-id="${b.id}" data-field="trail_start_pct" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">Trail distance % of stake</label>
              <input class="inline-input" type="number" step="0.1" min="0" value="${b.trail_distance_pct ?? ""}" placeholder="off" data-bot-id="${b.id}" data-field="trail_distance_pct" style="width:100%" />
            </div>
            <div>
              <label class="bot-field-label">Max Cycles (per coin)</label>
              <input class="inline-input" type="number" step="1" min="1" value="${b.default_pair_max_cycles ?? ""}" placeholder="∞" data-bot-id="${b.id}" data-field="default_pair_max_cycles" style="width:100%" />
              <div style="font-size:11px;color:var(--muted);margin-top:5px">${b.cycles_completed} total completed</div>
            </div>
            <div>
              <label class="bot-field-label">Session P&amp;L</label>
              <div class="${pnlCls}" style="font-family:monospace;font-weight:700;font-size:15px">${total >= 0 ? "+" : ""}${currency.format(total)}</div>
              <div style="font-size:11px;color:var(--muted);margin-top:2px">${pnlPct}%</div>
              <button class="mini-switch" data-reset-session="${b.id}" data-bot-name="${escapeAttr(b.name)}" style="margin-top:8px;background:transparent;border-color:var(--border);color:var(--text)">Reset session</button>
            </div>
          </div>
        </div>
        <div style="padding:0 0 0 0;border-top:1px solid var(--border)">
          <div style="padding:10px 16px 6px;font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)">Pairs</div>
          <table style="width:100%;border-collapse:collapse;background:var(--bg)">
            <thead><tr>
              <th style="${thStyle};background:var(--bg)">Symbol</th>
              <th style="${thStyle};background:var(--bg)">TP / SL</th>
              <th style="${thStyle};background:var(--bg)">Cycles</th>
              <th style="${thStyle};background:var(--bg)">Session P&amp;L</th>
              <th style="${thStyle};background:var(--bg)">Status</th>
            </tr></thead>
            <tbody id="pairs-${b.id}">
              <tr><td colspan="5" style="${tdStyle};color:var(--muted);font-size:12px">No pairs traded yet — pairs appear here automatically when signals arrive.</td></tr>
            </tbody>
          </table>
        </div>
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
      if (!open) loadBotPairs(id, row.dataset.botName);
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
      navigator.clipboard.writeText(withRealSecret(botTemplate(btn.dataset.copyTpl))).then(() => {
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = "Copy"; }, 2000);
      });
    });
  });

  el.querySelectorAll("[data-toggle-price-alert]").forEach(btn => {
    btn.addEventListener("click", () => {
      const panel = document.querySelector(`#bot-price-alert-${btn.dataset.togglePriceAlert}`);
      if (!panel) return;
      const open = panel.style.display !== "none";
      panel.style.display = open ? "none" : "block";
      btn.textContent = open ? "Price Alert Entry ▾" : "Price Alert Entry ▲";
    });
  });

  el.querySelectorAll("[data-copy-price-alert]").forEach(btn => {
    btn.addEventListener("click", () => {
      const text = priceAlertTemplate(btn.dataset.copyPriceAlert, btn.dataset.direction);
      navigator.clipboard.writeText(withRealSecret(text)).then(() => {
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = "Copy"; }, 2000);
      });
    });
  });

  el.querySelectorAll(".inline-input").forEach(inp => {
    inp.addEventListener("change", async () => {
      const nullableFields = ["default_pair_tp_pct", "default_pair_sl_pct", "default_pair_max_cycles", "trail_start_pct", "trail_distance_pct"];
      const val = inp.value === "" && nullableFields.includes(inp.dataset.field)
        ? null : Number(inp.value);
      try {
        await patchJson(`/api/signal-bots/${inp.dataset.botId}`, { [inp.dataset.field]: val });
      } catch (err) {
        showNotice("Couldn't save bot setting", err.message);
      }
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

  el.querySelectorAll("[data-reset-session]").forEach(btn => {
    btn.addEventListener("click", async e => {
      e.stopPropagation();
      await openDialog({
        title: `Reset ${btn.dataset.botName} session?`,
        message: "Sets session P&L and cycle counts back to zero for the bot and every one of its coins. Trade history is kept.",
        confirmLabel: "Reset session",
        tone: "danger",
        onSubmit: async () => {
          const r = await fetch(`/api/signal-bots/${btn.dataset.resetSession}/reset-session`, { method: "POST" });
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Couldn't reset the session (server error ${r.status}).`);
          await refreshBots();
        },
      });
    });
  });

  el.querySelectorAll("[data-delete-bot]").forEach(btn => {
    btn.addEventListener("click", async () => {
      await openDialog({
        title: `Delete ${btn.dataset.botName || "this bot"}?`,
        message: "Removes the bot and its per-coin settings. New entry signals for it will be rejected. Its trade history is kept.",
        confirmLabel: "Delete bot",
        tone: "danger",
        onSubmit: async () => {
          const r = await fetch(`/api/signal-bots/${btn.dataset.deleteBot}`, { method: "DELETE" });
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Couldn't delete the bot (server error ${r.status}).`);
          await refreshBots();
        },
      });
    });
  });
}

function wireBotForm() {
  if (_botFormWired) return;
  _botFormWired = true;


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
    const leverage = parseFloat(document.querySelector("#botLeverage")?.value || "0");
    const errEl    = document.querySelector("#botError");

    if (!name || size <= 0) {
      if (errEl) { errEl.textContent = "Strategy name and size are required."; errEl.style.display = "block"; }
      return;
    }
    if (errEl) errEl.style.display = "none";

    const hedge_mode    = document.querySelector("#botHedge")?.checked || false;
    const tp_raw        = parseFloat(document.querySelector("#botTp")?.value || "");
    const sl_raw        = parseFloat(document.querySelector("#botSl")?.value || "");
    const cycles_raw    = parseInt(document.querySelector("#botCycles")?.value || "");
    const trail_start_raw    = parseFloat(document.querySelector("#botTrailStart")?.value || "");
    const trail_distance_raw = parseFloat(document.querySelector("#botTrailDistance")?.value || "");
    const body = { name, size, leverage, hedge_mode, account: "options" };
    if (symbol) body.symbol = symbol.toUpperCase();
    if (!isNaN(tp_raw) && tp_raw > 0)          body.default_pair_tp_pct     = tp_raw;
    if (!isNaN(sl_raw) && sl_raw > 0)          body.default_pair_sl_pct     = sl_raw;
    if (!isNaN(cycles_raw) && cycles_raw > 0)  body.default_pair_max_cycles = cycles_raw;
    if (!isNaN(trail_start_raw) && trail_start_raw > 0)       body.trail_start_pct    = trail_start_raw;
    if (!isNaN(trail_distance_raw) && trail_distance_raw > 0) body.trail_distance_pct = trail_distance_raw;
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
    ["#botName","#botSymbol","#botSize","#botLeverage","#botTp","#botSl","#botCycles","#botTrailStart","#botTrailDistance"].forEach(sel => {
      const el = document.querySelector(sel); if (el) el.value = "";
    });
    const hedge = document.querySelector("#botHedge"); if (hedge) hedge.checked = false;
  });
}

// ─── CFD bots (Synthetic → CFDs, traded on cTrader) ─────────────────────────

function renderCfdBots(bots) {
  const active = bots.filter(b => b.enabled).length;
  document.querySelector("#cfdStatBots").textContent = bots.length ? `${active} of ${bots.length}` : "0";
  document.querySelector("#cfdBots").innerHTML = bots.length
    ? bots.map(b => `<tr>
      <td><span class="bot-dot ${b.enabled ? "active" : "paused"}"></span> <strong>${escapeAttr(b.name)}</strong></td>
      <td>${number.format(b.lots ?? 0)}</td>
      <td>${b.default_pair_max_cycles ?? '<span class="subtle">—</span>'}</td>
      <td>${b.cycles_completed}</td>
      <td>${statusBadge(b.enabled ? "enabled" : "paused")}</td>
      <td class="row-actions">
        <button type="button" class="mini-switch neutral" data-cfd-action="alert" data-bot-id="${b.id}">Copy alert</button>
        <button type="button" class="mini-switch neutral" data-cfd-action="edit" data-bot-id="${b.id}">Edit</button>
        <button type="button" class="mini-switch neutral" data-cfd-action="toggle" data-bot-id="${b.id}">${b.enabled ? "Pause" : "Resume"}</button>
        <button type="button" class="mini-switch" data-cfd-action="delete" data-bot-id="${b.id}">Delete</button>
      </td>
    </tr>`).join("")
    : emptyRow(6, "No CFD bots yet. Click + New Bot to create one.");
}

document.querySelector("#cfdBotNew").addEventListener("click", () => {
  const form = document.querySelector("#cfdBotForm");
  form.hidden = !form.hidden;
  if (!form.hidden) form.elements.name.focus();
});

document.querySelector("#cfdBotForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  const error = document.querySelector("#cfdBotError");
  const cycles = parseInt(form.elements.max_cycles.value, 10);
  error.textContent = "";
  try {
    // A CFD bot is sized by lots; size (the Multipliers stake) is unused.
    await sendJson("POST", "/api/signal-bots", {
      name: form.elements.name.value.trim(), account: "ctrader", lots: Number(form.elements.lots.value), size: 1,
      ...(cycles > 0 ? { default_pair_max_cycles: cycles } : {}),
    });
  } catch (e) {
    error.textContent = e.message;
    return;
  }
  form.reset();
  form.hidden = true;
  refreshBots();
});

document.querySelector("#cfdBots").addEventListener("click", async event => {
  const button = event.target.closest("[data-cfd-action]");
  if (!button) return;
  const bot = botsById[button.dataset.botId];
  if (!bot) return;
  const url = `/api/signal-bots/${bot.id}`;
  const action = button.dataset.cfdAction;
  if (action === "alert") {
    await navigator.clipboard.writeText(withRealSecret(botTemplate(bot.name)));
    button.textContent = "Copied!";
    setTimeout(() => { button.textContent = "Copy alert"; }, 2000);
  } else if (action === "toggle") {
    try {
      await patchJson(url, { enabled: !bot.enabled });
    } catch (e) {
      showNotice("CFD bot", e.message);
    }
    refreshBots();
  } else if (action === "edit") {
    const saved = await openDialog({
      title: `Edit ${bot.name}`,
      fields: [
        { name: "name", label: "Strategy name", value: bot.name },
        [{ name: "lots", label: "Lots", value: bot.lots },
         { name: "max_cycles", label: "Max Cycles (per symbol)", value: bot.default_pair_max_cycles ?? "", placeholder: "None" }],
      ],
      confirmLabel: "Save",
      onSubmit: values => sendJson("PATCH", url, {
        name: values.name, lots: Number(values.lots),
        default_pair_max_cycles: Number(values.max_cycles) > 0 ? Number(values.max_cycles) : null,
      }),
    });
    if (saved) refreshBots();
  } else if (action === "delete") {
    const deleted = await openDialog({
      title: "Delete CFD bot",
      message: `Delete ${bot.name}? Its entries will be refused until a bot with this name exists again.`,
      confirmLabel: "Delete", tone: "danger",
      onSubmit: () => sendJson("DELETE", url),
    });
    if (deleted) refreshBots();
  }
});

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

getJson("/api/me").then(me => {
  if (!me.email) return;
  document.querySelector("#signedInEmail").textContent = me.email;
  document.querySelector("#signedIn").style.display = "";
}).catch(() => {});

// ─── cTrader connections (Synthetic → CFDs and Forex Overviews) ───────────────────────

function escapeText(v) {
  return String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// Each market has its own panel and login: Synthetic → CFDs (Deriv accounts
// only, element ids "ctrader…") and Forex (other brokers, "fxCtrader…").
const CTRADER_PANELS = [
  { market: "synthetic", prefix: "ctrader" },
  { market: "forex", prefix: "fxCtrader" },
];
const ctraderEl = (panel, name) => document.querySelector(`#${panel.prefix}${name}`);

function ctraderMessage(panel, text, isError) {
  const el = ctraderEl(panel, "Message");
  if (!el) return;
  el.textContent = text || "";
  el.style.display = text ? "" : "none";
  el.style.color = isError ? "var(--red)" : "var(--green)";
}

function ctraderAccountLabel(a) {
  return `${a.broker || "cTrader"} · ${a.login || a.account_id}`;
}

async function refreshCTrader(panel) {
  const statusEl = ctraderEl(panel, "Status");
  const actions = ctraderEl(panel, "Actions");
  const picks = ctraderEl(panel, "Accounts");
  if (!statusEl || !actions || !picks) return;
  const q = `market=${panel.market}`;
  let st;
  try {
    st = await getJson(`/api/ctrader/status?${q}`);
  } catch {
    statusEl.textContent = "Couldn't load the cTrader status.";
    return;
  }
  if (!st.configured) {
    statusEl.textContent = `Not set up - add ${st.missing.join(" and ")} to the server's settings (from openapi.ctrader.com).`;
    actions.innerHTML = "";
    picks.style.display = "none";
    return;
  }
  if (!st.connected) {
    statusEl.textContent = panel.market === "synthetic"
      ? "Not connected - log in with the cTrader ID that holds your Deriv cTrader account."
      : "Not connected - log in with your forex broker's cTrader ID.";
    actions.innerHTML = `<a class="copy-btn" href="/ctrader/connect?${q}" style="text-decoration:none">Connect cTrader</a>`;
    picks.style.display = "none";
    return;
  }

  const fill = (name, live, current) => {
    const el = ctraderEl(panel, name);
    const options = st.accounts.filter(a => a.live === live);
    el.innerHTML = `<option value="">${options.length ? "Choose an account" : "None linked"}</option>` +
      options.map(a => `<option value="${a.account_id}" ${a.account_id === current ? "selected" : ""}>${escapeText(ctraderAccountLabel(a))}</option>`).join("");
  };
  fill("DemoAccount", false, st.demo_account_id);
  fill("LiveAccount", true, st.live_account_id);
  picks.style.display = "grid";
  actions.innerHTML = `
    <button class="nav-link-btn" data-ctrader="refresh">Refresh accounts</button>
    <button class="mini-switch" data-ctrader="disconnect" style="background:var(--red-dim);color:var(--red)">Disconnect</button>`;

  if (!st.accounts.length) {
    statusEl.textContent = panel.market === "synthetic"
      ? "Connected, but this cTrader ID has no Deriv accounts. Disconnect and log in with the one that holds your Deriv cTrader account."
      : "Connected, but this cTrader ID only has Deriv accounts (those trade under Synthetic → CFDs). Disconnect and log in with your forex broker's cTrader ID.";
    return;
  }
  const mode = st.mode === "live" ? "live" : "demo";
  const picked = mode === "live" ? st.live_account_id : st.demo_account_id;
  if (!picked) {
    statusEl.textContent = `Connected · pick a ${mode} account below.`;
    return;
  }
  try {
    const info = await getJson(`/api/ctrader/account?mode=${mode}&${q}`);
    const kind = info.hedging ? "hedging" : "netting";
    statusEl.textContent = `Connected · ${mode.toUpperCase()} ${info.broker || ""} ${info.login || ""} (${kind}) · balance ${currency.format(info.balance)}`;
  } catch {
    statusEl.textContent = `Connected · couldn't read the ${mode} account balance from cTrader right now.`;
  }
}

function wireCTraderPanel(panel, flash) {
  const q = `market=${panel.market}`;
  if (flash) ctraderMessage(panel, flash.text, flash.isError);
  const save = async (field, value) => {
    try {
      await patchJson(`/api/ctrader/accounts?${q}`, { [field]: value ? Number(value) : null });
      ctraderMessage(panel, "Saved.", false);
    } catch (err) {
      ctraderMessage(panel, err.message, true);
    }
    refreshCTrader(panel);
  };
  ctraderEl(panel, "DemoAccount")?.addEventListener("change", e => save("demo_account_id", e.target.value));
  ctraderEl(panel, "LiveAccount")?.addEventListener("change", e => save("live_account_id", e.target.value));
  ctraderEl(panel, "Actions")?.addEventListener("click", async e => {
    const action = e.target.dataset?.ctrader;
    if (action === "refresh") {
      const r = await fetch(`/api/ctrader/refresh-accounts?${q}`, { method: "POST" });
      const data = await r.json().catch(() => ({}));
      ctraderMessage(panel, r.ok ? "Accounts refreshed." : (data.detail || "Couldn't refresh the accounts."), !r.ok);
      refreshCTrader(panel);
    } else if (action === "disconnect") {
      const who = panel.market === "synthetic" ? "CFD bots" : "Forex bots";
      if (!confirm(`Disconnect this cTrader login? ${who} can't trade until you connect again.`)) return;
      await fetch(`/api/ctrader/disconnect?${q}`, { method: "POST" });
      ctraderMessage(panel, "Disconnected.", false);
      refreshCTrader(panel);
    }
  });
  refreshCTrader(panel);
  setInterval(() => refreshCTrader(panel), 60000);
}

function wireCTrader() {
  const params = new URLSearchParams(location.search);
  let flash = null;
  if (params.has("ctrader")) {
    const ok = params.get("ctrader") === "connected";
    flash = { market: params.get("market") || "synthetic", text: ok ? "cTrader connected." : (params.get("detail") || "The cTrader login failed."), isError: !ok };
    history.replaceState(null, "", location.pathname + location.hash);
  }
  for (const panel of CTRADER_PANELS) wireCTraderPanel(panel, flash && flash.market === panel.market ? flash : null);
}

wireCTrader();
loadWebhookSecret();
refresh();
setInterval(refresh, 10000);

// A tab left open across a deploy would otherwise keep running the old code
// (refresh() only re-fetches data). Reload onto the new version once nothing
// is in progress - no dialog open, no field being edited.
const LOADED_VERSION = new URL(document.currentScript?.src || location.href).searchParams.get("v");
if (LOADED_VERSION) {
  setInterval(async () => {
    try {
      const { version } = await getJson("/api/version");
      const busy = document.querySelector(".modal-overlay:not([style*='display:none']):not([style*='display: none'])")
        || document.activeElement?.matches("input, select, textarea");
      if (String(version) !== LOADED_VERSION && !busy) location.reload();
    } catch { /* offline or mid-deploy - try again next tick */ }
  }, 60000);
}
