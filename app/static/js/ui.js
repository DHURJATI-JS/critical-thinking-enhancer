// UI helpers: escaping, safe markdown, toasts, modals, icons, meters, confidence badges, confetti.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function h(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = String(html).trim();
  return tpl.content.firstElementChild;
}

// ---------------------------------------------------------------- markdown (escape first, then format)
function inline(t) {
  return t
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^\w])_([^_\n]+)_(?=[^\w]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

export function md(src) {
  const blocks = [];
  let s = String(src ?? "").replace(/\r\n/g, "\n");
  s = s.replace(/```([\w+#.-]*)[^\n]*\n([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
    blocks.push({ lang, code: code.replace(/\n$/, "") });
    return `\n\u0000${blocks.length - 1}\u0000\n`;
  });
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0001${codes.length - 1}\u0001`; });
  let html = "";
  let para = [];
  let list = null;
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  const flushList = () => {
    if (list) { html += `<${list.type}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.type}>`; list = null; }
  };
  for (const raw of s.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) { flushPara(); flushList(); html += line.trim(); continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flushPara(); flushList(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = line.match(/^&gt;\s?(.*)$/))) { flushPara(); flushList(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }
    if (/^(-{3,}|\*{3,})$/.test(line.trim())) { flushPara(); flushList(); html += "<hr>"; continue; }
    if (list) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
    para.push(line);
  }
  flushPara();
  flushList();
  html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => {
    const b = blocks[Number(i)];
    return `<pre><span class="code-lang">${esc(b.lang || "code")}</span><code>${esc(b.code)}</code></pre>`;
  });
  return html.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

export function codeBlock(code, lang = "code") {
  return `<div class="md"><pre><span class="code-lang">${esc(lang)}</span><code>${esc(code)}</code></pre></div>`;
}

// ---------------------------------------------------------------- icons (Icons8 colour PNGs with Line Awesome fallback)
let icons8 = new Set();
export function setIconManifest(manifest) { icons8 = new Set((manifest && manifest.icons8) || []); }
export function icon(name, fallback = "circle", cls = "") {
  if (icons8.has(name)) return `<img class="ico8 ${cls}" src="/static/vendor/icons8/${name}.png" alt="" aria-hidden="true">`;
  return `<i class="la la-${fallback} text-2xl ${cls}" aria-hidden="true"></i>`;
}

// ---------------------------------------------------------------- formatting
export const pct = (x, digits = 0) => (x == null ? "-" : `${(Number(x) * 100).toFixed(digits)}%`);
export function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
export const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "");

/** 0.4 s / 12 s / 3m 05s / 1h 02m */
export function fmtDuration(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** "running 1m 05s" (counts up live) / "took 12.4 s" / "waiting 3 s" for a job from the API. */
export function jobTime(job) {
  if (job.duration_seconds != null) return `<span class="job-time" title="How long it took">took ${fmtDuration(job.duration_seconds)}</span>`;
  const live = (prefix, s) => `<span class="job-time job-time-live" data-elapsed="${s}" data-at="${Date.now()}" data-prefix="${prefix}">${prefix} ${fmtDuration(s)}</span>`;
  if (job.running_seconds != null) return live("running", job.running_seconds);
  if (job.waiting_seconds != null) return live("waiting", job.waiting_seconds);
  return "";
}
setInterval(() => {  // one shared 1-second ticker for every live job timer on the page
  document.querySelectorAll("[data-elapsed]").forEach((el) => {
    const s = Number(el.dataset.elapsed) + (Date.now() - Number(el.dataset.at)) / 1000;
    el.textContent = `${el.dataset.prefix} ${fmtDuration(s)}`;
  });
}, 1000);

// ---------------------------------------------------------------- confidence (65 % gate)
export function confidenceBadge(meta, { compact = false } = {}) {
  if (!meta || meta.confidence == null || meta.rule_based) return "";
  const p = Math.round(Number(meta.confidence) * 100);
  const bar = Math.round(Number(meta.threshold ?? 0.65) * 100);
  if (meta.low_confidence) {
    return `<span class="chip chip-bad" title="Below the ${bar}% acceptance bar after ${meta.attempts ?? 1} attempt(s). Shown with a warning - treat with caution."><i class="la la-exclamation-triangle"></i>Low confidence ${p}%${compact ? "" : " - treat with caution"}</span>`;
  }
  return `<span class="chip chip-good" title="Accepted: above the ${bar}% confidence bar${meta.attempts > 1 ? ` (after ${meta.attempts} attempts)` : ""}"><i class="la la-check-circle"></i>${p}% confident</span>`;
}

export function statusChip(status) {
  const map = {
    queued: ["chip", "la-clock", "queued"], running: ["chip chip-accent", "la-cog la-spin", "running"],
    waiting: ["chip chip-accent", "la-stream", "parallel jobs"], analyzing: ["chip chip-accent", "la-cog la-spin", "analysing"],
    generating: ["chip chip-accent", "la-cog la-spin", "generating"], evaluating: ["chip chip-accent", "la-cog la-spin", "evaluating"],
    succeeded: ["chip chip-good", "la-check", "done"], ready: ["chip chip-good", "la-check", "ready"], open: ["chip chip-accent", "la-play", "open"],
    evaluated: ["chip chip-good", "la-check-double", "evaluated"], revealed: ["chip", "la-unlock", "revealed"],
    failed: ["chip chip-bad", "la-times-circle", "failed"], hidden: ["chip", "la-lock", "hidden"], found: ["chip chip-good", "la-bullseye", "found by you"],
  };
  const [cls, ico, label] = map[status] || ["chip", "la-circle", status];
  return `<span class="${cls}"><i class="la ${ico}"></i>${esc(label)}</span>`;
}

export function severityChip(sev) {
  const cls = { critical: "chip-bad", high: "chip-bad", medium: "chip-warn", low: "chip" }[sev] || "chip";
  const ico = { critical: "la-skull-crossbones", high: "la-exclamation-circle", medium: "la-exclamation", low: "la-info-circle" }[sev] || "la-info";
  return `<span class="chip ${cls}"><i class="la ${ico}"></i>${esc(sev)}</span>`;
}

// ---------------------------------------------------------------- meters (data viz: one hue, lighter track, uncertainty band)
export function meter(value, { max = 100, low = null, high = null, muted = false, label = "" } = {}) {
  const clampPct = (v) => Math.max(0, Math.min(100, (Number(v) / max) * 100));
  if (muted || value == null) {
    return `<div class="meter-track meter-muted" role="img" aria-label="${esc(label || "not enough data")}"></div>`;
  }
  const v = clampPct(value);
  const band = low != null && high != null ? `<div class="meter-band" style="left:${clampPct(low)}%;width:${Math.max(1, clampPct(high) - clampPct(low))}%"></div>` : "";
  const fill = band ? "" : `<div class="meter-fill" style="width:${v}%"></div>`;
  const mark = band ? `<div class="meter-mark" style="left:${v}%"></div>` : "";
  return `<div class="meter-track" role="img" aria-label="${esc(label || `${Math.round(value)} of ${max}`)}">${band}${fill}${mark}</div>`;
}

// ---------------------------------------------------------------- toasts
export function toast(message, type = "info", timeout = 4200) {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const ico = { success: "la-check-circle", error: "la-times-circle", warn: "la-exclamation-triangle", info: "la-info-circle" }[type] || "la-info-circle";
  const tone = { success: "chip-good", error: "chip-bad", warn: "chip-warn", info: "chip-accent" }[type] || "chip-accent";
  const el = h(`<div class="card card-tight pointer-events-auto fade-in flex items-start gap-3 text-sm">
      <span class="chip ${tone} !px-1.5"><i class="la ${ico} text-base"></i></span>
      <div class="min-w-0 flex-1 break-words">${esc(message)}</div>
      <button class="muted hover:opacity-80" aria-label="Dismiss"><i class="la la-times"></i></button></div>`);
  el.querySelector("button").onclick = () => el.remove();
  root.appendChild(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
}

// ---------------------------------------------------------------- modals
// While any dialog (or the lock overlay) is open, the page behind it is inert: no clicks, no Tab focus.
const BACKGROUND = ["app-header", "banners", "view", "app-footer"];
let blockers = 0;
export function blockBackground(on) {
  blockers = Math.max(0, blockers + (on ? 1 : -1));
  BACKGROUND.forEach((id) => { const node = document.getElementById(id); if (node) node.inert = blockers > 0; });
}
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
let modalSeq = 0;

export function modal({ title = "", body = "", icon: ico = "", actions = [], dismissible = true, onClose, wide = false, tone = "" } = {}) {
  const root = document.getElementById("modal-root");
  const titleId = `modal-title-${++modalSeq}`;
  const el = h(`<div class="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4 backdrop-blur-sm fade-in" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <div class="card ${tone === "danger" ? "" : "glow-border"} w-full ${wide ? "max-w-2xl" : "max-w-lg"} max-h-[90vh] overflow-y-auto scroll-thin">
        <div class="mb-3 flex items-start gap-3">
          ${ico ? `<div class="shrink-0">${ico}</div>` : ""}
          <h2 id="${titleId}" class="min-w-0 flex-1 text-lg font-bold">${esc(title)}</h2>
          ${dismissible ? '<button class="icon-btn" data-close aria-label="Close"><i class="la la-times"></i></button>' : ""}
        </div>
        <div class="modal-body text-sm text-2"></div>
        <div class="modal-actions mt-5 flex flex-wrap justify-end gap-2"></div>
      </div></div>`);
  const bodyEl = el.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
  const opener = document.activeElement;
  let closed = false;
  const close = (result) => {
    if (closed) return;
    closed = true;
    el.remove();
    document.removeEventListener("keydown", onKey);
    blockBackground(false);
    if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();  // focus goes back
    if (onClose) onClose(result);
  };
  const onKey = (e) => {
    if (!el.isConnected || root.lastElementChild !== el) return;  // only the top-most dialog reacts
    if (e.key === "Escape" && dismissible) { close(); return; }
    if (e.key === "Tab") {  // keep keyboard focus inside the dialog
      const items = [...el.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !el.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", onKey);
  el.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => close()));
  if (dismissible) el.addEventListener("click", (e) => { if (e.target === el) close(); });
  const actionsEl = el.querySelector(".modal-actions");
  for (const a of actions) {
    const b = h(`<button class="btn ${a.cls || "btn-ghost"}">${a.icon ? `<i class="la ${a.icon}"></i>` : ""}${esc(a.label)}</button>`);
    b.onclick = async () => {
      if (a.onClick) {
        b.disabled = true;
        try { const keep = await a.onClick(close); if (keep !== true) close(a.value); } finally { b.disabled = false; }
      } else close(a.value);
    };
    actionsEl.appendChild(b);
  }
  root.appendChild(el);
  blockBackground(true);
  // focus the first field, else the main action, else the close button
  const target = bodyEl.querySelector("input:not([type=hidden]), textarea, select")
    || actionsEl.querySelector(".btn-primary, .btn-danger") || actionsEl.querySelector("button") || el.querySelector("[data-close]");
  if (target) setTimeout(() => { if (!closed) target.focus(); }, 30);
  return { el, close, body: bodyEl };
}

export function confirmDialog(title, text, { okLabel = "Confirm", danger = false } = {}) {
  return new Promise((resolve) => {
    modal({
      title, body: `<p>${esc(text)}</p>`, onClose: (v) => resolve(v === true),
      actions: [{ label: "Cancel", value: false }, { label: okLabel, value: true, cls: danger ? "btn-danger" : "btn-primary" }],
    });
  });
}

// ---------------------------------------------------------------- confetti (level 10 celebration)
export function confetti(duration = 4200) {
  const canvas = document.getElementById("confetti");
  if (!canvas || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const ctx = canvas.getContext("2d");
  canvas.classList.remove("hidden");
  const resize = () => { canvas.width = innerWidth; canvas.height = innerHeight; };
  resize();
  const colors = ["#22d3ee", "#e879f9", "#a3e635", "#fbbf24", "#60a5fa", "#f472b6"];
  const parts = Array.from({ length: 180 }, () => ({
    x: Math.random() * canvas.width, y: -20 - Math.random() * canvas.height * 0.5, r: 4 + Math.random() * 5,
    vx: -2 + Math.random() * 4, vy: 2 + Math.random() * 4, rot: Math.random() * 6, vr: -0.2 + Math.random() * 0.4,
    c: colors[Math.floor(Math.random() * colors.length)],
  }));
  const end = performance.now() + duration;
  const frame = (t) => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy; p.vy += 0.03; p.rot += p.vr;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.fillStyle = p.c; ctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); ctx.restore();
    }
    if (t < end) requestAnimationFrame(frame); else { ctx.clearRect(0, 0, canvas.width, canvas.height); canvas.classList.add("hidden"); }
  };
  requestAnimationFrame(frame);
}

/** Keep what the student typed (values, focus, caret) when a list re-renders on auto-refresh. */
export function captureDrafts(root) {
  const drafts = {};
  root.querySelectorAll("[data-draft]").forEach((el) => { drafts[el.dataset.draft] = el.value; });
  const active = document.activeElement;
  const focused = active && root.contains(active) && active.dataset ? active.dataset.draft : null;
  let selection = null;
  try { selection = focused ? [active.selectionStart, active.selectionEnd] : null; } catch { selection = null; }
  return { drafts, focused, selection };
}

export function restoreDrafts(root, snapshot) {
  if (!snapshot) return;
  for (const [key, value] of Object.entries(snapshot.drafts)) {
    const el = root.querySelector(`[data-draft="${CSS.escape(key)}"]`);
    if (el && value !== undefined && el.value !== value) {
      el.value = value;
      el.dispatchEvent(new Event("input"));
    }
  }
  if (snapshot.focused) {
    const el = root.querySelector(`[data-draft="${CSS.escape(snapshot.focused)}"]`);
    if (el) {
      el.focus({ preventScroll: true });
      try { if (snapshot.selection) el.setSelectionRange(snapshot.selection[0], snapshot.selection[1]); } catch { /* range inputs */ }
    }
  }
}

export function autosize(textarea, max = 260) {
  const fit = () => { textarea.style.height = "auto"; textarea.style.height = Math.min(max, textarea.scrollHeight) + "px"; };
  textarea.addEventListener("input", fit);
  fit();
}
