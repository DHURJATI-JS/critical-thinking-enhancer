// Text-to-speech with lip sync.
//  * "neural" (default): local Piper voices rendered by the server (/api/tts) and played through a Web Audio
//    analyser - every animation frame the avatar's mouth opening follows the REAL loudness of the voice, and the
//    mouth shape follows the visemes of the words being spoken.
//  * "browser": the browser's Web Speech voices (fallback), lip sync from word-boundary events.
import { secureFetch } from "./api.js";
import { textVisemes, wordVisemes } from "./avatar.js";

function speakable(markdown) {
  return String(markdown || "")
    .replace(/```[\s\S]*?(```|$)/g, " (the code is on your screen) ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[*_#>|~]+/g, " ")
    .replace(/\p{Extended_Pictographic}/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1200);
}

function chunks(text, max = 220) {
  const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const out = [];
  let cur = "";
  for (const p of parts) {
    if ((cur + p).length > max && cur) { out.push(cur.trim()); cur = ""; }
    cur += p;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

export class Voice {
  constructor() {
    this.browserSupported = typeof window !== "undefined" && "speechSynthesis" in window;
    this.voices = [];
    this.neural = { available: null, voices: [], default: "en_US-amy-medium" };
    this.settings = { enabled: true, engine: "neural", neural: "", name: "", rate: 1, pitch: 1, volume: 1 };
    this.localMute = null;
    this.muteKey = "cte-muted";  // per profile once someone signs in (see setProfile)
    this.loadMute();
    this.avatar = null;  // the avatar currently speaking (its mouth is closed on cancel)
    this.pendingSpeech = null;
    this.listeners = new Set();
    this.session = 0;
    this.ctx = null;
    this.source = null;
    this.utterance = null;
    if (this.browserSupported) {
      this.loadBrowserVoices();
      window.speechSynthesis.addEventListener("voiceschanged", () => this.loadBrowserVoices());
    }
    // Browsers only allow audio after a user gesture: unlock the AudioContext on the first one.
    const unlock = () => { this.audioContext(); };
    ["pointerdown", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, unlock, { passive: true }));
    this.loadNeuralVoices();
  }

  // ------------------------------------------------------------------ voices
  loadBrowserVoices() {
    this.voices = window.speechSynthesis.getVoices() || [];
    this.listeners.forEach((fn) => fn(this.voices));
  }

  async loadNeuralVoices() {
    try {
      const res = await fetch("/api/tts/voices", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        this.neural = { available: !!data.available, voices: data.voices || [], default: data.default };
      } else this.neural.available = false;
    } catch { this.neural.available = false; }
    this.listeners.forEach((fn) => fn(this.voices));
    return this.neural;
  }

  onVoices(fn) { this.listeners.add(fn); fn(this.voices); return () => this.listeners.delete(fn); }

  loadMute() {
    this.localMute = null;
    try {
      const stored = localStorage.getItem(this.muteKey);
      if (stored !== null) this.localMute = stored === "1";
    } catch { /* storage blocked */ }
    if (this.localMute !== null) this.settings.enabled = !this.localMute;
  }

  /** Mute is remembered per profile on this device: one person muting does not silence the next one. */
  setProfile(username) {
    const key = username ? `cte-muted:${username}` : "cte-muted";
    if (key === this.muteKey) return;
    this.muteKey = key;
    this.settings.enabled = true;
    this.loadMute();
    window.dispatchEvent(new CustomEvent("cte:mute", { detail: { muted: this.muted } }));
  }

  configure(settings) {
    this.settings = { ...this.settings, ...(settings || {}) };
    if (this.localMute !== null) this.settings.enabled = !this.localMute; // this profile's mute button on this device wins
  }

  get muted() { return !this.settings.enabled; }

  /** Mute = no sound AND no lip sync. Remembered for this profile on this device. */
  setMuted(muted) {
    this.localMute = !!muted;
    this.settings.enabled = !muted;
    try { localStorage.setItem(this.muteKey, muted ? "1" : "0"); } catch { /* ignore */ }
    if (muted) this.cancel();
    window.dispatchEvent(new CustomEvent("cte:mute", { detail: { muted: !!muted } }));
  }

  pickBrowserVoice() {
    const v = this.voices;
    if (!v.length) return null;
    const wanted = this.settings.name;
    return (wanted && v.find((x) => x.voiceURI === wanted || x.name === wanted))
      || v.find((x) => x.localService && /^en/i.test(x.lang))
      || v.find((x) => /^en/i.test(x.lang)) || v[0];
  }

  audioContext() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!this.ctx) this.ctx = new AC();
    if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
    return this.ctx;
  }

  // ------------------------------------------------------------------ speak
  speak(text, avatar, { force = false } = {}) {
    this.cancel();
    const clean = speakable(text);
    if (!clean) return;
    if (!this.settings.enabled && !force) {  // muted: no sound and no lip movement
      if (avatar) avatar.stopTalking();
      return;
    }
    this.avatar = avatar || null;
    const ctx = this.audioContext();
    if (ctx && ctx.state === "suspended" && this.settings.engine !== "browser") {
      // The browser blocks audio until the first click/key press on the page: speak right after it.
      this.pendingSpeech = { text, avatar };
      window.dispatchEvent(new CustomEvent("cte:needs-gesture"));
      const resume = () => {
        window.removeEventListener("pointerdown", resume, true);
        window.removeEventListener("keydown", resume, true);
        const pending = this.pendingSpeech;
        this.pendingSpeech = null;
        if (pending) ctx.resume().then(() => this.speak(pending.text, pending.avatar, { force })).catch(() => {});
      };
      window.addEventListener("pointerdown", resume, true);
      window.addEventListener("keydown", resume, true);
      return;
    }
    if (this.settings.engine !== "browser" && this.neural.available !== false) {
      this.speakNeural(clean, avatar, this.session);
      return;
    }
    this.speakBrowser(clean, avatar, this.session);
  }

  async fetchAudio(text) {
    const res = await secureFetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: this.settings.neural || this.neural.default, rate: Number(this.settings.rate) || 1 }),
    });
    if (!res.ok) throw new Error(`tts ${res.status}`);
    const bytes = await res.arrayBuffer();
    return await this.ctx.decodeAudioData(bytes);
  }

  async speakNeural(text, avatar, session) {
    const ctx = this.audioContext();
    if (!ctx) { this.speakBrowser(text, avatar, session); return; }
    const parts = chunks(text);
    if (avatar) avatar.startTalking();
    let pending = this.fetchAudio(parts[0]);
    for (let i = 0; i < parts.length; i++) {
      let buffer;
      try {
        buffer = await pending;
      } catch {
        // server voice unavailable (still downloading / not installed): fall back to the browser voice
        if (session === this.session) {
          if (avatar) avatar.stopTalking();
          this.speakBrowser(parts.slice(i).join(" "), avatar, session);
        }
        return;
      }
      if (session !== this.session) return;
      if (i + 1 < parts.length) pending = this.fetchAudio(parts[i + 1]);
      if (ctx.state === "suspended") await ctx.resume().catch(() => {});
      await this.playBuffer(buffer, parts[i], avatar, session);
      if (session !== this.session) return;
    }
    if (avatar) avatar.stopTalking();
  }

  playBuffer(buffer, text, avatar, session) {
    return new Promise((resolve) => {
      const ctx = this.ctx;
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.35;
      const gain = ctx.createGain();
      gain.gain.value = Number(this.settings.volume ?? 1);
      source.connect(analyser);
      analyser.connect(gain);
      gain.connect(ctx.destination);
      const samples = new Float32Array(analyser.fftSize);
      const timeline = textVisemes(text);
      const started = ctx.currentTime;
      let frame = 0;
      const tick = () => {
        if (session !== this.session) return;
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
        const rms = Math.sqrt(sum / samples.length);
        const progress = (ctx.currentTime - started) / Math.max(0.05, buffer.duration);
        const hint = timeline[Math.min(timeline.length - 1, Math.max(0, Math.floor(progress * timeline.length)))];
        if (avatar) avatar.lipSync(rms, hint);
        frame = requestAnimationFrame(tick);
      };
      source.onended = () => { cancelAnimationFrame(frame); if (this.source === source) this.source = null; resolve(); };
      this.source = source;
      source.start();
      frame = requestAnimationFrame(tick);
    });
  }

  speakBrowser(text, avatar, session) {
    if (!this.browserSupported) { if (avatar) avatar.silentTalk(text); return; }
    const parts = chunks(text, 200);
    let index = 0;
    if (avatar) avatar.startTalking();
    const next = () => {
      if (session !== this.session) return;
      if (index >= parts.length) { if (avatar) avatar.stopTalking(); return; }
      const part = parts[index++];
      const u = new SpeechSynthesisUtterance(part);
      const voice = this.pickBrowserVoice();
      if (voice) { u.voice = voice; u.lang = voice.lang; }
      u.rate = Number(this.settings.rate) || 1;
      u.pitch = Number(this.settings.pitch ?? 1);
      u.volume = Number(this.settings.volume ?? 1);
      let boundary = false;
      let ended = false;
      let babbleTimer = 0;
      u.onboundary = (e) => {
        if (e.name && e.name !== "word") return;
        boundary = true;
        const rest = part.slice(e.charIndex);
        const word = e.charLength ? rest.slice(0, e.charLength) : (rest.match(/^\S+/) || [""])[0];
        if (avatar) avatar.playVisemes(wordVisemes(word), Math.max(45, 72 / u.rate));
      };
      // voices without word-boundary events: "babble" the mouth - but never after this sentence has ended
      u.onstart = () => {
        babbleTimer = setTimeout(() => { if (!boundary && !ended && avatar && session === this.session) avatar.babble(true); }, 380);
      };
      const finish = () => { ended = true; clearTimeout(babbleTimer); if (avatar) avatar.babble(false); next(); };
      u.onend = finish;
      u.onerror = finish;
      this.utterance = u; // keep a reference: Chrome garbage-collects utterances and then never fires onend
      window.speechSynthesis.resume();
      window.speechSynthesis.speak(u);
    };
    setTimeout(next, 60); // speaking immediately after cancel() is silently dropped by Chromium
  }

  cancel() {
    this.session++;
    this.pendingSpeech = null;
    if (this.source) { try { this.source.stop(); } catch { /* already stopped */ } this.source = null; }
    if (this.browserSupported) window.speechSynthesis.cancel();
    if (this.avatar) {  // stopping the voice (mute, navigation) also closes the avatar's mouth
      try { this.avatar.babble(false); this.avatar.stopTalking(); } catch { /* avatar already destroyed */ }
      this.avatar = null;
    }
  }
}
