// System status: AI endpoints (live), hardware & GPU, context budgets, OCR layers, worker queue, Redis, AI server setup.
import { api } from "../api.js";
import { esc, icon, jobTime, statusChip, timeAgo } from "../ui.js";

/** Adaptive gateway concurrency: all slots when the AI server is free, fewer when other work uses it. */
function parallelText(c) {
  const cpu = c.cpu_percent != null ? ` · CPU ${Math.round(c.cpu_percent)}%` : "";
  if (c.mode === "max") return `<b class="text-[color:var(--good-text)]">${c.limit} requests</b> <span class="muted">- maximum speed (the AI server is free)${cpu}</span>`;
  if (c.mode === "shared") return `<b>${c.limit} of ${c.slots} slots</b> <span class="muted">- sharing: ${c.others_busy} slot(s) busy with other work${cpu}</span>`;
  return `<b>${c.limit} requests</b> <span class="muted">- fixed (the server reports no slots)</span>`;
}

export async function render(root, app) {
  let timer = null;
  let alive = true;

  const endpoint = (key, info) => {
    const disabled = info && info.disabled;
    const ok = info && info.ok;
    return `<div class="rounded-xl border border-[color:var(--border)] p-4">
        <div class="flex items-center justify-between gap-2"><b>${esc((info && info.label) || key)}</b>
          ${disabled ? '<span class="chip">disabled</span>' : ok ? '<span class="chip chip-good"><span class="dot dot-on"></span>online</span>' : '<span class="chip chip-bad"><span class="dot dot-off"></span>offline</span>'}</div>
        <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          <dt class="muted">model</dt><dd class="font-mono break-all">${esc((info && info.model) || "-")}</dd>
          <dt class="muted">endpoint</dt><dd class="font-mono">${esc((info && info.endpoint) || "-")}</dd>
          <dt class="muted">latency</dt><dd>${info && info.latency_ms != null ? `${info.latency_ms} ms` : "-"}</dd>
          <dt class="muted">context</dt><dd>${info && info.max_model_len ? `${Number(info.max_model_len).toLocaleString()} tokens` : "not reported"}</dd>
          <dt class="muted">status</dt><dd class="${ok ? "" : "text-[color:var(--critical)]"}">${esc((info && info.detail) || "-")}</dd>
          ${info && info.concurrency ? `<dt class="muted">parallel</dt><dd>${parallelText(info.concurrency)}</dd>` : ""}</dl>
        ${info && info.ai_server ? `<div class="mt-3"><div class="label">AI server backends (load-balanced)</div><div class="flex flex-wrap gap-1.5">${
          (info.ai_server.backends || []).map((b) => `<span class="chip ${b.online ? "chip-good" : "chip-bad"}" title="${b.overloaded ? "overloaded" : "accepting work"}">
            <span class="dot ${b.online ? "dot-on" : "dot-off"}"></span>${esc(b.name)} <span class="font-mono">${esc(b.busy_slots || "")}</span></span>`).join("")}</div></div>` : ""}</div>`;
  };

  async function paint() {
    const d = await api("/api/system", { quiet: true });
    if (!alive) return;
    const hw = d.hardware;
    const gpu = hw.primary_gpu;
    const q = d.queue;
    const hlth = d.health;
    root.innerHTML = `<div class="flex flex-col gap-6">
      <div class="flex flex-wrap items-end justify-between gap-2"><h1 class="flex items-center gap-2 text-2xl font-extrabold">${icon("processor", "server")}System</h1>
        <span class="text-xs muted">v${esc(d.version)} · up ${Math.round(d.uptime_seconds / 60)} min · AI pinged every ${hlth.interval_seconds}s</span></div>

      <section class="card"><div class="flex flex-wrap items-center justify-between gap-2"><div class="section-title">${icon("artificial-intelligence", "brain")}AI servers</div>
        ${hlth.ai_online ? '<span class="chip chip-good"><i class="la la-check-circle"></i>all required AI online</span>' : '<span class="chip chip-bad"><i class="la la-plug"></i>required AI offline - tasks halted</span>'}</div>
        <div class="mt-4 grid gap-3 md:grid-cols-3">${endpoint("gateway", hlth.roles.gateway)}${endpoint("coder", hlth.roles.coder)}${endpoint("vision", hlth.roles.vision)}</div>
        <div class="mt-3 flex flex-wrap gap-2 text-xs">
          <span class="chip chip-wrap"><i class="la la-database"></i>PostgreSQL + pgvector: ${hlth.database.ok ? `ok (${hlth.database.latency_ms} ms)` : esc(hlth.database.detail)}</span>
          <span class="chip chip-wrap"><i class="la la-bolt"></i>Redis: ${d.redis.available ? "connected" : esc(d.redis.detail)}</span>
          <span class="chip"><i class="la la-history"></i>outages: ${hlth.outage_count}${hlth.last_outage ? ` (last ${timeAgo(hlth.last_outage.at)}, ${hlth.last_outage.halted_jobs} job(s) halted)` : ""}</span>
          <span class="chip"><i class="la la-shield-alt"></i>confidence bar ${Math.round(d.confidence_threshold * 100)}%</span>
          ${d.fallback_ready ? '<span class="chip chip-good"><i class="la la-cloud"></i>your fallback key is ready</span>' : ""}</div></section>

      <div class="grid gap-6 lg:grid-cols-2">
        <section class="card"><div class="section-title">${icon("processor", "microchip")}Hardware (auto-detected)</div>
          <dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt class="muted">OS</dt><dd>${esc(hw.os)} (${esc(hw.machine)})</dd>
            <dt class="muted">CPU</dt><dd>${esc(hw.cpu)} · ${hw.cpu_cores} threads</dd>
            <dt class="muted">RAM</dt><dd>${hw.ram_gb} GB</dd>
            <dt class="muted">GPU</dt><dd>${gpu ? `${esc(gpu.name)} · <b>${esc(String(gpu.vendor).toUpperCase())}</b> · ${gpu.vram_gb ?? "?"} GB VRAM` : "no NVIDIA / AMD GPU detected"}</dd>
            <dt class="muted">Tier</dt><dd><span class="chip chip-accent">${esc(hw.tier)}</span></dd>
            <dt class="muted">Recommended coder</dt><dd class="font-mono text-xs break-all">${esc(hw.recommended_coder_model || "use a remote AI server or your own API key")}</dd>
            <dt class="muted">vLLM GPU support</dt><dd>${hw.vllm_gpu_supported ? "yes" : "no on this OS/GPU combination"}</dd>
            <dt class="muted">Context budget</dt><dd>gateway ${d.budgets.gateway.toLocaleString()} · coder ${d.budgets.coder.toLocaleString()} tokens</dd></dl>
          ${d.low_end.low_end ? `<div class="mt-3 rounded-xl border border-[color:var(--critical)]/50 p-3 text-sm"><b>Too low-end:</b> ${esc(d.low_end.reasons.join(" "))}<br>${esc(d.low_end.advice)}</div>` : ""}
          ${(d.low_end.notes || []).map((n) => `<div class="mt-3 rounded-xl border border-[color:var(--warning)]/60 p-3 text-sm">${esc(n)}</div>`).join("")}</section>

        <section class="card"><div class="section-title">${icon("visible", "eye")}OCR layers</div>
          <ol class="mt-3 space-y-2 text-sm">${d.ocr.order.map((layer, i) => {
            const info = layer === "system" ? d.ocr.system : layer === "vision_llm" ? d.ocr.vision_llm : d.ocr[layer] || {};
            const avail = layer === "vision_llm" ? info.enabled && info.online : info.available;
            const detail = layer === "tesseract" ? (info.path || "not installed - the installer adds it")
              : layer === "rapidocr" ? "ONNX PP-OCR models" : layer === "system" ? `${info.engine || "none"} - ${info.detail || ""}${info.enabled ? "" : " (disabled)"}`
              : `MiniCPM-V on the GPU, temperature ${info.temperature} - used when ${info.used_when}`;
            return `<li class="flex items-start gap-2"><span class="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-[color:var(--surface-3)] text-xs font-bold">${i + 1}</span>
              <div class="min-w-0"><div class="flex items-center gap-2"><b>${esc(layer)}</b>${avail ? '<span class="chip chip-good">available</span>' : '<span class="chip">unavailable</span>'}</div>
              <div class="text-xs muted break-all">${esc(detail)}</div></div></li>`;
          }).join("")}</ol>
          <p class="mt-3 text-xs muted">First layer above ${Math.round(d.ocr.accept_confidence * 100)}% confidence wins; otherwise the best result is shown with a low-confidence warning.</p></section></div>

      <section class="card"><div class="flex flex-wrap items-center justify-between gap-2"><div class="section-title">${icon("workflow", "stream")}Parallel worker queue</div>
        <span class="text-xs muted">${esc(q.backend)} · ${q.workers} workers · ${q.in_flight} in flight</span></div>
        <div class="mt-3 flex flex-wrap gap-2">${Object.entries(q.by_status).map(([k, v]) => `<span class="chip">${esc(k)}: <b>${v}</b></span>`).join("") || '<span class="text-sm muted">empty</span>'}
          <span class="chip">processed this run: ${q.processed_this_run}</span><span class="chip">failed: ${q.failed_this_run}</span><span class="chip">halted: ${q.halted_this_run}</span></div>
        <div class="scroll-thin mt-3 overflow-x-auto"><table class="w-full min-w-[720px] text-left text-sm"><thead class="text-xs muted"><tr><th class="py-1 pr-3">#</th><th class="pr-3">Job</th><th class="pr-3">Status</th><th class="pr-3">Time</th><th class="pr-3">Confidence</th><th class="pr-3">Provider</th><th>Started</th></tr></thead>
          <tbody>${q.recent.length ? "" : `<tr><td colspan="7" class="py-3 text-sm muted">${app.me ? "No AI jobs of yours yet." : "Sign in to see your own AI jobs (other people's jobs are private)."}</td></tr>`}${q.recent.map((j) => `<tr class="border-t border-[color:var(--border)] align-top"><td class="py-1.5 pr-3 font-mono text-xs">${j.id}</td><td class="pr-3">${esc(j.label || j.kind)}${j.error ? `<div class="text-xs text-[color:var(--critical)]">${esc(j.error)}</div>` : ""}</td>
            <td class="pr-3">${statusChip(j.status)}</td><td class="whitespace-nowrap pr-3 text-xs tabular-nums">${jobTime(j) || "-"}</td><td class="pr-3 tabular-nums">${j.confidence != null ? `${Math.round(j.confidence * 100)}%${j.low_confidence ? " (low)" : ""}` : "-"}</td><td class="pr-3 font-mono text-xs">${esc(j.provider || "")}</td><td class="whitespace-nowrap text-xs muted">${timeAgo(j.started_at || j.created_at)}</td></tr>`).join("")}</tbody></table></div></section>

      <section class="card"><div class="section-title">${icon("graduation-cap", "layer-group")}Levels: scrutiny & context memory on this device</div>
        <div class="scroll-thin mt-3 overflow-x-auto"><table class="w-full min-w-[640px] text-left text-sm"><thead class="text-xs muted"><tr><th class="py-1">Level</th><th>Scrutiny</th><th class="tabular-nums">History</th><th class="tabular-nums">Memories</th><th class="tabular-nums">Context requested</th><th class="tabular-nums">Available here</th><th class="tabular-nums">Samples</th></tr></thead>
          <tbody>${d.levels.map((l) => `<tr class="border-t border-[color:var(--border)]"><td class="py-1.5 font-semibold">${l.number} · ${esc(l.name)}</td><td>${esc(l.scrutiny)}</td><td class="tabular-nums">${l.history_messages}</td><td class="tabular-nums">${l.memory_recalls}</td>
            <td class="tabular-nums">${l.context_tokens_requested.toLocaleString()}</td><td class="tabular-nums ${l.context_limited ? "text-[color:var(--critical)]" : ""}">${l.context_tokens_effective.toLocaleString()}${l.context_limited ? " (limited)" : ""}</td><td class="tabular-nums">${l.samples}</td></tr>`).join("")}</tbody></table></div></section>

      <section class="card"><div class="section-title">${icon("console", "terminal")}AI server setup</div>
        <ol class="mt-3 list-decimal space-y-1.5 pl-5 text-sm">${d.ai_server.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        ${d.ai_server.repo ? `<a class="btn btn-soft btn-sm mt-3" href="${esc(d.ai_server.repo)}" target="_blank" rel="noopener noreferrer"><i class="la la-download"></i>Open the AI server repo</a>` : '<p class="mt-3 text-xs muted">The AI server repo link has not been configured yet (config.json -> app.ai_server_repo).</p>'}
        ${Object.entries(d.ai_server.blocked).filter(([, v]) => v).map(([role, v]) => `<div class="mt-3 rounded-xl border border-[color:var(--critical)]/50 p-3 text-sm"><b>${esc(role)}:</b> ${esc(v)}</div>`).join("")}
        <p class="mt-3 text-xs muted">Embeddings: ${esc(d.embeddings.active)} (${d.embeddings.dim}-d, pgvector HNSW cosine)</p></section></div>`;
  }

  await paint();
  const tick = async () => { if (!alive) return; try { await paint(); } catch { /* keep trying */ } timer = setTimeout(tick, 5000); };
  timer = setTimeout(tick, 5000);
  return () => { alive = false; clearTimeout(timer); };
}
