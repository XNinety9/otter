"use strict";

const state = {
  devices: new Map(),
  firmwares: [],
  selected: new Set(),
  interval: 30,
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
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const { detail } = await res.json();
      msg = typeof detail === "string" ? detail : detail.map((d) => d.msg).join(", ");
    } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
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
    <td><button class="name link" title="Details and history"></button><div class="mac mono muted"></div></td>
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
    case "pending": return `waiting for check-in → ${v}`;
    case "downloading": return `downloading ${v} · ${dep.progress}%`;
    case "rebooting": return `rebooting into ${v}…`;
    case "success": return `✓ updated to ${v} · ${ago(dep.updated_at)}`;
    case "failed": return `✗ ${v} failed: ${dep.error || "unknown error"}`;
    case "cancelled": return `${v} cancelled`;
  }
}

function updateRow(tr, d) {
  const dep = d.last_deployment;
  const active = dep && ACTIVE.has(dep.status);

  $(".sel", tr).checked = state.selected.has(d.id);
  $(".name", tr).textContent = label(d);
  $(".mac", tr).textContent = d.name ? d.mac : "";
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
  $(".fill", cell).style.width = `${dep ? dep.progress : 0}%`;
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

function renderDevices() {
  const tbody = $("#devices tbody");
  const rows = new Map([...tbody.rows].map((tr) => [Number(tr.dataset.id), tr]));
  for (const [id, tr] of rows) {
    if (!state.devices.has(id)) { tr.remove(); rows.delete(id); }
  }
  const devices = sortedDevices();
  devices.forEach((d, i) => {
    let tr = rows.get(d.id);
    if (!tr) { tr = createRow(d.id); rows.set(d.id, tr); }
    // Only move rows when the order actually changed, so progress bars keep animating.
    if (tbody.rows[i] !== tr) tbody.insertBefore(tr, tbody.rows[i] || null);
    updateRow(tr, d);
  });
  $("#no-devices").hidden = devices.length > 0;
  $("#select-all").checked = devices.length > 0 && devices.every((d) => state.selected.has(d.id));
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
  const selected = [...state.selected].map((id) => state.devices.get(id)).filter(Boolean);
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

async function deploy(firmwareId, deviceIds) {
  const fw = state.firmwares.find((f) => f.id === firmwareId);
  const n = deviceIds.length;
  if (!confirm(`Deploy ${fw.app} ${fw.version} to ${n} device${n === 1 ? "" : "s"}?`)) return;
  try {
    await api("POST", "/api/deployments", { firmware_id: firmwareId, device_ids: deviceIds });
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
        <button class="rollout ghost" title="Deploy to every ${esc(f.app)} / ${esc(f.hw)} device not on this version">Roll out</button>
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
    const targets = [...state.devices.values()].filter(
      (d) => d.app === fw.app && d.hw === fw.hw && d.fw_version !== fw.version,
    );
    if (!targets.length) return toast(`Every ${fw.app} device already runs ${fw.version}.`);
    await deploy(fw.id, targets.map((d) => d.id));
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

// --- Wiring ------------------------------------------------------------------

$("#select-all").addEventListener("change", (e) => {
  for (const d of state.devices.values()) e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id);
  renderDevices();
  renderToolbar();
});

$("#deploy-btn").addEventListener("click", () => {
  deploy(Number($("#deploy-fw").value), [...state.selected]);
});

async function resync() {
  const [cfg, devices, firmwares] = await Promise.all([
    api("GET", "/api/config"),
    api("GET", "/api/devices"),
    api("GET", "/api/firmwares"),
  ]);
  state.interval = cfg.checkin_interval_s;
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
  es.addEventListener("error", () => {
    live.textContent = "reconnecting…";
    live.classList.remove("on");
  });
  es.addEventListener("device", (e) => {
    const d = JSON.parse(e.data);
    announce(state.devices.get(d.id), d);
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
    renderToolbar();
  });
  es.addEventListener("resync", () => resync());
}

// Keep "last seen" and online dots fresh between events.
setInterval(() => {
  for (const tr of $("#devices tbody").rows) {
    const d = state.devices.get(Number(tr.dataset.id));
    if (d) refreshSeen(tr, d);
  }
  renderSummary();
  if (panelDeviceId !== null) renderPanel();
}, 1000);

connect();
