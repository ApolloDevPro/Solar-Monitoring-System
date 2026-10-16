/* Automatic Solar Tracking System - dashboard front-end.
 * Talks to the FastAPI backend (same origin) and renders live state,
 * charts, tables and remote controls.
 */
"use strict";

const API = "";
const HIST_LIMIT = 300;

const $ = (id) => document.getElementById(id);

const el = {
  pillConn: $("pillConn"),
  pillLive: $("pillLive"),
  pillMode: $("pillMode"),
  pillState: $("pillState"),
  pillFault: $("pillFault"),
  pillSeen: $("pillSeen"),
  vLeft: $("vLeft"), sLeft: $("sLeft"), bLeft: $("bLeft"),
  vRight: $("vRight"), sRight: $("sRight"), bRight: $("bRight"),
  vDiff: $("vDiff"), sDiff: $("sDiff"), bBalance: $("bBalance"),
  vAngle: $("vAngle"), bAngle: $("bAngle"),
  vState: $("vState"), sState: $("sState"),
  vMode: $("vMode"), sMode: $("sMode"),
  svgAngle: $("svgAngle"),
  svgState: $("svgState"),
  panelImage: $("panelImage"),
  alignNote: $("alignNote"),
  modeSwitch: $("modeSwitch"),
  modeHint: $("modeHint"),
  angleSlider: $("angleSlider"),
  sliderOut: $("sliderOut"),
  btnMove: $("btnMove"),
  btnPark: $("btnPark"),
  btnResync: $("btnResync"),
  apiKeyInput: $("apiKeyInput"),
  btnSaveKey: $("btnSaveKey"),
  keyHint: $("keyHint"),
  btnNotifications: $("btnNotifications"),
  moveHint: $("moveHint"),
  stSamples: $("stSamples"),
  stOrient: $("stOrient"),
  stCmds: $("stCmds"),
  stRejected: $("stRejected"),
  stDegrees: $("stDegrees"),
  stAutoTime: $("stAutoTime"),
  lightChart: $("lightChart"),
  angleChart: $("angleChart"),
  cmdTable: $("cmdTable").querySelector("tbody"),
  cmdFilter: $("cmdFilter"),
  orientTable: $("orientTable").querySelector("tbody"),
  faultTable: $("faultTable").querySelector("tbody"),
  cfgGrid: $("cfgGrid"),
  toast: $("toast"),
};

let cfg = null;
let lastState = null;
let toastTimer = null;
let apiKey = sessionStorage.getItem("solarApiKey") || "";
let lastEventId = sessionStorage.getItem("solarLastEventId") || "";
let liveAbort = null;
let liveRunning = false;
let fallbackTimer = null;

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */
function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour12: false });
  } catch {
    return String(iso).slice(11, 19);
  }
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function toast(msg, kind = "ok") {
  el.toast.textContent = msg;
  el.toast.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.className = "toast " + kind;
  }, 3800);
}

function setPill(node, text, kind) {
  node.textContent = text;
  node.className = "pill" + (kind ? " pill-" + kind : "");
}

async function getJSON(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  if (apiKey) headers.set("X-API-Key", apiKey);
  const res = await fetch(API + path, { ...opts, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || res.statusText || "request failed");
    err.status = res.status;  // callers branch on 401/409/429
    throw err;
  }
  return body;
}

/* ------------------------------------------------------------------ */
/* Browser notification support                                        */
/* ------------------------------------------------------------------ */
let _lastOnline = null;
let _lastFault = null;

async function requestNotifPermission() {
  if (!("Notification" in window)) {
    toast("This browser does not support notifications.", "err");
    return;
  }
  const permission = await Notification.requestPermission();
  toast(permission === "granted" ? "Browser notifications enabled." : "Notification permission was not granted.",
    permission === "granted" ? "ok" : "err");
}

function sendNotif(title, body) {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body, icon: "" });
  }
}

function checkAlerts(s) {
  if (!s || !s.device_id) return;
  const online = !!s.is_online;
  const fault = s.fault || null;

  // Offline alert
  if (_lastOnline === true && !online) {
    sendNotif("Solar Tracker — Device Offline",
      "The device has gone offline. Last seen " + (s.age_seconds ? s.age_seconds.toFixed(0) + "s ago" : "unknown"));
    toast("⚠ Device went OFFLINE", "err");
  }
  _lastOnline = online;

  // Fault alert
  if (!_lastFault && fault) {
    sendNotif("Solar Tracker — Sensor Fault", fault);
    toast("🔴 FAULT detected: " + fault, "err");
  }
  _lastFault = fault;
}

/* ------------------------------------------------------------------ */
/* tiles / pills / svg                                                 */
/* ------------------------------------------------------------------ */
function renderState(s) {
  lastState = s;
  const online = !!s.is_online;

  if (!s.device_id && !s.mode) {
    setPill(el.pillConn, "no telemetry yet", "warn");
    setPill(el.pillMode, "—");
    setPill(el.pillState, "—");
    el.pillSeen.textContent = "device has not reported";
    return;
  }

  setPill(el.pillConn, online ? "device online" : "device offline", online ? "ok" : "danger");

  const mode = s.mode || "—";
  setPill(el.pillMode, mode, mode === "AUTO" ? "info" : "warn");
  setPill(el.pillState, s.tracking_state || "—",
    s.tracking_state === "ALIGNED" ? "ok"
    : s.tracking_state === "SENSOR_FAULT" ? "danger" : "info");

  if (s.fault) {
    el.pillFault.classList.remove("hidden");
    setPill(el.pillFault, "FAULT · " + s.fault, "danger");
  } else {
    el.pillFault.classList.add("hidden");
  }

  el.pillSeen.textContent =
    "seen " + (s.age_seconds != null ? s.age_seconds.toFixed(0) + "s ago" : "—");

  const left = s.ldr_left ?? null;
  const right = s.ldr_right ?? null;
  const diff = s.difference ?? (left != null && right != null ? right - left : null);
  const angle = s.angle ?? null;

  el.vLeft.textContent = left ?? "—";
  el.bLeft.style.width = left != null ? (left / 1023) * 100 + "%" : "0%";
  el.vRight.textContent = right ?? "—";
  el.bRight.style.width = right != null ? (right / 1023) * 100 + "%" : "0%";

  el.vDiff.textContent = diff ?? "—";
  el.sDiff.textContent = diff == null ? "right − left"
    : diff > 0 ? "right brighter (+" + diff + ")"
    : diff < 0 ? "left brighter (" + diff + ")"
    : "balanced";
  // balance bar: diff maps to -300..300 across the track
  const frac = diff == null ? 0 : Math.max(-1, Math.min(1, diff / 300));
  if (frac >= 0) {
    el.bBalance.style.left = "50%";
    el.bBalance.style.width = frac * 50 + "%";
    el.bBalance.style.background = "linear-gradient(90deg,#d97706,#ffb020)";
  } else {
    el.bBalance.style.left = 50 + frac * 50 + "%";
    el.bBalance.style.width = -frac * 50 + "%";
    el.bBalance.style.background = "linear-gradient(90deg,#22d3ee,#0891b2)";
  }

  el.vAngle.textContent = angle != null ? angle.toFixed(0) + "°" : "—";
  const softMin = cfg ? cfg.soft_min : 10;
  const softMax = cfg ? cfg.soft_max : 170;
  el.bAngle.style.width =
    angle != null ? ((angle - softMin) / (softMax - softMin)) * 100 + "%" : "0%";

  el.vState.textContent = s.tracking_state || "—";
  el.sState.textContent = s.fault ? s.fault : "controller";
  el.vMode.textContent = mode;
  el.sMode.textContent = mode === "AUTO" ? "following the light" : "operator controlled";

  // Use the servo's reported position to tilt the panel photo in the dashboard.
  const clampedAngle = angle == null
    ? null
    : Math.max(softMin, Math.min(softMax, angle));
  const tilt = clampedAngle == null
    ? 0
    : ((clampedAngle - (softMin + softMax) / 2) / (softMax - softMin)) * 70;
  el.panelImage.style.setProperty("--panel-rotation", tilt.toFixed(1) + "deg");
  el.svgAngle.textContent = angle != null ? angle.toFixed(1) + "°" : "—";
  el.svgState.textContent = (s.tracking_state || "—").toUpperCase();

  if (diff != null) {
    el.alignNote.textContent =
      Math.abs(diff) <= (cfg ? cfg.deadband : 30)
        ? "Aligned — light is balanced across the sensors"
        : (diff > 0 ? "more light on the RIGHT LDR" : "more light on the LEFT LDR") +
          " (Δ " + diff + ")";
  } else {
    el.alignNote.textContent = "Waiting for light sensor readings";
  }

  // keep mode buttons in sync
  for (const btn of el.modeSwitch.querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  }
  const manual = mode === "MANUAL";
  el.angleSlider.disabled = !manual || !online;
  el.btnMove.disabled = !manual || !online;
  el.btnPark.disabled = !manual || !online;
  el.moveHint.textContent = manual
    ? (online
        ? "Manual mode: direct moves and park are accepted."
        : "Manual mode, but the device is offline — commands will be rejected.")
    : "Direct moves are only accepted while the device is in MANUAL mode.";
  el.modeHint.textContent = manual
    ? "MANUAL: the panel only moves when you command it."
    : "AUTO lets the controller follow the stronger light source.";

  // Sync angle slider to current panel angle when in MANUAL mode
  if (manual && angle != null) {
    const sliderAngle = Math.round(Math.max(softMin, Math.min(softMax, angle)));
    if (el.angleSlider.value !== String(sliderAngle) && document.activeElement !== el.angleSlider) {
      el.angleSlider.value = sliderAngle;
      el.sliderOut.textContent = sliderAngle + "°";
    }
  }

  // Check for offline / fault alerts
  checkAlerts(s);
}

/* ------------------------------------------------------------------ */
/* charts (pure canvas, no external libs)                              */
/* ------------------------------------------------------------------ */
function drawChart(canvas, seriesList, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = parseInt(canvas.getAttribute("height"), 10) || 230;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const W = cssW;
  const H = cssH;
  const padL = 44;
  const padR = 10;
  const padT = 12;
  const padB = 36;  // increased from 24 to make room for X-axis time labels
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  ctx.clearRect(0, 0, W, H);

  const rows = Math.max(...seriesList.map((s) => s.points.length), 0);
  if (rows < 2) {
    ctx.fillStyle = "#8ea0c0";
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText("waiting for data…", padL + 8, H / 2);
    return;
  }

  let yMin = opts.min;
  let yMax = opts.max;
  if (yMin == null || yMax == null) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of seriesList) {
      for (const p of s.points) {
        if (p < lo) lo = p;
        if (p > hi) hi = p;
      }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi === lo) { hi = lo + 1; lo = lo - 1; }
    const span = hi - lo;
    yMin = yMin != null ? yMin : lo - span * 0.08;
    yMax = yMax != null ? yMax : hi + span * 0.08;
  }

  const xOf = (i) => padL + (i / (rows - 1)) * plotW;
  const yOf = (v) => padT + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // grid + labels
  ctx.strokeStyle = "#1e2a44";
  ctx.fillStyle = "#8ea0c0";
  ctx.font = "10px Segoe UI, sans-serif";
  ctx.lineWidth = 1;
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + ((yMax - yMin) * i) / ticks;
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(W - padR, y);
    ctx.stroke();
    ctx.fillText(String(Math.round(v)), 6, y + 3);
  }

  // zero line if within range
  if (yMin < 0 && yMax > 0) {
    ctx.strokeStyle = "#47587d";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yOf(0));
    ctx.lineTo(W - padR, yOf(0));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  for (const s of seriesList) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width || 2;
    ctx.beginPath();
    const n = s.points.length;
    for (let i = 0; i < n; i++) {
      const x = xOf(i);
      const y = yOf(s.points[i]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    if (s.fill) {
      ctx.lineTo(xOf(n - 1), padT + plotH);
      ctx.lineTo(xOf(0), padT + plotH);
      ctx.closePath();
      ctx.fillStyle = s.fill;
      ctx.fill();
    }
  }

  // legend
  ctx.font = "11px Segoe UI, sans-serif";
  let lx = padL;
  for (const s of seriesList) {
    ctx.fillStyle = s.color;
    ctx.fillRect(lx, H - padB + 2, 10, 3);
    ctx.fillStyle = "#8ea0c0";
    ctx.fillText(s.name, lx + 14, H - padB + 7);
    lx += 14 + ctx.measureText(s.name).width + 14;
  }

  // X-axis time labels
  if (opts.labels && opts.labels.length > 0) {
    const lbls = opts.labels;
    const step = Math.max(1, Math.floor(lbls.length / 6));
    ctx.fillStyle = "#8ea0c0";
    ctx.font = "9px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    for (let i = 0; i < lbls.length; i += step) {
      const x = xOf(i);
      ctx.fillText(lbls[i], x, H - padB + 22);
    }
    // always show last label
    ctx.fillText(lbls[lbls.length - 1], xOf(lbls.length - 1), H - padB + 22);
    ctx.textAlign = "left";
  }
}

async function renderCharts() {
  const [light, orient] = await Promise.all([
    getJSON("/api/history/light?limit=" + HIST_LIMIT),
    getJSON("/api/history/orientation?limit=200"),
  ]);
  const L = light.items || [];
  const O = orient.items || [];

  // Extract time labels (show every Nth to avoid clutter)
  const lightLabels = L.map(r => fmtTime(r.ts));
  const orientLabels = O.map(r => fmtTime(r.ts));

  drawChart(el.lightChart, [
    { name: "left LDR", color: "#22d3ee", points: L.map(r => r.ldr_left) },
    { name: "right LDR", color: "#ffb020", points: L.map(r => r.ldr_right) },
  ], { min: 0, max: 1023, labels: lightLabels });

  drawChart(el.angleChart, [
    { name: "angle °", color: "#7c3aed", points: O.map(r => r.angle), width: 2.5 },
  ], { min: 0, max: 180, labels: orientLabels });
}

/* ------------------------------------------------------------------ */
/* tables                                                              */
/* ------------------------------------------------------------------ */
function statusBadge(st) {
  const cls =
    { PENDING: "b-pending", EXECUTED: "b-executed", REJECTED: "b-rejected",
      EXPIRED: "b-expired", FAILED: "b-failed" }[st] || "b-expired";
  return '<span class="badge ' + cls + '">' + esc(st || "?") + "</span>";
}

function commandDescription(command) {
  const payload = command.payload || {};
  if (command.type === "set_mode") return "Mode → " + (payload.mode || "—");
  if (command.type === "move" && payload.target_angle != null) {
    return "Target angle · " + Number(payload.target_angle).toFixed(1) + "°";
  }
  if (command.type === "park") {
    return "Park at " + (cfg?.park_angle ?? 90) + "°";
  }
  return JSON.stringify(payload);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function renderTables() {
  const [cmds, orient] = await Promise.all([
    getJSON("/api/history/commands?limit=80"),
    getJSON("/api/history/orientation?limit=60"),
  ]);

  const filter = el.cmdFilter.value;
  const filteredCommands = (cmds.items || []).filter(c =>
    filter === "all" || c.status === filter
  );
  const crows = filteredCommands.map((c) =>
    "<tr><td class='id-cell'>" + c.id + "</td><td title='" + esc(c.ts) + "'>" +
    fmtDateTime(c.ts) + "</td><td><span class='command-type'>" + esc(c.type) +
    "</span></td><td class='payload-cell' title='" +
    esc(JSON.stringify(c.payload)) + "'>" + esc(commandDescription(c)) +
    "</td><td>" + esc(c.source) + "</td><td>" + statusBadge(c.status) +
    "</td><td class='reason'>" + esc(c.reason || "—") + "</td></tr>"
  );
  el.cmdTable.innerHTML = crows.length
    ? crows.join("")
    : '<tr><td colspan="7" class="empty">' +
      (filter === "all" ? "No commands yet." : "No " + esc(filter.toLowerCase()) + " commands.") +
      "</td></tr>";

  const orows = (orient.items || []).map((o) =>
    "<tr><td title='" + esc(o.ts) + "'>" + fmtDateTime(o.ts) + "</td><td>" + Number(o.angle).toFixed(1) +
    "°</td><td>" + esc(o.mode) + "</td><td>" + esc(o.tracking_state) +
    "</td><td>" + esc(o.reason || "") + "</td></tr>"
  );
  el.orientTable.innerHTML = orows.length
    ? orows.join("")
    : '<tr><td colspan="5" class="empty">no orientation events yet</td></tr>';
}

/* ------------------------------------------------------------------ */
/* fault timeline                                                      */
/* ------------------------------------------------------------------ */
async function renderFaults() {
  try {
    const data = await getJSON("/api/history/faults?limit=50");
    const rows = (data.items || []).map(f =>
      "<tr><td title='" + esc(f.ts) + "'>" + fmtDateTime(f.ts) + "</td><td>" + Number(f.angle).toFixed(1) +
      "°</td><td>" + esc(f.mode) + "</td><td class='reason'>" + esc(f.reason || 'sensor fault') + "</td></tr>"
    );
    el.faultTable.innerHTML = rows.length
      ? rows.join("")
      : '<tr><td colspan="4" class="empty">no fault events recorded</td></tr>';
  } catch (err) {
    el.faultTable.innerHTML =
      '<tr><td colspan="4" class="empty">Fault history unavailable: ' +
      esc(err.message) + "</td></tr>";
  }
}

/* ------------------------------------------------------------------ */
/* summary + config                                                    */
/* ------------------------------------------------------------------ */
async function renderSummary() {
  applyCounts(await getJSON("/api/summary"));
}

function applyCounts(s) {
  if (!s) return;
  el.stSamples.textContent = s.light_samples ?? s.counts?.light_samples ?? 0;
  el.stOrient.textContent = s.orientation_events ?? s.counts?.orientation_history ?? 0;
  el.stCmds.textContent = s.commands_total ?? s.counts?.commands ?? 0;
  el.stRejected.textContent = s.commands_rejected ?? s.counts?.rejected_commands ?? 0;
  if (s.efficiency) {
    el.stDegrees.textContent =
      Number(s.efficiency.total_degrees_moved || 0).toFixed(1) + "°";
    el.stAutoTime.textContent =
      formatDuration(s.efficiency.time_in_auto_seconds || 0);
  }
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return total + "s";
  if (total < 3600) return Math.floor(total / 60) + "m";
  return Math.floor(total / 3600) + "h " + Math.floor((total % 3600) / 60) + "m";
}

async function loadConfig() {
  cfg = await getJSON("/api/config");
  const labels = {
    deadband: "Deadband (ADC)",
    confirm_samples: "Confirm samples",
    filter_window: "Median filter window",
    settle_ms: "Settle delay (ms)",
    min_step: "Min step (°)",
    max_step: "Max step (°)",
    soft_min: "Soft min (°)",
    soft_max: "Soft max (°)",
    park_angle: "Park angle (°)",
    offline_after_s: "Offline after (s)",
    command_expire_s: "Command expiry (s)",
  };
  el.cfgGrid.innerHTML = Object.entries(labels)
    .map(([k, label]) =>
      "<div><span>" + label + "</span><strong>" +
      esc(cfg[k] != null ? cfg[k] : "—") + "</strong></div>"
    )
    .join("");
  el.angleSlider.min = cfg.soft_min;
  el.angleSlider.max = cfg.soft_max;
}

/* ------------------------------------------------------------------ */
/* commands                                                            */
/* ------------------------------------------------------------------ */
async function sendCommand(payload) {
  try {
    const body = await getJSON("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (body.accepted) {
      toast("Command #" + body.command.id + " accepted (" + payload.type + ")");
    } else {
      toast("Rejected: " + (body.reason || "unknown reason"), "err");
    }
    await Promise.all([renderTables(), renderSummary()]);
    return body;
  } catch (err) {
    toast("Network error: " + err.message, "err");
    return null;
  }
}

el.angleSlider.addEventListener("input", () => {
  el.sliderOut.textContent = el.angleSlider.value + "°";
});

for (const btn of el.modeSwitch.querySelectorAll("button")) {
  btn.addEventListener("click", () => {
    sendCommand({ type: "set_mode", mode: btn.dataset.mode });
  });
}

el.btnMove.addEventListener("click", () => {
  sendCommand({
    type: "move",
    target_angle: Number(el.angleSlider.value),
  });
});

el.btnPark.addEventListener("click", () => {
  sendCommand({ type: "park" });
});

el.btnResync.addEventListener("click", async () => {
  await refresh(true);
  toast("State refreshed");
});

/* ------------------------------------------------------------------ */
/* export helpers                                                      */
/* ------------------------------------------------------------------ */
function downloadBlob(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function toCSV(items, keys) {
  const header = keys.join(",");
  const cell = value => {
    let text = String(value ?? "");
    if (/^[\s=+\-@]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  };
  const rows = items.map(r => keys.map(k => cell(r[k])).join(","));
  return [header, ...rows].join("\n");
}

$("btnExportLightCSV").addEventListener("click", async () => {
  try {
    const data = await getJSON("/api/history/light?limit=5000");
    downloadBlob("light_history.csv",
      toCSV(data.items, ["id","ts","ldr_left","ldr_right","difference"]), "text/csv");
  } catch (err) {
    toast("Export failed: " + err.message, "err");
  }
});

$("btnExportOrientCSV").addEventListener("click", async () => {
  try {
    const data = await getJSON("/api/history/orientation?limit=2000");
    downloadBlob("orientation_history.csv",
      toCSV(data.items, ["id","ts","angle","mode","tracking_state","reason"]), "text/csv");
  } catch (err) {
    toast("Export failed: " + err.message, "err");
  }
});

$("btnExportJSON").addEventListener("click", async () => {
  try {
    const [light, orient, faults, cmds, state, summary] = await Promise.all([
      getJSON("/api/history/light?limit=5000"),
      getJSON("/api/history/orientation?limit=2000"),
      getJSON("/api/history/faults?limit=1000"),
      getJSON("/api/history/commands?limit=1000"),
      getJSON("/api/state"),
      getJSON("/api/summary"),
    ]);
    const json = JSON.stringify({
      state,
      summary,
      light: light.items,
      orientation: orient.items,
      faults: faults.items,
      commands: cmds.items,
    }, null, 2);
    downloadBlob("solar_tracker_export.json", json, "application/json");
  } catch (err) {
    toast("Export failed: " + err.message, "err");
  }
});

/* ------------------------------------------------------------------ */
/* live updates: SSE push (primary) + adaptive polling (fallback)      */
/* ------------------------------------------------------------------ */
let sseLive = false;
let lastHeavy = 0;
let lastSummaryRefresh = 0;
// The backend enforces a 200/min default limit on every route, so both
// refresh paths must stay comfortably below it: heavyRefresh issues up to
// ~6 GETs per cycle, fallback refresh issues ~7.
const HEAVY_MIN_INTERVAL = 4000;   // ~90 req/min in SSE mode
const FALLBACK_INTERVAL = 5000;    // ~84 req/min when SSE is down
let fallbackDelay = FALLBACK_INTERVAL;  // grows on 429, resets on success

function setLive(on) {
  sseLive = on;
  el.pillLive.classList.toggle("hidden", !on);
  scheduleFallback();
}

function scheduleFallback() {
  if (fallbackTimer) clearTimeout(fallbackTimer);
  fallbackTimer = setTimeout(async () => {
    if (!sseLive && apiKey) await refresh(false);
    scheduleFallback();
  }, sseLive || !apiKey ? 10000 : fallbackDelay);
}

function heavyRefresh() {
  const now = Date.now();
  if (now - lastHeavy < HEAVY_MIN_INTERVAL) return;
  lastHeavy = now;
  const tasks = [renderCharts(), renderTables(), renderFaults()];
  if (now - lastSummaryRefresh >= 10000) {
    lastSummaryRefresh = now;
    tasks.push(renderSummary());
  }
  // Surface failures instead of swallowing them silently.
  Promise.all(tasks).catch((err) => {
    if (err && err.status === 429) {
      setPill(el.pillConn, "rate limited", "warn");
    } else if (err) {
      toast("Refresh failed: " + err.message, "err");
    }
  });
}

function handleSseFrame(frame) {
  let id = "";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (id) {
    lastEventId = id;
    sessionStorage.setItem("solarLastEventId", id);
  }
  if (!data.length) return;
  let msg;
  try {
    msg = JSON.parse(data.join("\n"));
  } catch {
    // One malformed frame must not tear down the whole SSE connection.
    return;
  }
  if (msg.state) renderState(msg.state);
  if (msg.counts) applyCounts({ counts: msg.counts });
  heavyRefresh();
}

async function connectLive() {
  let retryMs = 500;
  while (apiKey) {
    liveAbort = new AbortController();
    try {
      const headers = new Headers({
        Accept: "text/event-stream",
        "X-API-Key": apiKey,
      });
      if (lastEventId) headers.set("Last-Event-ID", lastEventId);
      const response = await fetch(API + "/api/events", {
        headers,
        signal: liveAbort.signal,
      });
      if (!response.ok || !response.body) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || "SSE connection failed: " + response.status);
      }
      setLive(true);
      retryMs = 500;
      await refresh(true);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          handleSseFrame(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        el.keyHint.textContent = "Live connection interrupted: " + err.message;
      }
    }
    setLive(false);
    if (!apiKey) break;
    await new Promise(resolve => setTimeout(resolve, retryMs));
    retryMs = Math.min(retryMs * 2, 10000);
  }
  liveRunning = false;
}

function startLive() {
  if (!apiKey || liveRunning) return;
  liveRunning = true;
  connectLive();
}

async function refresh(force) {
  if (sseLive && !force) return;  // SSE is pushing state already
  try {
    const s = await getJSON("/api/state");
    renderState(s);
    await Promise.all([renderCharts(), renderTables(), renderSummary(), renderFaults()]);
    fallbackDelay = FALLBACK_INTERVAL;  // healthy: normal cadence
    return true;
  } catch (err) {
    if (err.status === 429) {
      // Backend is throttling us: back off instead of hammering it (and
      // never report an outage caused by our own request rate).
      fallbackDelay = Math.min(fallbackDelay * 2, 60000);
      setPill(el.pillConn, "rate limited - backing off", "warn");
      return false;
    }
    const authFailure = /api key|authentication|X-API-Key/i.test(err.message);
    setPill(el.pillConn, authFailure ? "API key required" : "backend unreachable", "danger");
    if (authFailure) el.keyHint.textContent = err.message;
    if (force) toast("Backend unreachable: " + err.message, "err");
    return false;
  }
}

async function boot() {
  el.apiKeyInput.value = apiKey;
  el.sliderOut.textContent = el.angleSlider.value + "°";
  try { await loadConfig(); } catch { /* config optional */ }
  startLive();
  await refresh(true);

  scheduleFallback();
}

el.btnSaveKey.addEventListener("click", async () => {
  apiKey = el.apiKeyInput.value.trim();
  if (apiKey) sessionStorage.setItem("solarApiKey", apiKey);
  else sessionStorage.removeItem("solarApiKey");
  if (liveAbort) liveAbort.abort();
  el.keyHint.textContent = apiKey
    ? "Checking API key…"
    : "Enter the shared API key from your .env file. It is kept for this browser session only.";
  const connected = await refresh(true);
  if (apiKey && connected) {
    startLive();
    el.keyHint.textContent = "Connected using the configured API key.";
  }
});

el.btnNotifications.addEventListener("click", requestNotifPermission);
el.cmdFilter.addEventListener("change", () => {
  renderTables().catch(err => toast("Could not load command history: " + err.message, "err"));
});

boot();
