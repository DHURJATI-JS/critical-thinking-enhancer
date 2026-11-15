// Family sign-in (like a home computer's login screen):
//   0 profiles  -> create one
//   1 profile   -> straight to that profile's password (or use another account / add a new profile)
//   2+ profiles -> choose who you are, or add another account
import { api } from "../api.js";
import { Avatar } from "../avatar.js";
import { deviceAccounts, forgetAccount, rememberAccount } from "../components.js";
import { $, esc, modal, toast } from "../ui.js";

async function loadProfiles() {
  let server = [];
  let hasAccounts = true;  // unknown (error / rate limit) -> still offer the username form
  try {
    const res = await api("/api/auth/profiles", { quiet: true });
    server = res.profiles || [];
    hasAccounts = res.has_accounts !== false;
  } catch { server = []; }
  const device = deviceAccounts();
  const byName = new Map();
  for (const p of server) byName.set(p.username, { ...p });
  for (const d of device) byName.set(d.username, { ...(byName.get(d.username) || {}), ...d });
  const order = new Map(device.map((d, i) => [d.username, i]));
  const list = [...byName.values()].sort((a, b) => (order.get(a.username) ?? 999) - (order.get(b.username) ?? 999));
  return { list, hasAccounts };
}

export async function render(root, app) {
  const avatars = [];
  const { list: profiles, hasAccounts } = await loadProfiles();
  // nobody has a profile on this computer yet -> create one; profiles exist but are not listed
  // (privacy setting, new device) -> type the username
  if (!profiles.length && !hasAccounts) { location.hash = "#/welcome"; return null; }
  let lock = { locked: false };
  try { lock = await api("/api/auth/lock", { quiet: true }); } catch { /* assume free */ }
  const lockedOut = (p) => lock.locked && (!lock.username || p.username !== lock.username);
  let mode = profiles.length === 0 ? "other" : profiles.length === 1 ? "password" : "pick";
  let selected = profiles.length === 1 ? profiles[0] : null;
  if (lock.locked && lock.username) {  // only the person using the app may continue (e.g. on another device)
    const holder = profiles.find((p) => p.username === lock.username);
    if (holder) { mode = "password"; selected = holder; }
  }
  const lockTimer = setInterval(async () => {
    try {
      const fresh = await api("/api/auth/lock", { quiet: true });
      if (fresh.locked !== lock.locked || fresh.username !== lock.username) { lock = fresh; paint(); }
    } catch { /* ignore */ }
  }, 5000);

  const clearAvatars = () => avatars.splice(0).forEach((a) => a.destroy());
  const mountAvatar = (el, p, size) => avatars.push(new Avatar(el, { face: p.face || "bot", hue: p.hue ?? 190, interactive: size === "big", name: p.name }));

  function paint() {
    clearAvatars();
    let body;
    if (mode === "pick") {
      body = `<div class="text-center"><h1 class="text-3xl font-extrabold">Who's training today?</h1>
          <p class="mt-1 text-sm muted">Choose your profile, or add another person.</p></div>
        <div class="grid w-full grid-cols-2 gap-4 sm:grid-cols-3">${profiles.map((p) => `
          <button class="card flex flex-col items-center gap-2 !p-4 transition ${lockedOut(p) ? "cursor-not-allowed opacity-40" : "hover:-translate-y-0.5 hover:border-[color:var(--accent)]"}"
            data-pick="${esc(p.username)}" ${lockedOut(p) ? "disabled" : ""}>
            <div class="w-24" data-face="${esc(p.username)}"></div>
            <div class="font-bold">${esc(p.name || p.username)}</div>
            <div class="font-mono text-xs muted">@${esc(p.username)}</div></button>`).join("")}
          ${lock.locked ? `<div class="card flex flex-col items-center justify-center gap-2 !p-4 opacity-40" aria-disabled="true">
            <div class="grid h-24 w-24 place-items-center rounded-full border-2 border-dashed border-[color:var(--border)] text-3xl muted"><i class="la la-plus"></i></div>
            <div class="font-bold">Add another account</div><div class="text-xs muted">when the app is free</div></div>`
          : `<a href="#/welcome" class="card flex flex-col items-center justify-center gap-2 !p-4 transition hover:-translate-y-0.5 hover:border-[color:var(--accent)]">
            <div class="grid h-24 w-24 place-items-center rounded-full border-2 border-dashed border-[color:var(--border)] text-3xl muted"><i class="la la-plus"></i></div>
            <div class="font-bold">Add another account</div><div class="text-xs muted">create a new profile</div></a>`}
        </div>
        ${lock.locked && lock.username ? "" : '<button id="lg-other" class="text-sm underline underline-offset-2 muted">Sign in with a username instead</button>'}`;
    } else if (mode === "password") {
      body = `<div class="flex flex-col items-center text-center">
          <div class="w-40" data-face="${esc(selected.username)}" data-big="1"></div>
          <h1 class="mt-2 text-3xl font-extrabold">Hi, ${esc(selected.name || selected.username)}!</h1>
          <p class="font-mono text-sm muted">@${esc(selected.username)}</p></div>
        <form id="lg-form" class="card w-full max-w-sm" autocomplete="on">
          <input type="text" name="username" autocomplete="username" value="${esc(selected.username)}" class="hidden" readonly>
          <label class="block"><span class="label">Password</span>
            <input id="lg-pass" name="password" type="password" class="input" autocomplete="current-password" placeholder="Enter your password"></label>
          <button class="btn btn-primary mt-3 w-full" type="submit" ${lockedOut(selected) ? "disabled" : ""}><i class="la la-sign-in-alt"></i>Sign in</button>
          <button type="button" id="lg-recover" class="mt-3 w-full text-center text-xs underline underline-offset-2 muted">Forgot your password? Use your recovery key</button>
        </form>
        ${lock.locked && lock.username ? "" : `<div class="flex flex-wrap items-center justify-center gap-2">
          ${profiles.length > 1 ? '<button id="lg-back" class="btn btn-ghost btn-sm"><i class="la la-users"></i>Choose another profile</button>'
            : '<button id="lg-other" class="btn btn-ghost btn-sm"><i class="la la-user"></i>Use another account</button>'}
          ${lock.locked ? "" : '<a href="#/welcome" class="btn btn-ghost btn-sm"><i class="la la-plus"></i>Add a new profile</a>'}
        </div>`}`;
    } else {
      body = `<div class="text-center"><h1 class="text-3xl font-extrabold">Sign in</h1><p class="mt-1 text-sm muted">Use your username and password.</p></div>
        <form id="lg-form" class="card w-full max-w-sm" autocomplete="on">
          <label class="block"><span class="label">Username</span>
            <input id="lg-user" name="username" class="input font-mono" autocomplete="username" autocapitalize="none" spellcheck="false"></label>
          <label class="mt-3 block"><span class="label">Password</span>
            <input id="lg-pass" name="password" type="password" class="input" autocomplete="current-password"></label>
          <button class="btn btn-primary mt-3 w-full" type="submit"><i class="la la-sign-in-alt"></i>Sign in</button>
          <button type="button" id="lg-recover" class="mt-3 w-full text-center text-xs underline underline-offset-2 muted">Forgot your password? Use your recovery key</button>
        </form>
        <div class="flex flex-wrap items-center justify-center gap-2">
          ${profiles.length ? '<button id="lg-back" class="btn btn-ghost btn-sm"><i class="la la-users"></i>Back to profiles</button>' : ""}
          ${lock.locked ? "" : '<a href="#/welcome" class="btn btn-ghost btn-sm"><i class="la la-plus"></i>Add a new profile</a>'}</div>`;
    }
    const lockBanner = lock.locked ? `<div class="card card-tight flex w-full items-start gap-3 border-[color:var(--warning)]/60 text-sm">
        <i class="la la-user-lock text-2xl text-[color:var(--warning)]"></i>
        <div><b>${esc(lock.name)} is using the app right now.</b> Only one person can use it at a time, on any device -
        ${lock.username ? `only ${esc(lock.name)} can sign in until they finish.` : "please wait until they finish."}
        <span class="muted">This page updates by itself.</span></div></div>` : "";
    root.innerHTML = `<div class="mx-auto flex max-w-3xl flex-col items-center gap-6 py-6 fade-in">${lockBanner}${body}
      <p class="max-w-md text-center text-xs muted"><i class="la la-lock"></i> Profiles live only on this computer. Passwords are stored as salted scrypt hashes and never leave it.</p></div>`;

    root.querySelectorAll("[data-face]").forEach((el) => {
      const p = profiles.find((x) => x.username === el.dataset.face);
      if (p) mountAvatar(el, p, el.dataset.big ? "big" : "small");
    });
    root.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => {
      const p = profiles.find((x) => x.username === b.dataset.pick);
      if (!p || lockedOut(p)) return;
      selected = p; mode = "password"; paint();
    }));
    const other = $("#lg-other", root);
    if (other) other.onclick = () => { mode = "other"; paint(); };
    const back = $("#lg-back", root);
    if (back) back.onclick = () => { mode = profiles.length === 1 ? "password" : "pick"; selected = profiles.length === 1 ? profiles[0] : null; paint(); };
    const recover = $("#lg-recover", root);
    if (recover) recover.onclick = recoverDialog;
    const form = $("#lg-form", root);
    if (form) {
      setTimeout(() => { const f = $(mode === "other" ? "#lg-user" : "#lg-pass", root); if (f) f.focus(); }, 60);
      form.onsubmit = (e) => { e.preventDefault(); signIn(); };
    }
  }

  async function signIn() {
    const username = mode === "other" ? $("#lg-user", root).value.trim().toLowerCase() : selected.username;
    const password = $("#lg-pass", root).value;
    if (!username || !password) { toast("Enter your password.", "warn"); return; }
    const btn = root.querySelector("#lg-form button[type=submit]");
    btn.disabled = true;
    btn.innerHTML = '<i class="la la-circle-notch la-spin"></i>Signing in...';
    try {
      const res = await api("/api/auth/login", { method: "POST", body: { username, password }, quiet: true });
      rememberAccount(res.user);
      await app.refreshMe();
      location.hash = "#/chat";
    } catch (err) {
      toast(err.message, "error");
      const pass = $("#lg-pass", root);
      if (pass) { pass.value = ""; pass.focus(); }
      btn.disabled = false;
      btn.innerHTML = '<i class="la la-sign-in-alt"></i>Sign in';
    }
  }

  function recoverDialog() {
    const m = modal({
      title: "Sign in with your recovery key",
      body: '<p class="mb-3">Paste the recovery key you saved when the profile was created.</p><input id="rk-in" class="input font-mono" placeholder="cte_..." autocomplete="off">',
      actions: [{ label: "Cancel" }, {
        label: "Continue", cls: "btn-primary", onClick: async () => {
          try {
            const res = await api("/api/auth/recover", { method: "POST", body: { key: $("#rk-in", m.el).value.trim() }, quiet: true });
            rememberAccount(res.user);
            await app.refreshMe();
            m.close();
            newPasswordDialog(res.user);
          } catch (err) { toast(err.message, "error"); }
          return true;
        },
      }],
    });
  }

  function newPasswordDialog(user) {
    const m = modal({
      title: user.username ? `Choose a new password for @${user.username}` : "Choose a username and password",
      dismissible: false,
      body: `<div class="space-y-3">${user.username ? "" : '<label class="block"><span class="label">Username</span><input id="np-user" class="input font-mono" autocomplete="username"></label>'}
        <label class="block"><span class="label">New password (8+ characters)</span><input id="np-pass" type="password" class="input" autocomplete="new-password"></label>
        <label class="block"><span class="label">Repeat it</span><input id="np-pass2" type="password" class="input" autocomplete="new-password"></label></div>`,
      actions: [{
        label: "Save", cls: "btn-primary", onClick: async () => {
          const pass = $("#np-pass", m.el).value;
          if (pass !== $("#np-pass2", m.el).value) { toast("The passwords don't match.", "warn"); return true; }
          try {
            if (user.username) await api("/api/auth/password", { method: "POST", body: { new_password: pass }, quiet: true });
            else await api("/api/auth/setup", { method: "POST", body: { username: $("#np-user", m.el).value.trim().toLowerCase(), password: pass }, quiet: true });
            const me = await app.refreshMe();
            rememberAccount(me);
            toast("Password saved. Your other devices were signed out.", "success");
            m.close();
            location.hash = "#/chat";
          } catch (err) { toast(err.message, "error"); }
          return true;
        },
      }],
    });
  }

  paint();
  return () => { clearInterval(lockTimer); clearAvatars(); };
}

export { forgetAccount };
