const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, char => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]
));
const privateMarkup = (value) => `<span class="ip-value">${escapeHtml(value)}</span>`;
const privateAttr = (value) => hideIps ? "" : escapeHtml(value);

let state = null;
let managedMiners = [];
let managedPools = [];
let settings = null;
let activeGroup = "All";
let activeOddsCoin = "all";
let poolTestCooldownUntil = 0;
let poolTestCooldownTimer = null;
let alertSnoozeUntil = 0;
let pointerStart = null;
let suppressNextClick = false;
let hideIps = localStorage.getItem("pocisys-hide-ips") === "true";
const difficultyRain = {
  canvas: null,
  ctx: null,
  width: 0,
  height: 0,
  dpr: 1,
  columns: [],
  raf: null,
  lastFrame: 0,
  values: [1000, 10000, 1000000],
  activeReach: 0.32,
  progress: 0,
  bestDifficulty: 1000,
  blockDifficulty: 1e14,
};

const DEFAULT_SETTINGS = {
  poll_interval_seconds: 10,
  request_timeout_seconds: 4,
  alert_cooldown_seconds: 600,
  offline_alert_grace_seconds: 60,
  dashboard_port: 8765,
  dashboard_density: "comfortable",
  difficulty_rain_enabled: true,
  dashboard_base_url: "",
  lan_access_enabled: false,
  discord_enabled: false,
  webhook_configured: false,
  hermes_enabled: false,
  hermes_token_configured: false,
  hermes_token_hint: "",
  hermes_mcp_path: "/mcp",
  send_offline_alerts: true,
  send_recovery_alerts: true,
  send_hashrate_alerts: true,
  send_temperature_alerts: true,
  send_chip_health_alerts: true,
  send_best_diff_alerts: true,
  send_block_found_alerts: true,
  send_pool_alerts: true,
  send_pool_switch_alerts: true,
  send_share_alerts: true,
  verbose_pool_events: false,
  btc_enabled: true,
  bch_enabled: true,
  bsv_enabled: true,
  xec_enabled: true,
  dgb_enabled: true,
  chta_enabled: true,
  auto_network_data: true,
  manual_btc_network_hashrate_eh: null,
  manual_bch_network_hashrate_eh: null,
  manual_bsv_network_hashrate_eh: null,
  manual_xec_network_hashrate_eh: null,
  manual_dgb_network_hashrate_eh: null,
  manual_chta_network_hashrate_eh: null,
};

const TYPE_LABELS = {
  axeos: "AxeOS",
  bitaxe: "Bitaxe",
  luxos: "LuxOS",
  nerdaxe: "NerdAxe",
  nerdqaxe: "NerdQaxe",
  canaan_avalon: "Avalon beta",
  avalon: "Avalon beta",
  cgminer: "cgminer beta",
};

function minerTypeLabel(type) {
  return TYPE_LABELS[String(type || "").toLowerCase()] || String(type || "unknown");
}

function setIpPrivacy(enabled = hideIps) {
  hideIps = Boolean(enabled);
  localStorage.setItem("pocisys-hide-ips", hideIps ? "true" : "false");
  document.body.classList.toggle("privacy-hide-ips", hideIps);
  const button = $("#ip-privacy-toggle");
  if (button) {
    button.textContent = hideIps ? "Show IPs" : "Hide IPs";
    button.setAttribute("aria-pressed", String(hideIps));
  }
}

function number(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, {maximumFractionDigits: decimals});
}

function nullableNumber(value) {
  return value === "" || value === null || value === undefined ? null : Number(value);
}

function compactHashrate(ths) {
  if (ths === null || ths === undefined) return ["—", "TH/s"];
  if (ths < 0.001) return [number(ths * 1e6, 1), "MH/s"];
  if (ths < 1) return [number(ths * 1000, 2), "GH/s"];
  return [number(ths, 2), "TH/s"];
}

function difficulty(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (/[KMGTP]$/i.test(String(value).trim())) return String(value);
  const parsed = Number(String(value).replaceAll(",", ""));
  if (!Number.isFinite(parsed)) return String(value);
  const units = [["P", 1e15], ["T", 1e12], ["G", 1e9], ["M", 1e6], ["K", 1e3]];
  const unit = units.find(([, threshold]) => parsed >= threshold);
  return unit ? `${number(parsed / unit[1], 2)}${unit[0]}` : number(parsed, 0);
}

function difficultyNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? value : null;
  const text = String(value).trim().replaceAll(",", "");
  const match = text.match(/^([0-9]*\.?[0-9]+)\s*([KMGTPE])?$/i);
  if (!match) return null;
  const multipliers = {K: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15, E: 1e18};
  const parsed = Number(match[1]) * (multipliers[(match[2] || "").toUpperCase()] || 1);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function collectDifficultyRainValues() {
  const values = [];
  for (const miner of state?.miners || []) {
    const diff = miner.difficulty || {};
    for (const item of [diff.best_session, diff.best_all_time]) {
      const parsed = difficultyNumber(item);
      if (parsed) values.push(parsed);
    }
  }
  for (const pool of state?.pools || []) {
    for (const worker of pool.workers || []) {
      const parsed = difficultyNumber(worker.best_difficulty);
      if (parsed) values.push(parsed);
    }
  }
  const blockDifficulty = difficultyNumber(state?.odds?.btc?.difficulty) || difficultyNumber(state?.odds?.bch?.difficulty) || 1e14;
  const best = Math.max(1000, ...values);
  const minLog = 3;
  const maxLog = Math.max(9, Math.log10(blockDifficulty));
  const progress = Math.max(0, Math.min(1, (Math.log10(best) - minLog) / (maxLog - minLog || 1)));
  difficultyRain.values = values.length ? values.slice(-24) : [1000, 10000, 1000000];
  difficultyRain.blockDifficulty = blockDifficulty;
  difficultyRain.bestDifficulty = best;
  difficultyRain.progress = progress;
  difficultyRain.activeReach = Math.max(0.34, Math.min(0.96, 0.30 + progress * 0.66));
}

function rainValueForX(xRatio) {
  const minLog = 3;
  const maxLog = Math.max(9, Math.log10(difficultyRain.blockDifficulty || 1e14));
  const blended = Math.max(0, Math.min(1, xRatio + (Math.random() - 0.5) * 0.12));
  const nearby = difficultyRain.values[Math.floor(Math.random() * difficultyRain.values.length)] || 1000;
  const nearbyLog = Math.log10(Math.max(1000, nearby));
  const targetLog = minLog + Math.pow(blended, 1.42) * (maxLog - minLog);
  const logValue = targetLog * 0.78 + nearbyLog * 0.22 + (Math.random() - 0.5) * 0.44;
  return Math.max(1000, Math.pow(10, logValue));
}

function rainText(value) {
  const rounded = Math.max(1, Math.round(value));
  return String(rounded);
}

function matrixSpacer(length = 3) {
  const chars = "01+-*/<>[]{}=:$#";
  let text = "";
  for (let i = 0; i < length; i++) text += chars[Math.floor(Math.random() * chars.length)];
  return text;
}

function rainStreamForX(xRatio) {
  const pieces = [];
  const pieceCount = 7 + Math.floor(Math.random() * 7);
  for (let i = 0; i < pieceCount; i++) {
    pieces.push(rainText(rainValueForX(Math.max(0, Math.min(1, xRatio + (Math.random() - 0.5) * 0.08)))));
    pieces.push(matrixSpacer(2 + Math.floor(Math.random() * 5)));
  }
  return pieces.join("");
}

function resetRainColumn(column, keepX = false) {
  const width = Math.max(1, difficultyRain.width);
  if (!keepX || column.x === undefined) {
    const activeWidth = width * difficultyRain.activeReach;
    const farSparkChance = 0.015 + difficultyRain.progress * 0.075;
    column.spark = Math.random() < farSparkChance && difficultyRain.bestDifficulty > 1e7;
    column.x = column.spark
      ? activeWidth + Math.random() * Math.max(1, width - activeWidth)
      : Math.pow(Math.random(), 1.85) * activeWidth;
    column.x += (Math.random() - 0.5) * 10;
  }
  const xRatio = Math.max(0, Math.min(1, column.x / width));
  const beyond = xRatio > difficultyRain.activeReach;
  column.spark = column.spark || (beyond && Math.random() < 0.015 + difficultyRain.progress * 0.05);
  column.y = Math.random() * Math.max(1, difficultyRain.height);
  column.speed = 7 + Math.random() * 15 + Math.max(0, difficultyRain.activeReach - xRatio) * 9;
  column.size = 11 + Math.random() * 4;
  column.rowGap = column.size * (1.05 + Math.random() * 0.18);
  column.stream = rainStreamForX(xRatio);
  column.streamOffset = Math.floor(Math.random() * column.stream.length);
  column.refreshAt = performance.now() + 1800 + Math.random() * 7200;
  column.alpha = beyond
    ? (column.spark ? 0.08 + difficultyRain.progress * 0.11 : 0.008)
    : 0.055 + (1 - xRatio) * 0.10 + Math.random() * 0.055;
}

function resizeDifficultyRain() {
  const canvas = difficultyRain.canvas || $("#difficulty-rain");
  if (!canvas) return false;
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  difficultyRain.canvas = canvas;
  difficultyRain.ctx = canvas.getContext("2d", {alpha: true});
  if (difficultyRain.width !== width || difficultyRain.height !== height || difficultyRain.dpr !== dpr) {
    difficultyRain.width = width;
    difficultyRain.height = height;
    difficultyRain.dpr = dpr;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    difficultyRain.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.max(42, Math.min(140, Math.floor(width / 13)));
    difficultyRain.columns = Array.from({length: count}, () => ({}));
    difficultyRain.columns.forEach(column => resetRainColumn(column, false));
  }
  return true;
}

function drawDifficultyRain(timestamp = 0) {
  difficultyRain.raf = null;
  const enabled = Boolean(state?.ui?.difficulty_rain_enabled ?? settings?.difficulty_rain_enabled ?? true);
  const active = document.body.classList.contains("screen-active") && enabled && document.visibilityState !== "hidden";
  const canvas = difficultyRain.canvas || $("#difficulty-rain");
  if (!canvas) return;
  canvas.classList.toggle("off", !enabled);
  if (!active || !resizeDifficultyRain()) {
    if (difficultyRain.ctx) difficultyRain.ctx.clearRect(0, 0, difficultyRain.width, difficultyRain.height);
    return;
  }
  if (timestamp - difficultyRain.lastFrame < 72) {
    difficultyRain.raf = requestAnimationFrame(drawDifficultyRain);
    return;
  }
  const dt = Math.min(0.12, Math.max(0.016, (timestamp - difficultyRain.lastFrame) / 1000 || 0.072));
  difficultyRain.lastFrame = timestamp;
  const ctx = difficultyRain.ctx;
  ctx.clearRect(0, 0, difficultyRain.width, difficultyRain.height);
  ctx.font = "700 13px 'Cascadia Code', Consolas, monospace";
  ctx.textBaseline = "top";
  ctx.textAlign = "center";
  for (const column of difficultyRain.columns) {
    if (timestamp > column.refreshAt) resetRainColumn(column, true);
    column.y = (column.y + column.speed * dt) % column.rowGap;
    const xRatio = Math.max(0, Math.min(1, column.x / Math.max(1, difficultyRain.width)));
    const reachFade = xRatio <= difficultyRain.activeReach ? 1 : column.spark ? 0.7 : 0.18;
    const stream = column.stream || "000000";
    let row = 0;
    for (let y = column.y - column.rowGap; y < difficultyRain.height + column.rowGap; y += column.rowGap) {
      const wave = 0.62 + Math.sin((timestamp / 1200) + y * 0.018 + column.x * 0.012) * 0.24;
      const head = ((y + column.y * 3) % (column.rowGap * 13)) < column.rowGap ? 2.2 : 1;
      const tail = 0.72 + ((row + column.streamOffset) % 9) / 28;
      const alpha = Math.max(0, column.alpha * wave * head * tail * reachFade);
      if (alpha < 0.006) continue;
      ctx.fillStyle = column.spark ? `rgba(150, 255, 226, ${alpha})` : `rgba(78, 215, 200, ${alpha})`;
      ctx.fillText(stream[(row + column.streamOffset) % stream.length], column.x, y);
      row += 1;
    }
  }
  difficultyRain.raf = requestAnimationFrame(drawDifficultyRain);
}

function updateDifficultyRain() {
  collectDifficultyRainValues();
  if (!difficultyRain.raf) difficultyRain.raf = requestAnimationFrame(drawDifficultyRain);
}

function percent(chance) {
  if (chance === null || chance === undefined) return "—";
  const value = chance * 100;
  if (!value) return "0%";
  if (value < 0.000001) return value.toExponential(2) + "%";
  if (value < 0.01) return value.toFixed(6) + "%";
  return value.toFixed(3) + "%";
}

function duration(days) {
  if (!days) return "Never at 0 H/s";
  if (days < 1) return `${number(days * 24, 1)} hours`;
  if (days < 365) return `${number(days, 1)} days`;
  if (days < 365000) return `${number(days / 365, 1)} years`;
  return `${Number(days / 365).toExponential(2)} years`;
}

function uptime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return [days ? `${days}d` : "", hours ? `${hours}h` : "", `${minutes}m`].filter(Boolean).join(" ");
}

function appDate(value) {
  if (!value) return null;
  const text = String(value);
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(text) ? text : `${text}Z`);
}

function appTime(value, options = {}) {
  const date = appDate(value);
  return date ? date.toLocaleTimeString([], options) : "--";
}

function highestTemp(miner) {
  const values = Object.values(miner?.temps || {}).filter(value => value !== null);
  return values.length ? Math.max(...values) : null;
}

function fanSummary(miner) {
  const rpms = (miner?.fans || []).map(fan => Number(fan.rpm)).filter(Number.isFinite);
  if (!rpms.length) return "Not reported";
  if (rpms.length === 1) return `${number(rpms[0], 0)} RPM`;
  return `${rpms.length} fans · ${number(Math.min(...rpms), 0)}–${number(Math.max(...rpms), 0)} RPM`;
}

function money(value) {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  const digits = numeric > 0 && numeric < 0.01 ? 8 : numeric < 1000 ? 2 : 0;
  return numeric.toLocaleString(undefined, {style: "currency", currency: "USD", maximumFractionDigits: digits});
}

function statusFor(id) {
  return state?.miners?.find(miner => miner.id === id) || null;
}

function configuredMiner(id) {
  return managedMiners.find(item => item.config.id === id)?.config || null;
}

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function request(path, {method = "GET", body, retries = 8} = {}) {
  let lastError = null;
  const payload = body === undefined ? undefined : JSON.stringify(body);
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(path, {
        method,
        cache: "no-store",
        headers: body === undefined ? {} : {"Content-Type": "application/json"},
        body: payload,
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok) return data;
      let message = data.detail || `Request failed (${response.status})`;
      if (Array.isArray(message)) message = message.map(item => item.msg).join(" · ");
      lastError = new Error(message);
      if (![502, 503, 504].includes(response.status)) throw lastError;
    } catch (error) {
      lastError = error;
    }
    if (attempt < retries) {
      await sleep(Math.min(7500, 400 + attempt * 650));
    }
  }
  throw lastError || new Error("Request failed");
}

function toast(message, kind = "info") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.className = "", 3800);
}

function navigate(path) {
  history.pushState({}, "", path);
  route();
}

function route() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const detailMatch = path.match(/^\/miners\/([^/]+)$/);
  const known = {"/":"dashboard", "/miners":"miners", "/pools":"pools", "/odds":"odds", "/alerts":"alerts", "/screen":"screen", "/settings":"settings"};
  const page = detailMatch ? "miner-detail" : (known[path] || "dashboard");
  document.body.classList.toggle("screen-active", page === "screen");
  updateDifficultyRain();
  $$(".page").forEach(element => element.classList.toggle("active", element.id === `page-${page}`));
  $$("nav a").forEach(element => {
    const target = element.getAttribute("href");
    element.classList.toggle("active", target === path || (detailMatch && target === "/miners"));
  });
  if (detailMatch) renderMinerDetail(detailMatch[1]);
  if (page === "pools") {
    loadPoolEvents();
    loadPoolConnectionTest();
  }
  if (page === "settings" && settings) fillSettings();
  if (location.hash) setTimeout(() => document.querySelector(location.hash)?.scrollIntoView({behavior: "smooth"}), 40);
  window.scrollTo({top: 0, behavior: "instant"});
}

function summaryMetric(label, value, sub = "", className = "") {
  return `<div class="metric"><span class="metric-label">${label}</span><strong class="metric-value ${className}">${value}</strong><span class="metric-sub">${sub}</span></div>`;
}

function renderSummary() {
  const summary = state.summary;
  const onlineClass = !summary.total_miners ? "" : summary.online_miners === summary.total_miners ? "good" : summary.online_miners ? "warn" : "bad";
  $("#summary-grid").innerHTML = [
    summaryMetric("Total hashrate", `${number(summary.total_hashrate_ths, 3)} <small>TH/s</small>`, "Live enabled devices", "good"),
    summaryMetric("Miners online", `${summary.online_miners} / ${summary.total_miners}`, `${summary.configured_miners} configured`, onlineClass),
    summaryMetric("Average response", summary.average_ping_ms === null ? "—" : `${number(summary.average_ping_ms)} ms`, "Miner API timing"),
    summaryMetric("Peak temperature", summary.highest_temperature_c === null ? "—" : `${number(summary.highest_temperature_c)} C`, "Across current sensors"),
    summaryMetric("Valid shares", number(summary.total_valid_shares, 0), "Current counters"),
    summaryMetric("Bad shares", number(summary.total_bad_shares, 0), "Invalid + stale + rejected", summary.total_bad_shares ? "warn" : ""),
    summaryMetric("BTC daily odds", state.odds.btc.available ? percent(state.odds.btc.daily_chance) : "—", state.odds.btc.network_source || "Needs setup"),
    summaryMetric("BCH daily odds", state.odds.bch.available ? percent(state.odds.bch.daily_chance) : "—", state.odds.bch.network_source || "Needs setup"),
  ].join("");
  $("#last-poll").textContent = summary.last_poll ? `Updated ${appTime(summary.last_poll)}` : "Waiting for first poll…";
}

function minerCard(miner) {
  const [hash, unit] = compactHashrate(miner.hashrate_ths);
  const temp = highestTemp(miner);
  const fans = fanSummary(miner);
  const badShares = ["invalid", "stale", "rejected"].reduce((sum, key) => sum + (miner.shares?.[key] || 0), 0);
  const healthy = miner.online && miner.api_ok;
  return `<article class="miner-card ${healthy ? "online" : "offline"}" data-action="open-miner" data-id="${escapeHtml(miner.id)}" tabindex="0">
    <div class="card-glow"></div>
    <div class="miner-head"><i class="status-dot"></i><div class="miner-title"><strong>${escapeHtml(miner.name)}</strong><small>${privateMarkup(miner.ip)}</small></div><span class="type-badge">${escapeHtml(minerTypeLabel(miner.type))}</span></div>
    <div class="hashrate"><strong>${hash}</strong><span>${unit}</span><small>${escapeHtml(miner.group)}</small></div>
    <div class="miner-details">
      <div class="detail"><label>Temperature</label><span>${temp === null ? "—" : number(temp) + " C"}</span></div>
      <div class="detail"><label>API response</label><span>${miner.ping_ms === null ? "Failed" : number(miner.ping_ms) + " ms"}</span></div>
      <div class="detail"><label>Fan</label><span title="${escapeHtml(fans)}">${escapeHtml(fans)}</span></div>
      <div class="detail"><label>Pool</label><span title="${privateAttr(miner.pool?.url)}">${escapeHtml(miner.pool?.status || "unknown")}</span></div>
      <div class="detail"><label>Valid shares</label><span>${number(miner.shares?.valid || 0, 0)}</span></div>
      <div class="detail"><label>Bad shares</label><span>${number(badShares, 0)}</span></div>
      <div class="detail"><label>Best session</label><span title="${escapeHtml(miner.difficulty?.best_session)}">${escapeHtml(difficulty(miner.difficulty?.best_session))}</span></div>
      <div class="detail"><label>Best all-time</label><span title="${escapeHtml(miner.difficulty?.best_all_time)}">${escapeHtml(difficulty(miner.difficulty?.best_all_time))}</span></div>
    </div>
    <div class="card-foot"><span>${healthy ? "Healthy" : escapeHtml(miner.status)}</span><strong>View details →</strong></div>
    ${(miner.warnings || []).length ? `<div class="warning-strip">${escapeHtml(miner.warnings[0])}</div>` : ""}
  </article>`;
}

function onboarding() {
  return `<section class="onboarding">
    <div class="onboarding-visual" aria-hidden="true">
      <div class="orbit orbit-one"><i></i></div>
      <div class="orbit orbit-two"><i></i></div>
      <div class="core-mark">P</div>
      <span class="signal signal-a"></span><span class="signal signal-b"></span><span class="signal signal-c"></span>
    </div>
    <div class="onboarding-copy"><p class="eyebrow">Your command center is ready</p><h2>Bring your first miner online</h2><p>Add an AxeOS, NerdQaxe, or LuxOS device. PoCiSys will test it, start live monitoring, and fold it into your fleet totals—without storing historical telemetry.</p>
      <div class="onboarding-steps"><span><b>1</b>Add its LAN address</span><span><b>2</b>Test the API</span><span><b>3</b>Watch it live</span></div>
      <button class="primary" data-action="add-miner">Add your first miner</button>
    </div>
  </section>`;
}

function renderMiners() {
  const miners = state.miners || [];
  const groups = ["All", ...new Set(miners.map(miner => miner.group || "Ungrouped"))];
  if (!groups.includes(activeGroup)) activeGroup = "All";
  $("#group-filters").innerHTML = miners.length > 1 ? groups.map(group => `<button class="filter-chip ${group === activeGroup ? "active" : ""}" data-action="filter-group" data-group="${escapeHtml(group)}">${escapeHtml(group)}</button>`).join("") : "";
  if (!miners.length) {
    $("#dashboard-miners").innerHTML = onboarding();
    return;
  }
  const visible = activeGroup === "All" ? miners : miners.filter(miner => (miner.group || "Ungrouped") === activeGroup);
  const grouped = Object.groupBy ? Object.groupBy(visible, miner => miner.group || "Ungrouped") : visible.reduce((result, miner) => {
    (result[miner.group || "Ungrouped"] ||= []).push(miner);
    return result;
  }, {});
  const density = state.ui?.dashboard_density === "compact" ? "compact" : "comfortable";
  $("#dashboard-miners").innerHTML = Object.entries(grouped).map(([group, items]) => `<section class="miner-group"><div class="group-label"><span>${escapeHtml(group)}</span><small>${items.length} miner${items.length === 1 ? "" : "s"}</small></div><div class="miner-grid ${density}">${items.map(minerCard).join("")}</div></section>`).join("");
}

function renderManagedMiners() {
  if (!managedMiners.length) {
    $("#miners-table").innerHTML = `<div class="empty-action"><span class="empty-icon">⛏</span><h2>No miners configured</h2><p>Add a device to start building your fleet.</p><button class="primary" data-action="add-miner">Add miner</button></div>`;
    return;
  }
  $("#miners-table").innerHTML = `<table><thead><tr><th>Order</th><th>Status</th><th>Miner</th><th>OS / API</th><th>Group</th><th>Hashrate</th><th>Temperature</th><th>Alerts</th><th>Actions</th></tr></thead><tbody>${managedMiners.map((entry, index) => {
    const config = entry.config;
    const status = statusFor(config.id);
    const [hash, unit] = compactHashrate(status?.hashrate_ths);
    const liveClass = !config.enabled ? "muted-status" : status?.api_ok ? "good" : "bad";
    const liveText = !config.enabled ? "Disabled" : status?.api_ok ? "Online" : "Offline";
    return `<tr>
      <td><div class="order-buttons"><button data-action="move-miner" data-id="${config.id}" data-direction="-1" ${index === 0 ? "disabled" : ""}>↑</button><button data-action="move-miner" data-id="${config.id}" data-direction="1" ${index === managedMiners.length - 1 ? "disabled" : ""}>↓</button></div></td>
      <td class="${liveClass}">● ${liveText}</td>
      <td><button class="table-link" data-action="open-miner" data-id="${config.id}"><strong>${escapeHtml(config.name)}</strong><small>${privateMarkup(config.ip)}</small></button></td>
      <td><span class="type-badge">${escapeHtml(minerTypeLabel(config.type))}</span></td><td>${escapeHtml(config.group)}</td>
      <td>${status ? `${hash} ${unit}` : "—"}</td><td>${status && highestTemp(status) !== null ? number(highestTemp(status)) + " C" : "—"}</td>
      <td><small>${config.min_hashrate_ths != null ? `Min ${number(config.min_hashrate_ths, 3)} TH/s` : status?.expected_hashrate_ths ? `Auto · 75% of ${number(status.expected_hashrate_ths, 3)} TH/s` : "No hash minimum"} · ${number(config.temp_warning_c, 0)} C / ${number(config.temp_critical_c, 0)} C</small></td>
      <td><div class="row-actions"><button data-action="edit-miner" data-id="${config.id}">Edit</button><button class="danger-text" data-action="delete-miner" data-id="${config.id}">Delete</button></div></td>
    </tr>`;
  }).join("")}</tbody></table>`;
}

function detailStat(label, value, sub = "") {
  return `<div class="detail-stat"><label>${label}</label><strong>${value}</strong>${sub ? `<small>${sub}</small>` : ""}</div>`;
}

function infoRow(label, value, suffix = "") {
  if (value === null || value === undefined || value === "") return "";
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}${suffix}</strong></div>`;
}

function renderChipHealth(status) {
  const health = status?.chip_health || {};
  const items = health.items || [];
  if (!health.reported || !items.length) {
    return `<div class="empty compact-empty">Chip-level health is not reported by this firmware.</div>`;
  }
  return `<div class="chip-grid">${items.map(item => {
    const count = item.chips_total ? `${number(item.chips_healthy, 0)} / ${number(item.chips_total, 0)} ASICs` : item.cores ? `${number(item.cores, 0)} cores reported` : "API health signal";
    const hash = item.hashrate_ths == null ? "" : `${number(item.hashrate_ths, 2)} TH/s`;
    const temp = item.temperature_c == null ? "" : `${number(item.temperature_c)} C`;
    const cores = item.chips_total && item.cores ? `${number(item.cores, 0)} cores` : "";
    const errorPct = item.hardware_error_percent == null ? "" : `${number(item.hardware_error_percent, 2)}% errors`;
    const meta = [count, cores, hash, temp, errorPct].filter(Boolean).join(" · ");
    return `<article class="chip-card ${item.status === "healthy" ? "healthy" : "warning"}"><div><i></i><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.status || "reported")}</span></div><p>${escapeHtml(meta)}</p>${item.hardware_errors != null ? `<small>${number(item.hardware_errors, 0)} hardware errors</small>` : ""}</article>`;
  }).join("")}</div>`;
}

function renderMinerDetail(id) {
  const config = configuredMiner(id);
  if (!config) {
    $("#miner-detail").innerHTML = `<div class="empty-action"><h2>Miner not found</h2><button data-link href="/miners">Back to miners</button></div>`;
    return;
  }
  const status = statusFor(id);
  const healthy = status?.api_ok;
  const [hash, unit] = compactHashrate(status?.hashrate_ths);
  const temps = status?.temps || {};
  const shares = status?.shares || {};
  const temperatureItems = [
    ["ASIC", temps.asic_c], ["VRM", temps.vrm_c], ["Board", temps.board_c], ["Chip", temps.chip_c],
  ].filter(([, value]) => value !== null && value !== undefined);
  const expected = Number(status?.expected_hashrate_ths || 0);
  const performance = expected > 0 ? Math.max(0, Math.min(125, Number(status?.hashrate_ths || 0) / expected * 100)) : null;
  $("#miner-detail").innerHTML = `<div class="detail-top">
    <button class="back-link" data-link href="/miners">← All miners</button>
    <div class="detail-actions"><a class="button-link" href="http://${escapeHtml(config.ip)}" target="_blank" rel="noopener">Open native dashboard ↗</a><button data-action="edit-miner" data-id="${config.id}">Edit setup</button></div>
  </div>
  <section class="miner-hero ${healthy ? "online" : "offline"}">
    <div class="hero-grid"></div><div class="hero-ident"><span class="hero-status"><i class="status-dot"></i>${!config.enabled ? "Monitoring disabled" : healthy ? "Live and healthy" : status?.status || "Waiting for miner"}</span><p class="eyebrow">${escapeHtml(config.group)}</p><h1>${escapeHtml(config.name)}</h1><p>${privateMarkup(config.ip)} · ${escapeHtml(minerTypeLabel(config.type))} · ${escapeHtml(status?.firmware || "Firmware unavailable")}</p></div>
    <div class="hero-hash"><strong>${hash}</strong><span>${unit}</span><small>Current hashrate</small></div>
  </section>
  <div class="detail-stat-grid">
    ${detailStat("Peak temperature", highestTemp(status) === null ? "—" : number(highestTemp(status)) + " C", "Current sensors")}
    ${detailStat("API response", status?.ping_ms === null || status?.ping_ms === undefined ? "—" : number(status.ping_ms) + " ms", status?.api_ok ? "API healthy" : "API unavailable")}
    ${detailStat("Uptime", uptime(status?.uptime_seconds), "Miner reported")}
    ${detailStat("Best difficulty", escapeHtml(difficulty(status?.difficulty?.best_all_time || status?.difficulty?.best_session)), "Best available")}
  </div>
  <div class="detail-panels">
    <section class="panel"><div class="panel-title"><h2>Hardware</h2><span class="type-badge">${escapeHtml(minerTypeLabel(config.type))}</span></div><div class="info-list">
      ${infoRow("Firmware", status?.firmware)}
      ${infoRow("Frequency", status?.frequency_mhz == null ? null : number(status.frequency_mhz), " MHz")}
      ${infoRow("Voltage", status?.voltage_mv == null ? null : number(status.voltage_mv), " mV")}
      ${infoRow("Wi-Fi signal", status?.wifi_rssi == null ? null : number(status.wifi_rssi), " dBm")}
      ${infoRow("Expected hashrate", status?.expected_hashrate_ths == null ? "Not reported" : `${number(status.expected_hashrate_ths, 3)} TH/s`)}
      ${infoRow("Blocks found", number(status?.blocks_found || 0, 0))}
      ${infoRow("Hardware errors", status?.hardware_errors == null ? "Not reported" : number(status.hardware_errors, 0))}
      ${infoRow("Cooling", fanSummary(status))}
    </div>${performance == null ? "" : `<div class="performance"><span><b>Live performance</b><strong>${number(performance, 1)}%</strong></span><div><i style="width:${Math.min(performance, 100)}%"></i></div></div>`}</section>
    <section class="panel"><div class="panel-title"><h2>Temperatures & cooling</h2></div>
      ${temperatureItems.length ? `<div class="sensor-grid">${temperatureItems.map(([label, value]) => detailStat(label, `${number(value)} C`)).join("")}</div>` : `<div class="empty compact-empty">No temperature sensors reported.</div>`}
      ${status?.fans?.length ? `<div class="fan-grid">${status.fans.map(fan => `<div><span>${escapeHtml(fan.name)}</span><strong>${number(fan.rpm, 0)} RPM</strong></div>`).join("")}</div>` : ""}
      <div class="threshold-note">Alerts at ${number(config.temp_warning_c)} C · Critical at ${number(config.temp_critical_c)} C</div></section>
    <section class="panel chip-health-panel"><div class="panel-title"><h2>Chip health</h2><span class="pill">${status?.chip_health?.reported ? `${number(status.chip_health.healthy, 0)} / ${number(status.chip_health.total, 0)} healthy` : "Not reported"}</span></div>${renderChipHealth(status)}</section>
    <section class="panel"><div class="panel-title"><h2>Pool</h2><span class="${status?.pool?.connected ? "good" : "warn"}">${escapeHtml(status?.pool?.status)}</span></div><div class="pool-url">${privateMarkup(status?.pool?.url || "Pool URL not reported")}</div><div class="info-list"><div><span>Connection</span><strong>${status?.pool?.connected == null ? "Unknown" : status.pool.connected ? "Connected" : "Disconnected"}</strong></div>${infoRow("Source", status?.pool?.source)}<div><span>Valid shares</span><strong>${number(shares.valid || 0, 0)}</strong></div></div></section>
    <section class="panel"><div class="panel-title"><h2>Share quality</h2></div><div class="sensor-grid">${detailStat("Valid", number(shares.valid || 0, 0))}${detailStat("Invalid", number(shares.invalid || 0, 0))}${detailStat("Stale", number(shares.stale || 0, 0))}${detailStat("Rejected", number(shares.rejected || 0, 0))}</div></section>
  </div>`;
}

function coinMark(symbol) {
  return {BTC: "₿", BCH: "B", BSV: "S", XEC: "X", DGB: "D", CHTA: "C"}[symbol] || symbol?.[0] || "?";
}

function networkHashrate(item) {
  if (!item?.network_hashrate_eh) return "—";
  const [value, unit] = compactHashrate(Number(item.network_hashrate_eh) * 1_000_000);
  return `${value} ${unit}`;
}

function networkLine(item) {
  if (!item?.enabled) return "";
  const symbol = item.symbol || "?";
  if (!item.available) return `<div class="network-line"><span class="coin ${symbol.toLowerCase()}">${coinMark(symbol)}</span><div><strong>${symbol}</strong><small>${escapeHtml(item.message)}</small></div><strong>—</strong></div>`;
  return `<div class="network-line"><span class="coin ${symbol.toLowerCase()}">${coinMark(symbol)}</span><div><strong>${symbol}</strong><small>${money(item.price_usd)} · ${networkHashrate(item)} · ${escapeHtml(item.network_source)}</small></div><strong>${percent(item.daily_chance)} / day</strong></div>`;
}

function oddsCard(item) {
  const symbol = item?.symbol || "?";
  if (!item?.available) return `<article class="odds-card"><header><span class="coin ${symbol.toLowerCase()}">${coinMark(symbol)}</span><div><h2>${symbol}</h2><p>${escapeHtml(item?.message || "Network data unavailable")}</p></div></header></article>`;
  return `<article class="odds-card"><header><span class="coin ${symbol.toLowerCase()}">${coinMark(symbol)}</span><div><h2>${symbol}</h2><p>${networkHashrate(item)} · ${escapeHtml(item.network_source)}</p></div><strong class="coin-price">${money(item.price_usd)}</strong></header><div class="odds-stats">
    <div class="odds-stat"><label>Daily chance</label><strong>${percent(item.daily_chance)}</strong></div><div class="odds-stat"><label>Weekly chance</label><strong>${percent(item.weekly_chance)}</strong></div>
    <div class="odds-stat"><label>Network difficulty</label><strong>${difficulty(item.difficulty)}</strong></div><div class="odds-stat"><label>Estimated time</label><strong>${duration(item.estimated_days_to_block)}</strong></div>
  </div>${item.estimate_note ? `<p class="odds-note">${escapeHtml(item.estimate_note)}</p>` : ""}</article>`;
}

function renderOdds() {
  const coins = [state.odds.btc, state.odds.bch, state.odds.bsv, state.odds.xec, state.odds.dgb, state.odds.chta].filter(item => item?.enabled);
  if (activeOddsCoin !== "all" && !coins.some(item => item.symbol.toLowerCase() === activeOddsCoin)) activeOddsCoin = "all";
  const visible = activeOddsCoin === "all" ? coins : coins.filter(item => item.symbol.toLowerCase() === activeOddsCoin);
  $("#dashboard-odds").innerHTML = coins.map(networkLine).join("") || `<div class="empty-inline">Enable coins in Block Odds settings.</div>`;
  $("#odds-filters").innerHTML = [`<button class="filter-chip ${activeOddsCoin === "all" ? "active" : ""}" data-action="filter-odds" data-coin="all">All</button>`, ...coins.map(item => `<button class="filter-chip ${activeOddsCoin === item.symbol.toLowerCase() ? "active" : ""}" data-action="filter-odds" data-coin="${item.symbol.toLowerCase()}">${item.symbol}</button>`)].join("");
  $("#odds-content").classList.toggle("single", visible.length === 1);
  $("#odds-content").innerHTML = visible.length ? visible.map(oddsCard).join("") : `<div class="empty-action"><h2>No coin trackers enabled</h2><p>Enable at least one SHA-256 coin in Settings.</p></div>`;
}

function renderDashboardPools() {
  const pools = state.pools || [];
  $("#dashboard-pools").innerHTML = pools.length ? pools.map(pool => `<div class="network-line"><span class="status-dot" style="background:${pool.available ? "var(--green)" : "var(--red)"}"></span><div><strong>${escapeHtml(pool.name)}</strong><small>${escapeHtml(pool.message || (pool.mode === "local_log" ? "Mounted local log" : "Waiting for local pool API"))}</small></div><strong>${pool.available && pool.total_hashrate_ths != null ? `${number(pool.total_hashrate_ths, 2)} TH/s` : pool.enabled ? "Enabled" : "Disabled"}</strong></div>`).join("") : `<div class="empty-inline"><span>No local pool monitors yet.</span><button data-action="add-pool">Add local pool</button></div>`;
}

function renderManagedPools() {
  if (!managedPools.length) {
    $("#pool-management").innerHTML = `<div class="empty-action pool-empty"><span class="empty-icon">≋</span><h2>No local pool monitors</h2><p>Connect a self-hosted Public Pool API or add a mounted local pool log viewer.</p><button data-action="add-pool">Connect local pool</button></div>`;
    return;
  }
  $("#pool-management").innerHTML = managedPools.map(entry => {
    const pool = entry.config;
    const live = state?.pools?.find(item => item.name === pool.name);
    const source = pool.mode === "public_pool_api" ? pool.api_url : pool.log_path;
    const stats = pool.mode === "public_pool_api" && live?.available ? `<div class="pool-live-stats"><span><small>Pool hashrate</small><strong>${number(live.total_hashrate_ths, 2)} TH/s</strong></span><span><small>Connected miners</small><strong>${number(live.total_miners, 0)}</strong></span><span><small>Block height</small><strong>${number(live.block_height, 0)}</strong></span><span><small>Your workers</small><strong>${live.workers_count == null ? "Address not detected" : number(live.workers_count, 0)}</strong></span></div>${live.workers?.length ? `<div class="worker-list">${live.workers.map(worker => `<div><strong>${escapeHtml(worker.name)}</strong><span>${number(worker.hashrate_ths, 2)} TH/s</span><span>Best ${difficulty(worker.best_difficulty)}</span></div>`).join("")}</div>` : ""}` : "";
    return `<article class="manage-card ${pool.mode === "public_pool_api" ? "public-pool-card" : ""}"><div class="manage-card-top"><span class="settings-icon">${pool.mode === "public_pool_api" ? "P" : "≋"}</span><div><h2>${escapeHtml(pool.name)}</h2><p>${pool.mode === "public_pool_api" ? "Self-hosted Public Pool · live API" : `${escapeHtml(pool.type)} · mounted local log`}</p></div><span class="pill ${live?.available ? "good-border" : "bad-border"}">${!pool.enabled ? "Disabled" : live?.available ? "Connected" : "Unavailable"}</span></div><div class="path-box">${privateMarkup(source)}</div>${stats}<div class="manage-card-actions"><button data-action="edit-pool" data-id="${pool.id}">Edit</button><button class="danger-text" data-action="delete-pool" data-id="${pool.id}">Delete</button></div></article>`;
  }).join("");
}

function alertFeed(events) {
  if (!events?.length) return `<div class="empty">No alert activity yet.</div>`;
  return events.map(event => `<div class="feed-item"><span class="feed-time">${appTime(event.time)}</span><span class="feed-source">${escapeHtml(event.source)}</span><div class="feed-message"><strong class="${event.severity === "critical" ? "bad" : event.severity === "warning" ? "warn" : ""}">${escapeHtml(event.title)}</strong><span>${privateMarkup(event.message)}</span></div></div>`).join("");
}

function snoozeRemaining() {
  return Math.max(0, Math.ceil((alertSnoozeUntil - Date.now()) / 1000));
}

function snoozeTime(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}

function snoozeControlsMarkup(compact = false) {
  const remaining = snoozeRemaining();
  if (remaining) {
    return `<span class="snooze-state warn">Snoozed · ${snoozeTime(remaining)}</span><button type="button" data-action="resume-alerts">Resume Alerts</button>`;
  }
  return `${compact ? `<span class="snooze-state good">Alerts active</span>` : `<span class="snooze-label">Snooze for</span>`}<button type="button" data-action="snooze-alerts" data-seconds="900">15m</button><button type="button" data-action="snooze-alerts" data-seconds="1800">30m</button><button type="button" data-action="snooze-alerts" data-seconds="3600">1h</button>`;
}

function updateSnoozeControls() {
  const remaining = snoozeRemaining();
  if (!remaining) alertSnoozeUntil = 0;
  const alertsControl = $("#alert-snooze-controls");
  const settingsControl = $("#settings-snooze-controls");
  const screenControl = $("#screen-snooze-controls");
  const update = (element, compact) => {
    if (!element) return;
    const mode = remaining ? "snoozed" : "active";
    if (element.dataset.snoozeMode !== mode) {
      element.innerHTML = snoozeControlsMarkup(compact);
      element.dataset.snoozeMode = mode;
    } else if (remaining) {
      const stateLabel = element.querySelector(".snooze-state");
      if (stateLabel) stateLabel.textContent = `Snoozed · ${snoozeTime(remaining)}`;
    }
  };
  update(alertsControl, false);
  update(settingsControl, false);
  update(screenControl, true);
}

function renderAlerts() {
  const discord = state.discord;
  alertSnoozeUntil = discord.snoozed ? Date.now() + Number(discord.snooze_remaining_seconds || 0) * 1000 : 0;
  const label = discord.snoozed ? `Routine alerts snoozed · ${snoozeTime(snoozeRemaining())}` : discord.discord_enabled && discord.discord_configured ? "Ready" : discord.discord_configured ? "Configured but disabled" : "Not configured";
  $("#discord-status").innerHTML = `<div class="discord-card"><span class="discord-icon">D</span><div><strong>Dashboard-wide Discord webhook · <span class="${discord.snoozed || !(discord.discord_enabled && discord.discord_configured) ? "warn" : "good"}">${label}</span></strong><p>One webhook covers miners and local pool critical events. Blocks/candidates send immediately; repeated conditions use cooldowns.</p></div><a class="button-link" href="/settings" data-link>Configure</a></div>`;
  $("#alert-events").innerHTML = alertFeed(discord.recent);
  updateSnoozeControls();
}

function screenStatusCard(label, value, sub = "", className = "") {
  return `<article class="screen-stat"><span>${label}</span><strong class="${className}">${value}</strong><small>${sub}</small></article>`;
}

function screenMiner(miner) {
  const [hash, unit] = compactHashrate(miner.hashrate_ths);
  const temp = highestTemp(miner);
  const badShares = ["invalid", "stale", "rejected"].reduce((sum, key) => sum + (miner.shares?.[key] || 0), 0);
  const healthy = miner.online && miner.api_ok;
  const poolOk = miner.pool?.connected === true || String(miner.pool?.status || "").toLowerCase() === "alive";
  const statusText = healthy ? "Online" : miner.offline_for_seconds ? `Offline ${number(miner.offline_for_seconds, 0)}s` : miner.status || "Offline";
  return `<article class="screen-miner ${healthy ? "online" : "offline"}">
    <div class="screen-miner-top"><i class="status-dot"></i><div><strong>${escapeHtml(miner.name)}</strong><span>${privateMarkup(miner.ip)}  -  ${escapeHtml(minerTypeLabel(miner.type))}</span></div><em>${escapeHtml(statusText)}</em></div>
    <div class="screen-hash"><strong>${hash}</strong><span>${unit}</span></div>
    <div class="screen-mini-grid">
      <span><small>Temp</small><b>${temp === null ? "--" : number(temp) + " C"}</b></span>
      <span><small>API</small><b>${miner.ping_ms === null ? "Fail" : number(miner.ping_ms, 0) + " ms"}</b></span>
      <span><small>Pool</small><b class="${poolOk ? "good" : "warn"}">${escapeHtml(miner.pool?.status || "unknown")}</b></span>
      <span><small>Bad</small><b class="${badShares ? "warn" : ""}">${number(badShares, 0)}</b></span>
      <span><small>Best</small><b>${escapeHtml(difficulty(miner.difficulty?.best_all_time || miner.difficulty?.best_session))}</b></span>
      <span><small>Fans</small><b title="${escapeHtml(fanSummary(miner))}">${escapeHtml(fanSummary(miner))}</b></span>
    </div>
    ${(miner.warnings || []).length ? `<div class="screen-warning">${escapeHtml(miner.warnings[0])}</div>` : ""}
  </article>`;
}

function renderScreen() {
  if (!state || !$("#screen-status")) return;
  const summary = state.summary || {};
  const miners = state.miners || [];
  const onlineClass = !summary.total_miners ? "" : summary.online_miners === summary.total_miners ? "good" : summary.online_miners ? "warn" : "bad";
  const hottest = summary.highest_temperature_c === null || summary.highest_temperature_c === undefined ? "--" : `${number(summary.highest_temperature_c)} C`;
  const lastPoll = appDate(summary.last_poll);
  $("#screen-time").textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  $("#screen-updated").textContent = lastPoll ? `Updated ${lastPoll.toLocaleTimeString()}` : "Waiting for first poll";
  $("#screen-status").innerHTML = [
    screenStatusCard("Hashrate", `${number(summary.total_hashrate_ths || 0, 2)} TH/s`, "Fleet total", "good"),
    screenStatusCard("Online", `${summary.online_miners || 0} / ${summary.total_miners || 0}`, `${summary.configured_miners || 0} configured`, onlineClass),
    screenStatusCard("Peak temp", hottest, "Current sensors", summary.highest_temperature_c >= 70 ? "warn" : ""),
    screenStatusCard("Bad shares", number(summary.total_bad_shares || 0, 0), "Invalid + stale + rejected", summary.total_bad_shares ? "warn" : ""),
    screenStatusCard("BTC odds", state.odds?.btc?.available ? `${percent(state.odds.btc.daily_chance)} / day` : "--", state.odds?.btc?.network_source || "Network data"),
    screenStatusCard("BCH odds", state.odds?.bch?.available ? `${percent(state.odds.bch.daily_chance)} / day` : "--", state.odds?.bch?.network_source || "Network data"),
  ].join("");
  $("#screen-fleet-count").textContent = `${miners.length} miner${miners.length === 1 ? "" : "s"}`;
  $("#screen-miners").innerHTML = miners.length ? miners.map(screenMiner).join("") : `<div class="screen-empty"><strong>No miners configured</strong><span>Add miners from the normal dashboard to populate watch screen mode.</span></div>`;
  const pools = state.pools || [];
  $("#screen-pools").innerHTML = pools.length ? pools.map(pool => `<div class="screen-row"><i class="status-dot" style="background:${pool.available ? "var(--green)" : "var(--red)"}"></i><div><strong>${escapeHtml(pool.name)}</strong><span>${escapeHtml(pool.message || "Local pool monitor")}</span></div><b>${pool.available && pool.total_hashrate_ths != null ? `${number(pool.total_hashrate_ths, 2)} TH/s` : pool.enabled ? "Enabled" : "Off"}</b></div>`).join("") : `<div class="screen-empty"><strong>No local pool monitors</strong><span>Local pool cards appear here when configured.</span></div>`;
  const alerts = state.discord?.recent || [];
  $("#screen-alerts").innerHTML = alerts.length ? alerts.slice(0, 5).map(event => `<div class="screen-row alert"><span>${appTime(event.time, {hour: "numeric", minute: "2-digit"})}</span><div><strong class="${event.severity === "critical" ? "bad" : event.severity === "warning" ? "warn" : ""}">${escapeHtml(event.title)}</strong><span>${escapeHtml(event.source)}  -  ${privateMarkup(event.message)}</span></div></div>`).join("") : `<div class="screen-empty"><strong>No recent alerts</strong><span>Quiet fleet. The cat approves.</span></div>`;
  updateDifficultyRain();
}

async function loadPoolEvents() {
  try {
    const events = (await request("/api/pool-events")).events || [];
    $("#pool-event-total").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
    $("#pool-events").innerHTML = events.length ? events.map(event => `<div class="feed-item"><span class="feed-time">${appTime(event.time)}</span><span class="feed-source">${escapeHtml(event.pool)}</span><div class="feed-message"><strong class="${event.severity === "critical" ? "bad" : event.severity === "warning" ? "warn" : ""}">${escapeHtml(event.title)}</strong><span>${privateMarkup(event.line)}</span></div></div>`).join("") : `<div class="empty">Waiting for new important local pool events.</div>`;
  } catch (_) {}
}

function updatePoolTestButton() {
  const button = $("#pool-test-button");
  if (!button) return;
  const remaining = Math.max(0, Math.ceil((poolTestCooldownUntil - Date.now()) / 1000));
  button.disabled = remaining > 0;
  button.textContent = remaining > 0 ? `Test again in ${remaining}s` : "Test connection";
  if (!remaining && poolTestCooldownTimer) {
    clearInterval(poolTestCooldownTimer);
    poolTestCooldownTimer = null;
  }
}

function setPoolTestCooldown(seconds) {
  poolTestCooldownUntil = Date.now() + Math.max(0, Number(seconds) || 0) * 1000;
  if (poolTestCooldownTimer) clearInterval(poolTestCooldownTimer);
  poolTestCooldownTimer = seconds > 0 ? setInterval(updatePoolTestButton, 250) : null;
  updatePoolTestButton();
}

function renderPoolConnectionTest(payload) {
  setPoolTestCooldown(payload?.cooldown_remaining || 0);
  const result = payload?.result;
  const container = $("#pool-test-result");
  if (!container) return;
  if (!result) {
    container.innerHTML = `<div class="empty compact-empty">No external pool has been tested during this app session.</div>`;
    return;
  }
  const input = $("#pool-test-target");
  if (input && !input.value) input.value = result.target || "";
  const stateClass = result.reachable ? "good" : "bad";
  const stateLabel = result.reachable ? "Endpoint reachable" : "Endpoint unreachable";
  container.innerHTML = `<div class="pool-test-status"><i class="status-dot" style="background:var(--${result.reachable ? "green" : "red"})"></i><div><strong class="${stateClass}">${stateLabel}</strong><span>${escapeHtml(result.target)} · Tested ${appTime(result.tested_at)}</span></div></div>
    <div class="pool-test-metrics">
      <div><label>DNS lookup</label><strong>${result.dns_ms == null ? "—" : `${number(result.dns_ms, 1)} ms`}</strong></div>
      <div><label>Successful attempts</label><strong>${number(result.successes || 0, 0)} / ${number(result.attempts || 3, 0)}</strong></div>
      <div><label>Minimum</label><strong>${result.min_ms == null ? "—" : `${number(result.min_ms, 1)} ms`}</strong></div>
      <div><label>Average</label><strong>${result.average_ms == null ? "—" : `${number(result.average_ms, 1)} ms`}</strong></div>
      <div><label>Maximum</label><strong>${result.max_ms == null ? "—" : `${number(result.max_ms, 1)} ms`}</strong></div>
      <div><label>Resolved endpoint</label><strong>${result.resolved_ip ? privateMarkup(result.resolved_ip) : "—"}</strong></div>
    </div>${result.error ? `<p class="pool-test-error">${escapeHtml(result.error)}</p>` : ""}`;
}

async function loadPoolConnectionTest() {
  try {
    renderPoolConnectionTest(await request("/api/pool-connection-test"));
  } catch (_) {}
}

async function testExternalPool() {
  const input = $("#pool-test-target");
  const button = $("#pool-test-button");
  const target = input.value.trim();
  if (!target) {
    input.focus();
    toast("Enter an external Stratum pool endpoint.", "warning");
    return;
  }
  button.disabled = true;
  button.textContent = "Testing…";
  $("#pool-test-result").innerHTML = `<div class="pool-test-running"><span class="testing-pulse"></span><strong>Testing from UmbrelOS…</strong><span>Three TCP connections, then every socket is closed.</span></div>`;
  try {
    const payload = await request("/api/pool-connection-test", {method: "POST", body: {target}});
    renderPoolConnectionTest(payload);
    toast(payload.result?.reachable ? "External pool endpoint is reachable." : "External pool endpoint could not be reached.", payload.result?.reachable ? "success" : "warning");
  } catch (error) {
    await loadPoolConnectionTest();
    throw error;
  }
}

function renderAll() {
  if (!state) return;
  renderSummary();
  renderMiners();
  renderManagedMiners();
  renderOdds();
  renderDashboardPools();
  renderManagedPools();
  renderAlerts();
  renderScreen();
  const detailMatch = location.pathname.match(/^\/miners\/([^/]+)$/);
  if (detailMatch) renderMinerDetail(detailMatch[1]);
}

async function refresh() {
  try {
    state = await request("/api/status");
    renderAll();
    $(".connection").className = "connection live";
    $("#connection-text").textContent = "Live · Read-only";
    if (location.pathname === "/pools") loadPoolEvents();
  } catch (error) {
    $(".connection").className = "connection down";
    $("#connection-text").textContent = "Disconnected";
  }
}

async function loadManagement() {
  const [miners, pools] = await Promise.all([request("/api/miners"), request("/api/pools")]);
  managedMiners = miners.miners || [];
  managedPools = pools.pools || [];
  renderAll();
  route();
}

async function loadSettings() {
  try {
    settings = await request("/api/settings");
  } catch (error) {
    settings = {...DEFAULT_SETTINGS};
    toast(`Settings using defaults until API reconnects: ${error.message}`, "warning");
  }
  fillSettings();
}

function fillSettings() {
  if (!settings) return;
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const field = form.elements.namedItem(key);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  }
  $("#webhook-state").textContent = settings.webhook_configured ? "A webhook is configured and hidden." : "No webhook configured.";
  const mcpPath = settings.hermes_mcp_path || "/mcp";
  $("#hermes-mcp-url").value = `${location.origin}${mcpPath}`;
  $("#hermes-token-state").textContent = settings.hermes_token_configured
    ? `Connection token configured · ends in ${settings.hermes_token_hint || "••••••"}`
    : "No connection token configured.";
}

function setFormValue(form, name, value) {
  const field = form.elements.namedItem(name);
  if (!field) return;
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value ?? "";
}

function openMinerDialog(id = null) {
  const dialog = $("#miner-dialog");
  const form = $("#miner-form");
  form.reset();
  setFormValue(form, "id", "");
  setFormValue(form, "enabled", true);
  setFormValue(form, "group", "BTC Solo");
  setFormValue(form, "temp_warning_c", 70);
  setFormValue(form, "temp_critical_c", 80);
  setFormValue(form, "display_order", managedMiners.length + 1);
  $("#miner-test-result").innerHTML = "";
  if (id) {
    const config = configuredMiner(id);
    if (!config) return;
    Object.entries(config).forEach(([key, value]) => setFormValue(form, key, value));
    $("#miner-dialog-title").textContent = `Edit ${config.name}`;
  } else {
    $("#miner-dialog-title").textContent = "Add miner";
  }
  dialog.showModal();
  setTimeout(() => form.elements.name.focus(), 20);
}

function openPoolDialog(id = null) {
  const dialog = $("#pool-dialog");
  const form = $("#pool-form");
  form.reset();
  setFormValue(form, "id", "");
  setFormValue(form, "enabled", true);
  setFormValue(form, "name", "My Public Pool");
  setFormValue(form, "type", "public_pool");
  setFormValue(form, "mode", "public_pool_api");
  $("#pool-detect-result").innerHTML = "";
  if (id) {
    const config = managedPools.find(item => item.config.id === id)?.config;
    if (!config) return;
    Object.entries(config).forEach(([key, value]) => setFormValue(form, key, value));
    $("#pool-dialog-title").textContent = `Edit ${config.name}`;
  } else {
    $("#pool-dialog-title").textContent = "Connect local pool";
  }
  syncPoolFields();
  dialog.showModal();
}

function syncPoolFields() {
  const form = $("#pool-form");
  const publicPool = form.elements.mode.value === "public_pool_api";
  $$(".public-pool-field", form).forEach(field => field.hidden = !publicPool);
  $$(".local-log-field", form).forEach(field => field.hidden = publicPool);
  form.elements.type.value = publicPool ? "public_pool" : "ckpool";
}

async function moveMiner(id, direction) {
  const ids = managedMiners.map(item => item.config.id);
  const index = ids.indexOf(id);
  const target = index + Number(direction);
  if (index < 0 || target < 0 || target >= ids.length) return;
  [ids[index], ids[target]] = [ids[target], ids[index]];
  await request("/api/miners/reorder", {method: "POST", body: {ids}});
  await loadManagement();
  toast("Miner order updated.", "success");
}

async function deleteMiner(id) {
  const config = configuredMiner(id);
  if (!config || !confirm(`Remove “${config.name}” from PoCiSys? This does not change the miner itself.`)) return;
  await request(`/api/miners/${encodeURIComponent(id)}`, {method: "DELETE"});
  if (location.pathname.includes(id)) navigate("/miners");
  await Promise.all([loadManagement(), refresh()]);
  toast("Miner removed.", "success");
}

async function deletePool(id) {
  const config = managedPools.find(item => item.config.id === id)?.config;
  if (!config || !confirm(`Remove the “${config.name}” local pool monitor? The original log file is untouched.`)) return;
  await request(`/api/pools/${encodeURIComponent(id)}`, {method: "DELETE"});
  await Promise.all([loadManagement(), refresh()]);
  toast("Local pool monitor removed.", "success");
}

async function testDiscord(button) {
  button.disabled = true;
  try {
    const result = await request("/api/test-discord", {method: "POST"});
    toast(result.sent ? "Discord test sent." : `Not sent: ${result.reason || "unknown error"}`, result.sent ? "success" : "warning");
    await refresh();
  } finally {
    button.disabled = false;
  }
}

async function clearRecentAlerts(button) {
  button.disabled = true;
  try {
    const result = await request("/api/alerts/clear", {method: "POST"});
    state.discord = result;
    renderAlerts();
    renderScreen();
    toast("Recent alerts cleared.", "success");
  } finally {
    button.disabled = false;
  }
}

async function setAlertSnooze(seconds) {
  const result = await request("/api/alerts/snooze", {method: "POST", body: {seconds}});
  state.discord = result;
  renderAlerts();
  renderScreen();
  toast(`Routine Discord alerts snoozed for ${seconds / 60} minutes.`, "success");
}

async function resumeAlerts() {
  state.discord = await request("/api/alerts/resume", {method: "POST"});
  renderAlerts();
  renderScreen();
  toast("Routine Discord alerts resumed.", "success");
}

async function generateHermesToken(button) {
  const replacing = Boolean(settings?.hermes_token_configured);
  if (replacing && !confirm("Rotate the Hermes connection token? The existing Hermes connection will stop working until its saved token is updated.")) return;
  button.disabled = true;
  try {
    const result = await request("/api/hermes/token", {method: "POST"});
    $("#hermes-token-value").value = result.token;
    $("#hermes-token-result").hidden = false;
    settings = await request("/api/settings");
    fillSettings();
    toast("Hermes token generated. Copy it now; it cannot be revealed again.", "success");
  } finally {
    button.disabled = false;
  }
}

async function copyHermesToken(button) {
  const token = $("#hermes-token-value").value;
  if (!token) {
    toast("Generate a Hermes token first.", "warning");
    return;
  }
  await navigator.clipboard.writeText(token);
  const original = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = original; }, 1200);
}

async function revokeHermesToken(button) {
  if (!settings?.hermes_token_configured) {
    toast("No Hermes token is configured.", "warning");
    return;
  }
  if (!confirm("Revoke the Hermes connection token and disable Hermes access?")) return;
  button.disabled = true;
  try {
    const result = await request("/api/hermes/token", {method: "DELETE"});
    settings = result.settings || await request("/api/settings");
    $("#hermes-token-value").value = "";
    $("#hermes-token-result").hidden = true;
    fillSettings();
    toast("Hermes access revoked.", "success");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", async event => {
  if (suppressNextClick || window.getSelection()?.toString()) {
    suppressNextClick = false;
    return;
  }
  const link = event.target.closest("[data-link]");
  if (link) {
    event.preventDefault();
    navigate(link.getAttribute("href"));
    return;
  }
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  try {
    if (action === "add-miner") openMinerDialog();
    if (action === "edit-miner") openMinerDialog(target.dataset.id);
    if (action === "delete-miner") await deleteMiner(target.dataset.id);
    if (action === "move-miner") await moveMiner(target.dataset.id, target.dataset.direction);
    if (action === "open-miner") navigate(`/miners/${encodeURIComponent(target.dataset.id)}`);
    if (action === "add-pool") openPoolDialog();
    if (action === "edit-pool") openPoolDialog(target.dataset.id);
    if (action === "delete-pool") await deletePool(target.dataset.id);
    if (action === "close-dialog") target.closest("dialog").close();
    if (action === "filter-group") { activeGroup = target.dataset.group; renderMiners(); }
    if (action === "filter-odds") { activeOddsCoin = target.dataset.coin || "all"; renderOdds(); }
    if (action === "poll-now") {
      target.disabled = true;
      await request("/api/poll-now", {method: "POST"});
      await refresh();
      toast("Fleet poll complete.", "success");
      target.disabled = false;
    }
    if (action === "test-discord") await testDiscord(target);
    if (action === "clear-alerts") await clearRecentAlerts(target);
    if (action === "snooze-alerts") await setAlertSnooze(Number(target.dataset.seconds));
    if (action === "resume-alerts") await resumeAlerts();
    if (action === "generate-hermes-token") await generateHermesToken(target);
    if (action === "copy-hermes-token") await copyHermesToken(target);
    if (action === "revoke-hermes-token") await revokeHermesToken(target);
    if (action === "test-external-pool") await testExternalPool();
    if (action === "toggle-ip-privacy") {
      setIpPrivacy(!hideIps);
      renderAll();
      return;
    }
    if (action === "detect-public-pool") {
      const form = $("#pool-form");
      target.disabled = true;
      $("#pool-detect-result").innerHTML = `<span class="testing-pulse"></span> Looking at miner pool connections…`;
      const result = await request("/api/pools/discover", {method: "POST", body: {host: null}});
      if (result.ok) {
        form.elements.api_url.value = result.api_url;
        $("#pool-detect-result").innerHTML = `<strong class="good">Self-hosted Public Pool found</strong><span>${privateMarkup(result.api_url)} · ${number(result.total_miners, 0)} connected miners</span>`;
      } else {
        $("#pool-detect-result").innerHTML = `<strong class="bad">Local Public Pool API not found</strong><span>Enter the self-hosted pool HTTP API URL manually. External stratum URLs are not stats APIs.</span>`;
      }
      target.disabled = false;
    }
    if (action === "test-miner") {
      const form = $("#miner-form");
      const ip = form.elements.ip.value.trim();
      if (!ip) { form.elements.ip.reportValidity(); return; }
      target.disabled = true;
      $("#miner-test-result").innerHTML = `<span class="testing-pulse"></span> Contacting miner…`;
      const result = await request("/api/miners/test", {method: "POST", body: {ip, type: form.elements.type.value}});
      $("#miner-test-result").innerHTML = result.ok
        ? `<strong class="good">Connection successful</strong><span>${escapeHtml(result.status.firmware || minerTypeLabel(result.status.type))} · ${number(result.status.hashrate_ths, 3)} TH/s · ${number(result.status.ping_ms)} ms</span>`
        : `<strong class="bad">Could not reach this API</strong><span>${escapeHtml(result.error || result.status?.warnings?.[0] || "Check the address and miner type.")}</span>`;
      target.disabled = false;
    }
  } catch (error) {
    target.disabled = false;
    toast(error.message, "error");
  }
});

document.addEventListener("keydown", event => {
  if (event.target.id === "pool-test-target" && event.key === "Enter") {
    event.preventDefault();
    $("#pool-test-button")?.click();
    return;
  }
  const card = event.target.closest?.('[data-action="open-miner"]');
  if (card && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    navigate(`/miners/${encodeURIComponent(card.dataset.id)}`);
  }
});

$("#miner-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value,
    ip: form.elements.ip.value,
    type: form.elements.type.value,
    group: form.elements.group.value || "Ungrouped",
    enabled: form.elements.enabled.checked,
    display_order: Number(form.elements.display_order.value || 0),
    min_hashrate_ths: nullableNumber(form.elements.min_hashrate_ths.value),
    temp_warning_c: nullableNumber(form.elements.temp_warning_c.value),
    temp_critical_c: nullableNumber(form.elements.temp_critical_c.value),
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    const result = await request(id ? `/api/miners/${encodeURIComponent(id)}` : "/api/miners", {method: id ? "PUT" : "POST", body: payload});
    $("#miner-dialog").close();
    await Promise.all([loadManagement(), refresh()]);
    toast(id ? "Miner updated." : "Miner added to your fleet.", "success");
    if (!id && result.miner?.id) navigate(`/miners/${encodeURIComponent(result.miner.id)}`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#pool-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value,
    type: form.elements.type.value,
    mode: form.elements.mode.value,
    enabled: form.elements.enabled.checked,
    log_path: form.elements.log_path.value,
    api_url: form.elements.api_url.value,
    bitcoin_address: form.elements.bitcoin_address.value,
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    await request(id ? `/api/pools/${encodeURIComponent(id)}` : "/api/pools", {method: id ? "PUT" : "POST", body: payload});
    $("#pool-dialog").close();
    toast(id ? "Local pool monitor updated." : "Local pool monitor added.", "success");
    loadManagement().catch(error => toast(`Saved, but refresh failed: ${error.message}`, "warning"));
    refresh().catch(() => {});
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    poll_interval_seconds: Number(form.elements.poll_interval_seconds.value),
    dashboard_port: Number(form.elements.dashboard_port.value),
    alert_cooldown_seconds: Number(form.elements.alert_cooldown_seconds.value),
    offline_alert_grace_seconds: Number(form.elements.offline_alert_grace_seconds.value),
    request_timeout_seconds: Number(form.elements.request_timeout_seconds.value),
    dashboard_density: form.elements.dashboard_density.value,
    difficulty_rain_enabled: form.elements.difficulty_rain_enabled.checked,
    dashboard_base_url: form.elements.dashboard_base_url.value || null,
    lan_access_enabled: form.elements.lan_access_enabled.checked,
    discord_enabled: form.elements.discord_enabled.checked,
    webhook_url: form.elements.webhook_url.value || null,
    clear_webhook: form.elements.clear_webhook.checked,
    send_offline_alerts: form.elements.send_offline_alerts.checked,
    send_recovery_alerts: form.elements.send_recovery_alerts.checked,
    send_hashrate_alerts: form.elements.send_hashrate_alerts.checked,
    send_temperature_alerts: form.elements.send_temperature_alerts.checked,
    send_chip_health_alerts: form.elements.send_chip_health_alerts.checked,
    send_best_diff_alerts: form.elements.send_best_diff_alerts.checked,
    send_block_found_alerts: form.elements.send_block_found_alerts.checked,
    send_pool_alerts: form.elements.send_pool_alerts.checked,
    send_pool_switch_alerts: form.elements.send_pool_switch_alerts.checked,
    send_share_alerts: form.elements.send_share_alerts.checked,
    verbose_pool_events: form.elements.verbose_pool_events.checked,
    hermes_enabled: form.elements.hermes_enabled.checked,
    btc_enabled: form.elements.btc_enabled.checked,
    bch_enabled: form.elements.bch_enabled.checked,
    bsv_enabled: form.elements.bsv_enabled.checked,
    xec_enabled: form.elements.xec_enabled.checked,
    dgb_enabled: form.elements.dgb_enabled.checked,
    chta_enabled: form.elements.chta_enabled.checked,
    auto_network_data: form.elements.auto_network_data.checked,
    manual_btc_network_hashrate_eh: nullableNumber(form.elements.manual_btc_network_hashrate_eh.value),
    manual_bch_network_hashrate_eh: nullableNumber(form.elements.manual_bch_network_hashrate_eh.value),
    manual_bsv_network_hashrate_eh: nullableNumber(form.elements.manual_bsv_network_hashrate_eh.value),
    manual_xec_network_hashrate_eh: nullableNumber(form.elements.manual_xec_network_hashrate_eh.value),
    manual_dgb_network_hashrate_eh: nullableNumber(form.elements.manual_dgb_network_hashrate_eh.value),
    manual_chta_network_hashrate_eh: nullableNumber(form.elements.manual_chta_network_hashrate_eh.value),
  };
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try {
    const result = await request("/api/settings", {method: "PUT", body: payload});
    if (result.settings) {
      settings = result.settings;
      fillSettings();
    } else {
      await loadSettings();
    }
    await refresh();
    form.elements.webhook_url.value = "";
    form.elements.clear_webhook.checked = false;
    toast(result.restart_required ? "Settings saved. Restart PoCiSys to apply the network change." : "Settings saved.", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

$("#pool-form").elements.mode.addEventListener("change", syncPoolFields);

document.addEventListener("pointerdown", event => {
  pointerStart = {x: event.clientX, y: event.clientY};
  suppressNextClick = false;
}, true);

document.addEventListener("pointermove", event => {
  if (!pointerStart) return;
  if (Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 6) {
    suppressNextClick = true;
  }
}, true);

document.addEventListener("pointerup", () => {
  pointerStart = null;
  setTimeout(() => {
    suppressNextClick = false;
  }, 0);
}, true);

window.addEventListener("popstate", route);
window.addEventListener("resize", () => {
  difficultyRain.width = 0;
  updateDifficultyRain();
});
document.addEventListener("visibilitychange", updateDifficultyRain);
setIpPrivacy(hideIps);
route();
Promise.all([refresh(), loadManagement(), loadSettings()]).catch(error => toast(error.message, "error"));
setInterval(refresh, 5000);
setInterval(updateSnoozeControls, 1000);
