// GERTY admin — shadcn/ui vanilla JS

const $ = id => document.getElementById(id);

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch {}
    throw new Error(`HTTP ${r.status}${detail ? ": " + detail : ""}`);
  }
  return r.json();
}
function escapeHTML(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;",
  }[c]));
}
function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024*1024) return `${(n/1024).toFixed(1)} KB`;
  return `${(n/(1024*1024)).toFixed(1)} MB`;
}

// ─── Lucide-style icons (inline SVG, single source of truth) ────────────
const ICONS = {
  overview:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/></svg>`,
  prompt:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>`,
  models:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/></svg>`,
  stats:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" x2="18" y1="20" y2="10"/><line x1="12" x2="12" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="14"/></svg>`,
  chats:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
  resources: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/></svg>`,
  routines:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
  memory:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`,
  files:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>`,
  mcp:       `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg>`,
  voice:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>`,
  refresh:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>`,
  external:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>`,
  send:      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>`,
  play:      `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21 6 3"/></svg>`,
  stop:      `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12"/></svg>`,
  trash:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
  scroll:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/></svg>`,
};

// Mount icons into sidebar slots
document.querySelectorAll("[data-icon]").forEach(el => {
  el.innerHTML = ICONS[el.dataset.icon] || "";
});

// ─── Tab navigation ────────────────────────────────────────────────────
const tabsEl = $("tabs");
let activeTab = "overview";
const PANE_LOADERS = {};
// Set initial active
document.querySelector('[data-tab="overview"]').classList.add('active');
document.querySelector('[data-pane="overview"]').classList.add('active');

tabsEl.addEventListener("click", e => {
  const t = e.target.closest(".tab");
  if (!t) return;
  const name = t.dataset.tab;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === t));
  document.querySelectorAll(".pane").forEach(p =>
    p.classList.toggle("active", p.dataset.pane === name));
  activeTab = name;
  if (PANE_LOADERS[name]) PANE_LOADERS[name]();
});

// ─── Overview ─────────────────────────────────────────────────────────
const compGrid = $("components");
const btnRefresh = $("btnRefresh");
const btnMagic   = $("btnMagicLink");
const lnkTunnel  = $("lnkTunnel");
const logPanel   = $("logPanel");
const logTitle   = $("logTitle");
const logBody    = $("logBody");
const logClose   = $("logClose");

let currentLogKey = null;

function statusBadge(status) {
  if (status === "up")      return `<span class="badge badge-success">Online</span>`;
  if (status === "down")    return `<span class="badge badge-destructive">Offline</span>`;
  return `<span class="badge badge-warning">Unknown</span>`;
}

function renderCompCell(c) {
  const actions = [];
  if (c.can_start)   actions.push(`<button class="btn-success btn-xs" data-action="start"   data-key="${c.key}">Start</button>`);
  if (c.can_stop)    actions.push(`<button class="btn-destructive btn-xs" data-action="stop"    data-key="${c.key}">Stop</button>`);
  if (c.can_restart) actions.push(`<button class="btn-outline btn-xs" data-action="restart" data-key="${c.key}">Restart</button>`);
  if (c.has_log)     actions.push(`<button class="btn-ghost btn-xs"   data-action="log"     data-key="${c.key}">Log</button>`);
  const note = c.note ? `<div class="comp-note">${escapeHTML(c.note)}</div>` : "";
  return `
    <div class="comp-cell" data-key="${c.key}">
      <div class="comp-head">
        <div class="comp-name">${escapeHTML(c.label)}</div>
        ${statusBadge(c.status)}
      </div>
      <div class="comp-detail">${escapeHTML(c.detail || "—")}</div>
      ${note}
      <div class="comp-actions">${actions.join("")}</div>
    </div>`;
}

function renderQuickStrip({voice, tg, tunnel}) {
  const cells = [];
  if (voice && voice.voice) {
    cells.push({k: "Voice", v: `<span class="led"></span><span>${escapeHTML(voice.voice)}</span><span class="text-muted-foreground text-xs">${escapeHTML(voice.engine)}</span>`});
  } else {
    cells.push({k: "Voice", v: `<span class="led warn"></span><span class="text-muted-foreground">not configured</span>`});
  }
  if (tg && tg.configured) {
    cells.push({k: "Telegram", v: `<span class="led"></span><span class="mono">chat ${escapeHTML(tg.chat_id)}</span>`});
  } else {
    cells.push({k: "Telegram", v: `<span class="led down"></span><span>NOT configured</span>`});
  }
  if (tunnel && tunnel.url) {
    cells.push({k: "Tunnel", v: `<span class="led"></span><span class="mono truncate">${escapeHTML(tunnel.url.replace(/^https?:\/\//,''))}</span>`});
  } else {
    cells.push({k: "Tunnel", v: `<span class="led warn"></span><span class="text-muted-foreground">not running</span>`});
  }
  return cells.map(c =>
    `<div class="quick-cell"><div class="k">${escapeHTML(c.k)}</div><div class="v">${c.v}</div></div>`
  ).join("");
}

async function refreshOverview() {
  try {
    const [status, voice, tunnel, tg] = await Promise.all([
      fetchJSON("/api/status"),
      fetchJSON("/api/voice"),
      fetchJSON("/api/tunnel-url"),
      fetchJSON("/api/telegram-config"),
    ]);
    compGrid.innerHTML = status.components.map(renderCompCell).join("");
    $("quickStrip").innerHTML = renderQuickStrip({voice, tg, tunnel});
    const up = status.components.filter(c => c.status === 'up').length;
    $("footStatus").textContent = `${up}/${status.components.length} up`;
    if (tunnel.url) {
      lnkTunnel.href = tunnel.url;
      lnkTunnel.style.opacity = "1";
      lnkTunnel.style.pointerEvents = "auto";
    } else {
      lnkTunnel.href = "#";
      lnkTunnel.style.opacity = "0.4";
      lnkTunnel.style.pointerEvents = "none";
    }
    if (currentLogKey) showLog(currentLogKey, true);
  } catch (e) {
    $("quickStrip").innerHTML = `<div class="quick-cell"><div class="k">Error</div><div class="v">refresh failed: ${escapeHTML(e.message)}</div></div>`;
  }
}

async function doAction(key, action) {
  const btns = compGrid.querySelectorAll(`.comp-cell[data-key="${key}"] button`);
  btns.forEach(b => b.disabled = true);
  try {
    const r = await fetch(`/api/${action}/${key}`, { method: "POST" });
    const data = await r.json();
    if (!data.ok) alert(`${action} failed:\n${data.output || "(no output)"}`);
  } catch (e) {
    alert(`${action} error: ${e.message}`);
  } finally {
    btns.forEach(b => b.disabled = false);
    refreshOverview();
  }
}

async function showLog(key, reopen = false) {
  currentLogKey = key;
  const comp = compGrid.querySelector(`.comp-cell[data-key="${key}"] .comp-name`);
  if (comp) logTitle.textContent = comp.textContent.trim();
  try {
    const data = await fetchJSON(`/api/log/${key}?lines=200`);
    if (data.error)              logBody.textContent = `error: ${data.error}`;
    else if (data.note)          logBody.textContent = data.note;
    else if (data.lines?.length) logBody.textContent = data.lines.join("\n");
    else                         logBody.textContent = "(log empty)";
  } catch (e) {
    logBody.textContent = `error: ${e.message}`;
  }
  logPanel.hidden = false;
  if (!reopen) logBody.scrollTop = logBody.scrollHeight;
}

compGrid.addEventListener("click", e => {
  const b = e.target.closest("button[data-action]");
  if (!b) return;
  const key = b.dataset.key, action = b.dataset.action;
  if (action === "log") showLog(key);
  else doAction(key, action);
});

logClose.addEventListener("click", () => { logPanel.hidden = true; currentLogKey = null; });
btnRefresh.addEventListener("click", () => {
  refreshOverview();
  refreshVram();
  if (PANE_LOADERS[activeTab]) PANE_LOADERS[activeTab]();
});

btnMagic.addEventListener("click", async () => {
  btnMagic.disabled = true;
  const orig = btnMagic.textContent;
  btnMagic.textContent = "Sending…";
  try {
    const r = await fetch("/api/magic-link", { method: "POST" });
    const data = await r.json();
    btnMagic.textContent = data.ok ? "Sent ✓" : "Failed";
  } catch (e) {
    btnMagic.textContent = "Failed";
  } finally {
    setTimeout(() => { btnMagic.textContent = orig; btnMagic.disabled = false; refreshOverview(); }, 1500);
  }
});

// ─── VRAM ──────────────────────────────────────────────────────────────
async function refreshVram() {
  try {
    const v = await fetchJSON("/api/vram");
    const fill = $("vramFill");
    const num  = $("vramNumbers");
    const sub  = $("vramSub");
    if (!v.ok) {
      num.textContent = v.error || "unavailable";
      sub.textContent = "—";
      fill.style.width = "0%";
      return;
    }
    const pct = (v.used_mb / v.total_mb) * 100;
    num.innerHTML = `<span class="text-foreground">${(v.used_mb/1024).toFixed(1)} GB</span> <span class="text-muted-foreground">/ ${(v.total_mb/1024).toFixed(0)} GB · ${pct.toFixed(1)}%</span>`;
    sub.textContent = `${(v.free_mb/1024).toFixed(1)} GB free`;
    fill.style.width = `${pct.toFixed(1)}%`;
    fill.dataset.pressure = pct >= 95 ? "critical" : pct >= 80 ? "hot" : "normal";
  } catch {
    $("vramNumbers").textContent = "(refresh failed)";
  }
}

// ─── Models ────────────────────────────────────────────────────────────
function kvCard(label, value, mono = true) {
  return `<div class="card">
    <div class="card-body" style="padding:1rem 1.125rem">
      <div class="text-xs text-muted-foreground mb-1.5">${escapeHTML(label)}</div>
      <div class="${mono ? 'mono ' : ''}text-sm break-all">${escapeHTML(String(value))}</div>
    </div>
  </div>`;
}

async function loadModelConfig() {
  const c = await fetchJSON("/api/model-config").catch(() => null);
  const g = $("modelConfig");
  if (!c) { g.innerHTML = `<div class="empty">load failed</div>`; return; }
  g.innerHTML = [
    kvCard("Main model",      c.model || "—"),
    kvCard("Context",         c.context ? c.context.toLocaleString() : "—"),
    kvCard("Vision model",    c.vision_model || "—"),
    kvCard("Vision idle TTL", c.vision_ttl ? `${c.vision_ttl}s` : "—"),
    kvCard("Live (voice WS)", c.live_model || "—"),
    kvCard("Source",          c.source || "—"),
  ].join("");
}

function renderLlmRow(m) {
  const isLoaded = m.state === "loaded";
  const tags = [];
  if (isLoaded) tags.push(`<span class="badge badge-success">loaded</span>`);
  tags.push(`<span class="badge badge-outline">${escapeHTML(m.type)}</span>`);
  const ctx = isLoaded
    ? `<span class="meta">ctx ${m.loaded_context?.toLocaleString() || "?"}</span>`
    : `<input type="number" class="ctx-input" min="2048" step="2048" placeholder="ctx" />`;
  const btn = isLoaded
    ? `<button class="btn-destructive btn-sm" data-llm-action="unload" data-llm-id="${escapeHTML(m.id)}">Unload</button>`
    : `<button class="btn-primary btn-sm" data-llm-action="load" data-llm-id="${escapeHTML(m.id)}">Load</button>`;
  return `
    <div class="llm-row ${isLoaded ? "loaded" : ""}" data-llm-id="${escapeHTML(m.id)}">
      <div class="name">
        <div class="name-row">
          ${tags.join("")}
        </div>
        <div>${escapeHTML(m.id)}</div>
      </div>
      <div class="meta">${m.max_context ? "max " + m.max_context.toLocaleString() : ""}</div>
      <div>${ctx}</div>
      <div>${btn}</div>
    </div>`;
}

async function refreshLlm() {
  const llmTable = $("llmTable");
  try {
    const r = await fetchJSON("/api/llm/models");
    if (!r.ok)            { llmTable.innerHTML = `<div class="empty" style="color:hsl(0 84% 70%)">${escapeHTML(r.error || "LM Studio not reachable")}</div>`; return; }
    if (!r.models.length) { llmTable.innerHTML = `<div class="empty">no models found</div>`; return; }
    llmTable.innerHTML = r.models.map(renderLlmRow).join("");
  } catch (e) {
    llmTable.innerHTML = `<div class="empty" style="color:hsl(0 84% 70%)">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

document.addEventListener("click", async e => {
  const b = e.target.closest("button[data-llm-action]");
  if (!b) return;
  const action = b.dataset.llmAction, id = b.dataset.llmId;
  b.disabled = true;
  try {
    if (action === "unload") {
      await fetch("/api/llm/unload", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: id }),
      }).then(r => r.json()).then(d => { if (!d.ok) alert(`unload failed:\n${d.output || d.detail || ""}`); });
    } else if (action === "load") {
      const row = b.closest(".llm-row");
      const ctx = row.querySelector(".ctx-input")?.value;
      await fetch("/api/llm/load", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: id, context_length: ctx ? parseInt(ctx,10) : 0 }),
      }).then(r => r.json()).then(d => { if (!d.ok) alert(`load failed:\n${d.output || d.detail || ""}`); });
    }
  } finally {
    b.disabled = false;
    refreshLlm();
    refreshVram();
  }
});

const btnLlmRescan = $("btnLlmRefresh2");
if (btnLlmRescan) btnLlmRescan.addEventListener("click", () => { refreshLlm(); refreshVram(); });

$("btnUnloadAll").addEventListener("click", async () => {
  if (!confirm("Unload ALL loaded models? This frees all VRAM.")) return;
  const btn = $("btnUnloadAll");
  btn.disabled = true;
  try {
    const r = await fetch("/api/llm/unload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all: true }),
    });
    const data = await r.json();
    if (!data.ok) alert(`unload-all failed:\n${data.output || data.detail || ""}`);
  } finally {
    btn.disabled = false;
    refreshLlm();
    refreshVram();
  }
});

async function loadThinking() {
  try {
    const r = await fetchJSON("/api/stats");
    const on = r.thinking_mode === "on";
    $("thinkingToggle").checked = on;
    $("thinkingLabel").textContent = on
      ? "Chain-of-thought reasoning enabled"
      : "Direct replies, faster";
  } catch {}
}
$("thinkingToggle").addEventListener("change", async e => {
  await fetch("/api/thinking", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on: e.target.checked }),
  });
  loadThinking();
});

PANE_LOADERS.models = async () => {
  await Promise.all([loadModelConfig(), refreshLlm(), loadThinking()]);
};

// ─── Prompt ────────────────────────────────────────────────────────────
async function loadPrompt() {
  const body = $("promptBody"), meta = $("promptMeta");
  const banner = $("promptBanner"), bannerText = $("promptNoteText");
  try {
    const r = await fetchJSON("/api/system-prompt");
    body.textContent = r.prompt || "(empty)";
    meta.textContent = r.chars ? `${r.chars.toLocaleString()} chars · ${r.lines} lines` : "—";
    if (r.note) {
      bannerText.textContent = r.note;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  } catch (e) {
    body.textContent = `load failed: ${e.message}`;
  }
}
PANE_LOADERS.prompt = loadPrompt;

// ─── Stats ─────────────────────────────────────────────────────────────
function readout(k, v, cls = "") {
  return `<div class="readout">
    <div class="k">${escapeHTML(k)}</div>
    <div class="v ${cls}">${escapeHTML(String(v))}</div>
  </div>`;
}

async function loadStats() {
  try {
    const r = await fetchJSON("/api/stats");
    const u = r.usage || {};
    $("usageReadouts").innerHTML = [
      readout("Calls", u.call_count ?? 0),
      readout("Last prompt", (u.last_prompt_tokens ?? 0).toLocaleString()),
      readout("Last completion", (u.last_completion_tokens ?? 0).toLocaleString()),
      readout("Peak prompt", (u.peak_prompt_tokens ?? 0).toLocaleString()),
      readout("Cumulative in",  (u.cumulative_input ?? 0).toLocaleString()),
      readout("Cumulative out", (u.cumulative_output ?? 0).toLocaleString()),
      readout("Model", u.model || "—", "small muted"),
      readout("Last update", u.last_update || "—", "small muted"),
    ].join("");

    const list = r.histories || [];
    $("histList").innerHTML = list.length
      ? list.map(h =>
          `<div class="item" data-history-chat="${escapeHTML(h.chat_id)}">
             <div>
               <div class="title">chat ${escapeHTML(h.chat_id)}</div>
               <div class="subtitle">${h.turns} turns · ${h.size_kb} KB · last ${escapeHTML(h.last_modified || "—")}</div>
             </div>
             <div class="actions">
               <button class="btn-outline btn-sm" data-history-action="view">View</button>
             </div>
           </div>`
        ).join("")
      : `<div class="empty">no chat histories yet</div>`;
  } catch (e) {
    $("usageReadouts").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}
PANE_LOADERS.stats = loadStats;

// ─── Chats pane (own tab) ────────────────────────────────────────────
let CURRENT_HISTORY_CHAT = null;

function turnHTML(turn, idx) {
  const role = String(turn.role || "?").toLowerCase();
  const content = typeof turn.content === "string"
    ? turn.content
    : JSON.stringify(turn.content, null, 2);
  return `
    <div class="turn ${escapeHTML(role)}">
      <button class="btn-destructive btn-xs turn-del" data-turn-action="delete" data-turn-idx="${idx}">×</button>
      <div class="turn-head">
        <span class="turn-role">${escapeHTML(role)}</span>
        <span class="turn-idx">#${idx}</span>
      </div>
      <div class="turn-body">${escapeHTML(content)}</div>
    </div>`;
}

function switchToTab(name) {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.dataset.pane === name));
  activeTab = name;
  if (PANE_LOADERS[name]) PANE_LOADERS[name]();
}

async function loadChats() {
  try {
    const r = await fetchJSON("/api/stats");
    const list = r.histories || [];
    $("chatsMeta").textContent = list.length
      ? `${list.length} conversation${list.length !== 1 ? "s" : ""}`
      : "no conversations yet";
    $("chatsList").innerHTML = list.length
      ? list.map(h => `
          <div class="item clickable" data-history-chat="${escapeHTML(h.chat_id)}">
            <div>
              <div class="title">chat ${escapeHTML(h.chat_id)}</div>
              <div class="subtitle">${h.turns} turns · ${h.size_kb} KB · last ${escapeHTML(h.last_modified || "—")}</div>
            </div>
            <div class="actions">
              <button class="btn-outline btn-sm" data-history-action="view">Open</button>
            </div>
          </div>`).join("")
      : `<div class="empty">no chat histories yet</div>`;
    // If we have a chat already selected, refresh its turn list too
    if (CURRENT_HISTORY_CHAT) openChat(CURRENT_HISTORY_CHAT);
  } catch (e) {
    $("chatsList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}
PANE_LOADERS.chats = loadChats;

async function openChat(chatId) {
  CURRENT_HISTORY_CHAT = chatId;
  $("chatChatId").textContent = chatId;
  $("chatViewer").hidden = false;
  $("chatTurns").innerHTML = `<div class="empty">loading…</div>`;
  try {
    const r = await fetchJSON(`/api/history/${encodeURIComponent(chatId)}`);
    if (!r.turns.length) {
      $("chatTurns").innerHTML = `<div class="empty">no turns in this chat</div>`;
      return;
    }
    $("chatTurns").innerHTML = r.turns.map((t, i) => turnHTML(t, i)).join("");
  } catch (e) {
    $("chatTurns").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

$("chatsList").addEventListener("click", e => {
  const item = e.target.closest("[data-history-chat]");
  if (!item) return;
  openChat(item.dataset.historyChat);
});

// From the Stats tab summary list, "View" jumps to the Chats tab and opens it
$("histList").addEventListener("click", e => {
  const item = e.target.closest("[data-history-chat]");
  if (!item) return;
  switchToTab("chats");
  openChat(item.dataset.historyChat);
});

$("chatClose").addEventListener("click", () => {
  $("chatViewer").hidden = true;
  CURRENT_HISTORY_CHAT = null;
});

$("chatClear").addEventListener("click", async () => {
  if (!CURRENT_HISTORY_CHAT) return;
  if (!confirm(`Clear ALL turns from chat ${CURRENT_HISTORY_CHAT}? This cannot be undone.`)) return;
  try {
    await fetch(`/api/history/${encodeURIComponent(CURRENT_HISTORY_CHAT)}`, { method: "DELETE" });
    await openChat(CURRENT_HISTORY_CHAT);
    loadChats();
  } catch (e) { alert(e.message); }
});

$("chatTurns").addEventListener("click", async e => {
  const b = e.target.closest("button[data-turn-action='delete']");
  if (!b || !CURRENT_HISTORY_CHAT) return;
  const idx = parseInt(b.dataset.turnIdx, 10);
  if (!confirm(`Delete turn #${idx}? This rewrites the history file.`)) return;
  b.disabled = true;
  try {
    await fetch(`/api/history/${encodeURIComponent(CURRENT_HISTORY_CHAT)}/${idx}`, { method: "DELETE" });
    await openChat(CURRENT_HISTORY_CHAT);
    loadChats();
  } catch (e) { alert(e.message); b.disabled = false; }
});

// ─── Resources ─────────────────────────────────────────────────────────
function resBar(label, used, total, unit, pct, sub = "") {
  const pressure = pct >= 95 ? "critical" : pct >= 80 ? "hot" : "normal";
  return `
    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">${escapeHTML(label)}</h2>
          <p class="card-desc">${escapeHTML(sub)}</p>
        </div>
        <div class="mono tabular text-sm">
          <span class="text-foreground">${escapeHTML(used)}</span>
          <span class="text-muted-foreground">/ ${escapeHTML(total)} ${escapeHTML(unit)} · ${pct.toFixed(1)}%</span>
        </div>
      </div>
      <div class="card-body">
        <div class="progress"><div class="progress-fill" style="width:${pct.toFixed(1)}%" data-pressure="${pressure}"></div></div>
      </div>
    </div>`;
}

async function loadResources() {
  try {
    const [r, v] = await Promise.all([
      fetchJSON("/api/resources"),
      fetchJSON("/api/vram"),
    ]);
    const bars = [];
    if (v && v.ok)             bars.push(resBar("VRAM", (v.used_mb/1024).toFixed(1), (v.total_mb/1024).toFixed(0), "GB", (v.used_mb/v.total_mb)*100, "GPU memory"));
    if (r.ram)                 bars.push(resBar("RAM",  (r.ram.used_mb/1024).toFixed(1), (r.ram.total_mb/1024).toFixed(1), "GB", r.ram.percent, "System memory"));
    if (r.disk)                bars.push(resBar("Disk", r.disk.used_gb, r.disk.total_gb, "GB", r.disk.percent, "D:\\ drive"));
    if (r.cpu_percent != null) bars.push(resBar("CPU",  r.cpu_percent.toFixed(1), 100, "%", r.cpu_percent, `${r.cpu_count} logical cores`));
    $("resBars").innerHTML = bars.length ? bars.join("") : `<div class="empty">${escapeHTML(r.error || "no data")}</div>`;
  } catch (e) {
    $("resBars").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}
PANE_LOADERS.resources = loadResources;

// ─── Routines ──────────────────────────────────────────────────────────
async function loadRoutines() {
  try {
    const r = await fetchJSON("/api/routines");
    $("routinesMeta").textContent = `${r.routines.length} routines · chat ${r.chat_id}`;
    $("routinesList").innerHTML = r.routines.length
      ? r.routines.map(rt => {
          const status = rt.enabled
            ? `<span class="badge badge-success">enabled</span>`
            : `<span class="badge badge-secondary">disabled</span>`;
          const kind = `<span class="badge badge-outline">${rt.one_shot ? "one-shot" : "loop"}</span>`;
          const sched = `<span class="mono text-xs text-muted-foreground">${escapeHTML(rt.schedule)}</span>`;
          return `
            <div class="item col" data-rid="${escapeHTML(rt.id)}">
              <div class="title">${status}${kind}<span>${escapeHTML(rt.id)}</span></div>
              <div class="subtitle">${sched}</div>
              <div class="body">${escapeHTML(rt.prompt || "")}</div>
              <div class="actions">
                <button class="btn-outline btn-sm" data-rt-action="toggle" data-rt-enabled="${!rt.enabled}">${rt.enabled ? "Disable" : "Enable"}</button>
                <button class="btn-destructive btn-sm" data-rt-action="delete">Delete</button>
              </div>
            </div>`;
        }).join("")
      : `<div class="empty">no routines configured</div>`;
  } catch (e) {
    $("routinesList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

$("routinesList").addEventListener("click", async e => {
  const b = e.target.closest("button[data-rt-action]");
  if (!b) return;
  const item = b.closest(".item");
  const rid  = item.dataset.rid;
  const act  = b.dataset.rtAction;
  b.disabled = true;
  try {
    if (act === "toggle") {
      const enabled = b.dataset.rtEnabled === "true";
      await fetch(`/api/routines/${encodeURIComponent(rid)}/toggle`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } else if (act === "delete") {
      if (!confirm(`Delete routine '${rid}'?`)) { b.disabled = false; return; }
      await fetch(`/api/routines/${encodeURIComponent(rid)}`, { method: "DELETE" });
    }
  } finally {
    loadRoutines();
  }
});
PANE_LOADERS.routines = loadRoutines;

// ─── Memory (with timeline + filters) ─────────────────────────────────
let MEMORY_CACHE = [];

function relTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso); if (isNaN(t)) return iso;
  const now = Date.now();
  const diff = (now - t.getTime()) / 1000;
  if (diff < 60)       return "just now";
  if (diff < 3600)     return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400)    return `${Math.floor(diff/3600)}h ago`;
  if (diff < 86400*7)  return `${Math.floor(diff/86400)}d ago`;
  return t.toLocaleDateString(undefined, { month: "short", day: "numeric", year: t.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}

function dayBucket(iso) {
  if (!iso) return "Older";
  const t = new Date(iso); if (isNaN(t)) return "Older";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const ts = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const dayMs = 86400000;
  if (ts === today)            return "Today";
  if (ts === today - dayMs)    return "Yesterday";
  if (ts > today - dayMs * 7)  return "This week";
  if (ts > today - dayMs * 30) return "This month";
  return "Older";
}

function renderMemoryList() {
  const search = $("memSearch").value.toLowerCase().trim();
  const subject = $("memSubject").value;
  const sort = $("memSort").value;
  const when = $("memWhen").value;

  let entries = MEMORY_CACHE.slice();

  // Time filter
  if (when) {
    const ms = { "24h": 86400000, "7d": 86400000*7, "30d": 86400000*30 }[when] || 0;
    const cutoff = Date.now() - ms;
    entries = entries.filter(e => {
      const t = new Date(e.saved_at).getTime();
      return !isNaN(t) && t >= cutoff;
    });
  }
  // Subject filter
  if (subject) entries = entries.filter(e => e.subject === subject);
  // Search
  if (search) entries = entries.filter(e =>
    (e.name || "").toLowerCase().includes(search) ||
    (e.description || "").toLowerCase().includes(search));
  // Sort
  if (sort === "newest")      entries.sort((a, b) => (b.saved_at || "").localeCompare(a.saved_at || ""));
  else if (sort === "oldest") entries.sort((a, b) => (a.saved_at || "").localeCompare(b.saved_at || ""));
  else                        entries.sort((a, b) => a.name.localeCompare(b.name));

  const meta = `${entries.length}/${MEMORY_CACHE.length} entries`;
  $("memoryMeta").textContent = meta;

  if (!entries.length) {
    $("memoryList").innerHTML = `<div class="empty">no entries match these filters</div>`;
    return;
  }

  // For newest/oldest: group by day bucket. For name sort: group by subject.
  let html = "";
  if (sort === "name") {
    const groups = {};
    entries.forEach(e => (groups[e.subject] = groups[e.subject] || []).push(e));
    Object.keys(groups).sort().forEach(sub => {
      html += `<div class="group-h">${escapeHTML(sub)}</div>`;
      html += groups[sub].map(memCardHTML).join("");
    });
  } else {
    let currentBucket = null;
    entries.forEach(e => {
      const b = dayBucket(e.saved_at);
      if (b !== currentBucket) {
        html += `<div class="group-h">${escapeHTML(b)}</div>`;
        currentBucket = b;
      }
      html += memCardHTML(e);
    });
  }
  $("memoryList").innerHTML = html;
}

function memCardHTML(e) {
  const stamp = e.saved_at ? relTime(e.saved_at) : "—";
  return `
    <div class="item clickable" data-mem-name="${escapeHTML(e.name)}">
      <div class="min-w-0">
        <div class="title">
          <span class="truncate">${escapeHTML(e.name)}</span>
          <span class="saved-at">${escapeHTML(stamp)}</span>
        </div>
        <div class="subtitle">
          <span class="badge badge-outline" style="margin-right:0.375rem">${escapeHTML(e.subject)}</span>
          ${escapeHTML(e.description || "—")}
        </div>
      </div>
      <div class="actions">
        <button class="btn-ghost btn-xs" data-mem-action="delete" title="Delete">×</button>
      </div>
    </div>`;
}

async function loadMemory() {
  try {
    const r = await fetchJSON("/api/memory");
    MEMORY_CACHE = r.entries || [];
    // Populate subject dropdown
    const subj = $("memSubject");
    const current = subj.value;
    const opts = ['<option value="">All subjects</option>']
      .concat((r.subjects || []).map(s => `<option value="${escapeHTML(s)}">${escapeHTML(s)}</option>`));
    subj.innerHTML = opts.join("");
    if (current && (r.subjects || []).includes(current)) subj.value = current;
    renderMemoryList();
  } catch (e) {
    $("memoryList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

// Re-render on filter change without re-fetching
["memSearch", "memSubject", "memSort", "memWhen"].forEach(id => {
  $(id).addEventListener("input", renderMemoryList);
  $(id).addEventListener("change", renderMemoryList);
});

$("memoryList").addEventListener("click", async e => {
  const item = e.target.closest(".item[data-mem-name]");
  if (!item) return;
  const name = item.dataset.memName;
  const delBtn = e.target.closest("button[data-mem-action='delete']");
  if (delBtn) {
    e.stopPropagation();
    if (!confirm(`Delete memory '${name}'?`)) return;
    delBtn.disabled = true;
    try {
      await fetch(`/api/memory/${encodeURIComponent(name)}`, { method: "DELETE" });
      loadMemory();
      $("memoryView").textContent = "Select an entry on the left to preview it.";
      $("memorySelLabel").textContent = "no entry selected";
    } catch (e) { alert(e.message); }
    return;
  }
  document.querySelectorAll("#memoryList .item").forEach(i => i.classList.toggle("selected", i === item));
  $("memorySelLabel").textContent = name;
  try {
    const r = await fetchJSON(`/api/memory/${encodeURIComponent(name)}`);
    $("memoryView").textContent = r.content;
  } catch (e) {
    $("memoryView").textContent = `load failed: ${e.message}`;
  }
});
PANE_LOADERS.memory = loadMemory;

// ─── Files ─────────────────────────────────────────────────────────────
async function loadFiles() {
  try {
    const r = await fetchJSON("/api/files");
    $("filesMeta").textContent = `${r.files.length} files · ${r.root}`;
    $("filesList").innerHTML = r.files.length
      ? r.files.map(f => `
          <div class="item" data-file-path="${escapeHTML(f.path)}">
            <div>
              <div class="title mono text-xs">${escapeHTML(f.path)}</div>
              <div class="subtitle">${fmtBytes(f.size)} · ${escapeHTML(f.modified)}</div>
            </div>
            <div class="actions">
              <button class="btn-destructive btn-sm" data-file-action="delete">Delete</button>
            </div>
          </div>`).join("")
      : `<div class="empty">sandbox is empty</div>`;
  } catch (e) {
    $("filesList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

$("filesList").addEventListener("click", async e => {
  const b = e.target.closest("button[data-file-action]");
  if (!b) return;
  const path = b.closest(".item").dataset.filePath;
  if (!confirm(`Delete '${path}'?`)) return;
  b.disabled = true;
  try {
    await fetch(`/api/files?path=${encodeURIComponent(path)}`, { method: "DELETE" });
  } catch (e) { alert(e.message); }
  loadFiles();
});
PANE_LOADERS.files = loadFiles;

// ─── MCP ───────────────────────────────────────────────────────────────
async function loadMcp() {
  try {
    const r = await fetchJSON("/api/mcp");
    $("mcpList").innerHTML = r.servers.length
      ? r.servers.map(s => {
          const status = s.enabled
            ? `<span class="badge badge-success">enabled</span>`
            : `<span class="badge badge-secondary">disabled</span>`;
          const cmd = `${escapeHTML(s.command || "")} ${escapeHTML((s.args || []).join(" "))}`;
          return `
            <div class="item col" data-mcp-name="${escapeHTML(s.name)}">
              <div class="title">${status}<span>${escapeHTML(s.name)}</span></div>
              <div class="subtitle">${cmd}</div>
              <div class="actions">
                <button class="btn-outline btn-sm" data-mcp-action="toggle" data-enabled="${!s.enabled}">${s.enabled ? "Disable" : "Enable"}</button>
              </div>
            </div>`;
        }).join("")
      : `<div class="empty">no mcp servers configured</div>`;
  } catch (e) {
    $("mcpList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

$("mcpList").addEventListener("click", async e => {
  const b = e.target.closest("button[data-mcp-action]");
  if (!b) return;
  const name = b.closest(".item").dataset.mcpName;
  const enabled = b.dataset.enabled === "true";
  b.disabled = true;
  try {
    const r = await fetch(`/api/mcp/${encodeURIComponent(name)}/toggle`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await r.json();
    if (data.note) alert(data.note);
  } catch (e) { alert(e.message); }
  loadMcp();
});
PANE_LOADERS.mcp = loadMcp;

// ─── Voice ─────────────────────────────────────────────────────────────
async function loadVoice() {
  try {
    const [cur, lst] = await Promise.all([
      fetchJSON("/api/voice"),
      fetchJSON("/api/voice/list"),
    ]);
    $("voiceMeta").textContent = cur.voice ? `Current: ${cur.voice} (${cur.engine})` : "no voice set";
    if (lst.error) { $("voiceList").innerHTML = `<div class="empty">${escapeHTML(lst.error)}</div>`; return; }
    const groups = {};
    (lst.voices || []).forEach(v => (groups[v.engine] = groups[v.engine] || []).push(v));
    const engines = Object.keys(groups).sort();
    $("voiceList").innerHTML = engines.map(eng => `
      <div class="group-h">${escapeHTML(eng)}</div>
      ${groups[eng].map(v => {
        const sel = v.id === cur.voice;
        return `
          <div class="item ${sel ? "selected" : ""}">
            <div>
              <div class="title">${escapeHTML(v.label)}</div>
              <div class="subtitle">${escapeHTML(v.id)}</div>
            </div>
            <div class="actions">
              ${sel
                ? `<span class="badge badge-success">Current</span>`
                : `<button class="btn-outline btn-sm" data-voice-id="${escapeHTML(v.id)}" data-voice-engine="${escapeHTML(v.engine)}">Use</button>`
              }
            </div>
          </div>`;
      }).join("")}
    `).join("");
  } catch (e) {
    $("voiceList").innerHTML = `<div class="empty">load failed: ${escapeHTML(e.message)}</div>`;
  }
}

$("voiceList").addEventListener("click", async e => {
  const b = e.target.closest("button[data-voice-id]");
  if (!b) return;
  b.disabled = true;
  try {
    await fetch("/api/voice", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine: b.dataset.voiceEngine, voice: b.dataset.voiceId }),
    });
  } catch (e) { alert(e.message); }
  loadVoice();
});
PANE_LOADERS.voice = loadVoice;

// Wire the Chats "Reload" button explicitly (and let it survive a tab switch)
const _chatsRefreshBtn = $("chatsRefresh");
if (_chatsRefreshBtn) _chatsRefreshBtn.addEventListener("click", () => loadChats());

// ─── Initial paint + poll ──────────────────────────────────────────────
refreshOverview();
refreshVram();
refreshLlm();
loadChats();          // preload so the Chats tab shows data the instant it opens
setInterval(() => {
  refreshOverview();
  refreshVram();
  if (activeTab === "resources") loadResources();
  if (activeTab === "stats")     loadStats();
  if (activeTab === "chats")     loadChats();
}, 5000);
