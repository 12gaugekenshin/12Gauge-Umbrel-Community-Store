const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, char => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]
));

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

function miningTargetLabel(value) {
  const target = String(value || "btc").toLowerCase();
  return target === "bch" ? "BCH Solo" : target === "pool" ? "Pool Mining" : "BTC Solo";
}

function miningTargetClass(value) {
  const target = String(value || "btc").toLowerCase();
  return target === "bch" ? "target-bch" : target === "pool" ? "target-pool" : "target-btc";
}

function fleetGroup(miner) {
  const group = String(miner?.group || "").trim();
  const automaticNames = ["", "ungrouped", "btc solo", "bch solo", "pool", "pool mining"];
  return automaticNames.includes(group.toLowerCase()) ? miningTargetLabel(miner?.mining_target) : group;
}

function groupTargetClass(group) {
  const normalized = String(group || "").toLowerCase();
  return normalized === "bch solo" ? "target-bch" : normalized === "pool mining" ? "target-pool" : normalized === "btc solo" ? "target-btc" : "";
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
