// Settings: theme, avatar face/colour/name, voice, AI fallback keys (encrypted), account.
import { api } from "../api.js";
import { Avatar, FACE_OPTIONS } from "../avatar.js";
import { eraseFromDevice, forgetAccount, LEVEL_NAMES, LEVEL_SCRUTINY, openLevelDialog } from "../components.js";
import { $, confirmDialog, esc, icon, modal, toast } from "../ui.js";

export async function render(root, app) {
  const data = await api("/api/settings");
  const s = data.settings || {};
  const avatarCfg = { face: "bot", hue: 190, name: "Byte", ...(s.avatar || {}) };
  const voiceCfg = { enabled: true, engine: "neural", neural: "", name: "", rate: 1, pitch: 1, volume: 1, ...(s.voice || {}) };
  const fb = { provider: "none", enabled: true, for_long_context: false, model: "", ...(s.fallback || {}) };
  let keys = data.keys;

  root.innerHTML = `<div class="flex flex-col gap-6">
    <h1 class="flex items-center gap-2 text-2xl font-extrabold">${icon("settings", "sliders-h")}Settings</h1>
    <div class="grid gap-6 lg:grid-cols-[340px_1fr]">
      <div class="card glow-border h-fit lg:sticky lg:top-24"><div id="s-preview" class="mx-auto w-52"></div>
        <p class="mt-2 text-center text-sm muted">Live preview - saved to your local PostgreSQL.</p>
        <button id="s-say" class="btn btn-soft mt-3 w-full"><i class="la la-volume-up"></i>Preview voice & lip-sync</button></div>
      <div class="flex flex-col gap-6">
        <section class="card" aria-labelledby="s-level-title"><div id="s-level-title" class="section-title"><i class="la la-sliders-h"></i>Level &amp; scrutiny</div>
          <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
            <div id="s-level-now"></div>
            <button id="s-level" class="btn btn-soft btn-sm"><i class="la la-sliders-h"></i>Change level</button></div>
          <p class="mt-2 text-xs muted">Too strict or too easy? Pick any level from 1 (gentle, plenty of hints) to 10 (relentless, no hints).
            The coach still asks before raising it by itself.</p></section>

        <section class="card"><div class="section-title"><i class="la la-adjust"></i>Appearance</div>
          <div class="mt-3 flex flex-wrap gap-2">${["dark", "light", "system"].map((t) => `<button data-theme="${t}" class="btn ${((s.theme || "dark") === t) ? "btn-primary" : "btn-ghost"} btn-sm"><i class="la ${t === "dark" ? "la-moon" : t === "light" ? "la-sun" : "la-desktop"}"></i>${t}</button>`).join("")}</div></section>

        <section class="card"><div class="section-title"><i class="la la-smile"></i>Avatar face</div>
          <div id="s-faces" class="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4"></div>
          <div class="mt-4 grid gap-4 sm:grid-cols-2">
            <label><span class="label">Colour (hue)</span><input id="s-hue" type="range" min="0" max="360" value="${avatarCfg.hue}" class="w-full"></label>
            <label><span class="label">Tutor name</span><input id="s-name" class="input" maxlength="40" value="${esc(avatarCfg.name)}"></label></div>
          <button id="s-save-avatar" class="btn btn-primary btn-sm mt-4"><i class="la la-save"></i>Save avatar</button></section>

        <section class="card"><div class="section-title"><i class="la la-microphone"></i>Voice &amp; lip sync</div>
          <div class="mt-3 grid gap-4 sm:grid-cols-2">
            <label><span class="label">Engine</span><select id="s-engine" class="input">
              <option value="neural" ${voiceCfg.engine !== "browser" ? "selected" : ""}>Local neural voice (Piper) - recommended</option>
              <option value="browser" ${voiceCfg.engine === "browser" ? "selected" : ""}>Browser voice</option></select></label>
            <label id="s-neural-wrap"><span class="label">Neural voice</span><select id="s-neural" class="input"></select>
              <span class="mt-1 block text-xs muted">Generated on this computer - nothing is sent anywhere. A new voice downloads once (about 60 MB).</span></label>
            <label id="s-browser-wrap"><span class="label">Browser voice</span><select id="s-voice" class="input"><option value="">Default voice</option></select>
              <span class="mt-1 block text-xs muted">Voices marked (cloud) are provided by your browser vendor and may send text online.</span></label>
            <label><span class="label">Speed <span id="s-rate-v">${voiceCfg.rate}</span></span><input id="s-rate" type="range" min="0.6" max="1.6" step="0.05" value="${voiceCfg.rate}" class="w-full"></label>
            <label id="s-pitch-wrap"><span class="label">Pitch (browser voices) <span id="s-pitch-v">${voiceCfg.pitch}</span></span><input id="s-pitch" type="range" min="0.4" max="1.8" step="0.05" value="${voiceCfg.pitch}" class="w-full"></label>
            <label class="flex items-center gap-2 text-sm font-semibold sm:col-span-2"><input id="s-venabled" type="checkbox" class="h-4 w-4" ${app.voice.muted ? "" : "checked"}>
              Sound on <span class="font-normal muted">(off = muted: no voice and no lip sync - same as the speaker button in the header)</span></label></div>
          <button id="s-save-voice" class="btn btn-primary btn-sm mt-4"><i class="la la-save"></i>Save voice</button></section>

        <section class="card" id="s-fallback"></section>

        <section class="card"><div class="section-title"><i class="la la-user-lock"></i>Account &amp; privacy</div>
          <p class="mt-2 text-sm text-2">${app.me.username ? `Signed in as <b class="font-mono">@${esc(app.me.username)}</b>. ` : ""}Your profile lives only in this computer's
            PostgreSQL and your password is stored as a salted scrypt hash. Nobody on the network can open it without your password or recovery key.</p>
          <form id="s-pw" class="mt-4 grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <input id="s-pw-cur" type="password" class="input" placeholder="Current password" autocomplete="current-password">
            <input id="s-pw-new" type="password" class="input" placeholder="New password (8+ characters)" autocomplete="new-password">
            <button class="btn btn-soft" type="submit"><i class="la la-key"></i>Change password</button></form>
          <div class="mt-4 flex flex-wrap gap-2">
            <button id="s-rotate" class="btn btn-soft btn-sm"><i class="la la-life-ring"></i>New recovery key</button>
            <button id="s-logout" class="btn btn-ghost btn-sm"><i class="la la-sign-out-alt"></i>Sign out</button>
            <button id="s-forget" class="btn btn-ghost btn-sm"><i class="la la-user-slash"></i>Sign out &amp; forget this device</button></div></section>

        <section class="card danger-zone" aria-labelledby="s-delete-title">
          <div id="s-delete-title" class="section-title text-[color:var(--critical)]"><i class="la la-user-times"></i>Delete account</div>
          <p class="mt-2 text-sm text-2">Permanently removes ${app.me.username ? `<b class="font-mono">@${esc(app.me.username)}</b>` : "your profile"} from this computer:
            your login and recovery key, chats, projects and uploaded files, challenges, memories, cognitive profile and SWOT, and any saved
            Claude / ChatGPT keys. Every device you're signed in on is signed out. <b>This cannot be undone.</b></p>
          <button id="s-delete" class="btn btn-danger btn-sm mt-4"><i class="la la-trash"></i>Delete my account</button></section>
      </div></div></div>`;

  const paintLevelNow = () => {
    const p = (app.me && app.me.progression) || {};
    const n = p.level || 1;
    $("#s-level-now", root).innerHTML = `<div class="text-lg font-extrabold">LVL ${n} <span class="gradient-text">${esc(LEVEL_NAMES[n - 1] || "")}</span></div>
      <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs"><span class="chip chip-accent">${esc(LEVEL_SCRUTINY[n - 1] || "")} scrutiny</span>
        <span class="chip">level bar ${Math.round(p.progress || 0)}%</span></div>`;
  };
  paintLevelNow();
  $("#s-level", root).onclick = async () => {
    try {
      const res = await openLevelDialog();
      if (!res || !res.message) return;
      await app.refreshMe();
      if (root.isConnected) paintLevelNow();
      toast(`Now at Level ${res.progression.level} - your coach adjusts from the next message.`, "success");
    } catch (e) { toast(e.message, "error"); }
  };

  const preview = new Avatar($("#s-preview", root), { face: avatarCfg.face, hue: avatarCfg.hue, name: avatarCfg.name });
  const minis = [];
  const facesEl = $("#s-faces", root);
  const paintFaces = () => {
    minis.splice(0).forEach((m) => m.destroy());
    facesEl.innerHTML = "";
    FACE_OPTIONS.forEach((f) => {
      const b = document.createElement("button");
      b.className = `rounded-2xl border p-2 ${avatarCfg.face === f.key ? "border-[color:var(--accent)] bg-[color:var(--surface-2)]" : "border-[color:var(--border)]"}`;
      b.innerHTML = `<div class="mx-auto w-20"></div><div class="mt-1 text-center text-xs font-semibold">${esc(f.label)}</div>`;
      facesEl.appendChild(b);
      minis.push(new Avatar(b.firstElementChild, { face: f.key, hue: avatarCfg.hue, interactive: false }));
      b.onclick = () => { avatarCfg.face = f.key; preview.setFace(f.key, avatarCfg.hue); preview.setExpression("happy"); paintFaces(); };
    });
  };
  paintFaces();
  $("#s-hue", root).oninput = (e) => { avatarCfg.hue = Number(e.target.value); preview.setFace(avatarCfg.face, avatarCfg.hue); minis.forEach((m, i) => m.setFace(FACE_OPTIONS[i].key, avatarCfg.hue)); };

  root.querySelectorAll("[data-theme]").forEach((b) => (b.onclick = () => {
    app.applyTheme(b.dataset.theme, true);
    root.querySelectorAll("[data-theme]").forEach((x) => x.className = `btn ${x === b ? "btn-primary" : "btn-ghost"} btn-sm`);
  }));

  const sel = $("#s-voice", root);
  const neuralSel = $("#s-neural", root);
  const unsub = app.voice.onVoices((voices) => {
    sel.innerHTML = '<option value="">Default voice</option>' + voices.map((v) => `<option value="${esc(v.voiceURI)}" ${voiceCfg.name === v.voiceURI ? "selected" : ""}>${esc(v.name)} (${esc(v.lang)})${v.localService ? "" : " (cloud)"}</option>`).join("");
    const nv = app.voice.neural;
    const current = voiceCfg.neural || nv.default;
    neuralSel.innerHTML = (nv.voices || []).map((v) => `<option value="${esc(v.id)}" ${current === v.id ? "selected" : ""}>${esc(v.label)}${v.installed ? "" : " - downloads on first use"}</option>`).join("")
      || '<option value="">Local voices unavailable - using the browser voice</option>';
  });
  const showEngine = () => {
    const neural = $("#s-engine", root).value === "neural";
    $("#s-neural-wrap", root).classList.toggle("hidden", !neural);
    $("#s-browser-wrap", root).classList.toggle("hidden", neural);
    $("#s-pitch-wrap", root).classList.toggle("hidden", neural);
  };
  $("#s-engine", root).onchange = showEngine;
  showEngine();
  $("#s-rate", root).oninput = (e) => { $("#s-rate-v", root).textContent = e.target.value; };
  $("#s-pitch", root).oninput = (e) => { $("#s-pitch-v", root).textContent = e.target.value; };
  const readVoice = () => ({ enabled: $("#s-venabled", root).checked, engine: $("#s-engine", root).value, neural: neuralSel.value,
    name: sel.value, rate: Number($("#s-rate", root).value), pitch: Number($("#s-pitch", root).value), volume: 1 });
  $("#s-say", root).onclick = () => {
    app.voice.configure({ ...readVoice() });
    preview.setExpression("happy");
    app.voice.speak(`Hi! I'm ${$("#s-name", root).value || "Byte"}. What happens if two users press save at exactly the same time?`, preview, { force: true });
  };
  $("#s-save-avatar", root).onclick = async () => {
    avatarCfg.name = $("#s-name", root).value.trim() || "Byte";
    try { await api("/api/settings", { method: "PUT", body: { avatar: avatarCfg } }); await app.refreshMe(); toast("Avatar saved", "success"); } catch (e) { toast(e.message, "error"); }
  };
  $("#s-save-voice", root).onclick = async () => {
    const v = readVoice();
    try {
      await api("/api/settings", { method: "PUT", body: { voice: v } });
      app.voice.configure(v);
      app.voice.setMuted(!v.enabled);
      await app.refreshMe();
      toast(v.enabled ? "Voice saved" : "Voice saved - muted (no voice, no lip sync)", "success");
    } catch (e) { toast(e.message, "error"); }
  };
  const onMute = () => { $("#s-venabled", root).checked = !app.voice.muted; };
  window.addEventListener("cte:mute", onMute);

  function paintFallback() {
    const keyRow = (provider, label, model) => {
      const k = keys[provider] || {};
      return `<div class="rounded-xl border border-[color:var(--border)] p-3">
          <div class="flex flex-wrap items-center justify-between gap-2"><b>${label}</b>${k.set ? `<span class="chip chip-good"><i class="la la-lock"></i>${esc(k.masked)} · encrypted</span>` : '<span class="chip">no key</span>'}</div>
          <div class="mt-1 text-xs muted">Default model: <code class="kbd">${esc(model)}</code>${k.detail ? ` · last check: ${esc(k.detail)}` : ""}</div>
          <div class="mt-2 flex gap-2"><input data-key="${provider}" type="password" autocomplete="off" class="input" placeholder="${provider === "anthropic" ? "sk-ant-..." : "sk-..."}">
            <button data-savekey="${provider}" class="btn btn-primary btn-sm">Validate & save</button>
            ${k.set ? `<button data-delkey="${provider}" class="icon-btn" title="Remove key"><i class="la la-trash"></i></button>` : ""}</div></div>`;
    };
    $("#s-fallback", root).innerHTML = `<div class="section-title"><i class="la la-cloud"></i>AI fallback (your own Claude / ChatGPT key)</div>
      <p class="mt-2 text-sm text-2">The local AI gateway + Qwen coder are the primary AI. If they go offline - or your device is too low-end for the context a level needs - your own key can take over.
        <b>Privacy note:</b> when the fallback is used, the text sent to the AI is processed by that provider. Keys are encrypted at rest (Fernet) and never shown again.</p>
      <div class="mt-3 grid gap-3">${keyRow("anthropic", "Anthropic Claude", data.fallback_models.anthropic)}${keyRow("openai", "OpenAI ChatGPT", data.fallback_models.openai)}</div>
      <div class="mt-4 grid gap-3 sm:grid-cols-2">
        <label><span class="label">Use which key</span><select id="s-fbprov" class="input">${[["none", "None"], ["anthropic", "Claude"], ["openai", "ChatGPT"]].map(([v, l]) => `<option value="${v}" ${fb.provider === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label><span class="label">Model override (optional)</span><input id="s-fbmodel" class="input" maxlength="80" value="${esc(fb.model || "")}" placeholder="leave empty for the default"></label>
        <label class="flex items-center gap-2 text-sm"><input id="s-fben" type="checkbox" class="h-4 w-4" ${fb.enabled ? "checked" : ""}> Use it when the local AI is offline</label>
        <label class="flex items-center gap-2 text-sm"><input id="s-fblong" type="checkbox" class="h-4 w-4" ${fb.for_long_context ? "checked" : ""}> Use it when a level needs more context memory than this device has</label></div>
      <button id="s-save-fb" class="btn btn-primary btn-sm mt-4"><i class="la la-save"></i>Save fallback settings</button>`;
    const box = $("#s-fallback", root);
    box.querySelectorAll("[data-savekey]").forEach((b) => (b.onclick = async () => {
      const provider = b.dataset.savekey;
      const key = box.querySelector(`[data-key="${provider}"]`).value.trim();
      if (key.length < 10) { toast("Paste a valid API key first.", "warn"); return; }
      b.disabled = true; b.innerHTML = '<i class="la la-cog la-spin"></i>Checking';
      try { const r = await api("/api/settings/keys", { method: "PUT", body: { provider, key } }); keys = r.keys; Object.assign(fb, r.settings.fallback || {}); toast("Key validated and stored encrypted", "success"); paintFallback(); }
      catch (e) { toast(e.message, "error", 8000); b.disabled = false; b.textContent = "Validate & save"; }
    }));
    box.querySelectorAll("[data-delkey]").forEach((b) => (b.onclick = async () => {
      try { const r = await api(`/api/settings/keys/${b.dataset.delkey}`, { method: "DELETE" }); keys = r.keys; Object.assign(fb, r.settings.fallback || {}); paintFallback(); toast("Key removed", "success"); }
      catch (e) { toast(e.message, "error"); }
    }));
    $("#s-save-fb", root).onclick = async () => {
      Object.assign(fb, { provider: $("#s-fbprov", root).value, model: $("#s-fbmodel", root).value.trim(), enabled: $("#s-fben", root).checked, for_long_context: $("#s-fblong", root).checked });
      try { await api("/api/settings", { method: "PUT", body: { fallback: fb } }); toast("Fallback settings saved", "success"); } catch (e) { toast(e.message, "error"); }
    };
  }
  paintFallback();

  // sensitive actions ask for the current password (a browser left signed in cannot be used to take a profile over)
  const needsPassword = !!(app.me && app.me.has_password);
  const passwordField = needsPassword
    ? '<label class="mt-3 block"><span class="label">Your current password</span><input id="cf-pass" type="password" class="input" autocomplete="current-password"></label>'
    : "";
  $("#s-rotate", root).onclick = () => {
    const m = modal({
      title: "Create a new recovery key?",
      body: `<p>Your old recovery key stops working. This device stays signed in.</p>${passwordField}`,
      actions: [{ label: "Cancel" }, {
        label: "Create", cls: "btn-primary", onClick: async () => {
          try {
            const r = await api("/api/auth/rotate", { method: "POST", quiet: true, body: { current_password: needsPassword ? $("#cf-pass", m.el).value : "" } });
            m.close();
            modal({ title: "Your new recovery key", body: `<code class="kbd select-all break-all !text-sm">${esc(r.recovery_key)}</code><p class="mt-2 text-xs muted">Shown once - store it safely.</p>`, actions: [{ label: "Done", cls: "btn-primary" }] });
          } catch (e) { toast(e.message, "error"); }
          return true;
        },
      }],
    });
  };
  $("#s-pw", root).onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/auth/password", { method: "POST", body: { current_password: $("#s-pw-cur", root).value, new_password: $("#s-pw-new", root).value } });
      $("#s-pw-cur", root).value = ""; $("#s-pw-new", root).value = "";
      toast("Password changed. Your other devices were signed out.", "success");
    } catch (err) { toast(err.message, "error"); }
  };
  const signOut = async (forget) => {
    await api("/api/auth/logout", { method: "POST" });
    if (forget && app.me && app.me.username) forgetAccount(app.me.username);
    app.me = null;
    location.hash = "#/login";
    location.reload();
  };
  $("#s-logout", root).onclick = () => signOut(false);
  $("#s-forget", root).onclick = async () => {
    if (!(await confirmDialog("Forget this device?", "You'll be signed out and your username won't be listed on this device's sign-in screen anymore. Your profile is not deleted.", { okLabel: "Sign out & forget" }))) return;
    signOut(true);
  };
  $("#s-delete", root).onclick = () => {
    const uname = app.me && app.me.username;
    const phrase = uname || "DELETE";  // type your own username - harder to confirm by accident than "yes"
    const m = modal({
      title: "Delete your account?", tone: "danger",
      icon: '<div class="grid h-11 w-11 place-items-center rounded-2xl bg-[color:var(--critical)]/15 text-[color:var(--critical)]"><i class="la la-user-times text-2xl"></i></div>',
      body: `<p>This permanently deletes everything that belongs to ${uname ? `<b class="font-mono">@${esc(uname)}</b>` : "this profile"}:</p>
        <ul class="mt-2 list-disc space-y-0.5 pl-5">
          <li>your login, password and recovery key - every device is signed out</li>
          <li>all chats, memories, your cognitive profile and SWOT analysis</li>
          <li>projects and uploaded files, challenges and hidden-bug hunts</li>
          <li>saved Claude / ChatGPT keys and any AI work still running</li></ul>
        <p class="mt-3 font-semibold text-[color:var(--critical)]">This cannot be undone.</p>
        <label class="mt-3 block"><span class="label">Type <b class="font-mono normal-case">${esc(phrase)}</b> to confirm</span>
          <input id="del-confirm" class="input font-mono" autocomplete="off" autocapitalize="none" spellcheck="false"></label>${passwordField}`,
      actions: [{ label: "Keep my account" }, {
        label: "Delete account forever", cls: "btn-danger", icon: "la-trash", onClick: async () => {
          if ($("#del-confirm", m.el).value.trim().replace(/^@/, "") !== phrase) { toast(`Type ${phrase} to confirm.`, "warn"); return true; }
          if (needsPassword && !$("#cf-pass", m.el).value) { toast("Enter your current password.", "warn"); return true; }
          try {
            await api("/api/me", { method: "DELETE", quiet: true, body: { current_password: needsPassword ? $("#cf-pass", m.el).value : "" } });
          } catch (e) { toast(e.message, "error"); return true; }
          if (uname) eraseFromDevice(uname);  // also forget it on this device's sign-in screen
          app.me = null;
          try { sessionStorage.setItem("cte-account-deleted", uname || "1"); } catch { /* ignore */ }
          location.hash = "#/";
          location.reload();
          return false;
        },
      }],
    });
  };

  return () => { unsub(); window.removeEventListener("cte:mute", onMute); preview.destroy(); minis.forEach((m) => m.destroy()); app.voice.cancel(); };
}
