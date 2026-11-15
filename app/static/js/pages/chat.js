// Chat: talking avatar (blink + lip-sync), Socratic tutor, level bar with raise offers, OCR attachments.
import { ApiError, api, pollJob } from "../api.js";
import { Avatar } from "../avatar.js";
import { openLevelDialog } from "../components.js";
import { $, autosize, confetti, confidenceBadge, esc, icon, md, meter, toast } from "../ui.js";

const TURN_LABEL = {
  opening: ["la-hand-sparkles", "opening"], recall: ["la-history", "memory check"], project: ["la-folder-open", "your project"],
  bug_hint: ["la-bug", "what-if"], challenge_suggest: ["la-puzzle-piece", "challenge idea"], challenge_nudge: ["la-puzzle-piece", "open challenge"],
  project_report: ["la-search", "project analysis"], challenge_result: ["la-clipboard-check", "challenge result"],
  bughunt_result: ["la-bug", "bug hunt"], level_up: ["la-rocket", "level up"], level_declined: ["la-pause-circle", "level kept"],
  level_change: ["la-sliders-h", "scrutiny changed"],
  bug_reveal: ["la-unlock", "reveal"], socratic: ["la-question-circle", "socratic"],
};
const MOOD_EXPR = { neutral: "neutral", happy: "happy", thinking: "thinking", surprised: "surprised", concerned: "concerned", proud: "proud" };

export async function render(root, app) {
  const me = app.me;
  const st = { lastId: 0, progression: me.progression, levelInfo: me.level, busy: false, alive: true, celebrated: new Set() };
  const settings = me.settings || {};
  const av = settings.avatar || {};
  app.voice.configure(settings.voice);

  root.innerHTML = `<div class="grid gap-6 lg:grid-cols-[330px_1fr]">
    <aside class="flex flex-col gap-4 lg:sticky lg:top-24 lg:h-fit">
      <div class="card glow-border">
        <div id="c-avatar" class="mx-auto w-44 sm:w-52"></div>
        <div class="mt-2 flex items-center justify-center gap-2">
          <span class="font-bold">${esc(av.name || "Byte")}</span><span id="c-mood" class="chip">ready</span></div>
        <div class="mt-3 flex justify-center gap-2">
          <button id="c-voice" class="btn btn-soft btn-sm"></button>
          <button id="c-stop" class="btn btn-ghost btn-sm" title="Stop speaking"><i class="la la-stop"></i></button></div>
      </div>
      <div id="c-level" class="card"></div>
      <div class="card">
        <div class="label">Quick actions</div>
        <div class="grid grid-cols-2 gap-2">
          <a href="#/challenges" class="btn btn-soft btn-sm"><i class="la la-puzzle-piece"></i>Challenge</a>
          <a href="#/projects" class="btn btn-soft btn-sm"><i class="la la-bug"></i>Hunt bugs</a>
          <button data-quick="Give me a tricky what-if question about my own code or projects." class="btn btn-soft btn-sm"><i class="la la-question"></i>What-if?</button>
          <label class="btn btn-soft btn-sm cursor-pointer focus-within:ring-2 focus-within:ring-[color:var(--accent)]"><i class="la la-eye"></i>OCR image<input id="c-ocr" type="file" accept="image/*" class="sr-only" aria-label="Read the text in an image (OCR)"></label>
        </div></div>
    </aside>
    <section class="card flex min-h-[70vh] flex-col !p-0">
      <div class="flex items-center justify-between gap-2 border-b border-[color:var(--border)] px-5 py-3">
        <div class="section-title">${icon("chat", "comments")}Training session</div>
        <div id="c-pipeline" class="flex flex-wrap justify-end gap-1.5"></div></div>
      <div id="c-log" role="log" aria-live="polite" aria-label="Conversation" class="scroll-thin flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-4 sm:px-5" style="max-height:62vh"></div>
      <div id="c-composer" class="border-t border-[color:var(--border)] p-3 sm:p-4">
        <div id="c-lock"></div>
        <div class="flex items-end gap-2">
          <textarea id="c-input" class="input flex-1" rows="2" maxlength="${app.config.max_message_chars || 6000}" placeholder="Answer, ask, or reason out loud... (Enter to send, Shift+Enter for a new line)"></textarea>
          <button id="c-send" class="btn btn-primary h-11"><i class="la la-paper-plane text-lg"></i><span class="hidden sm:inline">Send</span></button>
        </div>
        <div class="mt-1.5 flex justify-between text-[11px] muted"><span id="c-hint">AI conclusions below ${Math.round((app.config.confidence_threshold || 0.65) * 100)}% confidence are retried, then shown with a red warning.</span><span id="c-count">0</span></div>
      </div></section></div>`;

  const avatar = new Avatar($("#c-avatar", root), { face: av.face, hue: av.hue, name: av.name });
  const log = $("#c-log", root);
  const input = $("#c-input", root);
  autosize(input, 220);
  avatar.onChange(({ expression, speaking }) => { $("#c-mood", root).textContent = speaking ? "speaking..." : expression; });

  const voiceBtn = $("#c-voice", root);
  const paintVoice = () => {
    voiceBtn.innerHTML = app.voice.muted ? '<i class="la la-volume-mute"></i>Muted' : '<i class="la la-volume-up"></i>Sound on';
    voiceBtn.title = app.voice.muted ? "Muted: no voice and no lip sync. Click to unmute." : "Click to mute (stops voice and lip sync)";
  };
  paintVoice();
  voiceBtn.onclick = () => app.voice.setMuted(!app.voice.muted);
  const onMute = () => { paintVoice(); if (app.voice.muted) avatar.stopTalking(); };
  window.addEventListener("cte:mute", onMute);
  $("#c-stop", root).onclick = () => { app.voice.cancel(); avatar.stopTalking(); };

  // ---------------------------------------------------------------- level card
  function paintLevel() {
    const p = st.progression || {};
    const L = st.levelInfo || {};
    const declines = p.declines || 0;
    const maxDeclines = app.config.max_declines_before_lock || 3;
    let state = "";
    if (p.completed && p.level >= 10) state = `<div class="mt-3 rounded-xl border border-[color:var(--good)]/50 p-3 text-sm">${icon("trophy", "trophy")} <b>Level 10 completed!</b> Free practice mode.</div>`;
    else if (p.locked) state = `<div class="mt-3 rounded-xl border border-[color:var(--critical)]/50 p-3 text-sm"><b><i class="la la-ban"></i> Level ${p.level} cleared.</b> Upgrading is required to continue.
        <button class="btn btn-primary btn-sm mt-2 w-full" data-upgrade><i class="la la-rocket"></i>Upgrade to Level ${p.level + 1}</button></div>`;
    else if (p.offer_pending) state = `<div class="mt-3 rounded-xl border border-[color:var(--accent)]/50 p-3 text-sm"><b>Raise the difficulty?</b> The AI is waiting for your answer.
        <div class="mt-2 flex gap-2"><button class="btn btn-primary btn-sm flex-1" data-respond="yes">Yes, raise it</button><button class="btn btn-ghost btn-sm flex-1" data-respond="no">Not yet</button></div></div>`;
    const ctxNote = L.context_limited
      ? `<div class="mt-2 flex items-start gap-2 text-xs"><i class="la la-exclamation-triangle mt-0.5 text-[color:var(--serious)]"></i><span>Your device limits context memory at this level (${(L.context_tokens_effective || 0).toLocaleString()} of ${(L.context_tokens_requested || 0).toLocaleString()} tokens). Upgrade your setup or add a Claude / ChatGPT key with long-context mode in <a class="underline" href="#/settings">Settings</a>.</span></div>` : "";
    $("#c-level", root).innerHTML = `<div class="flex items-center justify-between gap-2">
        <div><div class="label">Scrutiny level</div><div class="text-2xl font-extrabold">LVL ${p.level} <span class="gradient-text">${esc(L.name || "")}</span></div></div>
        <div class="flex flex-col items-end gap-1.5"><span class="chip chip-accent">${esc(L.scrutiny || "")}</span>
          <button class="btn btn-ghost btn-sm" data-change-level title="Choose an easier or harder level"><i class="la la-sliders-h"></i>Change</button></div></div>
      <div class="mt-3 flex items-center justify-between text-xs"><span class="text-2">Level progress</span><span class="font-semibold">${Math.round(p.progress || 0)}%</span></div>
      <div class="mt-1">${meter(p.progress || 0, { label: "level progress" })}</div>
      <div class="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Context memory</div><div class="font-semibold">${(L.context_tokens_effective || 0).toLocaleString()} tok</div></div>
        <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Thinking depth</div><div class="font-semibold">${L.samples || 1}x sample · ${esc(L.reasoning_effort || "low")}</div></div>
        <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Memory recalls</div><div class="font-semibold">${L.memory_recalls || 0} per turn</div></div>
        <div class="rounded-lg bg-[color:var(--surface-2)] p-2"><div class="muted">Raise declines</div><div class="font-semibold">${declines}/${maxDeclines}</div></div>
      </div>${ctxNote}${state}`;
    $("#c-level", root).querySelectorAll("[data-respond]").forEach((b) => (b.onclick = () => respond(b.dataset.respond === "yes")));
    const up = $("#c-level", root).querySelector("[data-upgrade]");
    if (up) up.onclick = upgrade;
    $("#c-level", root).querySelector("[data-change-level]").onclick = changeLevel;
    paintLock();
  }

  async function changeLevel() {
    try {
      const res = await openLevelDialog();
      if (!res || !res.message || !st.alive) return;
      setProgression(res.progression);
      rerender();
      append([res.message], { animateSpeech: true });
      avatar.setExpression(res.event === "raised" ? "proud" : "happy");
      app.refreshMe();
    } catch (e) { toast(e.message, "error"); }
  }

  function paintLock() {
    const p = st.progression || {};
    const lock = $("#c-lock", root);
    const locked = !!p.locked;
    input.disabled = locked;
    $("#c-send", root).disabled = locked || st.busy;
    lock.innerHTML = locked ? `<div class="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[color:var(--critical)]/50 p-3 text-sm">
        <span><i class="la la-ban"></i> <b>Level ${p.level} is cleared.</b> The AI has stopped - upgrading to Level ${p.level + 1} is required to continue.</span>
        <button class="btn btn-primary btn-sm" data-upgrade2><i class="la la-rocket"></i>Upgrade now</button></div>` : "";
    const b = lock.querySelector("[data-upgrade2]");
    if (b) b.onclick = upgrade;
  }

  function setProgression(prog) {
    if (!prog) return;
    st.progression = prog;
    if (prog.level_info) st.levelInfo = prog.level_info;
    app.me.progression = prog;
    paintLevel();
  }

  // ---------------------------------------------------------------- messages
  function footer(m) {
    const meta = m.meta || {};
    const [ico, label] = TURN_LABEL[meta.turn_type] || [null, null];
    const tags = [];
    if (label && meta.turn_type !== "socratic") tags.push(`<span class="chip"><i class="la ${ico}"></i>${esc(label)}</span>`);
    tags.push(confidenceBadge(meta));
    if (meta.samples > 1) tags.push(`<span class="chip" title="Self-consistency: parallel samples compared">${meta.samples} samples</span>`);
    if (meta.progress_gain) tags.push(`<span class="chip chip-accent" title="Level progress earned">+${Number(meta.progress_gain).toFixed(1)}%</span>`);
    if (meta.provider) tags.push(`<span class="font-mono text-[10px] muted" title="${esc(meta.model || "")}">${esc(meta.provider)}</span>`);
    return `<div class="mt-2 flex flex-wrap items-center gap-1.5">${tags.join("")}
      <button class="icon-btn h-7 w-7" data-say="${m.id}" title="Read aloud"><i class="la la-volume-up"></i></button></div>`;
  }

  function messageHTML(m) {
    const meta = m.meta || {};
    if (m.role === "user") return `<div class="flex fade-in" data-mid="${m.id}"><div class="bubble-user"><div class="md">${md(m.content)}</div></div></div>`;
    if (meta.turn_type === "level_offer") {
      const live = st.progression && st.progression.offer_pending && m.id === latestOfferId();
      return `<div class="fade-in" data-mid="${m.id}"><div class="bubble-special glow-border"><div class="flex items-start gap-3">${icon("fire-element", "fire")}<div class="md flex-1">${md(m.content)}</div></div>
        ${live ? `<div class="mt-3 flex flex-wrap gap-2"><button class="btn btn-primary" data-respond="yes"><i class="la la-fire"></i>Yes, raise it</button>
        <button class="btn btn-ghost" data-respond="no">Not yet</button></div>` : '<div class="mt-2 text-xs muted">answered</div>'}</div></div>`;
    }
    if (meta.turn_type === "level_locked") {
      return `<div class="fade-in" data-mid="${m.id}"><div class="bubble-special border border-[color:var(--critical)]/50"><div class="flex items-start gap-3">${icon("lock", "lock")}<div class="md flex-1">${md(m.content)}</div></div>
        ${st.progression && st.progression.locked ? '<button class="btn btn-primary mt-3" data-upgrade><i class="la la-rocket"></i>Upgrade to the next level</button>' : ""}</div></div>`;
    }
    if (meta.turn_type === "congrats") {
      return `<div class="fade-in" data-mid="${m.id}"><div class="bubble-special glow-border text-center">${icon("trophy", "trophy", "ico8-lg mx-auto")}<div class="md mt-2">${md(m.content)}</div></div></div>`;
    }
    return `<div class="flex gap-2 fade-in" data-mid="${m.id}"><div class="mt-1 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[color:var(--surface-3)] text-[color:var(--accent)]"><i class="la la-robot"></i></div>
      <div class="bubble-ai ${meta.low_confidence ? "border-[color:var(--critical)]/60" : ""}">
        ${meta.low_confidence ? '<div class="mb-1.5 flex items-center gap-1.5 text-xs font-bold text-[color:var(--critical)]"><i class="la la-exclamation-triangle text-base"></i>LOW-CONFIDENCE ANSWER - the AI could not reach the confidence bar</div>' : ""}
        <div class="md">${md(m.content)}</div>${footer(m)}</div></div>`;
  }

  const messages = new Map();
  const latestOfferId = () => Math.max(0, ...[...messages.values()].filter((m) => (m.meta || {}).turn_type === "level_offer").map((m) => m.id));

  function wire(scope) {
    scope.querySelectorAll("[data-respond]").forEach((b) => (b.onclick = () => respond(b.dataset.respond === "yes")));
    scope.querySelectorAll("[data-upgrade]").forEach((b) => (b.onclick = upgrade));
    scope.querySelectorAll("[data-say]").forEach((b) => (b.onclick = () => { const m = messages.get(Number(b.dataset.say)); if (m) speak(m, true); }));
  }

  function append(list, { animateSpeech = false } = {}) {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
    let spoken = null;
    for (const m of list) {
      if (messages.has(m.id)) continue;
      messages.set(m.id, m);
      st.lastId = Math.max(st.lastId, m.id);
      const node = document.createElement("div");
      node.innerHTML = messageHTML(m);
      const el = node.firstElementChild;
      log.appendChild(el);
      wire(el);
      if (m.role === "assistant") spoken = m;
      if ((m.meta || {}).celebrate && animateSpeech && !st.celebrated.has(m.id)) { st.celebrated.add(m.id); confetti(); }
    }
    if (!messages.size) log.innerHTML = `<div class="m-auto max-w-sm text-center text-sm muted">${icon("idea", "lightbulb", "ico8-lg mx-auto")}<p class="mt-2">Your coach is preparing an opening question...</p></div>`;
    if (atBottom || animateSpeech) log.scrollTop = log.scrollHeight;
    if (spoken && animateSpeech) speak(spoken, false);
  }

  function rerender() {
    log.innerHTML = "";
    const all = [...messages.values()].sort((a, b) => a.id - b.id);
    messages.clear();
    append(all);
    log.scrollTop = log.scrollHeight;
  }

  function speak(m, gesture) {
    const meta = m.meta || {};
    avatar.setExpression(meta.low_confidence ? "concerned" : MOOD_EXPR[meta.mood] || "neutral");
    app.voice.speak(m.content, avatar, { force: !!gesture });
  }

  async function fetchNew({ animate = false } = {}) {
    if (!st.alive) return;
    try {
      const data = await api(`/api/chat/messages?after_id=${st.lastId}`, { quiet: true, background: true });
      if (data.progression) {
        const before = JSON.stringify(st.progression);
        setProgression(data.progression);
        if (before !== JSON.stringify(st.progression)) rerender();
      }
      if (data.messages.length) append(data.messages, { animateSpeech: animate });
      return data;
    } catch { return null; }
  }

  // ---------------------------------------------------------------- pipeline indicator
  function pipeline(active, failed = false) {
    const L = st.levelInfo || {};
    const el = $("#c-pipeline", root);
    if (!active) { el.innerHTML = failed ? '<span class="chip chip-bad"><i class="la la-times"></i>failed</span>' : ""; return; }
    el.innerHTML = [["la-chalkboard-teacher", "Tutor"], ["la-balance-scale", `Evaluator x${L.samples || 1}`], ["la-shield-alt", "Spam guard"]]
      .map(([i, t]) => `<span class="chip chip-accent"><i class="la ${i}"></i>${t}<i class="la la-circle-notch la-spin"></i></span>`).join("");
  }

  function thinking(on) {
    st.busy = on;
    $("#c-send", root).disabled = on || !!(st.progression && st.progression.locked);
    let t = $("#c-typing", root);
    if (on && !t) {
      t = document.createElement("div");
      t.id = "c-typing";
      t.className = "flex items-center gap-2 fade-in";
      t.innerHTML = '<div class="bubble-ai"><span class="typing"><span></span><span></span><span></span></span> <span class="ml-1 text-xs muted">thinking in parallel...</span></div>';
      log.appendChild(t);
      log.scrollTop = log.scrollHeight;
    } else if (!on && t) t.remove();
    if (on) avatar.setExpression("thinking");
    pipeline(on);
  }

  async function waitFor(jobId) {
    thinking(true);
    try {
      const job = await pollJob(jobId);
      thinking(false);
      if (job.status === "failed") {
        pipeline(false, true);
        avatar.setExpression("concerned");
        const node = document.createElement("div");
        node.innerHTML = `<div class="bubble-ai border-[color:var(--critical)]/60 fade-in"><div class="flex items-start gap-2 text-sm"><i class="la la-plug text-lg text-[color:var(--critical)]"></i>
          <div><b>Your message was not processed.</b><div class="muted">${esc(job.error || "The AI task failed.")}</div></div></div></div>`;
        log.appendChild(node.firstElementChild);
        log.scrollTop = log.scrollHeight;
        return;
      }
      await fetchNew({ animate: true });
    } catch (e) {
      thinking(false);
      if (!(e instanceof ApiError && e.status === 0)) toast(e.message, "error");
    }
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || st.busy) return;
    try {
      const res = await api("/api/chat/send", { method: "POST", body: { text } });
      input.value = "";
      input.dispatchEvent(new Event("input"));
      append([res.message]);
      log.scrollTop = log.scrollHeight;
      await waitFor(res.job_id);
    } catch (e) {
      if (e.code === "LEVEL_LOCKED") { await fetchNew(); toast(e.message, "warn", 7000); }
      else if (e.code === "SPAM_BLOCKED") { avatar.setExpression("concerned"); toast(e.message, "error", 7000); }
      else if (e.code !== "AI_OFFLINE") toast(e.message, "error");
    }
  }

  async function respond(accept) {
    try {
      const res = await api("/api/progression/respond", { method: "POST", body: { accept } });
      setProgression(res.progression);
      rerender();
      append([res.message], { animateSpeech: true });
      if (res.event === "level_up") avatar.setExpression("proud");
      app.refreshMe();
    } catch (e) { toast(e.message, "error"); }
  }

  async function upgrade() {
    try {
      const res = await api("/api/progression/upgrade", { method: "POST" });
      setProgression(res.progression);
      rerender();
      append([res.message], { animateSpeech: true });
      avatar.setExpression("proud");
      app.refreshMe();
    } catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- composer + OCR
  input.addEventListener("input", () => { $("#c-count", root).textContent = String(input.value.length); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } });
  $("#c-send", root).onclick = () => send(input.value);
  root.querySelectorAll("[data-quick]").forEach((b) => (b.onclick = () => send(b.dataset.quick)));
  $("#c-ocr", root).onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    toast("Reading your image through the OCR layers...", "info");
    try {
      const res = await api("/api/chat/ocr", { method: "POST", form });
      const job = await pollJob(res.job_id);
      if (job.status !== "succeeded") { toast(job.error || "OCR failed", "error"); return; }
      const r = job.result || {};
      if (!r.text) { toast("No text found in that image.", "warn"); return; }
      input.value = (input.value ? input.value + "\n\n" : "") + "```\n" + r.text + "\n```";
      input.dispatchEvent(new Event("input"));
      toast(`OCR via ${r.engine} (${Math.round((r.confidence || 0) * 100)}% confidence)${r.low_confidence ? " - LOW CONFIDENCE, please double-check the text" : ""}`, r.low_confidence ? "warn" : "success", 7000);
    } catch (err) { if (err.code !== "AI_OFFLINE") toast(err.message, "error"); }
  };

  // ---------------------------------------------------------------- boot
  paintLevel();
  const first = await api("/api/chat/messages?limit=80");
  setProgression(first.progression);
  append(first.messages);
  log.scrollTop = log.scrollHeight;
  if (first.pending_job) waitFor(first.pending_job);
  if (!first.messages.length && !me.has_opening) {
    try {
      const r = await api("/api/onboarding/analyze", { method: "POST", quiet: true });
      if (r.job_id) waitFor(r.job_id);
    } catch { /* AI offline - the outage dialog explains */ }
  } else if (first.messages.length) {
    const last = first.messages[first.messages.length - 1];
    if (last.role === "assistant") avatar.setExpression(MOOD_EXPR[(last.meta || {}).mood] || "happy");
  }
  const timer = setInterval(() => { if (!st.busy) fetchNew({ animate: true }); }, 4000);
  return () => { st.alive = false; clearInterval(timer); window.removeEventListener("cte:mute", onMute); avatar.destroy(); app.voice.cancel(); };
}
