// Onboarding wizard: name, age, gender, SWOT, biography, projects, start level, avatar & voice.
import { api } from "../api.js";
import { Avatar, FACE_OPTIONS } from "../avatar.js";
import { jobTracker, LEVEL_NAMES, LEVEL_SCRUTINY, rememberAccount } from "../components.js";
import { $, esc, icon, modal, toast } from "../ui.js";

const STEPS = ["Welcome", "About you", "SWOT", "Biography", "Projects", "Avatar & voice", "Review"];
const LINES = [
  "Hi! I'm {name}, your critical-thinking coach. I'll train your logic, analysis, metacognition and bug-hunting foresight. First, a quick disclaimer.",
  "Tell me a little about yourself.",
  "Now your SWOT: strengths, weaknesses, opportunities and threats. Be honest - I won't judge, and I won't jump to conclusions.",
  "Who are you and what do you do? I'll keep it as a reference while we train.",
  "Share a project if you have one - code files, a zip, or even screenshots. I'll analyse it with several AI jobs in parallel.",
  "Pick my face and my voice. You can change them any time in Settings.",
  "All set. Ready when you are!",
];

export async function render(root, app) {
  try {
    const lock = await api("/api/auth/lock", { quiet: true });
    if (lock.locked) {  // one person at a time: no new profiles while someone is using the app
      root.innerHTML = `<div class="mx-auto max-w-md py-10 text-center fade-in"><div class="card glow-border">
          <div class="mx-auto grid h-16 w-16 place-items-center rounded-2xl bg-[color:var(--surface-2)] text-3xl text-[color:var(--accent)]"><i class="la la-user-lock"></i></div>
          <h1 class="mt-4 text-xl font-extrabold">${esc(lock.name)} is using the app right now</h1>
          <p class="mt-2 text-sm text-2">Only one person can use the app at a time, on any device. You can create a profile when they finish.</p>
          <p class="mt-4 text-xs muted"><i class="la la-sync la-spin"></i> This page updates by itself.</p></div></div>`;
      let inner = null;  // cleanup of the wizard once it replaces this waiting card
      let gone = false;
      const timer = setInterval(async () => {
        try {
          if (!(await api("/api/auth/lock", { quiet: true })).locked) {
            clearInterval(timer);
            const cleanup = await render(root, app);
            if (gone) { if (cleanup) cleanup(); } else inner = cleanup;
          }
        } catch { /* keep waiting */ }
      }, 5000);
      return () => { gone = true; clearInterval(timer); if (inner) inner(); };
    }
  } catch { /* lock state unknown - continue */ }
  const started = performance.now();
  const data = {
    accepted: false, username: "", password: "", password2: "", name: "", age: "", gender: "prefer_not", gender_custom: "",
    strengths: "", weaknesses: "", opportunities: "", threats: "", bio: "", experience: "",
    project_name: "", project_description: "", pasted_code: "", files: [], start_level: 1,
    tutor_name: "Byte", avatar_face: "bot", avatar_hue: 190, voice_enabled: !app.voice.muted, voice_engine: "neural", voice_neural: "",
    voice_name: "", voice_rate: 1, voice_pitch: 1,
  };
  let step = 0;
  let avatar = null;
  let stopTracker = null;
  let minis = [];
  let usernameOk = null;
  let unsubVoices = null;  // one voice-list subscription at a time (step 5 re-renders on every face click)

  root.innerHTML = `<div class="grid gap-6 lg:grid-cols-[320px_1fr]">
      <aside class="card glow-border h-fit lg:sticky lg:top-24">
        <div id="ob-avatar" class="mx-auto w-48 lg:w-56"></div>
        <p id="ob-line" class="mt-3 min-h-[4.5rem] text-center text-sm text-2"></p>
        <ol id="ob-steps" class="mt-4 space-y-1 text-sm"></ol>
      </aside>
      <section><div id="ob-body" class="card fade-in"></div>
        <div class="mt-4 flex items-center justify-between gap-2">
          <button id="ob-back" class="btn btn-ghost"><i class="la la-arrow-left"></i>Back</button>
          <button id="ob-next" class="btn btn-primary">Next<i class="la la-arrow-right"></i></button>
        </div></section></div>`;

  avatar = new Avatar($("#ob-avatar", root), { face: data.avatar_face, hue: data.avatar_hue, name: data.tutor_name });
  app.voice.configure({ enabled: data.voice_enabled, rate: data.voice_rate, pitch: data.voice_pitch, name: data.voice_name });

  const say = (userGesture) => {
    const line = LINES[step].replace("{name}", data.tutor_name || "Byte");
    $("#ob-line", root).textContent = line;
    avatar.setExpression(step === 0 || step === 6 ? "happy" : step === 2 ? "thinking" : "neutral");
    app.voice.speak(line, avatar); // honours mute (no voice, no lip sync) and waits for the first click if audio is blocked
  };

  const stepsList = () => {
    $("#ob-steps", root).innerHTML = STEPS.map((s, i) => `<li class="flex items-center gap-2 rounded-lg px-2 py-1 ${i === step ? "bg-[color:var(--surface-2)] font-semibold" : "muted"}">
        <span class="grid h-6 w-6 place-items-center rounded-full text-[11px] font-bold ${i < step ? "bg-[color:var(--good)] text-white" : i === step ? "bg-[color:var(--accent)] text-black" : "border border-[color:var(--border)]"}">${i < step ? '<i class="la la-check"></i>' : i + 1}</span>${esc(s)}</li>`).join("");
  };

  const field = (label, html, hint = "") => `<label class="block"><span class="label">${esc(label)}</span>${html}${hint ? `<span class="mt-1 block text-xs muted">${esc(hint)}</span>` : ""}</label>`;

  function body() {
    minis.forEach((m) => m.destroy());
    minis = [];
    if (unsubVoices) { unsubVoices(); unsubVoices = null; }
    const el = $("#ob-body", root);
    el.classList.remove("fade-in"); void el.offsetWidth; el.classList.add("fade-in");
    if (step === 0) {
      el.innerHTML = `<div class="flex items-center gap-3">${icon("brain", "brain", "ico8-lg")}<div>
          <h1 class="text-2xl font-extrabold"><span class="gradient-text">Train how you think</span>, not just what you code.</h1>
          <p class="text-sm muted">A local-first cognitive trainer for student developers.</p></div></div>
        <div class="mt-5 grid gap-3 sm:grid-cols-2">
          ${[["idea", "lightbulb", "Logic & analytical thinking", "Socratic questions that scale over 10 scrutiny levels."],
             ["bug", "bug", "Bug foresight", "Hidden bugs in your own code - can you find them before I reveal them?"],
             ["puzzle", "puzzle-piece", "Qwen-coder challenges", "Bug hunts, 'Why?' questions, predict-the-output and build tasks."],
             ["combo-chart", "chart-bar", "Cognitive profile", "Skills, SWOT and an estimated-IQ range - only once there is enough evidence."]]
            .map(([i8, la, t, d]) => `<div class="flex gap-3 rounded-xl border border-[color:var(--border)] p-3">${icon(i8, la)}<div><div class="font-semibold">${t}</div><div class="text-sm muted">${d}</div></div></div>`).join("")}
        </div>
        <div class="mt-5 rounded-xl border border-[color:var(--warning)]/60 bg-[color:var(--warning)]/10 p-4 text-sm">
          <div class="mb-2 flex items-center gap-2 font-bold"><i class="la la-exclamation-triangle text-lg"></i>Disclaimer - please read</div>
          <ul class="list-disc space-y-1 pl-5 text-2">
            <li>This is a <b>student-grade project</b>. It is <b>not a medical, psychological or clinical tool</b> and <b>not a real IQ test</b> - any "estimated IQ" is a playful, low-precision estimate for self-reflection only.</li>
            <li><b>Private by design:</b> everything happens and is stored locally on this machine (PostgreSQL). Nothing is shared - unless you later choose to add your own Claude / ChatGPT key, in which case the text you send to the AI is processed by that provider.</li>
            <li>Provided "as is" under the <b>MIT License</b>, <b>without warranty</b> of any kind; the authors accept <b>no liability</b>.</li>
          </ul>
          <label class="mt-3 flex items-center gap-2 font-semibold"><input id="ob-accept" type="checkbox" class="h-4 w-4 accent-[color:var(--accent)]" ${data.accepted ? "checked" : ""}> I understand and accept.</label>
        </div>
        <a href="#/login" class="mt-4 inline-block text-sm underline underline-offset-2 muted">Already have a profile? Sign in</a>`;
      $("#ob-accept", el).onchange = (e) => { data.accepted = e.target.checked; };
    } else if (step === 1) {
      el.innerHTML = `<h2 class="section-title">${icon("user-male-circle", "user")}About you</h2>
        <div class="mt-4 rounded-xl border border-[color:var(--border)] p-4">
          <div class="label">Your login (stored only on this computer)</div>
          <div class="grid gap-4 sm:grid-cols-3">
            ${field("Username", `<input id="f-user" class="input font-mono" maxlength="32" autocomplete="username" autocapitalize="none" spellcheck="false" value="${esc(data.username)}" placeholder="e.g. asha_dev">`, "")}
            ${field("Password", `<input id="f-pass" type="password" class="input" maxlength="128" autocomplete="new-password" value="${esc(data.password)}" placeholder="at least 8 characters">`)}
            ${field("Confirm password", `<input id="f-pass2" type="password" class="input" maxlength="128" autocomplete="new-password" value="${esc(data.password2)}">`)}
          </div>
          <p id="f-user-msg" class="mt-2 text-xs muted">3-32 characters: lowercase letters, digits, dot, dash, underscore. Your password is hashed (scrypt) - nobody can read it.</p>
        </div>
        <div class="mt-4 grid gap-4 sm:grid-cols-2">
          ${field("Name", `<input id="f-name" class="input" maxlength="60" value="${esc(data.name)}" placeholder="What should I call you?">`)}
          ${field("Age", `<input id="f-age" class="input" type="number" min="8" max="100" value="${esc(data.age)}" placeholder="e.g. 19">`)}
          ${field("Gender", `<select id="f-gender" class="input">${[["prefer_not", "Prefer not to say"], ["female", "Female"], ["male", "Male"], ["non_binary", "Non-binary"], ["self_describe", "Self-describe"]]
            .map(([v, l]) => `<option value="${v}" ${data.gender === v ? "selected" : ""}>${l}</option>`).join("")}</select>`, "Only used to address you - never to judge abilities.")}
          ${field("Self-describe (optional)", `<input id="f-gcustom" class="input" maxlength="40" value="${esc(data.gender_custom)}" ${data.gender === "self_describe" ? "" : "disabled"}>`)}
          ${field("Coding experience", `<select id="f-exp" class="input">${["", "Just starting", "Under 1 year", "1-2 years", "3+ years", "Professional"].map((v) => `<option ${data.experience === v ? "selected" : ""} value="${v}">${v || "Choose..."}</option>`).join("")}</select>`)}
        </div>`;
      $("#f-gender", el).onchange = (e) => { $("#f-gcustom", el).disabled = e.target.value !== "self_describe"; };
      const userInput = $("#f-user", el);
      userInput.addEventListener("input", () => { userInput.value = userInput.value.toLowerCase().replace(/\s/g, ""); });
      userInput.addEventListener("blur", async () => {
        const u = userInput.value.trim();
        if (!u) return;
        try {
          const r = await api(`/api/auth/username-available?u=${encodeURIComponent(u)}`, { quiet: true });
          const msg = $("#f-user-msg", el);
          msg.textContent = r.available ? `@${u} is available.` : r.problem;
          msg.className = `mt-2 text-xs ${r.available ? "text-[color:var(--good-text)]" : "text-[color:var(--critical)]"}`;
          usernameOk = r.available;
        } catch { /* checked again on submit */ }
      });
    } else if (step === 2) {
      const quad = (key, title, tone, ph) => `<div class="rounded-xl border p-3 ${tone}"><span class="label">${title}</span>
          <textarea id="f-${key}" class="input" rows="4" maxlength="2000" placeholder="${ph}">${esc(data[key])}</textarea></div>`;
      el.innerHTML = `<h2 class="section-title">${icon("target", "bullseye")}Your SWOT</h2>
        <p class="mt-1 text-sm muted">The AI analyses these against real evidence over time - and says so when it needs more chatting before concluding.</p>
        <div class="mt-4 grid gap-3 sm:grid-cols-2">
          ${quad("strengths", "Strengths", "border-emerald-500/40", "e.g. I break problems into small steps")}
          ${quad("weaknesses", "Weaknesses", "border-amber-500/50", "e.g. I rush and skip edge cases")}
          ${quad("opportunities", "Opportunities", "border-cyan-500/40", "e.g. hackathons, open source")}
          ${quad("threats", "Threats", "border-rose-500/40", "e.g. relying on AI without understanding")}
        </div>`;
    } else if (step === 3) {
      el.innerHTML = `<h2 class="section-title">${icon("graduation-cap", "user-graduate")}Biography</h2>
        <p class="mt-1 text-sm muted">What do you do and who are you? The AI uses this as reference whenever it helps.</p>
        <textarea id="f-bio" class="input mt-4" rows="9" maxlength="6000" placeholder="I'm a second-year CS student who loves building Discord bots...">${esc(data.bio)}</textarea>`;
    } else if (step === 4) {
      el.innerHTML = `<h2 class="section-title">${icon("source-code", "code")}Projects & starting level</h2>
        <p class="mt-1 text-sm muted">Optional but recommended: the AI estimates your level from real code (AI-assisted code is fine - the idea being yours is what matters).</p>
        <div class="mt-4 grid gap-4 sm:grid-cols-2">
          ${field("Project name", `<input id="f-pname" class="input" maxlength="160" value="${esc(data.project_name)}" placeholder="Weather CLI">`)}
          ${field("What does it do?", `<input id="f-pdesc" class="input" maxlength="4000" value="${esc(data.project_description)}" placeholder="Fetches forecasts and caches them">`)}
        </div>
        <label id="f-drop" class="mt-4 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-[color:var(--border)] p-6 text-center transition hover:border-[color:var(--accent)] focus-within:border-[color:var(--accent)] focus-within:ring-2 focus-within:ring-[color:var(--accent)]">
          ${icon("upload", "cloud-upload-alt", "ico8-lg")}<span class="font-semibold">Drop code files, a .zip, or screenshots</span>
          <span class="text-xs muted">Screenshots go through the OCR layers (Tesseract -> RapidOCR -> system OCR -> MiniCPM-V)</span>
          <input id="f-files" type="file" multiple class="sr-only"></label>
        <div id="f-filelist" class="mt-2 flex flex-wrap gap-2"></div>
        <label class="mt-4 block"><span class="label">...or paste code</span>
          <textarea id="f-paste" class="input input-mono" rows="6" placeholder="def solve(items): ...">${esc(data.pasted_code)}</textarea></label>
        <div class="mt-5"><span class="label">Starting scrutiny level: <b id="f-level-label"></b></span>
          <input id="f-level" type="range" min="1" max="10" value="${data.start_level}" class="w-full accent-[color:var(--accent)]">
          <div class="flex justify-between font-mono text-[10px] muted"><span>1 gentle</span><span>10 relentless</span></div></div>`;
      const showFiles = () => {
        $("#f-filelist", el).innerHTML = data.files.map((f) => `<span class="chip chip-wrap"><i class="la la-file-code"></i>${esc(f.name)} <span class="muted">${Math.ceil(f.size / 1024)} KB</span></span>`).join("");
      };
      const addFiles = (list) => { data.files = [...data.files, ...Array.from(list)].slice(0, 60); showFiles(); };
      $("#f-files", el).onchange = (e) => addFiles(e.target.files);
      const drop = $("#f-drop", el);
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("border-[color:var(--accent)]"); };
      drop.ondragleave = () => drop.classList.remove("border-[color:var(--accent)]");
      drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("border-[color:var(--accent)]"); addFiles(e.dataTransfer.files); };
      const lvl = $("#f-level", el);
      const lbl = () => { $("#f-level-label", el).textContent = `${lvl.value} - ${LEVEL_NAMES[lvl.value - 1]} (${LEVEL_SCRUTINY[lvl.value - 1]})`; };
      lvl.oninput = lbl;
      lbl();
      showFiles();
    } else if (step === 5) {
      el.innerHTML = `<h2 class="section-title">${icon("robot-2", "robot")}Avatar & voice</h2>
        <div class="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4" id="f-faces"></div>
        <div class="mt-4 grid gap-4 sm:grid-cols-2">
          ${field("Colour", `<input id="f-hue" type="range" min="0" max="360" value="${data.avatar_hue}" class="w-full accent-[color:var(--accent)]">`)}
          ${field("Tutor name", `<input id="f-tname" class="input" maxlength="40" value="${esc(data.tutor_name)}">`)}
          ${field("Voice", `<select id="f-neural" class="input"><option value="">Loading local voices...</option></select>`, "Natural voices generated on this computer - nothing is sent anywhere. The lips follow the real audio.")}
          ${field("Speed", `<input id="f-rate" type="range" min="0.6" max="1.6" step="0.05" value="${data.voice_rate}" class="w-full">`)}
          <label class="flex items-center gap-2 text-sm font-semibold"><input id="f-venabled" type="checkbox" class="h-4 w-4" ${data.voice_enabled ? "checked" : ""}> Sound on <span class="font-normal muted">(off = muted: no voice and no lip sync)</span></label>
          <button id="f-test" class="btn btn-soft w-fit"><i class="la la-volume-up"></i>Test voice</button>
        </div>`;
      const faces = $("#f-faces", el);
      FACE_OPTIONS.forEach((f) => {
        const b = document.createElement("button");
        b.className = `rounded-2xl border p-2 transition ${data.avatar_face === f.key ? "border-[color:var(--accent)] bg-[color:var(--surface-2)]" : "border-[color:var(--border)]"}`;
        b.innerHTML = `<div class="mx-auto w-20"></div><div class="mt-1 text-center text-xs font-semibold">${esc(f.label)}</div>`;
        faces.appendChild(b);
        minis.push(new Avatar(b.firstElementChild, { face: f.key, hue: data.avatar_hue, interactive: false }));
        b.onclick = () => { collect(); data.avatar_face = f.key; avatar.setFace(f.key, data.avatar_hue); avatar.setExpression("happy"); body(); };
      });
      $("#f-hue", el).oninput = (e) => { data.avatar_hue = Number(e.target.value); avatar.setFace(data.avatar_face, data.avatar_hue); minis.forEach((m, i) => m.setFace(FACE_OPTIONS[i].key, data.avatar_hue)); };
      const neuralSel = $("#f-neural", el);
      const fillVoices = () => {
        const nv = app.voice.neural;
        if (nv.available === false || !(nv.voices || []).length) {
          neuralSel.innerHTML = '<option value="">Browser voice (local voices unavailable)</option>';
          return;
        }
        const current = data.voice_neural || nv.default;
        neuralSel.innerHTML = nv.voices.map((v) => `<option value="${esc(v.id)}" ${current === v.id ? "selected" : ""}>${esc(v.label)}${v.installed ? "" : " - downloads after you create your profile"}</option>`).join("");
      };
      unsubVoices = app.voice.onVoices(fillVoices);
      app.voice.loadNeuralVoices().then(() => { if (neuralSel.isConnected) fillVoices(); });
      $("#f-test", el).onclick = () => {
        collect();
        app.voice.configure({ engine: data.voice_engine, neural: data.voice_neural, rate: data.voice_rate });
        app.voice.speak(`Hello ${data.name || "there"}! I'm ${data.tutor_name}. What if your input list is empty?`, avatar, { force: true });
      };
    } else {
      const swotFilled = ["strengths", "weaknesses", "opportunities", "threats"].filter((k) => data[k].trim()).length;
      el.innerHTML = `<h2 class="section-title">${icon("rocket", "rocket")}Review</h2>
        <dl class="mt-4 grid gap-3 text-sm sm:grid-cols-2">
          ${[["Name", data.name], ["Age", data.age], ["SWOT", `${swotFilled}/4 filled`], ["Biography", data.bio ? `${data.bio.length} characters` : "skipped"],
             ["Project", data.files.length || data.pasted_code ? `${data.project_name || "untitled"} - ${data.files.length} file(s)${data.pasted_code ? " + pasted code" : ""}` : "none yet"],
             ["Starting level", `${data.start_level} - ${LEVEL_NAMES[data.start_level - 1]}`], ["Tutor", `${data.tutor_name} (${data.avatar_face})`]]
            .map(([k, v]) => `<div class="rounded-xl border border-[color:var(--border)] p-3"><dt class="label">${k}</dt><dd class="font-medium">${esc(v)}</dd></div>`).join("")}
        </dl>
        <div aria-hidden="true" style="position:absolute;left:-9999px;top:auto;width:1px;height:1px;overflow:hidden">
          <label>Leave this empty<input id="f-website" type="text" tabindex="-1" autocomplete="off"></label></div>
        <p class="mt-4 text-sm muted">Creating your profile starts the AI analysis in parallel worker jobs. You sign in with your username and password on any device on your network (one person uses the app at a time), and you'll get a one-time recovery key in case you forget your password.</p>`;
    }
  }

  function collect() {
    const v = (id) => { const e = $(id, root); return e ? e.value : undefined; };
    if (step === 1) {
      data.username = (v("#f-user") || "").trim().toLowerCase(); data.password = v("#f-pass") || ""; data.password2 = v("#f-pass2") || "";
      data.name = (v("#f-name") || "").trim(); data.age = v("#f-age"); data.gender = v("#f-gender");
      data.gender_custom = v("#f-gcustom") || ""; data.experience = v("#f-exp") || "";
    } else if (step === 2) {
      for (const k of ["strengths", "weaknesses", "opportunities", "threats"]) data[k] = v(`#f-${k}`) || "";
    } else if (step === 3) data.bio = v("#f-bio") || "";
    else if (step === 4) {
      data.project_name = v("#f-pname") || ""; data.project_description = v("#f-pdesc") || "";
      data.pasted_code = v("#f-paste") || ""; data.start_level = Number(v("#f-level") || 1);
    } else if (step === 5) {
      data.avatar_hue = Number(v("#f-hue") || 190); data.tutor_name = (v("#f-tname") || "Byte").trim() || "Byte";
      data.voice_neural = v("#f-neural") || ""; data.voice_engine = data.voice_neural ? "neural" : "browser";
      data.voice_rate = Number(v("#f-rate") || 1);
      const en = $("#f-venabled", root); data.voice_enabled = en ? en.checked : true;
    }
  }

  function validate() {
    if (step === 0 && !data.accepted) return "Please read and accept the disclaimer to continue.";
    if (step === 1) {
      if (!/^[a-z0-9][a-z0-9_.-]{2,31}$/.test(data.username)) return "Choose a username: 3-32 lowercase letters, digits, dots, dashes or underscores.";
      if (usernameOk === false) return "That username is taken - please pick another one.";
      if (data.password.length < 8) return "Your password needs at least 8 characters.";
      if (data.password !== data.password2) return "The two passwords don't match.";
      if (data.password.toLowerCase() === data.username) return "Your password can't be the same as your username.";
      if (!data.name) return "Please tell me your name.";
      const age = Number(data.age);
      if (!Number.isInteger(age) || age < 8 || age > 100) return "Please enter an age between 8 and 100.";
    }
    return null;
  }

  function show(userGesture = false) {
    stepsList();
    body();
    $("#ob-back", root).disabled = step === 0;
    $("#ob-next", root).innerHTML = step === STEPS.length - 1 ? '<i class="la la-magic"></i>Create my profile' : 'Next<i class="la la-arrow-right"></i>';
    say(userGesture);
  }

  $("#ob-back", root).onclick = () => { collect(); if (step > 0) { step--; show(true); } };
  $("#ob-next", root).onclick = async () => {
    collect();
    const problem = validate();
    if (problem) { toast(problem, "warn"); avatar.setExpression("concerned"); return; }
    if (step < STEPS.length - 1) { step++; show(true); return; }
    await submit();
  };

  async function submit() {
    const btn = $("#ob-next", root);
    btn.disabled = true;
    btn.innerHTML = '<i class="la la-cog la-spin"></i>Creating...';
    const form = new FormData();
    const fields = { ...data, accepted_disclaimer: data.accepted, form_elapsed_ms: Math.round(performance.now() - started), website: ($("#f-website", root) || {}).value || "" };
    delete fields.files; delete fields.accepted; delete fields.password2;
    for (const [k, v] of Object.entries(fields)) form.append(k, typeof v === "boolean" ? (v ? "true" : "false") : String(v ?? ""));
    data.files.forEach((f) => form.append("files", f, f.name));
    try {
      const res = await api("/api/onboarding", { method: "POST", form });
      app.me = res.user;
      data.password = ""; data.password2 = "";
      rememberAccount(res.user);
      app.voice.setMuted(!data.voice_enabled);
      await app.refreshMe();
      avatar.setExpression("proud");
      afterCreate(res);
    } catch (e) {
      toast(e.message, "error", 7000);
      btn.disabled = false;
      btn.innerHTML = '<i class="la la-magic"></i>Create my profile';
    }
  }

  function afterCreate(res) {
    const el = $("#ob-body", root);
    $("#ob-back", root).classList.add("hidden");
    $("#ob-next", root).classList.add("hidden");
    el.innerHTML = `<h2 class="section-title">${icon("ok", "check-circle")}Profile created</h2>
      <div class="mt-4 rounded-xl border border-[color:var(--accent)]/50 bg-[color:var(--accent)]/10 p-4">
        <div class="label">Your one-time recovery key</div>
        <div class="flex flex-wrap items-center gap-2"><code id="rk" class="kbd select-all break-all !text-sm">${esc(res.recovery_key)}</code>
          <button id="rk-copy" class="btn btn-soft btn-sm"><i class="la la-copy"></i>Copy</button></div>
        <p class="mt-2 text-xs muted">Save it somewhere safe - if you ever forget your password, it is the only way back into this profile. It is stored only as a hash.</p></div>
      ${res.skipped && res.skipped.length ? `<p class="mt-3 text-xs muted">Skipped files: ${esc(res.skipped.join(", "))}</p>` : ""}
      <div class="mt-5"><div class="label">Parallel AI jobs</div><div id="ob-jobs" class="text-sm"></div></div>
      ${res.ai_online ? "" : '<p class="mt-3 text-sm text-[color:var(--critical)]"><i class="la la-plug"></i> The AI servers are offline, so analysis will start once they are back.</p>'}
      <div class="mt-5 flex flex-wrap gap-2"><a href="#/chat" class="btn btn-primary"><i class="la la-comments"></i>Start training</a>
        <a href="#/projects" class="btn btn-ghost"><i class="la la-code"></i>View project analysis</a></div>`;
    $("#rk-copy", el).onclick = () => navigator.clipboard.writeText(res.recovery_key).then(() => toast("Recovery key copied", "success"));
    stopTracker = jobTracker($("#ob-jobs", el), { ids: res.jobs });
    $("#ob-line", root).textContent = "Analysing you in parallel... the opening question will be waiting in Chat.";
  }

  show(false);
  return () => {
    avatar && avatar.destroy(); minis.forEach((m) => m.destroy());
    if (stopTracker) stopTracker();
    if (unsubVoices) unsubVoices();
  };
}
