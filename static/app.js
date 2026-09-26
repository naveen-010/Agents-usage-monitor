"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
};

// Colors framed around *remaining* budget: low left = bad.
const leftClass = (left) => (left <= 20 ? "crit" : left <= 50 ? "warn" : "safe");

// Escape any string before it goes into innerHTML (defense-in-depth: labels are
// server constants today, but this keeps the template safe if that ever changes).
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- One-click key import (bookmarklet target) ----
// The bookmarklet (run on claude.ai) opens this app with the key in the URL
// hash. Hashes never reach the server, so the key isn't logged; we read it,
// save it, and immediately scrub it from the URL/history.
let pendingKey = null;
(function () {
  const m = window.location.hash.match(/key=([^&]+)/);
  if (m) {
    pendingKey = decodeURIComponent(m[1]);
    history.replaceState(null, "", window.location.pathname);
  }
})();

function toast(msg, bad) {
  const t = $("#toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  setTimeout(() => t.classList.add("hidden"), 4000);
}

function bookmarkletCode() {
  const origin = window.location.origin;
  return (
    "javascript:(function(){var m=document.cookie.match(/sessionKey=([^;]+)/);" +
    "if(!m){alert('sessionKey not found \\u2014 are you logged in to claude.ai?');return;}" +
    "window.open('" + origin + "/#key='+encodeURIComponent(m[1]),'_blank');})();"
  );
}

function populateBookmarklet() {
  const box = $("#bookmarklet-box");
  if (!box || box.dataset.ready) return;
  const code = bookmarkletCode();
  const a = document.createElement("a");
  a.className = "bookmarklet";
  a.textContent = "↗ Send key to Usage Dashboard";
  a.setAttribute("href", code); // dragging this to the bookmarks bar installs it
  a.addEventListener("click", (e) => e.preventDefault());
  const copy = document.createElement("button");
  copy.className = "ghost";
  copy.type = "button";
  copy.textContent = "Copy code";
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(code).then(() => {
      copy.textContent = "Copied!";
      setTimeout(() => (copy.textContent = "Copy code"), 1500);
    });
  });
  box.appendChild(a);
  box.appendChild(copy);
  box.dataset.ready = "1";
}

async function applyImportedKey(key) {
  toast("Importing key…");
  try {
    const r = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({ provider: "claude", session_key: key }),
    });
    toast(r.message || "Key imported.");
  } catch (e) {
    toast("Import failed: " + e.message, true);
  }
  loadUsage();
}

function fmtDuration(seconds) {
  let remaining = Math.max(0, Math.floor(seconds));
  const parts = [];
  for (const [unit, size] of [["w", 604800], ["d", 86400], ["h", 3600], ["m", 60], ["s", 1]]) {
    const count = Math.floor(remaining / size);
    if (count) {
      parts.push(`${count}${unit}`);
      if (parts.length === 2) break;
    }
    remaining %= size;
  }
  return parts.join(" ") || "0s";
}

function fmtReset(iso) {
  if (!iso) return "no reset time";
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms)) return "no reset time";
  if (ms <= 0) return "resetting…";
  return `resets in ${fmtDuration(Math.ceil(ms / 1000))}`;
}

function fmtAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return "unknown";
  if (ms < 1000) return "just now";
  return `${fmtDuration(ms / 1000)} ago`;
}

function renderMetric(m) {
  const used = Math.max(0, Math.min(100, m.utilization));
  const left = 100 - used;
  const cls = leftClass(left);
  return `
    <div class="metric">
      <div class="metric-top">
        <span class="metric-label">${esc(m.label)}</span>
        <span class="metric-pct ${cls}">${left.toFixed(0)}%<span class="pct-unit">left</span></span>
      </div>
      <div class="bar"><span class="${cls}" style="width:${left}%"></span></div>
      <div class="metric-foot">
        <span>${esc(fmtReset(m.resets_at))}</span>
        <span>${esc(fmtAgo(m.fetched_at))}</span>
      </div>
    </div>`;
}

// The standard Claude limit windows, always shown (placeholder when null) so the
// full limit structure is visible even before weekly usage accrues.
const CLAUDE_WINDOWS = [
  ["five_hour", "Session (5h)"],
  ["seven_day", "Weekly (all models)"],
  ["seven_day_opus", "Weekly (Opus)"],
  ["seven_day_sonnet", "Weekly (Sonnet)"],
];

function renderPending(label) {
  return `
    <div class="metric pending">
      <div class="metric-top">
        <span class="metric-label">${esc(label)}</span>
        <span class="metric-pct muted">—</span>
      </div>
      <div class="metric-foot"><span>no usage reported yet</span><span></span></div>
    </div>`;
}

function renderProvider(p) {
  const head = `<h2>${esc(p.display_name)}${p.configured ? "" : '<span class="pill">not configured</span>'}</h2>`;
  let body = "";
  if (!p.configured) {
    body = `<div class="empty">Add credentials in ⚙ Settings to start tracking.</div>`;
  } else if (p.error) {
    body += `<div class="err-row">⚠ ${esc(p.error)} — open ⚙ Settings to update your key.</div>`;
    body += p.metrics.map(renderMetric).join("");
  } else if (p.provider === "claude") {
    const byKey = Object.fromEntries(p.metrics.map((m) => [m.key, m]));
    const canonical = new Set(CLAUDE_WINDOWS.map((w) => w[0]));
    for (const [key, label] of CLAUDE_WINDOWS) {
      body += byKey[key] ? renderMetric(byKey[key]) : renderPending(label);
    }
    // Anything extra the API returned (e.g. pay-as-you-go credits).
    body += p.metrics.filter((m) => !canonical.has(m.key)).map(renderMetric).join("");
    body += `<p class="muted small note">Weekly windows fill in once your 7-day usage accrues — Claude reports them as empty until then.</p>`;
  } else {
    if (p.metrics.length === 0) {
      body += `<div class="err-row">No data yet — waiting for first successful poll.</div>`;
    }
    body += p.metrics.map(renderMetric).join("");
  }
  return `<section class="provider-block">${head}${body}</section>`;
}

async function loadUsage() {
  try {
    const data = await api("/api/usage");
    $("#providers").innerHTML = data.providers.map(renderProvider).join("");
    populateChartControls(data.providers);
    const newest = data.providers
      .flatMap((p) => p.metrics.map((m) => m.fetched_at))
      .sort()
      .pop();
    $("#last-updated").textContent = newest ? `Updated ${fmtAgo(newest)}` : "No data yet";
  } catch (e) {
    if (String(e.message).includes("401")) return showLogin();
    $("#providers").innerHTML = `<div class="err-row">${esc(e.message)}</div>`;
  }
}

async function loadConfig() {
  try {
    const c = await api("/api/config");
    $("#channels").textContent = c.notify_channels.length
      ? `Alerts: ${c.notify_channels.join(", ")}`
      : "No alert channels configured";
  } catch (_) {}
}

// ---- History trend chart (plots % LEFT over time) ----
let chartHours = 24;
let _chart = null; // cached geometry + points, for hover redraws

function populateChartControls(providers) {
  const sel = $("#chart-metric");
  if (!sel) return;
  const prev = sel.value;
  const opts = [];
  for (const p of providers) {
    for (const m of p.metrics) {
      opts.push(`<option value="${esc(p.provider)}|${esc(m.key)}">${esc(p.display_name)} · ${esc(m.label)}</option>`);
    }
  }
  if (opts.length === 0) {
    sel.innerHTML = `<option value="">No data yet</option>`;
    return;
  }
  sel.innerHTML = opts.join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  loadChart();
}

async function loadChart() {
  const sel = $("#chart-metric");
  if (!sel || !sel.value) return;
  const [provider, metric] = sel.value.split("|");
  try {
    const d = await api(`/api/history?provider=${encodeURIComponent(provider)}&metric=${encodeURIComponent(metric)}&hours=${chartHours}`);
    drawChart(d.points || []);
  } catch (_) {}
}

function fmtTimeTick(ms, spanMs) {
  const d = new Date(ms);
  if (spanMs <= 6 * 3600e3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (spanMs <= 3 * 24 * 3600e3) return d.toLocaleString([], { weekday: "short", hour: "2-digit" });
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function drawChart(points) {
  const c = $("#chart");
  if (!c) return;
  const ctx = c.getContext("2d");
  const rect = c.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  c.width = rect.width * dpr;
  c.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width, H = rect.height;
  const padL = 30, padR = 12, padT = 12, padB = 24;
  const bottom = padT + (H - padT - padB);
  ctx.clearRect(0, 0, W, H);
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  _chart = null;

  // y gridlines + labels (% remaining)
  ctx.strokeStyle = "#2a2a31";
  ctx.fillStyle = "#8a8a96";
  ctx.lineWidth = 1;
  for (const yv of [0, 25, 50, 75, 100]) {
    const py = padT + (H - padT - padB) * (1 - yv / 100);
    ctx.beginPath();
    ctx.moveTo(padL, py);
    ctx.lineTo(W - padR, py);
    ctx.stroke();
    ctx.fillText(yv + "%", 2, py + 3);
  }
  if (!points || points.length < 2) {
    ctx.fillStyle = "#8a8a96";
    ctx.fillText("collecting data — a line appears after a few polls", padL + 8, (padT + bottom) / 2);
    return;
  }

  const t0 = new Date(points[0].fetched_at).getTime();
  const t1 = new Date(points[points.length - 1].fetched_at).getTime();
  const span = Math.max(1, t1 - t0);
  const X = (t) => padL + (W - padL - padR) * ((new Date(t).getTime() - t0) / span);
  const Y = (used) => padT + (H - padT - padB) * (1 - used / 100); // plot % USED

  // x-axis time labels
  ctx.fillStyle = "#8a8a96";
  ctx.textAlign = "center";
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + (span * i) / ticks;
    const px = Math.min(Math.max(X(t), padL + 14), W - padR - 14);
    ctx.fillText(fmtTimeTick(t, span), px, H - 6);
  }
  ctx.textAlign = "left";

  // area fill under the line
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = X(p.fetched_at), y = Y(p.utilization);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.lineTo(X(points[points.length - 1].fetched_at), bottom);
  ctx.lineTo(X(points[0].fetched_at), bottom);
  ctx.closePath();
  ctx.fillStyle = "rgba(217, 119, 87, 0.14)";
  ctx.fill();

  // line
  ctx.strokeStyle = "#d97757";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = X(p.fetched_at), y = Y(p.utilization);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();

  // current-value marker + label
  const last = points[points.length - 1];
  const lx = X(last.fetched_at), ly = Y(last.utilization);
  ctx.fillStyle = "#d97757";
  ctx.beginPath();
  ctx.arc(lx, ly, 3, 0, 2 * Math.PI);
  ctx.fill();
  ctx.fillStyle = "#ececf0";
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.fillText(`${last.utilization.toFixed(0)}% used now`, W - padR, padT + 2);
  ctx.textAlign = "left";

  _chart = { points, X, Y, padT, bottom, W, padR, span };
}

function drawHoverOverlay(idx) {
  if (!_chart) return;
  const { points, X, Y, padT, bottom, W, padR } = _chart;
  const p = points[idx];
  const x = X(p.fetched_at), y = Y(p.utilization);
  const ctx = $("#chart").getContext("2d");

  ctx.strokeStyle = "#8a8a96";
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(x, padT);
  ctx.lineTo(x, bottom);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = "#ececf0";
  ctx.beginPath();
  ctx.arc(x, y, 3.5, 0, 2 * Math.PI);
  ctx.fill();

  const label = `${p.utilization.toFixed(0)}% used · ${new Date(p.fetched_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  ctx.font = "10px system-ui, sans-serif";
  const tw = ctx.measureText(label).width;
  let bx = x + 8;
  if (bx + tw + 12 > W - padR) bx = x - tw - 20;
  const by = Math.max(padT, y - 24);
  ctx.fillStyle = "rgba(29, 29, 34, 0.96)";
  ctx.fillRect(bx, by, tw + 12, 18);
  ctx.strokeStyle = "#2a2a31";
  ctx.strokeRect(bx, by, tw + 12, 18);
  ctx.fillStyle = "#ececf0";
  ctx.fillText(label, bx + 6, by + 12);
}

function onChartHover(e) {
  if (!_chart) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  let best = 0, bestd = Infinity;
  _chart.points.forEach((p, i) => {
    const d = Math.abs(_chart.X(p.fetched_at) - mx);
    if (d < bestd) { bestd = d; best = i; }
  });
  const pts = _chart.points;
  drawChart(pts);
  drawHoverOverlay(best);
}

async function sendSnapshot() {
  const s = $("#snapshot-status");
  try {
    const r = await api("/api/notify/snapshot", { method: "POST" });
    s.className = "status ok";
    s.textContent = r.sent_to && r.sent_to.length ? `Sent to: ${r.sent_to.join(", ")}` : "No channels configured.";
  } catch (e) {
    s.className = "status bad";
    s.textContent = e.message;
  }
}

// ---- Settings actions ----
async function saveClaude() {
  const status = $("#claude-status");
  status.className = "status";
  status.textContent = "Validating…";
  try {
    const r = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        provider: "claude",
        session_key: $("#claude-key").value.trim(),
        org_id: $("#claude-org").value.trim() || null,
      }),
    });
    status.className = "status ok";
    status.textContent = r.message || "Saved.";
    $("#claude-key").value = "";
    loadUsage();
  } catch (e) {
    status.className = "status bad";
    status.textContent = e.message;
  }
}

async function clearClaude() {
  await api("/api/credentials/claude", { method: "DELETE" });
  $("#claude-status").className = "status";
  $("#claude-status").textContent = "Removed.";
  loadUsage();
}

async function saveCodex() {
  const status = $("#codex-status");
  status.className = "status";
  status.textContent = "Validating…";
  try {
    const r = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        provider: "codex",
        access_token: $("#codex-token").value.trim(),
        account_id: $("#codex-account").value.trim() || null,
      }),
    });
    status.className = "status ok";
    status.textContent = r.message || "Saved.";
    $("#codex-token").value = "";
    loadUsage();
  } catch (e) {
    status.className = "status bad";
    status.textContent = e.message;
  }
}

async function clearCodex() {
  await api("/api/credentials/codex", { method: "DELETE" });
  $("#codex-status").className = "status";
  $("#codex-status").textContent = "Removed.";
  loadUsage();
}

async function testNotify() {
  const s = $("#notify-status");
  try {
    const r = await api("/api/notify/test");
    s.className = "status ok";
    s.textContent = r.sent_to.length ? `Sent to: ${r.sent_to.join(", ")}` : "No channels configured.";
  } catch (e) {
    s.className = "status bad";
    s.textContent = e.message;
  }
}

// ---- Auth / boot ----
function showLogin() { $("#app").classList.add("hidden"); $("#settings").classList.add("hidden"); $("#login").classList.remove("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }

async function boot() {
  const me = await api("/api/me");
  if (me.auth_required && !me.authenticated) return showLogin();
  showApp();
  if (pendingKey) {
    const k = pendingKey;
    pendingKey = null;
    await applyImportedKey(k);
  }
  await Promise.all([loadConfig(), loadUsage()]);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#login-pw").value }) });
    $("#login-error").textContent = "";
    boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#refresh").addEventListener("click", async () => {
  $("#refresh").textContent = "…";
  try { await api("/api/poll", { method: "POST" }); } catch (_) {}
  await loadUsage();
  $("#refresh").textContent = "↻";
});
$("#open-settings").addEventListener("click", () => $("#settings").classList.remove("hidden"));
$("#close-settings").addEventListener("click", () => $("#settings").classList.add("hidden"));
$("#save-claude").addEventListener("click", saveClaude);
$("#clear-claude").addEventListener("click", clearClaude);
$("#save-codex").addEventListener("click", saveCodex);
$("#clear-codex").addEventListener("click", clearCodex);

$("#chart-metric")?.addEventListener("change", loadChart);
document.querySelectorAll(".range-btn").forEach((b) =>
  b.addEventListener("click", () => {
    chartHours = Number(b.dataset.hours);
    document.querySelectorAll(".range-btn").forEach((x) => x.classList.toggle("active", x === b));
    loadChart();
  })
);
$("#send-snapshot")?.addEventListener("click", sendSnapshot);

const _canvas = $("#chart");
if (_canvas) {
  _canvas.addEventListener("mousemove", onChartHover);
  _canvas.addEventListener("mouseleave", () => { if (_chart) drawChart(_chart.points); });
}
$("#test-notify").addEventListener("click", testNotify);
$("#logout").addEventListener("click", () => { document.cookie = "session=; Max-Age=0; path=/"; showLogin(); });

// Refresh the view periodically (server polls on its own schedule).
setInterval(loadUsage, 30000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

boot();
