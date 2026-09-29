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

function isOnline(d) {
  return Date.now() - Date.parse(d.last_seen) < (state.interval * 2.5 + 5) * 1000;
}

function bytes(n) {
  return n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(2)} MB`;
}

// --- Devices -----------------------------------------------------------------

function createRow(id) {
  const tr = document.createElement("tr");
  tr.dataset.id = id;
  tr.innerHTML = `
    <td class="check"><input type="checkbox" class="sel" aria-label="Select"></td>
    <td><button class="name link" title="Details and history"></button><div class="mac mono muted"></div><div class="row-tags"></div></td>
    <td><span class="app"></span> <span class="hw tag"></span></td>
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
  $(".name", tr).addEventListener("click", () => openPanel(id));
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

function updateRow(tr, d) {
  const dep = d.last_deployment;
  const active = dep && ACTIVE.has(dep.status);

  $(".sel", tr).checked = state.selected.has(d.id);
  $(".name", tr).textContent = label(d);
  $(".mac", tr).textContent = d.name ? d.mac : "";
  $(".row-tags", tr).innerHTML = d.tags.map((t) => `<span class="tag-chip">#${esc(t)}</span>`).join("");
  $(".app", tr).textContent = d.app;
  $(".hw", tr).textContent = d.hw;
  $(".fw", tr).innerHTML = active
    ? `${esc(d.fw_version)} <span class="target">→ ${esc(dep.firmware.version)}</span>`
    : esc(d.fw_version);
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

const compareVersions = (a, b) => a.localeCompare(b, undefined, { numeric: true });

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
  return [d.name, d.mac, d.ip, d.app, d.hw, d.fw_version, ...d.tags].some((v) => v && v.toLowerCase().includes(q));
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
  $(".chips").innerHTML = Object.entries(FILTERS).map(([key, f]) => `
    <button class="chip" data-show="${key}" aria-pressed="${state.filter.show === key}">
      ${f.label}<span class="n">${searched.filter((d) => f.test(d, latest)).length}</span>
    </button>`).join("");
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
$(".chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
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

function renderSummary() {
  const all = [...state.devices.values()];
  const online = all.filter(isOnline).length;
  const updating = all.filter((d) => d.last_deployment && ACTIVE.has(d.last_deployment.status)).length;
  let text = `${all.length} device${all.length === 1 ? "" : "s"} · ${online} online`;
  if (updating) text += ` · ${updating} updating`;
  $("#summary").textContent = text;
}

function renderToolbar() {
  const selected = visibleSelected();
  const hws = new Set(selected.map((d) => d.hw));
  const select = $("#deploy-fw");
  const previous = select.value;
  const choices = state.firmwares.filter((f) => hws.size === 0 || (hws.size === 1 && hws.has(f.hw)));

  select.innerHTML = choices.length
    ? choices.map((f) => `<option value="${f.id}">${esc(f.app)} ${esc(f.version)} (${esc(f.hw)})</option>`).join("")
    : `<option value="">${hws.size > 1 ? "mixed hardware selected" : "no compatible firmware"}</option>`;
  if (choices.some((f) => String(f.id) === previous)) select.value = previous;

  const btn = $("#deploy-btn");
  btn.disabled = selected.length === 0 || !choices.length;
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
  renderPanel();
  panel.showModal();
}

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

function renderPanel() {
  const d = state.devices.get(panelDeviceId);
  if (!d) return panel.close();
  $("#panel-title").textContent = label(d);
  $(".panel-sub", panel).textContent = d.name ? d.mac : "";
  const info = [
    ["App", d.app],
    ["Hardware", d.hw],
    ["Firmware", d.fw_version],
    ["IP", d.ip || "—"],
    ["MAC", d.mac],
    ["Signal", d.rssi == null ? "—" : `${d.rssi} dBm`],
    ["Uptime", uptime(d.uptime_s)],
    ["Last seen", `${ago(d.last_seen)} · ${isOnline(d) ? "online" : "offline"}`],
    ["First seen", new Date(d.first_seen).toLocaleString()],
  ];
  $(".tag-list", panel).innerHTML = d.tags.map((t) => `
    <span class="tag-chip">#${esc(t)}<button data-tag="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("");
  $(".info", panel).innerHTML = info.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");

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
panel.addEventListener("close", () => { panelDeviceId = null; });

// --- Firmwares ---------------------------------------------------------------

function renderFirmwares() {
  const tbody = $("#firmwares tbody");
  tbody.innerHTML = state.firmwares.map((f) => `
    <tr data-id="${f.id}">
      <td>${esc(f.app)}</td>
      <td><span class="tag">${esc(f.hw)}</span></td>
      <td class="mono">${esc(f.version)}</td>
      <td>${bytes(f.size)}</td>
      <td class="mono muted" title="${esc(f.sha256)}">${esc(f.sha256.slice(0, 12))}…</td>
      <td title="${esc(new Date(f.uploaded_at).toLocaleString())}">${ago(f.uploaded_at)}</td>
      <td class="muted">${esc(f.notes)}</td>
      <td class="actions">
        <button class="staged ghost" title="Release ${esc(f.app)} ${esc(f.version)} progressively, in stages">Staged…</button>
        <button class="rollout ghost" title="Deploy to every ${esc(f.app)} / ${esc(f.hw)} device${state.filter.tag ? ` tagged #${esc(state.filter.tag)}` : ""} not on this version">Roll out${state.filter.tag ? ` to #${esc(state.filter.tag)}` : ""}</button>
        <button class="delete ghost danger" title="Delete">✕</button>
      </td>
    </tr>`).join("");
  $("#no-firmwares").hidden = state.firmwares.length > 0;

  const known = (key) => [...new Set([...state.devices.values(), ...state.firmwares].map((x) => x[key]))].sort();
  $("#known-apps").innerHTML = known("app").map((v) => `<option value="${esc(v)}">`).join("");
  $("#known-hws").innerHTML = known("hw").map((v) => `<option value="${esc(v)}">`).join("");
}

$("#firmwares tbody").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const fw = state.firmwares.find((f) => f.id === Number(tr.dataset.id));

  if (e.target.closest(".rollout")) {
    const tag = state.filter.tag;
    const targets = [...state.devices.values()].filter(
      (d) => d.app === fw.app && d.hw === fw.hw && d.fw_version !== fw.version && (!tag || d.tags.includes(tag)),
    );
    const scope = tag ? `${fw.app} device tagged #${tag}` : `${fw.app} device`;
    if (!targets.length) return toast(`Every ${scope} already runs ${fw.version}.`);
    // With a tag, let the server resolve its members: same result, one source of truth.
    await deploy(fw.id, tag ? { tags: [tag] } : { device_ids: targets.map((d) => d.id) }, targets.length, tag);
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
    const target = r.tags.length ? r.tags.map((t) => `#${esc(t)}`).join(" ") : `all ${esc(r.firmware.app)} devices`;
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
  form.tag.innerHTML = `<option value="">All ${esc(fw.app)} devices</option>` +
    [...tagCounts().keys()].map((t) => `<option value="${esc(t)}">#${esc(t)}</option>`).join("");
  form.tag.value = state.filter.tag;
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
    tags: form.tag.value ? [form.tag.value] : [],
  };
}

function updateRolloutPreview() {
  const fw = rolloutFirmware;
  const { stages, tags } = rolloutFormValues();
  const targets = [...state.devices.values()].filter(
    (d) => d.app === fw.app && d.hw === fw.hw && d.fw_version !== fw.version && (!tags.length || d.tags.includes(tags[0])),
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
    if (d.id === panelDeviceId) renderPanel();
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
  loadFilterFromUrl();
  connect();
}

start();
