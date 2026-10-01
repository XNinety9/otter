"use strict";

const state = {
  devices: new Map(),
  firmwares: [],
  selected: new Set(),
  rollouts: [],
  interval: 30,
  maxAttempts: 3,
  filter: { q: "", show: "all", tag: "" },
};

const $ = (sel, el = document) => el.querySelector(sel);
const ACTIVE = new Set(["pending", "downloading", "rebooting"]);

// --- Helpers -----------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    goToLogin();
    throw new Error("session expired");
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const { detail } = await res.json();
      msg = typeof detail === "string" ? detail : detail.map((d) => d.msg.replace(/^Value error, /, "")).join(", ");
    } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function goToLogin() {
  location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.search)}`;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 5000);
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const label = (d) => d.name || d.mac;

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// Same rule as the server: a device that said it would sleep may stay silent that long.
function silenceAllowed(d) {
  const base = state.interval * 2.5 + 5;
  return d.next_checkin_s ? Math.max(base, d.next_checkin_s * 1.25 + 60) : base;
}

function isOnline(d) {
  return Date.now() - Date.parse(d.last_seen) < silenceAllowed(d) * 1000;
}

// --- Device credentials (per-device tokens) ---------------------------------------

const AUTH_CHIPS = { revoked: "⛔ revoked", awaiting_approval: "awaiting approval", token_lost: "🔑 lost its token" };
const AUTH_TEXT = {
  token: "Uses its own token: the fleet key alone can't act as this device.",
  fleet_key: "Uses the fleet key (agent without token support, or not enrolled yet).",
  revoked: "Revoked: its check-ins are refused until you re-enroll it.",
  awaiting_approval: "New device awaiting your approval: its check-ins are refused until then.",
  token_lost: "Checks in with the fleet key only, though it had its own token: refused. Erased or re-flashed? " +
    "Re-enroll it. If not, someone else may be using the fleet key.",
};

// Same list as the server: restarts that mean the firmware crashed or the power is weak.
const CRASH_RESETS = new Set(["panic", "int_watchdog", "task_watchdog", "watchdog", "brownout", "power_glitch", "cpu_lockup"]);
const crashed = (d) => CRASH_RESETS.has(d.reset_reason);

function lastReset(d) {
  if (!d.reset_reason) return "—";
  const reason = d.reset_reason.replaceAll("_", " ");
  return d.boot_count == null ? reason : `${reason} · boot #${d.boot_count}`;
}

// Same rule as the server: an unknown OTA slot size is trusted.
function fits(d, fw) {
  return d.ota_slot_size == null || fw.size <= d.ota_slot_size;
}

function bytes(n) {
  return n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(2)} MB`;
}

// --- Devices -----------------------------------------------------------------

const CHIP_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
  <rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/></svg>`;

function createRow(id) {
  const tr = document.createElement("tr");
  tr.dataset.id = id;
  tr.innerHTML = `
    <td class="check"><input type="checkbox" class="sel" aria-label="Select"></td>
    <td class="who"><div class="who-wrap"><span class="avatar" aria-hidden="true">${CHIP_ICON}</span><div>
      <button class="name link" title="Details and history"></button><div class="mac mono muted"></div><div class="row-tags"></div>
    </div></div></td>
    <td><div class="app"></div><div class="hw"></div></td>
    <td class="fw mono"></td>
    <td class="ip mono"></td>
    <td><span class="signal"><i></i><i></i><i></i><i></i></span></td>
    <td class="seen"><span class="dot"></span><span class="ago"></span></td>
    <td class="update"><div class="bar" hidden><div class="fill"></div></div><div class="ustate"></div></td>
    <td class="actions">
      <button class="cancel ghost" hidden>Cancel</button>
      <button class="forget ghost danger" title="Forget device">✕</button>
    </td>`;

  $(".sel", tr).addEventListener("change", (e) => {
    e.target.checked ? state.selected.add(id) : state.selected.delete(id);
    renderToolbar();
  });
  // The whole row opens the details, except its own controls.
  tr.addEventListener("click", (e) => {
    if (!e.target.closest("input, button:not(.name), select, a")) openPanel(id);
  });
  $(".cancel", tr).addEventListener("click", async () => {
    const dep = state.devices.get(id).last_deployment;
    try { await api("POST", `/api/deployments/${dep.id}/cancel`); } catch (e) { toast(e.message, "err"); }
  });
  $(".forget", tr).addEventListener("click", () => forgetDevice(id));
  return tr;
}

async function renameDevice(id) {
  const d = state.devices.get(id);
  const name = prompt(`Name for ${d.mac}`, d.name || "");
  if (name === null) return;
  try { await api("PATCH", `/api/devices/${id}`, { name }); } catch (e) { toast(e.message, "err"); }
}

async function forgetDevice(id) {
  const d = state.devices.get(id);
  if (!confirm(`Forget ${label(d)}? It will reappear at its next check-in.`)) return;
  try { await api("DELETE", `/api/devices/${id}`); } catch (e) { toast(e.message, "err"); }
}

function updateStatusText(d) {
  const dep = d.last_deployment;
  if (!dep) return "";
  const v = dep.firmware.version;
  switch (dep.status) {
    case "pending":
      return dep.attempts > 1
        ? `retry ${dep.attempts}/${state.maxAttempts} of ${v} soon · ${dep.error}`
        : `waiting for check-in → ${v}`;
    case "downloading": {
      const attempt = dep.attempts > 1 ? ` · attempt ${dep.attempts}/${state.maxAttempts}` : "";
      return `downloading ${v} · ${dep.progress}%${attempt}`;
    }
    case "rebooting": return `rebooting into ${v}…`;
    case "success": return `✓ updated to ${v} · ${ago(dep.updated_at)}`;
    case "failed": return `✗ ${v} failed: ${dep.error || "unknown error"}`;
    case "cancelled": return `${v} cancelled`;
    case "queued": return `queued for ${v} · rollout stage ${dep.stage + 1}`;
  }
}

// The exact chip ("ESP32-C6FH4 (QFN32)"), or the family's name for agents that don't report it.
const chipName = (d) => d.chip || d.hw.toUpperCase().replace(/^ESP32(?=\w)/, "ESP32-");
// Without the package, for the device list: "ESP32-C6FH4".
const shortChip = (d) => chipName(d).replace(/ \(.*\)$/, "");
// Flash and PSRAM sizes come in powers of two: "4 MB", "512 KB".
const memSize = (n) => (n >= 1024 * 1024 ? `${+(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);
// "4 MB + 8 MB PSRAM" ("4 MB flash + …" with flashLabel), "" when the agent doesn't report it.
const memory = (d, flashLabel = "") =>
  [d.flash_size && memSize(d.flash_size) + flashLabel, d.psram_size && `${memSize(d.psram_size)} PSRAM`]
    .filter(Boolean)
    .join(" + ");

function updateRow(tr, d) {
  const dep = d.last_deployment;
  const active = dep && ACTIVE.has(dep.status);

  $(".sel", tr).checked = state.selected.has(d.id);
  $(".name", tr).textContent = label(d);
  $(".mac", tr).textContent = d.name ? d.mac : "";
  $(".row-tags", tr).innerHTML = d.tags.map((t) => `<span class="tag-chip">#${esc(t)}</span>`).join("") +
    (d.channel ? `<span class="chan-chip" title="Follows the ${esc(d.channel)} channel">⇢ ${esc(d.channel)}</span>` : "") +
    (AUTH_CHIPS[d.auth] ? `<span class="auth-chip ${d.auth}">${AUTH_CHIPS[d.auth]}</span>` : "") +
    (crashed(d) ? `<span class="crash-chip" title="Last restart: ${esc(lastReset(d))}">⚠ ${esc(d.reset_reason.replaceAll("_", " "))}</span>` : "");
  $(".app", tr).textContent = d.app;
  const hw = $(".hw", tr);
  hw.textContent = [shortChip(d), memory(d)].filter(Boolean).join(" · ");
  hw.title = `${chipName(d)}${d.chip_rev ? `, revision ${d.chip_rev}` : ""} · firmware images for ${d.hw}`;
  tr.dataset.status = deviceStatus(d)[0];
  const newest = latestVersions().get(`${d.app}/${d.hw}`);
  $(".fw", tr).innerHTML = active
    ? `${esc(d.fw_version)} <span class="target">→ ${esc(dep.firmware.version)}</span>`
    : esc(d.fw_version) + (newest && compareVersions(d.fw_version, newest) < 0
      ? ` <span class="newer" title="${esc(newest)} is available">↑ ${esc(newest)}</span>` : "");
  $(".ip", tr).textContent = d.ip || "—";

  const bars = d.rssi == null ? 0 : d.rssi > -55 ? 4 : d.rssi > -65 ? 3 : d.rssi > -75 ? 2 : 1;
  const signal = $(".signal", tr);
  signal.title = d.rssi == null ? "unknown" : `${d.rssi} dBm`;
  signal.querySelectorAll("i").forEach((el, i) => el.classList.toggle("on", i < bars));

  const cell = $(".update", tr);
  cell.className = `update${dep ? ` st-${dep.status}` : ""}`;
  $(".bar", cell).hidden = !(active || dep?.status === "success");
  const fill = $(".fill", cell);
  // The pending bar is full-width stripes: leaving it, like starting a new deployment,
  // must jump to the real progress instead of animating backwards.
  const barKey = dep ? `${dep.id}:${dep.status === "pending"}` : "";
  if (fill.dataset.key !== barKey) {
    fill.dataset.key = barKey;
    fill.style.transition = "none";
    fill.style.width = `${dep ? dep.progress : 0}%`;
    void fill.offsetWidth;  // apply the jump before re-enabling transitions
    fill.style.transition = "";
  } else {
    fill.style.width = `${dep ? dep.progress : 0}%`;
  }
  $(".ustate", cell).textContent = updateStatusText(d);
  $(".cancel", tr).hidden = !active;

  refreshSeen(tr, d);
}

function refreshSeen(tr, d) {
  const online = isOnline(d);
  tr.classList.toggle("offline", !online);
  const seen = $(".seen", tr);
  seen.classList.toggle("online", online);
  seen.title = new Date(d.last_seen).toLocaleString();
  $(".ago", seen).textContent = ago(d.last_seen);
}

function sortedDevices() {
  return [...state.devices.values()].sort(
    (a, b) => a.app.localeCompare(b.app) || label(a).localeCompare(label(b)),
  );
}

// --- Filters -----------------------------------------------------------------

// Same ordering as the server (otter/versions.py): 1.10.0 > 1.9.0, 2.0.0-beta < 2.0.0, v1.2 == 1.2.0.
function parseVersion(v) {
  const [core, pre = ""] = v.trim().replace(/^[vV]/, "").split("+")[0].split(/-(.*)/s);
  const part = (p) => (/^\d+$/.test(p) ? [0, Number(p), ""] : [1, 0, p]);
  const numbers = core.split(".").map(part);
  while (numbers.length < 3) numbers.push([0, 0, ""]);
  return { numbers, pre: pre ? pre.split(".").map(part) : null };
}

function compareParts(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (!a[i]) return -1;
    if (!b[i]) return 1;
    for (let j = 0; j < 3; j++) {
      if (a[i][j] < b[i][j]) return -1;
      if (a[i][j] > b[i][j]) return 1;
    }
  }
  return 0;
}

function compareVersions(a, b) {
  const x = parseVersion(a), y = parseVersion(b);
  const c = compareParts(x.numbers, y.numbers);
  if (c || (!x.pre && !y.pre)) return c;
  if (!x.pre) return 1;  // a release comes after its pre-releases
  if (!y.pre) return -1;
  return compareParts(x.pre, y.pre);
}

// Release channels: a device follows its channel and "stable".
const followsChannel = (d, channel) => Boolean(d.channel) && (channel === "stable" || d.channel === channel);

function knownChannels() {
  const names = new Set(["stable", "beta"]);
  for (const x of [...state.devices.values(), ...state.firmwares]) if (x.channel) names.add(x.channel);
  return [...names].sort((a, b) => (a === "stable" ? -1 : b === "stable" ? 1 : a.localeCompare(b)));
}

function channelOptions(current, none) {
  const names = knownChannels();
  if (current && !names.includes(current)) names.push(current);
  return `<option value="">${none}</option>` +
    names.map((c) => `<option value="${esc(c)}"${c === current ? " selected" : ""}>${esc(c)}</option>`).join("") +
    `<option value="__other">Other…</option>`;
}

// Asks for a channel name when "Other…" is picked; null = cancelled.
function pickedChannel(select) {
  if (select.value !== "__other") return select.value || null;
  const name = prompt("Channel name (a-z, 0-9, - or _):");
  return name && name.trim() ? name.trim().toLowerCase() : undefined;
}

// Newest firmware version per app/hw, to spot outdated devices.
function latestVersions() {
  const latest = new Map();
  for (const f of state.firmwares) {
    const key = `${f.app}/${f.hw}`;
    if (!latest.has(key) || compareVersions(f.version, latest.get(key)) > 0) latest.set(key, f.version);
  }
  return latest;
}

const FILTERS = {
  all: { label: "All", test: () => true },
  online: { label: "Online", test: (d) => isOnline(d) },
  offline: { label: "Offline", test: (d) => !isOnline(d) },
  updating: { label: "Updating", test: (d) => ACTIVE.has(d.last_deployment?.status) },
  failed: { label: "Failed", test: (d) => d.last_deployment?.status === "failed" },
  outdated: {
    label: "Outdated",
    test: (d, latest) => {
      const newest = latest.get(`${d.app}/${d.hw}`);
      return newest !== undefined && compareVersions(d.fw_version, newest) < 0;
    },
  },
};

function matchesSearch(d, q) {
  if (!q) return true;
  return [d.name, d.mac, d.ip, d.app, d.hw, d.chip, d.fw_version, d.channel, ...d.tags].some((v) => v && v.toLowerCase().includes(q));
}

const matchesTag = (d) => !state.filter.tag || d.tags.includes(state.filter.tag);

// Devices matching the search box and the tag, before the status chips.
function searchedDevices() {
  const q = state.filter.q.trim().toLowerCase();
  return sortedDevices().filter((d) => matchesSearch(d, q) && matchesTag(d));
}

function visibleDevices() {
  const latest = latestVersions();
  const test = FILTERS[state.filter.show].test;
  return searchedDevices().filter((d) => test(d, latest));
}

// Tags in use, with their device counts.
function tagCounts() {
  const counts = new Map();
  for (const d of state.devices.values()) for (const t of d.tags) counts.set(t, (counts.get(t) || 0) + 1);
  return new Map([...counts].sort(([a], [b]) => a.localeCompare(b)));
}

function renderTagFilter() {
  const counts = tagCounts();
  const select = $("#tag-filter");
  select.innerHTML = `<option value="">All tags</option>` +
    [...counts].map(([t, n]) => `<option value="${esc(t)}">#${esc(t)} (${n})</option>`).join("") +
    // Keep a tag from the URL selectable even if no device carries it (yet).
    (state.filter.tag && !counts.has(state.filter.tag) ? `<option value="${esc(state.filter.tag)}">#${esc(state.filter.tag)} (0)</option>` : "");
  select.value = state.filter.tag;
  $("#known-tags").innerHTML = [...counts.keys()].map((t) => `<option value="${esc(t)}">`).join("");
}

// Deploy only ever targets devices that are both selected and visible.
function visibleSelected() {
  return visibleDevices().filter((d) => state.selected.has(d.id));
}

function renderFilters() {
  const latest = latestVersions();
  const searched = searchedDevices();
  renderTagFilter();
  const n = (key) => searched.filter((d) => FILTERS[key].test(d, latest)).length;
  const total = n("all"), online = n("online"), updating = n("updating"), outdated = n("outdated"), failed = n("failed");
  const apps = new Set(searched.map((d) => d.app)).size;
  const progress = searched.filter(FILTERS.updating.test).map((d) => d.last_deployment.progress);
  const tiles = [
    ["all", "Devices", total, plural(apps, "app"), ""],
    ["online", "Online", `${online}<small> / ${total}</small>`,
      `<div class="bar"><div class="fill" style="width:${total ? (100 * online) / total : 0}%"></div></div>${
        total - online ? `${total - online} offline` : "all connected"}`, "ok"],
    ["updating", "Updating", updating,
      updating ? `${Math.round(progress.reduce((a, b) => a + b, 0) / updating)} % on average` : "nothing in progress", updating ? "accent" : ""],
    ["outdated", "Outdated", outdated, outdated ? "a newer firmware exists" : "all up to date", outdated ? "warn" : ""],
    ["failed", "Failed", failed, failed ? "last update failed" : "no failed update", failed ? "err" : ""],
  ];
  $(".overview").innerHTML = tiles.map(([key, name, value, foot, tone]) => `
    <button class="tile ${tone}" data-show="${key}" aria-pressed="${state.filter.show === key}">
      <span class="tile-label">${name}</span><span class="tile-value">${value}</span><span class="tile-foot">${foot}</span>
    </button>`).join("");
  $("#device-count").textContent = state.filter.show === "all" ? total : `${n(state.filter.show)} of ${total}`;
}

function setFilter(change) {
  Object.assign(state.filter, change);
  const params = new URLSearchParams();
  if (state.filter.q) params.set("q", state.filter.q);
  if (state.filter.show !== "all") params.set("show", state.filter.show);
  if (state.filter.tag) params.set("tag", state.filter.tag);
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
  renderDevices();
  renderFirmwares();  // "Roll out" follows the tag filter
  renderToolbar();
}

function loadFilterFromUrl() {
  const params = new URLSearchParams(location.search);
  const show = params.get("show");
  state.filter.q = params.get("q") || "";
  state.filter.show = Object.hasOwn(FILTERS, show ?? "") ? show : "all";
  state.filter.tag = (params.get("tag") || "").toLowerCase();
  $("#search").value = state.filter.q;
}

$("#tag-filter").addEventListener("change", (e) => setFilter({ tag: e.target.value }));

$("#search").addEventListener("input", (e) => setFilter({ q: e.target.value }));
$(".overview").addEventListener("click", (e) => {
  const chip = e.target.closest(".tile");
  if (chip) setFilter({ show: chip.dataset.show });
});
$(".clear-filters").addEventListener("click", () => {
  $("#search").value = "";
  setFilter({ q: "", show: "all", tag: "" });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, textarea, select, dialog")) {
    e.preventDefault();
    $("#search").focus();
  }
});

function renderDevices() {
  const tbody = $("#devices tbody");
  const devices = visibleDevices();
  const visible = new Set(devices.map((d) => d.id));
  const rows = new Map([...tbody.rows].map((tr) => [Number(tr.dataset.id), tr]));
  for (const [id, tr] of rows) {
    if (!visible.has(id)) { tr.remove(); rows.delete(id); }
  }
  devices.forEach((d, i) => {
    let tr = rows.get(d.id);
    if (!tr) { tr = createRow(d.id); rows.set(d.id, tr); }
    // Only move rows when the order actually changed, so progress bars keep animating.
    if (tbody.rows[i] !== tr) tbody.insertBefore(tr, tbody.rows[i] || null);
    updateRow(tr, d);
  });
  $("#no-devices").hidden = state.devices.size > 0;
  $("#no-match").hidden = state.devices.size === 0 || devices.length > 0;
  $("#select-all").checked = devices.length > 0 && devices.every((d) => state.selected.has(d.id));
  renderFilters();
  renderSummary();
}

// The tab's title says when updates are running, for a dashboard left in the background.
function renderSummary() {
  const updating = [...state.devices.values()].filter((d) => ACTIVE.has(d.last_deployment?.status)).length;
  document.title = updating ? `Otter · ${updating} updating` : "Otter";
}

function renderToolbar() {
  const selected = visibleSelected();
  const hws = new Set(selected.map((d) => d.hw));
  const select = $("#deploy-fw");
  const previous = select.value;
  const choices = state.firmwares.filter((f) => hws.size === 0 || (hws.size === 1 && hws.has(f.hw)));
  const tooBig = (f) => selected.some((d) => !fits(d, f));

  select.innerHTML = choices.length
    ? choices.map((f) => `<option value="${f.id}"${tooBig(f) ? " disabled" : ""}>${esc(f.app)} ${esc(f.version)} (${esc(f.hw)})${
      tooBig(f) ? " · too big for the OTA slot" : ""}</option>`).join("")
    : `<option value="">${hws.size > 1 ? "mixed hardware selected" : "no compatible firmware"}</option>`;
  const usable = choices.filter((f) => !tooBig(f));
  if (usable.some((f) => String(f.id) === previous)) select.value = previous;
  else if (usable.length) select.value = String(usable[0].id);

  document.querySelectorAll(".selection-cmd").forEach((b) => { b.hidden = selected.length === 0; });
  const btn = $("#deploy-btn");
  btn.disabled = selected.length === 0 || !usable.length;
  btn.textContent = selected.length ? `Deploy to ${selected.length}` : "Deploy";
}

async function deploy(firmwareId, targets, n, tag = "") {
  const fw = state.firmwares.find((f) => f.id === firmwareId);
  const where = tag ? ` tagged #${tag}` : "";
  if (!confirm(`Deploy ${fw.app} ${fw.version} to ${n} device${n === 1 ? "" : "s"}${where}?`)) return;
  try {
    await api("POST", "/api/deployments", { firmware_id: firmwareId, ...targets });
    state.selected.clear();
    renderDevices();
    renderToolbar();
  } catch (e) {
    toast(e.message, "err");
  }
}

// --- Device panel ------------------------------------------------------------

const panel = $("#device-panel");
let panelDeviceId = null;
let panelHistoryKey = null;

function openPanel(id) {
  panelDeviceId = id;
  panelHistoryKey = null;
  $(".history tbody", panel).innerHTML = "";
  panelCommands = [];
  renderCommands();
  panelConfig = null;
  renderPanel();
  panel.showModal();
  panel.focus();  // not its first button, which would show a focus ring
  loadCommands(id);
  loadLogs(id);
  historyData = null;
  panel.querySelectorAll(".history-charts .plot").forEach((p) => { p.innerHTML = ""; });
  loadDeviceHistory(id);
  loadConfig(id, "device");
  loadCrashes(id);
}

// --- Crash reports ------------------------------------------------------------------

async function loadCrashes(id) {
  let crashes;
  try {
    crashes = await api("GET", `/api/devices/${id}/crashes`);
  } catch {
    return;
  }
  if (id !== panelDeviceId) return;
  $(".crashes-empty", panel).hidden = crashes.length > 0;
  $(".crash-list", panel).innerHTML = crashes.map((c, i) => `
    <details${i === 0 ? " open" : ""}>
      <summary>${ago(c.created_at)} · ${esc(c.fw_version || "unknown version")}${c.task ? ` · task ${esc(c.task)}` : ""} ·
        <span class="reason">${esc(c.reason || "crash")}</span></summary>
      <ol>${c.frames.map((f) => `<li class="${esc(f.kind)}" title="${f.kind === "stack" ? "found on the stack: probably a caller" : esc(f.kind)}">${
        f.function ? `${esc(f.function)}+${f.offset}` : esc(f.address)}${
        f.file ? ` <span class="where">${esc(f.file)}:${f.line}</span>` : ""}</li>`).join("")}</ol>
      ${c.decoded ? "" : `<p class="hint">Upload this firmware's ELF file to see function names and lines.</p>`}
    </details>`).join("");
}

// --- Remote configuration -------------------------------------------------------

let panelConfig = null; // GET /api/devices/{id}/config

async function loadConfig(id, scope = null) {
  try {
    const config = await api("GET", `/api/devices/${id}/config`);
    if (id !== panelDeviceId) return;
    panelConfig = config;
    renderConfig();
    if (scope) await loadConfigScope(scope);
  } catch {}
}

function renderConfig() {
  const d = state.devices.get(panelDeviceId);
  const c = panelConfig;
  if (!d || !c) return;
  const stateEl = $(".config-state", panel);
  const keys = Object.keys(c.values);
  let text, cls = "";
  if (d.config_version == null) {
    [text, cls] = ["This firmware doesn't support remote configuration.", "unsupported"];
  } else if (d.config_version === c.version) {
    text = keys.length ? "✓ In sync: the device has these values." : "No configuration.";
  } else {
    [text, cls] = ["Waiting for the device to fetch these values…", "pending"];
  }
  stateEl.textContent = text;
  stateEl.className = `config-state ${cls}`;
  $(".config-table tbody", panel).innerHTML = keys.map((k) => `
    <tr><td class="key">${esc(k)}</td><td>${esc(JSON.stringify(c.values[k]))}</td>
      <td class="src">${c.sources[k] === "device" ? "this device" : esc(c.sources[k])}</td></tr>`).join("");

  const select = $(".config-edit select", panel);
  const scopes = ["device", ...d.tags.map((t) => `#${t}`)];
  if (select.dataset.scopes !== scopes.join(",")) {
    const previous = select.value;
    select.innerHTML = scopes.map((s) => `<option value="${esc(s)}">${s === "device" ? "this device" : `tag ${esc(s)} (all its devices)`}</option>`).join("");
    select.value = scopes.includes(previous) ? previous : "device";
    select.dataset.scopes = scopes.join(",");
  }
}

function configPath(scope) {
  return scope === "device" ? `/api/devices/${panelDeviceId}/config` : `/api/tags/${encodeURIComponent(scope.slice(1))}/config`;
}

async function loadConfigScope(scope) {
  const values = scope === "device" ? panelConfig.own : (await api("GET", configPath(scope))).values;
  $(".config-edit textarea", panel).value = Object.keys(values).length ? JSON.stringify(values, null, 2) : "{}";
}

$(".config-edit select", panel).addEventListener("change", (e) => loadConfigScope(e.target.value).catch(() => {}));

$(".config-edit", panel).addEventListener("submit", async (e) => {
  e.preventDefault();
  const scope = e.target.scope.value;
  let values;
  try {
    values = JSON.parse(e.target.values.value || "{}");
  } catch {
    return toast("Values must be a JSON object, e.g. {\"interval\": 60}", "err");
  }
  if (typeof values !== "object" || Array.isArray(values) || values === null) return toast("Values must be a JSON object", "err");
  try {
    await api("PUT", configPath(scope), { values });
    toast(`Configuration of ${scope === "device" ? label(state.devices.get(panelDeviceId)) : scope} saved`, "ok");
    await loadConfig(panelDeviceId);
  } catch (err) {
    toast(err.message, "err");
  }
});

// --- Remote commands ----------------------------------------------------------

let panelCommands = [];
const COMMAND_STATUS = {
  queued: "waiting for check-in", sent: "sent, no answer yet", done: "done", failed: "failed", expired: "expired",
};

async function loadCommands(id) {
  try {
    const list = await api("GET", `/api/devices/${id}/commands`);
    if (id === panelDeviceId) {
      panelCommands = list;
      renderCommands();
    }
  } catch {}
}

function renderCommands() {
  $(".command-log", panel).innerHTML = panelCommands.slice(0, 8).map((c) => `
    <li><span class="cmd-name">${esc(c.name)}${c.args ? ` ${esc(JSON.stringify(c.args))}` : ""}</span>
      <span class="${esc(c.status)}">${esc(COMMAND_STATUS[c.status] || c.status)}</span>
      <span class="muted">${ago(c.done_at || c.sent_at || c.created_at)}</span>
      ${c.result ? `<span class="cmd-result">${esc(c.result)}</span>` : ""}</li>`).join("");
}

function commandArrived(c) {
  if (c.device_id !== panelDeviceId) return;
  panelCommands = [c, ...panelCommands.filter((x) => x.id !== c.id)].sort((a, b) => b.id - a.id);
  renderCommands();
}

async function sendCommand(devices, name, args = null) {
  const who = devices.length === 1 ? label(devices[0]) : plural(devices.length, "device");
  if (name === "reboot" && !confirm(`Reboot ${who}?`)) return;
  try {
    const sent = await api("POST", "/api/commands", { device_ids: devices.map((d) => d.id), name, args });
    sent.forEach(commandArrived);
    toast(`${name} sent to ${who}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function accessAction(action, question, done) {
  const d = state.devices.get(panelDeviceId);
  if (question && !confirm(question.replace("{}", label(d)))) return;
  try {
    const updated = await api("POST", `/api/devices/${d.id}/${action}`);
    state.devices.set(updated.id, updated);
    renderPanel();
    renderDevices();
    toast(done.replace("{}", label(d)), "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

$(".access .revoke", panel).addEventListener("click", () => accessAction("revoke",
  "Revoke {}?\n\nIts check-ins will be refused, with its token or the fleet key, until you re-enroll it.", "{} revoked"));
$(".access .reenroll", panel).addEventListener("click", () => accessAction("reenroll",
  "Re-enroll {}?\n\nIts current token stops working; its next check-in with the fleet key gets a new one.", "{} can enroll again"));
$(".access .approve", panel).addEventListener("click", () => accessAction("approve", null, "{} approved"));

$(".command-bar", panel).addEventListener("click", (e) => {
  const btn = e.target.closest(".cmd");
  if (btn) sendCommand([state.devices.get(panelDeviceId)], btn.dataset.cmd);
});

$(".cmd-custom", panel).addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  let args = null;
  if (form.args.value.trim()) {
    try {
      args = JSON.parse(form.args.value);
    } catch {
      return toast("Arguments must be a JSON object, e.g. {\"level\": 2}", "err");
    }
    if (typeof args !== "object" || Array.isArray(args) || args === null) return toast("Arguments must be a JSON object", "err");
  }
  sendCommand([state.devices.get(panelDeviceId)], form.name.value, args);
  form.reset();
});

document.querySelectorAll(".selection-cmd").forEach((btn) =>
  btn.addEventListener("click", () => sendCommand(visibleSelected(), btn.dataset.cmd)));

function duration(from, to) {
  const s = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

function uptime(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

// Overall state shown in the panel's header: [class, text].
function deviceStatus(d) {
  const dep = d.last_deployment;
  if (d.auth === "revoked") return ["err", "Revoked"];
  if (d.auth === "awaiting_approval") return ["warn", "Awaiting approval"];
  if (d.auth === "token_lost") return ["warn", "Lost its token"];
  if (dep && ACTIVE.has(dep.status)) return ["accent", `Updating to ${dep.firmware.version}`];
  if (!isOnline(d)) return ["off", "Offline"];
  return ["ok", d.next_checkin_s ? "Online · sleeps" : "Online"];
}

const SIGNAL_WORDS = ["", "Weak", "Fair", "Good", "Excellent"];
const signalBars = (rssi) => (rssi == null ? 0 : rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : 1);

// A stat tile of the panel: [label, value HTML, footnote HTML, extra class].
const statTile = ([name, value, foot, cls = ""]) => `
  <div class="stat ${cls}"><div class="stat-label">${esc(name)}</div><div class="stat-value">${value}</div><div class="stat-foot">${foot}</div></div>`;

function firmwareTile(d) {
  const dep = d.last_deployment;
  const value = `<span class="mono">${esc(d.fw_version)}</span>`;
  if (dep && ACTIVE.has(dep.status)) {
    return ["Firmware", value, `<div class="bar"><div class="fill" style="width:${dep.progress}%"></div></div>
      <span class="accent">${esc(dep.status)} ${esc(dep.firmware.version)} · ${dep.progress}%</span>`, "busy"];
  }
  const newest = latestVersions().get(`${d.app}/${d.hw}`);
  if (newest && compareVersions(d.fw_version, newest) < 0) return ["Firmware", value, `<span class="accent">${esc(newest)} available</span>`];
  if (dep?.status === "failed") return ["Firmware", value, `<span class="err">last update failed</span>`];
  return ["Firmware", value, newest ? `<span class="ok">✓ up to date</span>` : esc(d.app)];
}

function statTiles(d) {
  const bars = signalBars(d.rssi);
  return [
    firmwareTile(d),
    ["Signal",
      d.rssi == null ? "—" : `<span class="signal big">${[1, 2, 3, 4].map((i) => `<i class="${i <= bars ? "on" : ""}"></i>`).join("")}</span> ${d.rssi} <small>dBm</small>`,
      SIGNAL_WORDS[bars] || "no report", bars === 1 ? "weak" : ""],
    ["Uptime", esc(uptime(d.uptime_s)), crashed(d) ? `<span class="err">⚠ ${esc(lastReset(d))}</span>` : esc(lastReset(d))],
    ["Free memory",
      d.free_heap == null ? "—" : esc(bytes(d.free_heap)),
      d.min_free_heap == null ? "heap" : `lowest ${esc(bytes(d.min_free_heap))}`],
  ].map(statTile).join("");
}

// The OTA slot, with how much of it the running image takes when Otter knows that image.
function slotInfo(d) {
  if (d.ota_slot_size == null) return "—";
  const fw = state.firmwares.find((f) => f.app === d.app && f.hw === d.hw && f.version === d.fw_version);
  if (!fw) return esc(bytes(d.ota_slot_size));
  const pct = Math.min(100, Math.round((fw.size / d.ota_slot_size) * 100));
  return `${esc(bytes(d.ota_slot_size))} <span class="muted">· image ${pct}%</span>
    <div class="meter${pct > 85 ? " full" : ""}"><i style="width:${pct}%"></i></div>`;
}

const infoList = (rows) =>
  rows.filter(Boolean).map(([k, v, cls = ""]) => `<div><dt>${esc(k)}</dt><dd class="${cls}">${v}</dd></div>`).join("");

function renderPanel() {
  const d = state.devices.get(panelDeviceId);
  if (!d) return panel.close();
  const [statusCls, statusText] = deviceStatus(d);
  panel.dataset.status = statusCls;
  $("#panel-title").textContent = label(d);
  $(".status-pill", panel).textContent = statusText;
  $(".panel-sub", panel).innerHTML = [
    `<span class="app-name">${esc(d.app)}</span>`,
    esc(shortChip(d)),
    d.name ? `<span class="mono">${esc(d.mac)}</span>` : "",
    d.ip ? `<span class="mono">${esc(d.ip)}</span>` : "",
  ].filter(Boolean).join('<span class="sep">·</span>');
  $(".stat-row", panel).innerHTML = statTiles(d);
  $(".info-hw", panel).innerHTML = infoList([
    ["Chip", esc(chipName(d))],
    d.chip_rev && ["Revision", esc(`v${d.chip_rev}`)],
    ["Flash", d.flash_size ? esc(memSize(d.flash_size)) : "—"],
    d.psram_size && ["PSRAM", esc(memSize(d.psram_size))],
    d.radio && ["Radio", d.radio.split(", ").map((r) => `<span class="radio">${esc(r)}</span>`).join("")],
    ["OTA slot", slotInfo(d)],
    ["Images built for", `<span class="tag">${esc(d.hw)}</span>`],
  ]);
  $(".info-net", panel).innerHTML = infoList([
    ["IP address", d.ip ? `<span class="mono">${esc(d.ip)}</span>` : "—"],
    ["MAC address", `<span class="mono">${esc(d.mac)}</span>`],
    ["Check-ins", esc(d.next_checkin_s ? `wakes every ${d.next_checkin_s < 120 ? `${d.next_checkin_s} s` : uptime(d.next_checkin_s)}` : "stays connected")],
    ["Last seen", `${esc(ago(d.last_seen))}`],
    ["First seen", esc(new Date(d.first_seen).toLocaleDateString(undefined, { dateStyle: "medium" }))],
    d.boot_count != null && ["Boots", esc(`#${d.boot_count}`)],
  ]);
  const access = $(".access-state", panel);
  access.textContent = AUTH_TEXT[d.auth] || d.auth;
  access.className = `access-state ${d.auth}`;
  $(".access .approve", panel).hidden = d.auth !== "awaiting_approval";
  $(".access .reenroll", panel).hidden = !["token", "revoked", "token_lost"].includes(d.auth);
  $(".access .revoke", panel).hidden = d.auth === "revoked";
  const follow = $(".channel-follow select", panel);
  if (document.activeElement !== follow) follow.innerHTML = channelOptions(d.channel, "Manual updates only");
  $(".tag-list", panel).innerHTML = d.tags.map((t) => `
    <span class="tag-chip">#${esc(t)}<button data-tag="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("");

  // Refetch the history only when the latest deployment changes state.
  const dep = d.last_deployment;
  const key = dep ? `${dep.id}:${dep.status}` : "";
  if (key !== panelHistoryKey) {
    panelHistoryKey = key;
    loadHistory(d.id);
  }
}

async function loadHistory(id) {
  let history;
  try {
    history = await api("GET", `/api/devices/${id}/deployments`);
  } catch (e) {
    return toast(e.message, "err");
  }
  if (id !== panelDeviceId) return;
  $(".history tbody", panel).innerHTML = history.map((h) => `
    <tr>
      <td class="mono">${esc(h.firmware.version)}</td>
      <td><span class="badge ${esc(h.status)}">${esc(h.status)}</span></td>
      <td title="${esc(new Date(h.created_at).toLocaleString())}">${ago(h.created_at)}</td>
      <td>${ACTIVE.has(h.status) ? "—" : duration(h.created_at, h.updated_at)}</td>
      <td class="details">${esc(h.error)}</td>
    </tr>`).join("");
  $(".history-empty", panel).hidden = history.length > 0;
  $(".history", panel).hidden = history.length === 0;
}

$(".close", panel).addEventListener("click", () => panel.close());
$(".rename", panel).addEventListener("click", () => renameDevice(panelDeviceId));
$(".forget", panel).addEventListener("click", () => forgetDevice(panelDeviceId));

async function setTags(id, tags) {
  try {
    await api("PATCH", `/api/devices/${id}`, { tags });
    return true;
  } catch (e) {
    toast(e.message, "err");
    return false;
  }
}

$(".channel-follow select", panel).addEventListener("change", async (e) => {
  const channel = pickedChannel(e.target);
  if (channel === undefined) return renderPanel();
  try {
    await api("PATCH", `/api/devices/${panelDeviceId}`, { channel });
  } catch (err) {
    toast(err.message, "err");
  }
  e.target.blur();
});

$(".tag-add", panel).addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = e.target.tag;
  const tag = input.value.trim();
  if (!tag) return;
  const d = state.devices.get(panelDeviceId);
  if (await setTags(d.id, [...d.tags, tag])) input.value = "";
});
$(".tag-list", panel).addEventListener("click", (e) => {
  const button = e.target.closest("button[data-tag]");
  if (!button) return;
  const d = state.devices.get(panelDeviceId);
  setTags(d.id, d.tags.filter((t) => t !== button.dataset.tag));
});
panel.addEventListener("click", (e) => {
  // Clicks on the backdrop land on the dialog itself, outside its box.
  const r = panel.getBoundingClientRect();
  if (e.target === panel && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) {
    panel.close();
  }
});
panel.addEventListener("close", () => {
  stopWatchingLogs();
  panelDeviceId = null;
});

// --- Firmwares ---------------------------------------------------------------

function renderFirmwares() {
  const tbody = $("#firmwares tbody");
  tbody.innerHTML = state.firmwares.map((f) => `
    <tr data-id="${f.id}">
      <td class="fw-app">${esc(f.app)}</td>
      <td><span class="tag">${esc(f.hw)}</span></td>
      <td><span class="ver">${esc(f.version)}</span>${f.signed ? ` <span class="signed" title="Signed: devices built with the public key check it">🔏</span>` : ""}${
        f.crash_count ? ` <span class="crash-chip" title="Crash reports from devices running it">💥 ${f.crash_count}</span>` : ""}</td>
      <td${f.compressed_size ? ` title="Downloaded compressed: ${bytes(f.compressed_size)}"` : ""}>${bytes(f.size)}${
        f.compressed_size ? `<div class="sub muted">${bytes(f.compressed_size)} zipped</div>` : ""}</td>
      <td class="mono muted" title="${esc(f.sha256)}">${esc(f.sha256.slice(0, 12))}…</td>
      <td title="${esc(new Date(f.uploaded_at).toLocaleString())}">${ago(f.uploaded_at)}</td>
      <td><select class="channel-select" aria-label="Publish on channel">${channelOptions(f.channel, "—")}</select></td>
      <td class="muted notes">${esc(f.notes)}</td>
      <td class="actions"><div class="row-actions">
        ${f.has_factory && installFamily(f.hw) ? `<button class="install" title="Install on a board plugged into this computer">Install…</button>` : ""}
        <button class="staged" title="Release ${esc(f.app)} ${esc(f.version)} progressively, in stages">Staged…</button>
        <button class="rollout" title="Deploy to every ${esc(f.app)} / ${esc(f.hw)} device${state.filter.tag ? ` tagged #${esc(state.filter.tag)}` : ""} not on this version">Roll out${state.filter.tag ? ` to #${esc(state.filter.tag)}` : ""}</button>
        <button class="delete ghost danger" title="Delete">✕</button>
      </div></td>
    </tr>`).join("");
  $("#no-firmwares").hidden = state.firmwares.length > 0;
  $("#firmware-count").textContent = state.firmwares.length || "";

  const known = (key) => [...new Set([...state.devices.values(), ...state.firmwares].map((x) => x[key]))].sort();
  $("#known-apps").innerHTML = known("app").map((v) => `<option value="${esc(v)}">`).join("");
  $("#known-hws").innerHTML = known("hw").map((v) => `<option value="${esc(v)}">`).join("");
  $("#known-channels").innerHTML = knownChannels().map((v) => `<option value="${esc(v)}">`).join("");
}

$("#firmwares tbody").addEventListener("change", async (e) => {
  const select = e.target.closest(".channel-select");
  if (!select) return;
  const fw = state.firmwares.find((f) => f.id === Number(select.closest("tr").dataset.id));
  const channel = pickedChannel(select);
  if (channel === undefined || (channel && !confirmPublish(fw, channel))) return renderFirmwares();
  try {
    await api("PATCH", `/api/firmwares/${fw.id}`, { channel });
    toast(channel ? `${fw.app} ${fw.version} published on ${channel}` : `${fw.app} ${fw.version} unpublished`, "ok");
  } catch (err) {
    toast(err.message, "err");
    renderFirmwares();
  }
});

function confirmPublish(fw, channel) {
  const n = [...state.devices.values()].filter(
    (d) => d.app === fw.app && d.hw === fw.hw && followsChannel(d, channel) && compareVersions(fw.version, d.fw_version) > 0 &&
      fits(d, fw),
  ).length;
  const who = channel === "stable" ? "every device following a channel" : `devices following ${channel}`;
  return confirm(`Publish ${fw.app} ${fw.version} on ${channel}?\n\n${plural(n, "device")} (${who}, on an older version) will be updated right away.`);
}

$("#firmwares tbody").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const fw = state.firmwares.find((f) => f.id === Number(tr.dataset.id));

  if (e.target.closest(".rollout")) {
    const tag = state.filter.tag;
    const outdated = [...state.devices.values()].filter(
      (d) => d.app === fw.app && d.hw === fw.hw && d.fw_version !== fw.version && (!tag || d.tags.includes(tag)),
    );
    const targets = outdated.filter((d) => fits(d, fw));
    const scope = tag ? `${fw.app} device tagged #${tag}` : `${fw.app} device`;
    if (!outdated.length) return toast(`Every ${scope} already runs ${fw.version}.`);
    if (!targets.length) return toast(`${fw.app} ${fw.version} is too big for the OTA slot of every ${scope}.`, "err");
    // With a tag, let the server resolve its members: same result, one source of truth.
    await deploy(fw.id, tag ? { tags: [tag] } : { device_ids: targets.map((d) => d.id) }, targets.length, tag);
  } else if (e.target.closest(".install")) {
    openInstallDialog(fw.id);
  } else if (e.target.closest(".staged")) {
    openRolloutDialog(fw);
  } else if (e.target.closest(".delete")) {
    if (!confirm(`Delete ${fw.app} ${fw.version} (${fw.hw})?`)) return;
    try { await api("DELETE", `/api/firmwares/${fw.id}`); } catch (err) { toast(err.message, "err"); }
  }
});

$("#upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const btn = $("button[type=submit]", form);
  btn.disabled = true;
  try {
    const fw = await api("POST", "/api/firmwares", new FormData(form));
    toast(`Uploaded ${fw.app} ${fw.version} (${fw.hw})`, "ok");
    form.reset();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

// --- Staged rollouts ---------------------------------------------------------

const OPEN_ROLLOUTS = new Set(["running", "paused", "halted"]);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

async function loadRollouts() {
  try {
    state.rollouts = await api("GET", "/api/rollouts?limit=10");
  } catch (e) {
    return toast(e.message, "err");
  }
  renderRollouts();
}

// Device events change rollout stats: refetch, at most twice a second.
let rolloutsTimer = null;
function refreshRolloutsSoon() {
  if (rolloutsTimer) return;
  rolloutsTimer = setTimeout(() => {
    rolloutsTimer = null;
    loadRollouts();
  }, 500);
}

function until(iso) {
  const s = Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function rolloutActions(r) {
  const last = r.current_stage + 1 >= r.stages.length;
  return [
    r.status === "running" && `<button class="ghost" data-action="pause">Pause</button>`,
    r.status === "paused" && `<button class="ghost" data-action="resume">Resume</button>`,
    OPEN_ROLLOUTS.has(r.status) && !last && `<button class="ghost" data-action="advance">Next stage now</button>`,
    OPEN_ROLLOUTS.has(r.status) && `<button class="ghost danger" data-action="abort">Abort</button>`,
  ].filter(Boolean).join("");
}

function renderRollouts() {
  // Everything still open, plus the three most recent finished ones.
  const open = state.rollouts.filter((r) => OPEN_ROLLOUTS.has(r.status));
  const done = state.rollouts.filter((r) => !OPEN_ROLLOUTS.has(r.status)).slice(0, 3);
  const shown = [...open, ...done];
  $("#no-rollouts").hidden = shown.length > 0;
  $("#rollouts").innerHTML = shown.map((r) => {
    const target = r.channel ? `channel ${esc(r.channel)}`
      : r.tags.length ? r.tags.map((t) => `#${esc(t)}`).join(" ") : `all ${esc(r.firmware.app)} devices`;
    const stages = r.stages.map((st, i) => {
      const seg = (cls, n) => (n ? `<i class="${cls}" style="width:${(100 * n) / st.size}%"></i>` : "");
      const current = i === r.current_stage && OPEN_ROLLOUTS.has(r.status);
      return `
        <div class="stage${current ? " current" : ""}">
          <div class="label"><span>Stage ${i + 1} · ${plural(st.size, "device")}</span>
            <span>${st.success}/${st.size}${st.failed ? ` · <span style="color:var(--err)">${st.failed} failed</span>` : ""}</span></div>
          <div class="segments">${seg("success", st.success)}${seg("failed", st.failed)}${seg("active", st.active)}${seg("cancelled", st.cancelled)}</div>
        </div>`;
    }).join("");
    const meta = [
      `started ${ago(r.created_at)}`,
      `soak ${Math.round(r.soak_s / 60)} min`,
      `halts above ${Math.round(r.max_failure_rate * 100)} % failed`,
      r.next_stage_at && `<strong>next stage in ${until(r.next_stage_at)}</strong>`,
    ].filter(Boolean).join(" · ");
    return `
      <article class="rollout card" data-id="${r.id}">
        <div class="rollout-head">
          <span class="title">${esc(r.firmware.app)} <span class="mono">${esc(r.firmware.version)}</span> → ${target}</span>
          <span class="badge ${esc(r.status)}">${esc(r.status)}</span>
          <div class="toolbar">${rolloutActions(r)}</div>
        </div>
        ${r.message ? `<p class="message">⚠ ${esc(r.message)}</p>` : ""}
        <div class="stages">${stages}</div>
        <p class="meta">${meta}</p>
      </article>`;
  }).join("");
}

$("#rollouts").addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.closest(".rollout").dataset.id);
  const { action } = button.dataset;
  if (action === "abort" && !confirm("Abort this rollout? Deployments that haven't started flashing are cancelled.")) return;
  try {
    await api("POST", `/api/rollouts/${id}/${action}`);
  } catch (err) {
    toast(err.message, "err");
  }
});

// Same split as the server (otter/rollouts.py stage_sizes).
function stageSizes(total, percentages) {
  const sizes = [];
  let done = 0;
  for (const pct of percentages) {
    const upto = pct >= 100 ? total : Math.min(total, Math.max(done + 1, Math.floor((total * pct) / 100)));
    if (upto > done) {
      sizes.push(upto - done);
      done = upto;
    }
  }
  return sizes;
}

const rolloutDialog = $("#rollout-dialog");
let rolloutFirmware = null;

function openRolloutDialog(fw) {
  rolloutFirmware = fw;
  const form = $("form", rolloutDialog);
  $("#rollout-title").textContent = `Staged rollout of ${fw.app} ${fw.version} (${fw.hw})`;
  form.target.innerHTML = `<option value="">All ${esc(fw.app)} devices</option>` +
    (tagCounts().size ? `<optgroup label="Tag">${[...tagCounts().keys()].map((t) => `<option value="tag:${esc(t)}">#${esc(t)}</option>`).join("")}</optgroup>` : "") +
    `<optgroup label="Channel (also publishes it there)">${knownChannels().map((c) => `<option value="channel:${esc(c)}">${esc(c)}</option>`).join("")}</optgroup>`;
  form.target.value = state.filter.tag ? `tag:${state.filter.tag}` : "";
  updateRolloutPreview();
  rolloutDialog.returnValue = "";  // otherwise Esc would replay the previous "start"
  rolloutDialog.showModal();
}

function rolloutFormValues() {
  const form = $("form", rolloutDialog);
  return {
    stages: form.stages.value.split(/[\s,;]+/).filter(Boolean).map(Number),
    soak_s: Math.round(Number(form.soak.value) * 60),
    max_failure_rate: Number(form.rate.value) / 100,
    tags: form.target.value.startsWith("tag:") ? [form.target.value.slice(4)] : [],
    channel: form.target.value.startsWith("channel:") ? form.target.value.slice(8) : null,
  };
}

function updateRolloutPreview() {
  const fw = rolloutFirmware;
  const { stages, tags, channel } = rolloutFormValues();
  // Same targeting as the server: channels only move devices forward.
  const targets = [...state.devices.values()].filter(
    (d) => d.app === fw.app && d.hw === fw.hw && d.fw_version !== fw.version &&
      (!tags.length || d.tags.includes(tags[0])) &&
      (!channel || (followsChannel(d, channel) && compareVersions(fw.version, d.fw_version) > 0)) && fits(d, fw),
  );
  const valid = stages.length && stages.every((n, i) => n >= 1 && n <= 100 && (i === 0 || n > stages[i - 1])) && stages.at(-1) === 100;
  const preview = $(".preview", rolloutDialog);
  if (!valid) preview.textContent = "Stages must be increasing percentages ending with 100, e.g. 10, 50, 100.";
  else if (!targets.length) preview.textContent = `No device needs ${fw.version}.`;
  else preview.textContent = `${plural(targets.length, "device")} → stages of ${stageSizes(targets.length, stages).join(", ")}.`;
  $("button[value=start]", rolloutDialog).disabled = !valid || !targets.length;
}

$("form", rolloutDialog).addEventListener("input", updateRolloutPreview);
rolloutDialog.addEventListener("close", async () => {
  if (rolloutDialog.returnValue !== "start") return;
  try {
    await api("POST", "/api/rollouts", { firmware_id: rolloutFirmware.id, ...rolloutFormValues() });
    toast(`Rollout of ${rolloutFirmware.app} ${rolloutFirmware.version} started`, "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});

// --- Wiring ------------------------------------------------------------------

$("#select-all").addEventListener("change", (e) => {
  for (const d of visibleDevices()) e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id);
  renderDevices();
  renderToolbar();
});

$("#deploy-btn").addEventListener("click", () => {
  const ids = visibleSelected().map((d) => d.id);
  deploy(Number($("#deploy-fw").value), { device_ids: ids }, ids.length);
});

async function resync() {
  const [cfg, devices, firmwares, rollouts] = await Promise.all([
    api("GET", "/api/config"),
    api("GET", "/api/devices"),
    api("GET", "/api/firmwares"),
    api("GET", "/api/rollouts?limit=10"),
  ]);
  state.rollouts = rollouts;
  renderRollouts();
  state.interval = cfg.checkin_interval_s;
  state.maxAttempts = cfg.deploy_attempts;
  state.devices = new Map(devices.map((d) => [d.id, d]));
  for (const id of state.selected) if (!state.devices.has(id)) state.selected.delete(id);
  state.firmwares = firmwares;
  renderDevices();
  renderFirmwares();
  renderToolbar();
  if (panelDeviceId !== null) {
    panelHistoryKey = null;  // may have missed events while disconnected
    renderPanel();
  }
}

function announce(prev, d) {
  const before = prev?.last_deployment;
  const after = d.last_deployment;
  if (!after || (before?.id === after.id && before.status === after.status)) return;
  if (after.status === "success") toast(`${label(d)} updated to ${after.firmware.version}`, "ok");
  if (after.status === "failed") toast(`${label(d)}: update failed (${after.error})`, "err");
}

function connect() {
  const live = $("#live");
  const es = new EventSource("/api/events");
  es.addEventListener("open", () => {
    live.textContent = "live";
    live.classList.add("on");
    resync().catch((e) => toast(e.message, "err"));
  });
  es.addEventListener("error", async () => {
    live.textContent = "reconnecting…";
    live.classList.remove("on");
    // The stream also fails when the session ends: go back to the login page then.
    try {
      const status = await (await fetch("/api/auth/status")).json();
      if (!status.user) goToLogin();
    } catch {}
  });
  es.addEventListener("device", (e) => {
    const d = JSON.parse(e.data);
    announce(state.devices.get(d.id), d);
    if (d.last_deployment?.rollout_id) refreshRolloutsSoon();
    state.devices.set(d.id, d);
    renderDevices();
    renderToolbar();
    if (d.id === panelDeviceId) {
      renderPanel();
      renderConfig(); // the device may just have fetched its configuration
    }
  });
  es.addEventListener("device_deleted", (e) => {
    const { id } = JSON.parse(e.data);
    state.devices.delete(id);
    state.selected.delete(id);
    if (id === panelDeviceId) panel.close();
    renderDevices();
    renderToolbar();
  });
  es.addEventListener("firmwares", async () => {
    state.firmwares = await api("GET", "/api/firmwares");
    renderFirmwares();
    renderDevices();  // "outdated" depends on the newest firmware
    renderToolbar();
  });
  es.addEventListener("resync", () => resync());
  es.addEventListener("rollouts", () => loadRollouts());
  es.addEventListener("command", (e) => commandArrived(JSON.parse(e.data)));
  es.addEventListener("logs", (e) => {
    const { device_id, lines } = JSON.parse(e.data);
    if (device_id === panelDeviceId) appendLogs(lines);
  });
  es.addEventListener("crash", (e) => {
    if (JSON.parse(e.data).device_id === panelDeviceId) loadCrashes(panelDeviceId);
    api("GET", "/api/firmwares").then((f) => { state.firmwares = f; renderFirmwares(); }).catch(() => {});
  });
  es.addEventListener("config", (e) => {
    if (JSON.parse(e.data).device_ids.includes(panelDeviceId)) loadConfig(panelDeviceId);
  });
}

// Keep "last seen" and online dots fresh between events.
setInterval(() => {
  for (const tr of $("#devices tbody").rows) {
    const d = state.devices.get(Number(tr.dataset.id));
    if (d) refreshSeen(tr, d);
  }
  renderSummary();
  // Devices drift online/offline as time passes: re-filter when that matters.
  if (state.filter.show === "online" || state.filter.show === "offline") renderDevices();
  else renderFilters();
  if (panelDeviceId !== null) renderPanel();
  if (state.rollouts.some((r) => r.next_stage_at)) renderRollouts();  // countdowns
}, 1000);

$("#logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/login.html";
});

async function start() {
  const status = await (await fetch("/api/auth/status")).json();
  if (!status.user) return goToLogin();
  $("#user").textContent = status.user;
  // A viewer sees everything and changes nothing (the server refuses anyway, #100).
  document.body.classList.toggle("read-only", status.role === "viewer");
  loadFilterFromUrl();
  connect();
}

start();

// --- Installing on a new board (#92) -------------------------------------------
// ESP Web Tools writes a firmware's factory image over Web Serial, then sets the Wi-Fi with
// Improv. Web Serial needs Chrome or Edge, and a secure page: localhost, or HTTPS.

const ESP_WEB_TOOLS = "https://unpkg.com/esp-web-tools@10/dist/web/install-button.js?module";
const installDialog = $("#install-dialog");

// Same rule as the server: "esp32s3", "esp32s3-cam" → ESP32-S3; "esp8266-1m" → ESP8266.
function installFamily(hw) {
  const m = /^esp(8266|32(c2|c3|c5|c6|c61|h2|p4|s2|s3)?)(?![a-z0-9])/i.exec(hw);
  return m && (m[1] === "8266" ? "ESP8266" : `ESP32${m[2] ? `-${m[2].toUpperCase()}` : ""}`);
}

function renderInstallButton() {
  const id = $("#install-fw").value;
  $(".install-action", installDialog).innerHTML = id && !$(".install-unsupported", installDialog).textContent
    ? `<esp-web-install-button manifest="/api/firmwares/${id}/manifest.json">
         <button slot="activate" type="button" class="primary">Install on the board…</button>
       </esp-web-install-button>`
    : "";
}

function openInstallDialog(id) {
  const installable = state.firmwares
    .filter((f) => f.has_factory && installFamily(f.hw))
    .sort((a, b) => a.app.localeCompare(b.app) || a.hw.localeCompare(b.hw) || compareVersions(b.version, a.version));
  $("#install-fw").innerHTML = installable.map((f) =>
    `<option value="${f.id}">${esc(f.app)} ${esc(f.version)} · ${esc(installFamily(f.hw))} (${esc(f.hw)})</option>`).join("");
  if (id) $("#install-fw").value = String(id);
  $("#install-fw").closest("label").hidden = !installable.length;
  $(".install-none", installDialog).hidden = installable.length > 0;
  const unsupported = $(".install-unsupported", installDialog);
  // Insecure pages don't even get navigator.serial: check that first.
  unsupported.textContent = !window.isSecureContext
    ? `USB access needs a secure page: open the dashboard on http://localhost:${location.port || 80} from the computer the board is plugged into, or over HTTPS.`
    : !("serial" in navigator)
      ? "This browser can't reach USB ports: open the dashboard in Chrome or Edge."
      : "";
  unsupported.hidden = !unsupported.textContent;
  if (!unsupported.textContent && !customElements.get("esp-web-install-button")) {
    import(ESP_WEB_TOOLS).catch(() => toast("Couldn't load ESP Web Tools (no Internet access?)", "err"));
  }
  renderInstallButton();
  installDialog.showModal();
}

$("#install-btn").addEventListener("click", () => openInstallDialog());
$("#install-fw").addEventListener("change", renderInstallButton);

// --- Live logs (#96) ---------------------------------------------------------
// The details page asks the device for its logs while it's open (renewed every few minutes),
// and shows the lines as they come over the event stream.

const LOGS_RENEW_MS = 4 * 60 * 1000;
let logsWatching = false;
let logsRenew = null;

const logLevel = (text) => (/^[EWIDV] \(/.test(text) ? text[0] : "");

function logLine({ t, text }) {
  const time = new Date(t * 1000).toLocaleTimeString([], { hour12: false });
  return `<span class="log ${logLevel(text)}"><span class="log-time">${time}</span>${esc(text)}</span>`;
}

function appendLogs(lines) {
  const box = $(".logs", panel);
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  box.insertAdjacentHTML("beforeend", lines.map(logLine).join(""));
  while (box.childElementCount > 500) box.firstElementChild.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
  if (logsWatching) $(".logs-state", panel).textContent = "live";
}

function renderLogsState() {
  $(".logs-toggle", panel).textContent = logsWatching ? "■ Stop" : "▶ Live logs";
  $(".logs-toggle", panel).classList.toggle("on", logsWatching);
  $(".logs", panel).classList.toggle("watching", logsWatching);
}

async function loadLogs(id) {
  $(".logs", panel).innerHTML = "";
  $(".logs-state", panel).textContent = "";
  try {
    const lines = await api("GET", `/api/devices/${id}/logs`);
    if (id === panelDeviceId) appendLogs(lines);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function watchLogs() {
  const id = panelDeviceId;
  try {
    await api("POST", `/api/devices/${id}/logs/watch`);
  } catch (e) {
    toast(e.message, "err");
    return stopWatchingLogs(false);
  }
  if (!logsWatching || id !== panelDeviceId) return;
  if (!$(".logs", panel).childElementCount || $(".logs-state", panel).textContent !== "live") {
    $(".logs-state", panel).textContent = "waiting for the device…";
  }
}

function stopWatchingLogs(tell = true) {
  if (logsWatching && tell && panelDeviceId != null) {
    api("POST", `/api/devices/${panelDeviceId}/logs/stop`).catch(() => {});
  }
  logsWatching = false;
  clearInterval(logsRenew);
  logsRenew = null;
  $(".logs-state", panel).textContent = "";
  renderLogsState();
}

$(".logs-toggle", panel).addEventListener("click", () => {
  if (logsWatching) return stopWatchingLogs();
  logsWatching = true;
  renderLogsState();
  watchLogs();
  logsRenew = setInterval(watchLogs, LOGS_RENEW_MS);
});

// --- History charts (#98) ------------------------------------------------------
// A sample every 5 minutes: signal and free memory, one chart each (never two scales on one),
// restarts marked. Hovering shows the nearest sample.

let historyHours = 24;
let historyData = null;
const SAMPLE_GAP_MS = 16 * 60 * 1000;  // more than 3 missed samples: the device was away
const CHARTS = {
  rssi: { value: (s) => s.rssi, format: (v) => `${v} dBm` },
  free_heap: { value: (s) => (s.free_heap == null ? null : s.free_heap / 1024), format: (v) => `${Math.round(v)} KB` },
};

function niceTicks(min, max, count = 3) {
  const span = max - min || 1;
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000].find((s) => span / s <= count) || span / count;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max; v += step) ticks.push(v);
  return ticks;
}

// Over a week, 5-minute samples are noise: their mean per bucket (a gap still breaks the line).
function averaged(points, bucketMs) {
  const buckets = new Map();
  for (const p of points) {
    const key = Math.floor(p.t / bucketMs);
    const b = buckets.get(key) || buckets.set(key, { t: 0, v: 0, n: 0 }).get(key);
    b.t += p.t; b.v += p.v; b.n += 1;
  }
  return [...buckets.values()].map((b) => ({ t: b.t / b.n, v: b.v / b.n }));
}

function drawChart(figure, samples, restarts, hours) {
  const plot = $(".plot", figure);
  const { value, format } = CHARTS[figure.dataset.key];
  let points = samples.map((s) => ({ t: Date.parse(s.t), v: value(s) })).filter((p) => p.v != null);
  if (hours > 24) points = averaged(points, 30 * 60 * 1000);
  if (points.length < 2) {
    plot.innerHTML = `<p class="chart-empty">${points.length ? "One sample so far" : "No samples yet"}: one comes every 5 minutes.</p>`;
    return;
  }
  const W = Math.max(240, plot.clientWidth), H = 150, L = 40, R = 8, T = 8, B = 22;
  const end = Date.now(), start = end - hours * 3600 * 1000;
  let lo = Math.min(...points.map((p) => p.v)), hi = Math.max(...points.map((p) => p.v));
  const pad = (hi - lo) * 0.15 || Math.abs(hi) * 0.05 || 1;
  lo -= pad; hi += pad;
  const x = (t) => L + ((t - start) / (end - start)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  // Grid and axes: recessive.
  const yTicks = niceTicks(lo, hi).map((v) =>
    `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`);
  const stepH = hours <= 24 ? 6 : 24;
  const xTicks = [];
  const first = new Date(start); first.setMinutes(0, 0, 0);
  for (let t = first.getTime() + 3600e3; t < end; t += 3600e3) {
    const d = new Date(t);
    if (d.getHours() % stepH) continue;
    const text = stepH === 24 ? d.toLocaleDateString([], { weekday: "short" }) : `${String(d.getHours()).padStart(2, "0")}:00`;
    xTicks.push(`<text class="axis" x="${x(t)}" y="${H - 6}" text-anchor="middle">${text}</text>`);
  }
  // The line, broken where the device was away.
  let path = "";
  points.forEach((p, i) => {
    const gap = i === 0 || p.t - points[i - 1].t > (hours > 24 ? 2.5 * 30 * 60 * 1000 : SAMPLE_GAP_MS);
    path += `${gap ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
  });
  const marks = restarts.map((r) => {
    const crash = CRASH_RESETS.has(r.reason);
    const rx = x(Date.parse(r.t));
    return `<line class="restart${crash ? " crash" : ""}" x1="${rx}" x2="${rx}" y1="${T}" y2="${H - B}"><title>Restarted: ${esc(r.reason.replaceAll("_", " "))}</title></line>`;
  });
  plot.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
         aria-label="${esc($("figcaption", figure).textContent)}: from ${format(Math.min(...points.map((p) => p.v)))} to ${format(Math.max(...points.map((p) => p.v)))}, last ${format(points.at(-1).v)}">
      ${yTicks.join("")}${xTicks.join("")}${marks.join("")}
      <path class="line" d="${path}"/>
      <g class="hover" visibility="hidden"><line class="crosshair" y1="${T}" y2="${H - B}"/><circle r="4"/></g>
      <rect class="hit" x="${L}" y="0" width="${W - L - R}" height="${H}"/>
    </svg>
    <div class="chart-tip" hidden></div>`;
  const svg = $("svg", plot), hover = $(".hover", svg), tip = $(".chart-tip", plot);
  $(".hit", svg).addEventListener("mousemove", (e) => {
    const box = svg.getBoundingClientRect();
    const t = start + ((e.clientX - box.left) * (W / box.width) - L) / (W - L - R) * (end - start);
    const p = points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
    const px = x(p.t), py = y(p.v);
    hover.setAttribute("visibility", "visible");
    $(".crosshair", hover).setAttribute("x1", px); $(".crosshair", hover).setAttribute("x2", px);
    $("circle", hover).setAttribute("cx", px); $("circle", hover).setAttribute("cy", py);
    const when = new Date(p.t).toLocaleString([], { weekday: hours > 24 ? "short" : undefined, hour: "2-digit", minute: "2-digit" });
    tip.innerHTML = `<strong>${format(p.v)}</strong> <span>${when}</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(px * (box.width / W), box.width - tip.offsetWidth)}px`;
  });
  $(".hit", svg).addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

function renderHistory() {
  if (!historyData) return;
  const { samples, restarts } = historyData;
  panel.querySelectorAll(".history-charts .chart").forEach((f) => drawChart(f, samples, restarts, historyHours));
  const crashes = restarts.filter((r) => CRASH_RESETS.has(r.reason)).length;
  const plain = restarts.length - crashes;
  $(".chart-legend", panel).innerHTML = [
    plain && `<span class="key"></span> restart${plain > 1 ? `s (${plain})` : ""}`,
    crashes && `<span class="key crash"></span> ⚠ restart after a crash${crashes > 1 ? `es (${crashes})` : ""}`,
  ].filter(Boolean).join(" · ") || "No restart in this period.";
}

async function loadDeviceHistory(id) {
  try {
    const data = await api("GET", `/api/devices/${id}/history?hours=${historyHours}`);
    if (id !== panelDeviceId) return;
    historyData = data;
    renderHistory();
  } catch (e) {
    toast(e.message, "err");
  }
}

panel.querySelector(".seg").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-hours]");
  if (!button) return;
  historyHours = Number(button.dataset.hours);
  panel.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  loadDeviceHistory(panelDeviceId);
});
let historyResize;
window.addEventListener("resize", () => {
  clearTimeout(historyResize);
  historyResize = setTimeout(() => panel.open && renderHistory(), 150);
});

// --- Activity log (#100) --------------------------------------------------------

const activityDialog = $("#activity-dialog");

async function openActivity() {
  activityDialog.showModal();
  const list = $(".activity-list", activityDialog);
  list.innerHTML = `<li class="muted">Loading…</li>`;
  try {
    const events = await api("GET", "/api/audit?limit=200");
    list.innerHTML = events.length ? events.map((e) => `
      <li class="${e.status >= 400 ? "refused" : ""}">
        <span class="when" title="${esc(new Date(e.at).toLocaleString())}">${ago(e.at)}</span>
        <span class="who">${esc(e.user)}${e.via_token ? ` <span class="tag" title="Through an API token">token</span>` : ""}</span>
        <span class="what">${esc(e.text)}${e.status >= 400 ? ` <span class="err" title="HTTP ${e.status}">✗ refused or failed</span>` : ""}</span>
      </li>`).join("") : `<li class="muted">Nothing yet: changes made in the dashboard or through the API show up here.</li>`;
  } catch (err) {
    list.innerHTML = `<li class="err">${esc(err.message)}</li>`;
  }
}

$("#activity-link").addEventListener("click", (e) => {
  e.preventDefault();
  openActivity();
});
