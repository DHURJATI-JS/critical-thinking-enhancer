// Cognitive profile: estimated-IQ range (hero figure), skills meter list with uncertainty, KPI row, AI-analysed SWOT.
import { api, pollJob } from "../api.js";
import { LEVEL_NAMES } from "../components.js";
import { $, confidenceBadge, esc, icon, meter, modal, timeAgo, toast } from "../ui.js";

const SWOT_META = {
  strengths: { title: "Strengths", la: "la-dumbbell" },
  weaknesses: { title: "Weaknesses", la: "la-tools" },
  opportunities: { title: "Opportunities", la: "la-door-open" },
  threats: { title: "Threats", la: "la-exclamation-triangle" },
};
const STATUS = {
  supported: ["chip chip-good", "la-check-circle", "supported by evidence"],
  contradicted: ["chip chip-bad", "la-times-circle", "contradicted"],
  unverified: ["chip", "la-question-circle", "self-reported, unverified"],
  needs_more_chat: ["chip chip-warn", "la-comments", "needs more AI chatting"],
};
const DIM_STATUS = {
  insufficient: ["chip chip-warn", "la-comments", "needs more chatting"],
  emerging: ["chip chip-accent", "la-seedling", "emerging"],
  established: ["chip chip-good", "la-check-circle", "established"],
};
const SOURCE_LABEL = { chat: "chat", project: "project", challenge: "challenge", bughunt: "bug hunt", calibration: "self-rating", recall: "memory check" };

/** Several observations of the same skill (e.g. two projects both showing logic) are listed together under that
 *  skill - newest first, with where each one came from - instead of looking like duplicate rows. */
function evidenceBySkill(observations, dimensions) {
  if (!observations.length) return '<p class="py-3 text-sm muted">No evidence yet - chat, upload a project or try a challenge.</p>';
  const groups = new Map();
  observations.forEach((o) => { if (!groups.has(o.dimension)) groups.set(o.dimension, []); groups.get(o.dimension).push(o); });
  return [...groups.entries()].map(([dim, obs]) => {
    const label = (dimensions[dim] && dimensions[dim].label) || dim;
    const avg = Math.round(obs.reduce((sum, o) => sum + o.score, 0) / obs.length);
    const items = obs.map((o) => {
      const match = /^\[([^\]]{1,160})\]\s*/.exec(o.evidence || "");  // "[Project name] evidence..." -> subject + text
      const subject = match ? match[1] : "";
      const text = match ? o.evidence.slice(match[0].length) : o.evidence || "";
      return `<li class="flex items-start gap-3">
          <span class="grid h-9 w-10 shrink-0 place-items-center rounded-lg bg-[color:var(--surface-2)] text-sm font-bold tabular-nums">${Math.round(o.score)}</span>
          <div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-1.5 text-xs">
              <span class="chip">${esc(SOURCE_LABEL[o.source] || o.source)}</span>${subject ? `<span class="min-w-0 truncate font-medium text-2" title="${esc(subject)}">${esc(subject)}</span>` : ""}
              <span class="muted">${timeAgo(o.at)}</span></div>
            ${text ? `<p class="evidence-text mt-1 line-clamp-3 cursor-pointer text-sm text-2" title="Click to show all">${esc(text)}</p>` : ""}</div></li>`;
    }).join("");
    const more = obs.length > 2 ? `<button class="mt-2 text-xs font-semibold underline underline-offset-2 muted" data-more-toggle data-label="Show ${obs.length - 2} more">Show ${obs.length - 2} more</button>` : "";
    return `<div class="py-3"><div class="flex flex-wrap items-baseline justify-between gap-2"><b>${esc(label)}</b>
        <span class="text-xs muted">${obs.length} observation${obs.length === 1 ? "" : "s"}${obs.length > 1 ? ` · average ${avg}` : ""}</span></div>
      <ul class="evidence-list mt-2 space-y-3">${items}</ul>${more}</div>`;
  }).join('<div class="divider"></div>');
}

export async function render(root, app) {
  const data = await api("/api/profile");
  const s = data.summary;
  const p = data.user.profile || {};
  const prog = data.user.progression || {};
  const iq = s.iq;
  const genderLabel = { female: "Female", male: "Male", non_binary: "Non-binary", prefer_not: "Prefer not to say", self_describe: p.gender_custom || "Self-described" }[p.gender] || "";

  const dims = Object.values(s.dimensions);
  const skillRows = dims.map((d) => {
    const [cls, ico, label] = DIM_STATUS[d.status];
    const insufficient = d.status === "insufficient";
    const tip = `${d.observations} observation(s) · evidence weight ${d.evidence_weight} · sources: ${(d.sources || []).join(", ") || "none"}${d.trend != null ? ` · trend ${d.trend > 0 ? "+" : ""}${d.trend}` : ""}`;
    return `<div class="grid grid-cols-1 items-center gap-x-4 gap-y-1 py-2.5 sm:grid-cols-[200px_1fr_120px]" title="${esc(tip)}">
        <div class="font-semibold">${esc(d.label)}</div>
        <div>${insufficient ? meter(null, { muted: true, label: `${d.label}: not enough data` }) : meter(d.score, { low: d.low, high: d.high, label: `${d.label}: ${d.score} (range ${d.low}-${d.high})` })}</div>
        <div class="flex items-center gap-2 sm:justify-end">${insufficient ? `<span class="${cls}"><i class="la ${ico}"></i>${label}</span>`
          : `<span class="text-sm font-semibold tabular-nums">${Math.round(d.score)} <span class="muted font-normal">±${Math.round(d.margin)}</span></span><span class="${cls}" title="${esc(d.message)}"><i class="la ${ico}"></i></span>`}</div>
      </div>`;
  }).join('<div class="divider"></div>');

  const kpi = (label, value, sub = "") => `<div class="card card-tight"><div class="text-xs muted">${esc(label)}</div><div class="mt-1 text-2xl font-bold">${esc(value)}</div>${sub ? `<div class="text-xs muted">${esc(sub)}</div>` : ""}</div>`;
  const st = data.stats;
  const kpis = [
    kpi("Messages", st.messages),
    kpi("Projects", st.projects, data.coding.project_level_avg != null ? `avg level ${data.coding.project_level_avg}/10` : ""),
    kpi("Challenges done", st.challenges_done, st.avg_challenge_score != null ? `avg ${Math.round(st.avg_challenge_score)}/100` : ""),
    kpi("Bugs found", `${st.bugs_found}/${st.bugs_total}`, "hidden bugs hunted"),
    kpi("Level", `LVL ${prog.level}`, LEVEL_NAMES[(prog.level || 1) - 1]),
    kpi("AI assistance", data.coding.ai_assistance_avg != null ? `${Math.round(data.coding.ai_assistance_avg * 100)}%` : "-", "avg across projects"),
  ].join("");

  const iqBlock = iq.ready
    ? `<div class="flex flex-wrap items-end gap-4"><div class="hero-figure gradient-text">${iq.estimate}</div>
         <div class="pb-1"><div class="text-sm font-semibold">Estimated range ${iq.low}-${iq.high}</div><div class="text-xs muted">${s.total_observations} observations</div></div></div>
       <p class="mt-3 text-xs muted"><i class="la la-info-circle"></i> ${esc(iq.message)}</p>`
    : `<div class="flex items-center gap-3">${icon("stopwatch", "hourglass-half", "ico8-lg")}<div><div class="text-lg font-bold">Not enough data yet</div>
         <div class="text-sm muted">${esc(iq.message)}</div></div></div>
       <ul class="mt-3 space-y-1 text-sm">${Object.entries(iq.needed).map(([k, v]) => `<li class="flex justify-between gap-3"><span class="text-2">${esc(k.replace(/_/g, " "))}</span><span class="font-mono">${esc(v)}</span></li>`).join("")}</ul>`;

  const swotAI = data.swot_ai || {};
  const swotCards = Object.entries(SWOT_META).map(([key, meta]) => {
    const points = swotAI[key] || [];
    const self = (data.swot_self || {})[key];
    return `<div class="rounded-xl border border-[color:var(--border)] p-4">
        <div class="section-title"><i class="la ${meta.la} text-[color:var(--accent)]"></i>${meta.title}</div>
        <div class="mt-2 text-xs muted"><b>You wrote:</b> ${self ? esc(self) : "<i>nothing</i>"}</div>
        <ul class="mt-3 space-y-2">${points.length ? points.map((pt) => {
          const [cls, ico, label] = STATUS[pt.status] || STATUS.needs_more_chat;
          return `<li class="text-sm"><div class="flex flex-wrap items-center gap-2"><span>${esc(pt.point)}</span><span class="${cls}"><i class="la ${ico}"></i>${label}</span></div>${pt.note ? `<div class="text-xs muted">${esc(pt.note)}</div>` : ""}</li>`;
        }).join("") : `<li class="text-sm"><span class="chip chip-warn"><i class="la la-comments"></i>needs more AI chatting</span></li>`}</ul></div>`;
  }).join("");

  root.innerHTML = `<div class="flex flex-col gap-6">
    <div class="card glow-border flex flex-col gap-4 sm:flex-row sm:items-center">
      <div class="grid h-16 w-16 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-cyan-500 via-purple-500 to-pink-500 text-2xl font-extrabold text-white">${esc((p.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="min-w-0 flex-1"><h1 class="text-2xl font-extrabold">${esc(p.name || data.user.name)}</h1>
        <div class="mt-1 flex flex-wrap gap-1.5 text-xs">${p.age ? `<span class="chip">${esc(p.age)} years</span>` : ""}${genderLabel ? `<span class="chip">${esc(genderLabel)}</span>` : ""}${p.experience ? `<span class="chip">${esc(p.experience)}</span>` : ""}<span class="chip chip-accent">LVL ${prog.level} · ${LEVEL_NAMES[(prog.level || 1) - 1]}</span>${prog.completed ? '<span class="chip chip-good"><i class="la la-trophy"></i>Level 10 completed</span>' : ""}</div>
        ${p.bio ? `<p class="mt-2 line-clamp-3 text-sm text-2">${esc(p.bio)}</p>` : ""}</div>
      <div class="flex gap-2"><button id="pf-edit" class="btn btn-ghost btn-sm"><i class="la la-pen"></i>Edit</button><button id="pf-refresh" class="btn btn-primary btn-sm"><i class="la la-sync"></i>Refresh with AI</button></div></div>

    <div class="card flex items-start gap-3 ${data.readiness.ready ? "" : "border-[color:var(--warning)]/60"}">
      <i class="la ${data.readiness.ready ? "la-check-circle text-[color:var(--good)]" : "la-comments text-[color:var(--warning)]"} text-2xl"></i>
      <div class="text-sm"><b>${data.readiness.ready ? "Built from repeated evidence:" : "No jumping to conclusions:"}</b> ${esc(data.readiness.message)} <span class="muted">(${data.readiness.observations} observations)</span></div></div>

    <div class="grid gap-6 lg:grid-cols-[360px_1fr]">
      <div class="card"><div class="label">Estimated IQ (playful)</div>${iqBlock}</div>
      <div class="card"><div class="flex flex-wrap items-center justify-between gap-2"><div class="label !mb-0">Thinking skills (0-100, 50 = typical CS student)</div>
        <div class="flex items-center gap-3 text-[11px] muted"><span class="flex items-center gap-1"><span class="inline-block h-2 w-5 rounded-full" style="background:var(--viz-band)"></span>uncertainty range</span>
        <span class="flex items-center gap-1"><span class="inline-block h-3 w-[3px] rounded" style="background:var(--text)"></span>estimate</span></div></div>
        <div class="mt-2">${skillRows}</div></div></div>

    <div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">${kpis}</div>

    <div class="card"><div class="flex flex-wrap items-center justify-between gap-2"><div class="section-title">${icon("target", "bullseye")}SWOT - analysed by AI</div>
      <div class="flex items-center gap-2">${confidenceBadge(data.swot_meta)}${data.refreshed_at ? `<span class="text-xs muted">updated ${timeAgo(data.refreshed_at)}</span>` : ""}</div></div>
      <div class="mt-4 grid gap-4 md:grid-cols-2">${swotCards}</div></div>

    <div class="grid items-start gap-6 lg:grid-cols-2">
      <div class="card"><div class="section-title">${icon("brain", "brain")}What the AI sees so far</div>
        <div class="mt-3 text-sm text-2">${data.narrative ? esc(data.narrative) : (data.initial && data.initial.summary ? esc(data.initial.summary) + ' <span class="muted">(initial impression from onboarding - tentative)</span>' : '<span class="muted">No narrative yet - keep training and press "Refresh with AI".</span>')}</div>
        ${(data.focus_next || []).length ? `<div class="mt-4"><div class="label">Focus next</div>${data.focus_next.map((f) => `<div class="text-sm">- ${esc(f)}</div>`).join("")}</div>` : ""}
        ${(data.insufficient_areas || []).length ? `<div class="mt-4"><div class="label">Requires more AI chatting before concluding</div>${data.insufficient_areas.map((f) => `<div class="text-sm"><i class="la la-comments text-[color:var(--warning)]"></i> ${esc(f)}</div>`).join("")}</div>` : ""}</div>
      <div class="card"><div class="flex flex-wrap items-baseline justify-between gap-2"><div class="section-title">${icon("survey", "list")}Recent evidence</div>
          <span class="text-xs muted">latest ${data.recent_observations.length} observations, grouped by skill</span></div>
        <div class="mt-2">${evidenceBySkill(data.recent_observations, s.dimensions)}</div></div></div>

    <p class="card card-tight text-xs muted"><i class="la la-info-circle"></i> ${esc(data.disclaimer)}</p></div>`;

  // evidence: "Show N more" per skill, and click a long evidence text to read all of it
  root.querySelectorAll("[data-more-toggle]").forEach((b) => (b.onclick = () => {
    const list = b.previousElementSibling;
    const open = list.classList.toggle("show-all");
    b.textContent = open ? "Show less" : b.dataset.label;
  }));
  root.querySelectorAll(".evidence-text").forEach((p) => (p.onclick = () => p.classList.toggle("line-clamp-3")));

  // Re-analysis: one at a time. While one runs (started here, in another tab, or automatically) the button is
  // busy, and the page reloads with the new results as soon as it finishes.
  const refreshBtn = $("#pf-refresh", root);
  const setBusy = (busy) => {
    refreshBtn.disabled = busy;
    refreshBtn.setAttribute("aria-busy", String(busy));
    refreshBtn.innerHTML = busy ? '<i class="la la-circle-notch la-spin"></i>Re-analysing...' : '<i class="la la-sync"></i>Refresh with AI';
  };
  async function watchRefresh(jobId) {
    setBusy(true);
    try {
      const job = await pollJob(jobId);
      if (!root.isConnected) return;  // the user left this page
      if (job.status !== "succeeded") { toast(job.error || "The re-analysis failed", "error"); setBusy(false); return; }
      toast("Profile re-analysed - showing the new results.", "success");
      await render(root, app);  // refresh the whole page with the new SWOT, narrative and skills
    } catch (err) {
      if (!root.isConnected) return;
      setBusy(false);
      if (err.code !== "AI_OFFLINE") toast(err.message, "error");
    }
  }
  refreshBtn.onclick = async () => {
    setBusy(true);
    try {
      const res = await api("/api/profile/refresh", { method: "POST" });
      toast(res.already_running ? "A re-analysis is already running - this page updates when it's done."
        : "The AI is re-analysing your SWOT and profile...", "info");
      await watchRefresh(res.job_id);
    } catch (err) { setBusy(false); if (err.code !== "AI_OFFLINE") toast(err.message, "error"); }
  };
  if (data.refresh_job_id) watchRefresh(data.refresh_job_id);  // e.g. an automatic re-analysis is running

  $("#pf-edit", root).onclick = () => {
    const swot = data.swot_self || {};
    const m = modal({
      title: "Edit your profile", wide: true,
      body: `<div class="grid gap-3 sm:grid-cols-2">
          <label><span class="label">Name</span><input id="e-name" class="input" maxlength="60" value="${esc(p.name || "")}"></label>
          <label><span class="label">Age</span><input id="e-age" class="input" type="number" min="8" max="100" value="${esc(p.age || "")}"></label>
          <label class="sm:col-span-2"><span class="label">Biography</span><textarea id="e-bio" class="input" rows="4" maxlength="6000">${esc(p.bio || "")}</textarea></label>
          ${Object.keys(SWOT_META).map((k) => `<label><span class="label">${SWOT_META[k].title}</span><textarea id="e-${k}" class="input" rows="3" maxlength="2000">${esc(swot[k] || "")}</textarea></label>`).join("")}</div>`,
      actions: [{ label: "Cancel" }, {
        label: "Save", cls: "btn-primary", onClick: async () => {
          const v = (id) => $(id, m.el).value;
          try {
            await api("/api/profile", { method: "PUT", body: {
              name: v("#e-name").trim() || undefined, age: Number(v("#e-age")) || undefined, bio: v("#e-bio"),
              swot: Object.fromEntries(Object.keys(SWOT_META).map((k) => [k, v(`#e-${k}`)])),
            } });
            toast("Profile saved (stored in your local PostgreSQL)", "success");
            await app.refreshMe();
            if (root.isConnected) render(root, app);
          } catch (err) { toast(err.message, "error"); return true; }
          return false;
        },
      }],
    });
  };
}
