// App shell: header/nav, theme, 5-second AI health polling, outage dialog, banners, router.
import { api, onApiError } from "./api.js";
import { $, blockBackground, esc, h, icon, modal, setIconManifest, toast } from "./ui.js";
import { Voice } from "./voice.js";
import * as about from "./pages/about.js";
import * as challenges from "./pages/challenges.js";
import * as chat from "./pages/chat.js";
import { deviceAccounts, rememberAccount } from "./components.js";
import * as login from "./pages/login.js";
import * as onboarding from "./pages/onboarding.js";
import * as profile from "./pages/profile.js";
import * as projects from "./pages/projects.js";
import * as settingsPage from "./pages/settings.js";
import * as system from "./pages/system.js";

const ROUTES = {
  chat: { page: chat, label: "Chat", la: "comments" },
  challenges: { page: challenges, label: "Challenges", la: "puzzle-piece" },
  projects: { page: projects, label: "Projects", la: "code" },
  profile: { page: profile, label: "Profile", la: "brain" },
  system: { page: system, label: "System", la: "server" },
  settings: { page: settingsPage, label: "Settings", la: "sliders-h" },
  about: { page: about, label: "About", la: "info-circle" },
};

export const app = {
  config: {},
  me: null,
  profileCount: 0,
  health: null,
  voice: new Voice(),
  cleanup: null,
  outage: { modal: null, dismissed: null, wasOnline: null },
  dismissedBanners: new Set((() => { try { return JSON.parse(sessionStorage.getItem("cte-dismissed") || "[]"); } catch { return []; } })()),

  navigate(route) { location.hash = `#/${route}`; },

  async refreshMe() {
    try {
      const s = await api("/api/session", { quiet: true });
      this.me = s.user;
      this.lock = s.lock || { locked: false };
      if (s.blocked) showLockOverlay(s.lock); // signed in on this device, but someone else is using the app
    } catch { this.me = null; }
    // mute + voice are per profile: one person muting does not silence the next person on this device
    this.voice.setProfile(this.me && this.me.username);
    if (this.me) this.voice.configure(this.me.settings && this.me.settings.voice);
    renderHeader();
    renderFooter();
    return this.me;
  },

  applyTheme(theme, persist = false) {
    const t = theme || "dark";
    try { localStorage.setItem("cte-theme", t); } catch { /* ignore */ }
    const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const btn = $("#theme-toggle");
    if (btn) btn.innerHTML = `<i class="la ${dark ? "la-sun" : "la-moon"} text-lg"></i>`;
    if (persist && this.me) {
      this.me.settings = { ...(this.me.settings || {}), theme: t };  // so a re-render before the PUT lands keeps it
      api("/api/settings", { method: "PUT", body: { theme: t }, quiet: true }).catch(() => {});
    }
  },

  showOutage(force = false) { showOutageDialog(this.health, force); },
};

// ---------------------------------------------------------------- header / footer
function renderHeader() {
  const header = $("#app-header");
  const signedIn = !!app.me;
  const items = signedIn
    ? Object.entries(ROUTES).map(([key, r]) => [key, r.label, r.la])
    : [["login", "Sign in", "sign-in-alt"], ["welcome", "Create profile", "rocket"], ["system", "System", "server"], ["about", "About", "info-circle"]];
  // full nav needs ~1280px signed in (7 items), ~768px signed out (3 items); below that: hamburger menu
  const desktopNav = signedIn ? "hidden xl:flex" : "hidden md:flex";
  const mobileOnly = signedIn ? "xl:hidden" : "md:hidden";
  const level = signedIn && app.me.progression ? app.me.progression.level : null;
  // the header may use more width than the page content (1536 px), so every label fits on normal screens
  header.innerHTML = `<div class="mx-auto flex max-w-screen-2xl items-center gap-3 px-4 py-2.5 sm:px-6">
      <a href="#/${signedIn ? "chat" : "welcome"}" class="flex shrink-0 items-center gap-2.5" aria-label="${esc(app.config.name || "Critical Thinking Enhancer")} - home">
        <img src="/static/img/logo.svg" alt="" class="h-9 w-9">
        <div class="brand-text hidden leading-tight sm:block">
          <div class="gradient-text text-[15px] font-extrabold tracking-tight">${esc(app.config.name || "Critical Thinking Enhancer")}</div>
          <div class="font-mono text-[10px] uppercase tracking-[.2em] muted">v${esc(app.config.version || "1")} · local-first</div>
        </div></a>
      <nav class="${desktopNav} min-w-0 flex-1 items-center gap-1" aria-label="Main">${items.map(([key, label, la]) => {
        const utility = key === "system" || key === "about";  // first to lose their label if space ever runs out
        return `<a href="#/${key}" data-route="${key}" class="nav-link"${utility ? ` data-utility title="${esc(label)}" aria-label="${esc(label)}"` : ""}>`
          + `<i class="la la-${la} text-base"></i><span class="nav-label">${esc(label)}</span></a>`;
      }).join("")}</nav>
      <div class="ml-auto flex shrink-0 items-center gap-2">
        <button id="ai-pill" class="chip" title="AI availability (pinged every ${app.config.health_interval_seconds || 5}s)"><span class="dot dot-wait"></span><span class="pill-text hidden sm:inline">checking AI</span></button>
        ${level ? `<a href="#/chat" class="chip chip-accent hidden sm:inline-flex" title="${esc(app.me.level?.name || "")}"><i class="la la-signal"></i>LVL ${level}</a>` : ""}
        ${app.config.vercel_url ? `<a href="${esc(app.config.vercel_url)}" target="_blank" rel="noopener noreferrer" class="icon-btn hidden sm:inline-grid" title="Author's website"><i class="la la-globe text-lg"></i></a>` : ""}
        <button id="mute-toggle" class="icon-btn" title="Mute / unmute the AI voice"></button>
        <button id="theme-toggle" class="icon-btn" title="Toggle dark / light mode"></button>
        <button id="nav-toggle" class="icon-btn ${mobileOnly}" aria-label="Open menu" aria-expanded="false" aria-controls="mobile-nav"><i class="la la-bars text-lg"></i></button>
      </div></div>
    <nav id="mobile-nav" class="mobile-nav hidden border-t border-[color:var(--border)] ${mobileOnly}" aria-label="Main (mobile)">
      <div class="mx-auto grid max-w-7xl gap-1 px-4 py-3 sm:grid-cols-2 sm:px-6">${items.map(([key, label, la]) =>
        `<a href="#/${key}" data-route="${key}"><i class="la la-${la} text-lg"></i>${esc(label)}</a>`).join("")}</div></nav>`;
  const toggle = $("#nav-toggle");
  toggle.onclick = () => {
    const open = $("#mobile-nav").classList.toggle("hidden") === false;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.innerHTML = `<i class="la ${open ? "la-times" : "la-bars"} text-lg"></i>`;
  };
  $("#theme-toggle").onclick = () => app.applyTheme(document.documentElement.classList.contains("dark") ? "light" : "dark", true);
  paintMute();
  $("#mute-toggle").onclick = () => app.voice.setMuted(!app.voice.muted);
  $("#ai-pill").onclick = () => (app.health && !app.health.ai_online ? app.showOutage(true) : app.navigate("system"));
  // a signed-in profile's own theme wins over whatever the last person on this device used
  const own = app.me && app.me.settings && app.me.settings.theme;
  app.applyTheme(own || localStorage.getItem("cte-theme") || "dark");
  highlightNav();
  updatePill();
  fitHeader();
}

function paintMute() {
  const mute = $("#mute-toggle");
  if (!mute) return;
  const muted = app.voice.muted;
  mute.innerHTML = `<i class="la ${muted ? "la-volume-mute" : "la-volume-up"} text-lg"></i>`;
  mute.title = muted ? "Sound is off (no voice, no lip sync) - click to unmute" : "Mute the AI voice (also stops lip sync)";
  mute.setAttribute("aria-label", mute.title);
  mute.setAttribute("aria-pressed", String(muted));
  mute.classList.toggle("text-[color:var(--critical)]", muted);
}
window.addEventListener("cte:mute", paintMute);  // registered once (the header is re-rendered often)

function renderFooter() {
  const c = app.config;
  const name = c.name || "Critical Thinking Enhancer";
  const explore = app.me
    ? [["chat", "Chat"], ["challenges", "Challenges"], ["projects", "Projects"], ["profile", "Cognitive profile"], ["system", "System status"]]
    : [["welcome", "Get started"], ["system", "System status"], ["about", "About"]];
  const author = c.author && !/^your name$/i.test(c.author) ? c.author : name;
  $("#app-footer").innerHTML = `<div class="mx-auto w-full max-w-7xl px-4 sm:px-6">
      <div class="grid gap-10 py-10 sm:grid-cols-2 lg:grid-cols-[1.6fr_1fr_1fr]">
        <div class="space-y-4 sm:col-span-2 lg:col-span-1">
          <a href="#/${app.me ? "chat" : "welcome"}" class="inline-flex items-center gap-3">
            <img src="/static/img/logo.svg" alt="" class="h-10 w-10">
            <span class="gradient-text text-lg font-extrabold tracking-tight">${esc(name)}</span></a>
          <p class="max-w-md text-sm leading-relaxed text-2">Train how you think, not just what you code - logic, critical thinking, metacognition and bug foresight for student developers.</p>
          <div class="privacy-badge"><i class="la la-user-shield text-lg"></i>
            <span><b>Private by design.</b> Everything happens and is stored locally on this machine.</span></div>
        </div>
        <nav aria-label="Footer navigation">
          <div class="footer-head">Explore</div>
          <ul class="mt-4 space-y-2.5 text-sm">${explore.map(([r, l]) => `<li><a class="footer-link" href="#/${r}">${l}</a></li>`).join("")}</ul>
        </nav>
        <div>
          <div class="footer-head">Trust &amp; legal</div>
          <ul class="mt-4 space-y-2.5 text-sm">
            <li><a class="footer-link" href="#/about">Disclaimers</a></li>
            <li><a class="footer-link" href="#/about">MIT License</a></li>
            <li class="text-2">Student-grade project</li>
            <li class="text-2">Not a medical tool or an IQ test</li>
          </ul>
        </div>
      </div>
      <div class="flex flex-col gap-3 border-t border-[color:var(--border)] py-6 text-xs muted sm:flex-row sm:items-center sm:justify-between">
        <span>&copy; ${new Date().getFullYear()} ${esc(author)} &middot; v${esc(c.version || "1.0.0")} &middot; provided as is, without warranty or liability</span>
        <span class="flex flex-wrap items-center gap-x-5 gap-y-2">
          ${c.vercel_url ? `<a class="footer-link inline-flex items-center gap-1" href="${esc(c.vercel_url)}" target="_blank" rel="noopener noreferrer"><i class="la la-globe"></i>Website</a>` : ""}
          <a class="footer-link" href="https://icons8.com" target="_blank" rel="noopener noreferrer">Icons by Icons8</a>
        </span>
      </div>
    </div>`;
}

function highlightNav() {
  const current = (location.hash.replace(/^#\/?/, "").split("?")[0]) || (app.me ? "chat" : signedOutHome());
  document.querySelectorAll("#app-header [data-route]").forEach((a) => a.classList.toggle("active", a.dataset.route === current));
}

// ---------------------------------------------------------------- health (every 5 s) + outage dialog
function updatePill() {
  const pill = $("#ai-pill");
  const hl = app.health;
  if (!pill) return;
  let cls = "chip chip-bad", dot = "dot-off", text = "AI offline";
  if (!hl || !hl.checked_once) { cls = "chip"; dot = "dot-wait"; text = "checking AI"; }
  else if (hl.ai_online) { cls = "chip chip-good"; dot = "dot-on"; text = `AI online${hl.test_mode ? " (test)" : ""}`; }
  else if (hl.user && hl.user.fallback_ready) { cls = "chip chip-warn"; dot = "dot-wait"; text = "fallback key"; }
  const html = `<span class="dot ${dot}"></span><span class="pill-text hidden sm:inline">${text}</span>`;
  if (pill.className === cls && pill.innerHTML === html) return;  // unchanged (polled every 5 s)
  pill.className = cls;
  pill.innerHTML = html;
  pill.setAttribute("aria-label", text);
  fitHeader();  // the pill's width changed
}

/** Keep every header item visible without overlaps: measure, and only if the nav would run into the controls
 *  compact step by step - AI pill to its dot, then the brand title, then the System / About labels. */
const FIT_STEPS = ["fit-pill", "fit-brand", "fit-utility"];
function fitHeader() {
  const header = $("#app-header");
  const nav = header && header.querySelector('nav[aria-label="Main"]');
  const controls = header && header.querySelector(".ml-auto");
  if (!nav || !controls) return;
  header.classList.remove(...FIT_STEPS);
  if (getComputedStyle(nav).display === "none") return;  // narrow screen: hamburger menu
  const fits = () => {
    const links = nav.querySelectorAll("a");
    const last = links[links.length - 1];
    return nav.scrollWidth <= nav.clientWidth + 1
      && (!last || last.getBoundingClientRect().right <= controls.getBoundingClientRect().left - 8);
  };
  for (const step of FIT_STEPS) {
    if (fits()) return;
    header.classList.add(step);
  }
}
let fitTimer = 0;
const refit = () => { clearTimeout(fitTimer); fitTimer = setTimeout(fitHeader, 60); };
window.addEventListener("resize", refit);
if (document.fonts) {  // text gets narrower when the web fonts replace the fallback font: measure again
  document.fonts.addEventListener("loadingdone", refit);
  document.fonts.ready.then(refit);
}

function banner(id, tone, iconHtml, html, actions = "") {
  if (app.dismissedBanners.has(id)) return "";
  return `<div class="card card-tight fade-in flex flex-col gap-2 text-sm sm:flex-row sm:items-center ${tone}" data-banner="${id}">
      <div class="flex min-w-0 flex-1 items-start gap-2.5">${iconHtml}<div class="min-w-0">${html}</div></div>
      <div class="flex shrink-0 items-center gap-2">${actions}<button class="icon-btn h-8 w-8" data-dismiss="${id}" aria-label="Dismiss"><i class="la la-times"></i></button></div></div>`;
}

let bannersHtml = "";
function renderBanners() {
  const hl = app.health;
  const el = $("#banners");
  if (!hl) { el.innerHTML = ""; bannersHtml = ""; return; }
  const parts = [];
  if (hl.test_mode) {
    parts.push(banner("test", "", icon("test-tube", "flask"), `<b>Test mode:</b> the AI endpoints are the bundled mock servers (canned answers). Point <code class="kbd">.env</code> / <code class="kbd">config.json</code> at your real AI gateway and Qwen coder.`));
  }
  if (hl.checked_once && !hl.ai_online && app.outage.dismissed === hl.outage_count) {
    parts.push(banner("offline-" + hl.outage_count, "", '<span class="dot dot-off mt-1.5"></span>',
      `<b>AI server stopped working.</b> All AI tasks were halted and marked as failed. <span class="muted">${esc(hl.reason || "")}</span>`,
      '<button class="btn btn-soft btn-sm" data-act="outage">Details</button>'));
  }
  const low = hl.low_end;
  if (low && low.low_end) {
    parts.push(banner("lowend", "", icon("warning-shield", "exclamation-triangle"),
      `<b>Device too low-end for local AI.</b> ${esc(low.reasons.join(" "))} ${esc(low.advice)}`,
      app.me ? '<a class="btn btn-primary btn-sm" href="#/settings">Add API key</a>' : ""));
  }
  if (low && low.notes && low.notes.length) {
    parts.push(banner("gpu-note", "", icon("processor", "microchip"), `<span class="text-2">${esc(low.notes.join(" "))}</span>`, '<a class="btn btn-soft btn-sm" href="#/system">System</a>'));
  }
  const html = parts.join("");
  if (html === bannersHtml) return;  // unchanged: don't rebuild (and re-animate) every 5 s
  bannersHtml = html;
  el.innerHTML = html;
  el.querySelectorAll("[data-dismiss]").forEach((b) => (b.onclick = () => {
    app.dismissedBanners.add(b.dataset.dismiss);
    try { sessionStorage.setItem("cte-dismissed", JSON.stringify([...app.dismissedBanners])); } catch { /* storage blocked */ }
    renderBanners();
  }));
  const act = el.querySelector('[data-act="outage"]');
  if (act) act.onclick = () => app.showOutage(true);
}

function roleLine(role, info) {
  const ok = info && info.ok;
  return `<li class="flex items-start gap-2"><span class="dot ${ok ? "dot-on" : "dot-off"} mt-1.5"></span>
      <span><b>${esc((info && info.label) || role)}</b> <span class="muted">(${esc((info && info.model) || "?")} @ ${esc((info && info.endpoint) || "?")})</span><br>
      <span class="${ok ? "text-[color:var(--good-text)]" : "text-[color:var(--critical)]"}">${esc((info && info.detail) || "unknown")}</span></span></li>`;
}

function showOutageDialog(hl, force) {
  if (!hl || hl.ai_online || !hl.checked_once) return;
  if (app.outage.modal) { app.outage.modal.close("refresh"); }
  if (!force && app.outage.dismissed === hl.outage_count) return;
  const repo = app.config.ai_server_repo;
  const fallback = hl.user && hl.user.fallback_ready;
  const body = `<div class="space-y-3">
      <p class="text-[15px] text-[color:var(--text)]">The AI servers this app depends on are not responding, so <b>everything was halted</b>: all queued and running AI tasks were stopped and <b>marked as failed</b>. Nothing new will be processed until they are back.</p>
      <ul class="space-y-2">${roleLine("gateway", hl.roles.gateway)}${roleLine("coder", hl.roles.coder)}</ul>
      <div class="rounded-xl border border-[color:var(--border)] p-3">
        <div class="label">How to fix</div>
        <ol class="list-decimal space-y-1 pl-5">
          <li>Start your AI gateway - ${repo ? `download / run the AI server repo: <a class="underline" href="${esc(repo)}" target="_blank" rel="noopener noreferrer">${esc(repo)}</a>` : "download / run the AI server repo (its link goes in <code class=\"kbd\">config.json</code> &rarr; <code class=\"kbd\">app.ai_server_repo</code>)"}. Locally-run Gemma is not supported.</li>
          <li>Start the Qwen coder: <code class="kbd">docker compose --profile coder up -d</code></li>
          <li>Or add your own <b>Claude</b> or <b>ChatGPT</b> API key in Settings as a fallback.</li>
        </ol></div>
      <p class="text-xs muted"><i class="la la-sync la-spin"></i> Re-checking every ${app.config.health_interval_seconds || 5} seconds - this dialog closes automatically when the AI is back.</p></div>`;
  const actions = [{ label: "System status", icon: "la-server", onClick: () => app.navigate("system") }];
  if (app.me) actions.push({ label: "Add API key", icon: "la-key", onClick: () => app.navigate("settings") });
  if (fallback) actions.push({ label: "Continue with my key", cls: "btn-primary", icon: "la-cloud" });
  actions.push({ label: "Dismiss", cls: fallback ? "btn-ghost" : "btn-primary" });
  app.outage.modal = modal({
    title: "AI server stopped working", tone: "danger", body, actions, wide: true,
    icon: `<div class="grid h-11 w-11 place-items-center rounded-2xl bg-[color:var(--critical)]/15 text-[color:var(--critical)]"><i class="la la-plug text-2xl"></i></div>`,
    onClose: (reason) => {
      app.outage.modal = null;
      if (reason !== "refresh" && reason !== "recovered") { app.outage.dismissed = hl.outage_count; renderBanners(); }
    },
  });
}

async function pollHealth() {
  try {
    const hl = await api("/api/health", { quiet: true });
    const was = app.health;
    app.health = hl;
    // someone else took over after we went idle. Only trust "blocked": a check that started before this browser
    // signed in reports the lock as held - by ourselves - and must not lock us out of our own session.
    if (app.me && hl.blocked) showLockOverlay(hl.lock);
    if (hl.checked_once) {
      if (!hl.ai_online) {
        const newOutage = !was || was.ai_online || (was.outage_count !== hl.outage_count);
        if (!app.outage.modal && (newOutage || app.outage.dismissed !== hl.outage_count)) showOutageDialog(hl, false);
      } else if (was && was.checked_once && !was.ai_online) {
        if (app.outage.modal) app.outage.modal.close("recovered");
        toast("AI servers are back online - you can continue.", "success");
      }
    }
  } catch (e) {
    app.health = app.health ? { ...app.health, ai_online: false, reason: "The app server itself is unreachable." } : null;
  }
  updatePill();
  renderBanners();
}

// ---------------------------------------------------------------- router
const PUBLIC_ROUTES = new Set(["welcome", "login", "about", "system"]);
// signed out: if any profile exists on this computer (or was used on this device) -> sign-in screen
const signedOutHome = () => (app.hasAccounts || app.profileCount > 0 || deviceAccounts().length ? "login" : "welcome");

let navSeq = 0;
async function route() {
  const nav = ++navSeq;
  const name = (location.hash.replace(/^#\/?/, "").split("?")[0]) || (app.me ? "chat" : signedOutHome());
  if (!app.me && !PUBLIC_ROUTES.has(name)) { location.hash = `#/${signedOutHome()}`; return; }
  if (app.me && (name === "welcome" || name === "login")) { location.hash = "#/chat"; return; }
  if (app.cleanup) { try { app.cleanup(); } catch { /* ignore */ } app.cleanup = null; }
  app.voice.cancel();
  const mobileNav = $("#mobile-nav");
  if (mobileNav && !mobileNav.classList.contains("hidden")) $("#nav-toggle").click();
  const entry = name === "welcome" ? { page: onboarding } : name === "login" ? { page: login } : ROUTES[name] || ROUTES.chat;
  // Every navigation renders into its own container. When the user moves on, it is detached, so late async
  // work of the old page (a finished job, a slow fetch) can only touch invisible, detached nodes.
  const page = document.createElement("div");
  $("#view").replaceChildren(page);
  highlightNav();
  try {
    const cleanup = (await entry.page.render(page, app)) || null;
    if (nav !== navSeq) { if (cleanup) { try { cleanup(); } catch { /* ignore */ } } return; } // a newer navigation won
    app.cleanup = cleanup;
  } catch (e) {
    if (nav !== navSeq) return;
    console.error(e);
    page.innerHTML = `<div class="card"><h2 class="section-title"><i class="la la-bug"></i>Something went wrong</h2><p class="mt-2 text-sm muted">${esc(e.message || e)}</p></div>`;
  }
  window.scrollTo({ top: 0 });
}

onApiError((err) => {
  if (err.code === "APP_IN_USE" && app.me) { showLockOverlay(err.detail); return; }
  if (err.code === "AI_OFFLINE") { app.showOutage(true); return; }
  if (err.status === 401 && app.me) { app.me = null; renderHeader(); renderFooter(); location.hash = `#/${signedOutHome()}`; }
});

/** Single active user: someone else is using the app - this device shows nothing until they finish.
 *  When the app is free again we ask before continuing, so a tab left open never grabs the app by itself. */
let lockTimer = null;
function showLockOverlay(lock) {
  let el = document.getElementById("lock-overlay");
  if (!el) {
    el = h(`<div id="lock-overlay" class="fixed inset-0 z-[70] grid place-items-center bg-[color:var(--bg)] p-6 fade-in" role="alertdialog" aria-live="polite" aria-labelledby="lock-title">
        <div class="card glow-border w-full max-w-md text-center">
          <div class="mx-auto grid h-16 w-16 place-items-center rounded-2xl bg-[color:var(--surface-2)] text-3xl text-[color:var(--accent)]"><i class="la la-user-lock" data-lock-icon></i></div>
          <h2 id="lock-title" class="mt-4 text-xl font-extrabold" data-lock-title></h2>
          <p class="mt-2 text-sm text-2" data-lock-text></p>
          <p class="mt-4 text-xs muted" data-lock-status><i class="la la-sync la-spin"></i> Checking every 5 seconds</p>
          <button class="btn btn-primary mt-4 hidden" data-lock-continue><i class="la la-play"></i>Continue</button></div></div>`);
    document.body.appendChild(el);
    el.querySelector("[data-lock-continue]").onclick = () => location.reload();
    blockBackground(true);  // header / page / footer become inert: no clicks or Tab focus behind the overlay
    el.querySelector(".card").tabIndex = -1;
    setTimeout(() => el.querySelector(".card").focus(), 30);
  }
  app.voice.cancel();
  // tear the page down underneath: its polling would only collect "app in use" refusals
  navSeq++;  // any page still loading is abandoned too
  if (app.cleanup) { try { app.cleanup(); } catch { /* ignore */ } app.cleanup = null; }
  const view = $("#view");
  if (view) view.replaceChildren();
  el.querySelector("[data-lock-icon]").className = "la la-user-lock";
  el.querySelector("[data-lock-title]").textContent = `${(lock && (lock.name || lock.holder)) || "Someone"} is using the app right now`;
  el.querySelector("[data-lock-text]").textContent = "Only one person can use the app at a time, on any device. You can continue when they sign out, close the app or step away.";
  el.querySelector("[data-lock-status]").classList.remove("hidden");
  el.querySelector("[data-lock-continue]").classList.add("hidden");
  if (lockTimer) return;
  lockTimer = setInterval(async () => {
    try {
      const state = await api("/api/auth/lock", { quiet: true });
      if (state.locked) return;
      clearInterval(lockTimer);
      lockTimer = null;
      el.querySelector("[data-lock-icon]").className = "la la-door-open";
      el.querySelector("[data-lock-title]").textContent = "The app is free again";
      el.querySelector("[data-lock-text]").textContent = "The other person has finished. Press Continue to pick up where you left off.";
      el.querySelector("[data-lock-status]").classList.add("hidden");
      el.querySelector("[data-lock-continue]").classList.remove("hidden");
      el.querySelector("[data-lock-continue]").focus();
    } catch { /* keep waiting */ }
  }, 5000);
}

/** Profiles created before username/password login existed choose credentials once. */
function promptCredentialSetup() {
  if (!app.me || app.me.has_password || sessionStorage.getItem("cte-setup-asked")) return;
  try { sessionStorage.setItem("cte-setup-asked", "1"); } catch { /* ignore */ }
  const m = modal({
    title: "Secure your profile",
    body: `<p class="mb-3">Choose a username and password so you (and only you) can sign in on any device on this network.</p>
      <div class="space-y-3"><label class="block"><span class="label">Username</span><input id="su-user" class="input font-mono" autocomplete="username" autocapitalize="none"></label>
      <label class="block"><span class="label">Password (8+ characters)</span><input id="su-pass" type="password" class="input" autocomplete="new-password"></label></div>`,
    actions: [{ label: "Later" }, {
      label: "Save", cls: "btn-primary", onClick: async () => {
        try {
          const res = await api("/api/auth/setup", { method: "POST", quiet: true, body: { username: $("#su-user", m.el).value.trim().toLowerCase(), password: $("#su-pass", m.el).value } });
          app.me = res.user;
          rememberAccount(res.user);
          toast("Profile secured.", "success");
          renderHeader();
        } catch (err) { toast(err.message, "error"); return true; }
        return false;
      },
    }],
  });
}

async function boot() {
  const [config, manifest] = await Promise.all([
    api("/api/config", { quiet: true }).catch(() => ({})),
    fetch("/static/vendor/manifest.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
  ]);
  app.config = config || {};
  setIconManifest(manifest);
  await app.refreshMe();
  if (!app.me) {
    try {
      const p = await api("/api/auth/profiles", { quiet: true });
      app.profileCount = (p.profiles || []).length;
      app.hasAccounts = !!p.has_accounts;  // true even when the sign-in screen does not list profiles
    } catch { app.profileCount = 0; app.hasAccounts = true; }  // unknown: offer sign-in, not sign-up
  }
  renderFooter();
  window.addEventListener("hashchange", route);
  let soundHintShown = false;
  window.addEventListener("cte:needs-gesture", () => {
    if (soundHintShown) return;
    soundHintShown = true;
    toast("Click anywhere to hear the AI's voice (your browser blocks sound until you interact with the page).", "info", 6000);
  });
  await route();
  if (app.me) { rememberAccount(app.me); promptCredentialSetup(); }
  try {
    if (sessionStorage.getItem("cte-account-deleted")) {
      sessionStorage.removeItem("cte-account-deleted");
      toast("Your account and everything in it was deleted from this computer.", "success", 7000);
    }
  } catch { /* storage blocked */ }
  pollHealth();
  setInterval(pollHealth, Math.max(2, Number(app.config.health_interval_seconds) || 5) * 1000);
}

boot();
