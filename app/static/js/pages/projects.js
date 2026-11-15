// Projects: upload -> 4 parallel AI analyses, level estimate, AI-assistance (non-judgmental), secret bug hunt.
import { api, pollJob } from "../api.js";
import { emptyState, jobTracker } from "../components.js";
import { $, captureDrafts, confidenceBadge, confirmDialog, esc, icon, md, meter, restoreDrafts, severityChip, statusChip, timeAgo, toast } from "../ui.js";

export async function render(root, app) {
  let projects = [];
  const lastHunt = {};
  const trackers = [];
  let refreshTimer = null;
  const files = [];

  root.innerHTML = `<div class="flex flex-col gap-6">
    <div class="flex flex-wrap items-end justify-between gap-3"><div>
      <h1 class="flex items-center gap-2 text-2xl font-extrabold">${icon("source-code", "code")}Projects</h1>
      <p class="text-sm muted">Upload code, a zip or screenshots. Four AI jobs analyse it in parallel - and any bugs found stay secret until you hunt them.</p></div></div>
    <div class="card glow-border">
      <div class="grid gap-4 lg:grid-cols-2">
        <div class="space-y-3">
          <label class="block"><span class="label">Project name</span><input id="p-name" class="input" maxlength="160" placeholder="My first API"></label>
          <label class="block"><span class="label">What does it do? (your idea, in your words)</span><textarea id="p-desc" class="input" rows="3" maxlength="4000"></textarea></label>
          <label id="p-drop" class="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-2xl border-2 border-dashed border-[color:var(--border)] p-5 text-center transition hover:border-[color:var(--accent)] focus-within:border-[color:var(--accent)] focus-within:ring-2 focus-within:ring-[color:var(--accent)]">
            ${icon("upload", "cloud-upload-alt", "ico8-lg")}<span class="font-semibold">Drop files / .zip / screenshots</span>
            <span class="text-xs muted">max ${app.config.max_upload_mb || 20} MB · node_modules, .git and binaries are skipped</span>
            <input id="p-files" type="file" multiple class="sr-only" aria-label="Choose project files"></label>
          <div id="p-filelist" class="flex flex-wrap gap-2"></div>
        </div>
        <div class="flex flex-col"><span class="label">...or paste code</span>
          <textarea id="p-paste" class="input input-mono flex-1" rows="10" placeholder="Paste a file or snippet here"></textarea>
          <button id="p-submit" class="btn btn-primary mt-3 self-end"><i class="la la-rocket"></i>Analyse in parallel</button></div>
      </div></div>
    <div id="p-list" class="flex flex-col gap-5"></div></div>`;

  const showFiles = () => { $("#p-filelist", root).innerHTML = files.map((f, i) => `<span class="chip chip-wrap"><i class="la la-file"></i>${esc(f.name)}<button data-rm="${i}" class="ml-1" aria-label="Remove ${esc(f.name)}"><i class="la la-times"></i></button></span>`).join(""); };
  $("#p-filelist", root).onclick = (e) => { const b = e.target.closest("[data-rm]"); if (b) { files.splice(Number(b.dataset.rm), 1); showFiles(); } };
  const add = (list) => { files.push(...Array.from(list)); showFiles(); };
  $("#p-files", root).onchange = (e) => { add(e.target.files); e.target.value = ""; };
  const drop = $("#p-drop", root);
  drop.ondragover = (e) => { e.preventDefault(); };
  drop.ondrop = (e) => { e.preventDefault(); add(e.dataTransfer.files); };

  $("#p-submit", root).onclick = async () => {
    const btn = $("#p-submit", root);
    const paste = $("#p-paste", root).value;
    if (!files.length && !paste.trim()) { toast("Add files or paste some code first.", "warn"); return; }
    const form = new FormData();
    form.append("name", $("#p-name", root).value);
    form.append("description", $("#p-desc", root).value);
    form.append("pasted_code", paste);
    files.forEach((f) => form.append("files", f, f.name));
    btn.disabled = true;
    btn.innerHTML = '<i class="la la-cog la-spin"></i>Uploading...';
    try {
      await api("/api/projects", { method: "POST", form });
      files.length = 0; showFiles();
      $("#p-paste", root).value = ""; $("#p-name", root).value = ""; $("#p-desc", root).value = "";
      toast("Uploaded - parallel analysis started.", "success");
      await load();
    } catch (e) { if (e.code !== "AI_OFFLINE") toast(e.message, "error", 7000); }
    finally { btn.disabled = false; btn.innerHTML = '<i class="la la-rocket"></i>Analyse in parallel'; }
  };

  function review(p) {
    const r = (p.analysis || {}).review;
    if (!r) return "";
    const list = (items, ico) => (items || []).map((x) => `<li class="flex gap-2"><i class="la ${ico} mt-1"></i><span>${esc(x)}</span></li>`).join("");
    return `<div class="rounded-xl border border-[color:var(--border)] p-4">
        <div class="flex flex-wrap items-center justify-between gap-2"><div class="label !mb-0">Qwen coder review</div>${confidenceBadge(r.meta)}</div>
        <p class="mt-2 text-sm">${esc(r.summary || "")}</p>
        <div class="mt-3 flex items-center justify-between text-xs"><span class="text-2">Estimated level</span><span class="font-semibold">${r.level_estimate != null ? Math.round(r.level_estimate) : "-"} / 10</span></div>
        <div class="mt-1">${meter(r.level_estimate, { max: 10, label: "estimated level" })}</div>
        <p class="mt-2 text-xs muted">${esc(r.level_reason || "")}</p>
        <div class="mt-3 grid gap-3 sm:grid-cols-2 text-sm">
          <div><div class="label">Strengths</div><ul class="space-y-1">${list(r.strengths, "la-check text-[color:var(--good-text)]")}</ul></div>
          <div><div class="label">Improve</div><ul class="space-y-1">${list(r.improvements, "la-arrow-up text-[color:var(--accent)]")}</ul></div>
        </div>
        ${(r.design_decisions || []).length ? `<div class="mt-3"><div class="label">Design decisions you'll be asked "why?" about</div><ul class="space-y-1 text-sm">${list(r.design_decisions, "la-question-circle")}</ul></div>` : ""}
      </div>`;
  }

  function aiDetection(p) {
    const d = (p.analysis || {}).ai_detection;
    if (!d || d.likelihood == null) return "";
    return `<div class="rounded-xl border border-[color:var(--border)] p-4">
        <div class="flex flex-wrap items-center justify-between gap-2"><div class="label !mb-0">AI-assistance estimate</div>${confidenceBadge(d.meta)}</div>
        <div class="mt-2 flex items-center justify-between text-xs"><span class="text-2">Likelihood the code is heavily AI-generated</span><span class="font-semibold">${Math.round(d.likelihood * 100)}%</span></div>
        <div class="mt-1">${meter(d.likelihood * 100, { label: "AI-assistance likelihood" })}</div>
        <p class="mt-2 text-sm"><i class="la la-handshake"></i> ${esc(d.note || "AI assistance is fine - the idea being yours is what matters.")}</p>
        <div class="mt-2 grid gap-2 text-xs sm:grid-cols-2">
          <div><div class="label">AI-style signals</div>${(d.ai_signals || []).map((s) => `<div>- ${esc(s)}</div>`).join("") || '<div class="muted">none</div>'}</div>
          <div><div class="label">Human signals</div>${(d.human_signals || []).map((s) => `<div>- ${esc(s)}</div>`).join("") || '<div class="muted">none</div>'}</div>
        </div>
        ${(d.parts_to_explain || []).length ? `<div class="mt-2 text-xs"><div class="label">Be ready to explain</div>${d.parts_to_explain.map((s) => `<div>- ${esc(s)}</div>`).join("")}</div>` : ""}
      </div>`;
  }

  function ocr(p) {
    const entries = (p.analysis || {}).ocr || [];
    if (!entries.length) return "";
    return `<div class="rounded-xl border border-[color:var(--border)] p-4 text-sm"><div class="label">Screenshots (multi-layer OCR)</div>
      ${entries.map((o) => `<div class="mt-1 flex flex-wrap items-center gap-2"><i class="la la-image"></i><span class="font-medium">${esc(o.file)}</span>
        ${o.error ? `<span class="chip chip-bad chip-wrap">${esc(o.error)}</span>` : `<span class="chip">via ${esc(o.engine)}</span>${confidenceBadge({ confidence: o.confidence, low_confidence: o.low_confidence }, { compact: true })}`}
        <span class="text-xs muted">${(o.layers || []).map((l) => `${esc(l.engine)}${l.skipped ? " (skipped)" : l.ok ? ` ${Math.round((l.confidence || 0) * 100)}%` : " x"}`).join(" -> ")}</span></div>`).join("")}</div>`;
  }

  function bugs(p) {
    const b = p.bugs || {};
    const bugMeta = (p.analysis || {}).bug_meta;
    if (!b.total && p.status !== "ready") return "";
    const sev = Object.entries(b.hidden_by_severity || {}).map(([k, n]) => `${n} ${k}`).join(", ");
    const visible = (b.visible || []).map((bug) => `<div class="rounded-xl border border-[color:var(--border)] p-3 text-sm">
        <div class="flex flex-wrap items-center gap-2">${severityChip(bug.severity)}${statusChip(bug.status)}<b>${esc(bug.title)}</b><span class="font-mono text-xs muted">${esc(bug.location)}</span>
        ${bug.low_confidence ? '<span class="chip chip-bad"><i class="la la-exclamation-triangle"></i>low confidence</span>' : ""}</div>
        <p class="mt-1 text-2">${esc(bug.description)}</p>${bug.fix ? `<div class="mt-2"><div class="label">Fix</div><div class="md">${md(bug.fix)}</div></div>` : ""}</div>`).join("");
    const attempts = ((p.analysis || {}).bughunt_attempts || []).length;
    return `<div class="rounded-xl border border-[color:var(--accent-2)]/40 p-4">
        <div class="flex flex-wrap items-center justify-between gap-2"><div class="section-title">${icon("bug", "bug")}Bug hunt</div>${confidenceBadge(bugMeta, { compact: true })}</div>
        ${b.hidden_count ? `<p class="mt-2 text-sm">${icon("lock", "lock", "!h-5 !w-5 inline")} <b>${b.hidden_count} hidden bug(s)</b> (${esc(sev)}) - kept secret until the next step. List what could go wrong (one hypothesis per line):</p>
          <textarea data-guess="${p.id}" data-draft="guess-${p.id}" class="input mt-2" rows="3" placeholder="What if the list is empty?&#10;What if two requests write the same file at once?"></textarea>
          <div class="mt-2 flex flex-wrap items-center gap-3 text-sm"><label class="flex items-center gap-2">How sure are you? <input data-conf="${p.id}" data-draft="pconf-${p.id}" type="range" min="0" max="100" value="50" class="w-28"> <span data-conf-v="${p.id}">50%</span></label>
            <button class="btn btn-primary btn-sm" data-hunt="${p.id}"><i class="la la-search"></i>Check my hypotheses</button>
            <button class="btn btn-soft btn-sm" data-hint="${p.id}"><i class="la la-lightbulb"></i>What-if hint</button>
            <button class="btn btn-ghost btn-sm" data-reveal="${p.id}"><i class="la la-unlock"></i>Reveal (next step)</button>
            ${attempts ? `<span class="text-xs muted">${attempts} attempt(s)</span>` : ""}</div>
          <div data-hunt-out="${p.id}">${lastHunt[p.id] || ""}</div>`
        : b.total ? '<p class="mt-2 text-sm text-[color:var(--good-text)]"><i class="la la-check-circle"></i> All bugs found or revealed.</p>' : '<p class="mt-2 text-sm muted">No bugs were found in this project.</p>'}
        ${visible ? `<div class="mt-3 grid gap-2">${visible}</div>` : ""}</div>`;
  }

  function card(p) {
    const a = p.analysis || {};
    const busy = ["queued", "analyzing"].includes(p.status);
    return `<article class="card fade-in" data-pid="${p.id}">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0"><h2 class="truncate text-lg font-bold">${esc(p.name)}</h2>
          <div class="mt-1 flex flex-wrap items-center gap-1.5">${statusChip(p.status)}${(p.languages || []).map((l) => `<span class="chip">${esc(l)}</span>`).join("")}
          <span class="text-xs muted">${p.files.length} file(s) · ${timeAgo(p.created_at)}</span></div>
          ${p.description ? `<p class="mt-2 text-sm text-2">${esc(p.description)}</p>` : ""}</div>
        <div class="flex flex-wrap items-center gap-2">
          ${busy ? '<span class="chip"><i class="la la-circle-notch la-spin"></i>analysing...</span>'
            : p.status === "failed" ? `<button class="btn btn-primary btn-sm" data-analyze="${p.id}"><i class="la la-redo"></i>Analyse again</button>`
              : `<button class="btn btn-ghost btn-sm" data-analyze="${p.id}" data-confirm="1" title="Run the 4 AI analyses again"><i class="la la-redo"></i>Re-analyse</button>`}
          <button class="icon-btn" data-del="${p.id}" title="Delete project" aria-label="Delete project ${esc(p.name)}"><i class="la la-trash"></i></button></div></div>
      ${p.status === "failed" && a.error ? `<p class="mt-3 rounded-xl border border-[color:var(--critical)]/40 p-3 text-sm text-[color:var(--critical)]"><i class="la la-exclamation-circle"></i> ${esc(a.error)}</p>` : ""}
      ${busy ? `<div class="mt-4 rounded-xl border border-[color:var(--border)] p-3"><div class="label">Parallel analysis</div><div data-tracker="${p.id}" class="text-sm"></div></div>` : ""}
      ${p.status === "ready" ? `<div class="mt-4 grid gap-4 xl:grid-cols-2">${review(p)}<div class="flex flex-col gap-4">${aiDetection(p)}${ocr(p)}</div></div><div class="mt-4">${bugs(p)}</div>` : ocr(p)}
      ${(a.skipped || []).length ? `<details class="mt-3 text-xs muted"><summary>${a.skipped.length} file(s) skipped</summary>${a.skipped.map((s) => `<div>${esc(s)}</div>`).join("")}</details>` : ""}
    </article>`;
  }

  let lastHtml = "";
  async function load() {
    if (!root.isConnected) return;  // the user left this page: stop polling
    const data = await api("/api/projects", { background: true });
    if (!root.isConnected) return;
    projects = data.projects;
    const list = $("#p-list", root);
    const html = projects.length ? projects.map(card).join("")
      : emptyState(icon("opened-folder", "folder-open", "ico8-lg"), "No projects yet", "Upload something you built - the AI will estimate your level and hide a few bugs for you to hunt.");
    if (html !== lastHtml) {  // unchanged: keep the DOM (focus, typed guesses, job trackers) as it is
      lastHtml = html;
      trackers.splice(0).forEach((stop) => stop());
      const drafts = captureDrafts(list);
      list.innerHTML = html;
      list.querySelectorAll("[data-tracker]").forEach((el) => trackers.push(jobTracker(el, { limit: 6 })));
      wire(list);
      restoreDrafts(list, drafts);
    }
    clearTimeout(refreshTimer);
    if (projects.some((p) => ["queued", "analyzing"].includes(p.status))) refreshTimer = setTimeout(load, 5000);
  }

  function wire(list) {
    list.querySelectorAll("[data-conf]").forEach((r) => (r.oninput = () => { list.querySelector(`[data-conf-v="${r.dataset.conf}"]`).textContent = `${r.value}%`; }));
    list.querySelectorAll("[data-hunt]").forEach((b) => (b.onclick = () => hunt(b.dataset.hunt, b)));
    list.querySelectorAll("[data-hint]").forEach((b) => (b.onclick = async () => {
      try { const r = await api(`/api/projects/${b.dataset.hint}/hint`); toast(`What if... ${r.what_if}`, "info", 12000); } catch (e) { toast(e.message, "warn"); }
    }));
    list.querySelectorAll("[data-reveal]").forEach((b) => (b.onclick = async () => {
      if (!(await confirmDialog("Reveal all hidden bugs?", "This is the next step: the remaining bugs and their fixes will be shown. Try one more hunt first?", { okLabel: "Reveal" }))) return;
      try { await api(`/api/projects/${b.dataset.reveal}/reveal`, { method: "POST" }); await load(); } catch (e) { toast(e.message, "error"); }
    }));
    list.querySelectorAll("[data-analyze]").forEach((b) => (b.onclick = async () => {
      if (b.dataset.confirm && !(await confirmDialog("Re-analyse this project?",
        "The 4 AI analyses run again and replace the current review, AI-assistance estimate and hidden bugs (bugs you have not found yet are replaced too).",
        { okLabel: "Re-analyse" }))) return;
      b.disabled = true;
      try { await api(`/api/projects/${b.dataset.analyze}/analyze`, { method: "POST" }); await load(); }
      catch (e) { b.disabled = false; if (e.code !== "AI_OFFLINE") toast(e.message, "error"); }
    }));
    list.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
      if (!(await confirmDialog("Delete project?", "This removes the project, its analysis and its hidden bugs.", { okLabel: "Delete", danger: true }))) return;
      try { await api(`/api/projects/${b.dataset.del}`, { method: "DELETE" }); await load(); } catch (e) { toast(e.message, "error"); }
    }));
  }

  async function hunt(id, btn) {
    const area = root.querySelector(`[data-guess="${id}"]`);
    const guesses = area.value.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 10);
    if (!guesses.length) { toast("Write at least one hypothesis.", "warn"); return; }
    const confidence = Number(root.querySelector(`[data-conf="${id}"]`).value);
    const out = root.querySelector(`[data-hunt-out="${id}"]`);
    btn.disabled = true;
    out.innerHTML = '<p class="mt-3 text-sm muted"><i class="la la-cog la-spin"></i> The Qwen coder is comparing your hypotheses with the hidden list...</p>';
    try {
      const res = await api(`/api/projects/${id}/bughunt`, { method: "POST", body: { guesses, confidence } });
      const job = await pollJob(res.job_id);
      if (job.status !== "succeeded") throw new Error(job.error || "Bug hunt check failed");
      const r = job.result;
      lastHunt[id] = `<div class="mt-3 rounded-xl bg-[color:var(--surface-2)] p-3 text-sm">
          <div class="flex flex-wrap items-center gap-2"><b>Found ${r.found.length}</b> · ${r.remaining} still hidden · foresight ${Math.round(r.foresight_score)}/100
          ${confidenceBadge({ confidence: job.confidence, low_confidence: job.low_confidence }, { compact: true })}</div>
          <div class="md mt-2">${md(r.feedback || "")}</div>
          ${(r.valid_new_findings || []).length ? `<div class="mt-2"><div class="label">Real issues you found that we missed</div>${r.valid_new_findings.map((x) => `<div>- ${esc(x)}</div>`).join("")}</div>` : ""}</div>`;
      out.innerHTML = lastHunt[id];
      await load();
    } catch (e) { out.innerHTML = ""; if (e.code !== "AI_OFFLINE") toast(e.message, "error"); }
    finally { btn.disabled = false; }
  }

  await load();
  return () => { trackers.forEach((stop) => stop()); clearTimeout(refreshTimer); };
}
