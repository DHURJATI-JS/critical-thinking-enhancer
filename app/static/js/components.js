// Shared components: parallel job tracker, level names, small renderers.
import { api, sleep } from "./api.js";
import { confidenceBadge, esc, jobTime, modal, statusChip, toast } from "./ui.js";

// ---------------------------------------------------------------- accounts remembered on THIS device
// Only public bits (username, display name, avatar) are stored - never passwords or tokens.
const ACCOUNTS_KEY = "cte-accounts";

export function deviceAccounts() {
  try {
    const list = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "[]");
    return Array.isArray(list) ? list.filter((a) => a && a.username) : [];
  } catch { return []; }
}

export function rememberAccount(user) {
  if (!user || !user.username) return;
  const avatar = (user.settings && user.settings.avatar) || {};
  const list = deviceAccounts().filter((a) => a.username !== user.username);
  list.unshift({ username: user.username, name: user.name, face: avatar.face || "bot", hue: avatar.hue ?? 190, last: Date.now() });
  try { localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(list.slice(0, 12))); } catch { /* storage blocked */ }
}

export function forgetAccount(username) {
  try { localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(deviceAccounts().filter((a) => a.username !== username))); } catch { /* ignore */ }
}

/** A deleted account leaves nothing behind in this browser: not on the sign-in screen, not its mute setting. */
export function eraseFromDevice(username) {
  forgetAccount(username);
  try { localStorage.removeItem(`cte-muted:${username}`); } catch { /* ignore */ }
}

export const LEVEL_NAMES = ["Warm-up", "Explorer", "Apprentice", "Debugger", "Analyst", "Architect", "Skeptic", "Adversary", "Sentinel", "Grandmaster"];
export const LEVEL_SCRUTINY = ["gentle", "gentle", "light", "moderate", "moderate", "firm", "strict", "strict", "severe", "relentless"];

/** Pick your own scrutiny level (Settings + the chat level card). Resolves with the server's answer, or null. */
export async function openLevelDialog() {
  const data = await api("/api/progression/levels");
  const levels = data.levels || [];
  const current = data.current || 1;
  return new Promise((resolve) => {
    let chosen = current;
    let result = null;
    const preview = (n) => {
      const L = levels[n - 1] || {};
      const delta = n === current ? '<span class="chip">your current level</span>'
        : n > current ? '<span class="chip chip-accent"><i class="la la-arrow-up"></i>harder</span>'
          : '<span class="chip chip-good"><i class="la la-arrow-down"></i>easier</span>';
      return `<div class="flex flex-wrap items-center gap-2"><span class="text-xl font-extrabold">LVL ${n} <span class="gradient-text">${esc(L.name || "")}</span></span>
          <span class="chip chip-accent">${esc(L.scrutiny || "")} scrutiny</span>${delta}</div>
        <p class="mt-2 text-sm text-2">${esc(L.rules || "")}</p>
        <div class="mt-3 grid grid-cols-3 gap-2 text-xs">
          <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Context memory</div><div class="font-semibold">${(L.context_tokens_effective || 0).toLocaleString()} tok</div></div>
          <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Thinking depth</div><div class="font-semibold">${L.samples || 1}x · ${esc(L.reasoning_effort || "low")}</div></div>
          <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Memory recalls</div><div class="font-semibold">${L.memory_recalls || 0} / turn</div></div></div>`;
    };
    const m = modal({
      title: "Change level & scrutiny",
      wide: true,
      body: `<p>How strict should your coach be? Lower it when it feels like too much, raise it when you want a real challenge.</p>
        <label class="mt-4 block"><span class="label">Level <b id="lv-num">${current}</b> of 10</span>
          <input id="lv-range" type="range" min="1" max="10" step="1" value="${current}" class="w-full accent-[color:var(--accent)]"
            aria-label="Scrutiny level, 1 gentle to 10 relentless"></label>
        <div class="flex justify-between font-mono text-[10px] muted"><span>1 gentle</span><span>10 relentless</span></div>
        <div id="lv-preview" class="mt-4 rounded-xl border border-[color:var(--border)] p-4">${preview(current)}</div>
        <p class="mt-3 text-xs muted"><i class="la la-info-circle"></i> Your level bar starts again from 0 % at the new level. A completed Level 10 stays completed.</p>`,
      onClose: () => resolve(result),
      actions: [{ label: "Cancel" }, {
        label: "Apply level", cls: "btn-primary", icon: "la-check", onClick: async () => {
          if (chosen === current) return false;
          try {
            result = await api("/api/progression/level", { method: "PUT", body: { level: chosen }, quiet: true });
          } catch (e) { toast(e.message, "error"); return true; }
          return false;
        },
      }],
    });
    const range = m.el.querySelector("#lv-range");
    range.oninput = () => {
      chosen = Number(range.value);
      m.el.querySelector("#lv-num").textContent = String(chosen);
      m.el.querySelector("#lv-preview").innerHTML = preview(chosen);
    };
  });
}

const KIND_ICON = {
  chat_turn: "la-comments", onboarding_analysis: "la-user-astronaut", project_ingest: "la-file-import", project_group: "la-stream",
  project_review: "la-code", project_bughunt: "la-bug", project_ai_detect: "la-robot", project_cognitive: "la-brain",
  project_aggregate: "la-layer-group", challenge_generate: "la-puzzle-piece", challenge_evaluate: "la-clipboard-check",
  bug_guess_eval: "la-search", profile_refresh: "la-sync", ocr_image: "la-eye",
};

function jobRow(job, child = false) {
  const kids = (job.children || []).map((c) => jobRow(c, true)).join("");
  const err = job.status === "failed" && job.error ? `<div class="mt-1 text-xs text-[color:var(--critical)]">${esc(job.error.slice(0, 220))}</div>` : "";
  return `<div class="${child ? "ml-6 border-l border-[color:var(--border)] pl-3" : ""} py-1.5">
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <i class="la ${KIND_ICON[job.kind] || "la-cog"} text-lg text-[color:var(--accent)]"></i>
        <span class="font-medium">${esc(job.label || job.kind)}</span>
        ${statusChip(job.status)}
        ${job.status === "succeeded" ? confidenceBadge({ confidence: job.confidence, low_confidence: job.low_confidence }, { compact: true }) : ""}
        ${job.provider ? `<span class="font-mono text-[11px] muted">${esc(job.provider)}</span>` : ""}
        ${jobTime(job)}
      </div>${err}${kids}</div>`;
}

/** Live view of the user's recent jobs (parents with their parallel children). */
export function jobTracker(container, { ids = null, limit = 8, onDone } = {}) {
  let stopped = false;
  (async () => {
    while (!stopped) {
      try {
        const data = await api("/api/jobs", { quiet: true, background: true });
        let jobs = data.jobs || [];
        if (ids) {
          const wanted = new Set(ids);
          jobs = jobs.filter((j) => wanted.has(j.id) || (j.children || []).some((c) => wanted.has(c.id)) || j.kind === "project_group" || j.kind === "project_aggregate");
        }
        jobs = jobs.slice(0, limit);
        container.innerHTML = jobs.length ? jobs.map((j) => jobRow(j)).join('<div class="divider my-1"></div>') : '<p class="text-sm muted">No AI jobs yet.</p>';
        const active = jobs.some((j) => ["queued", "running", "waiting"].includes(j.status) || (j.children || []).some((c) => ["queued", "running"].includes(c.status)));
        if (!active && onDone) { onDone(jobs); onDone = null; }
      } catch { /* keep polling */ }
      await sleep(1200);
    }
  })();
  return () => { stopped = true; };
}

export function emptyState(iconHtml, title, text, actionHtml = "") {
  return `<div class="card grid place-items-center gap-3 py-12 text-center">
      <div class="opacity-90">${iconHtml}</div><h3 class="text-lg font-bold">${esc(title)}</h3>
      <p class="max-w-md text-sm muted">${esc(text)}</p>${actionHtml}</div>`;
}
