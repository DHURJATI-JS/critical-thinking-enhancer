// Animated SVG avatar: 4 faces, hue theming, eye blinking, cursor-following eyes,
// facial expressions and viseme-based lip sync (driven by speech boundary events).
import { esc } from "./ui.js";

const hsl = (h, s, l, a = 1) => `hsla(${(((h % 360) + 360) % 360).toFixed(0)}, ${s}%, ${l}%, ${a})`;
let uid = 0;

export const MOUTHS = {
  rest: "M82 139 Q100 146 118 139 Q100 149 82 139 Z",
  closed: "M83 141 Q100 143 117 141 Q100 145 83 141 Z",
  a: "M80 135 Q100 130 120 135 Q100 164 80 135 Z",
  e: "M78 137 Q100 134 122 137 Q100 153 78 137 Z",
  i: "M82 138 Q100 135 118 138 Q100 149 82 138 Z",
  o: "M89 134 Q100 126 111 134 Q115 146 100 153 Q85 146 89 134 Z",
  u: "M92 137 Q100 132 108 137 Q110 145 100 149 Q90 145 92 137 Z",
  fv: "M83 139 Q100 136 117 139 Q100 146 83 139 Z",
  smile: "M78 134 Q100 158 122 134 Q100 146 78 134 Z",
  grin: "M76 132 Q100 166 124 132 Q100 141 76 132 Z",
  frown: "M84 147 Q100 136 116 147 Q100 142 84 147 Z",
  wow: "M88 131 Q100 121 112 131 Q117 148 100 157 Q83 148 88 131 Z",
  smirk: "M86 142 Q104 141 118 135 Q104 146 86 142 Z",
};

// brows: [leftDy, leftRotDeg, rightDy, rightRotDeg]
export const EXPRESSIONS = {
  neutral: { mouth: "rest", brows: [0, 0, 0, 0], eye: 1, blush: 0, gaze: null },
  happy: { mouth: "smile", brows: [-3, -3, -3, 3], eye: 0.88, blush: 0.55, gaze: null },
  thinking: { mouth: "smirk", brows: [-8, -6, 0, 4], eye: 1, blush: 0, gaze: [4, -4] },
  surprised: { mouth: "wow", brows: [-9, 0, -9, 0], eye: 1.14, blush: 0.15, gaze: null },
  concerned: { mouth: "frown", brows: [-2, -12, -2, 12], eye: 0.95, blush: 0, gaze: [0, 2] },
  proud: { mouth: "grin", brows: [-5, -5, -5, 5], eye: 0.84, blush: 0.7, gaze: null, sparkle: true },
};

function eyes({ sclera, pupil, highlight = "#fff", prx = 7, pry = 7, ring = "" }) {
  const one = (cx, side) => `
    <g class="eye-group" data-eye="${side}">
      <ellipse cx="${cx}" cy="100" rx="15" ry="17" fill="${sclera}" ${ring ? `stroke="${ring}" stroke-width="2"` : ""}/>
      <g class="pupil"><ellipse cx="${cx}" cy="101" rx="${prx}" ry="${pry}" fill="${pupil}"/>
      <circle cx="${cx + 3}" cy="96.5" r="2.6" fill="${highlight}" opacity=".95"/></g>
    </g>`;
  return one(72, "l") + one(128, "r");
}
const brows = (color, w = 5) => `
  <path class="brow" data-brow="l" d="M58 78 Q72 70 86 77" fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round"/>
  <path class="brow" data-brow="r" d="M114 77 Q128 70 142 78" fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round"/>`;
const blush = `<ellipse class="blush" cx="57" cy="124" rx="10" ry="5.5" fill="#fb7185" opacity="0"/>
  <ellipse class="blush" cx="143" cy="124" rx="10" ry="5.5" fill="#fb7185" opacity="0"/>`;
const mouth = (fill) => `<path class="mouth" d="${MOUTHS.rest}" fill="${fill}" stroke-linejoin="round"/>`;
const sparkles = (c) => `<g class="sparkles" opacity="0">
  <path class="sparkle" d="M34 44 l3 8 l8 3 l-8 3 l-3 8 l-3 -8 l-8 -3 l8 -3 z" fill="${c}"/>
  <path class="sparkle" style="animation-delay:.5s" d="M168 38 l2 6 l6 2 l-6 2 l-2 6 l-2 -6 l-6 -2 l6 -2 z" fill="#fde047"/>
  <path class="sparkle" style="animation-delay:1s" d="M164 150 l2 5 l5 2 l-5 2 l-2 5 l-2 -5 l-5 -2 l5 -2 z" fill="${c}"/></g>`;

export const FACES = {
  bot: {
    label: "Robot",
    build(hue, id) {
      const glow = hsl(hue, 95, 62);
      const edge = hsl(hue, 30, 36);
      return `<defs>
        <linearGradient id="${id}-m" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${hsl(hue, 18, 90)}"/><stop offset="1" stop-color="${hsl(hue, 22, 64)}"/></linearGradient>
        <filter id="${id}-g" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
        <line x1="100" y1="40" x2="100" y2="21" stroke="${edge}" stroke-width="4" stroke-linecap="round"/>
        <circle class="led" cx="100" cy="16" r="7" fill="${glow}" filter="url(#${id}-g)"/>
        <rect x="27" y="86" width="15" height="34" rx="6" fill="${edge}"/><rect x="158" y="86" width="15" height="34" rx="6" fill="${edge}"/>
        <rect x="38" y="38" width="124" height="128" rx="38" fill="url(#${id}-m)" stroke="${edge}" stroke-width="3"/>
        <circle cx="54" cy="52" r="3" fill="${edge}"/><circle cx="146" cy="52" r="3" fill="${edge}"/>
        <rect x="51" y="60" width="98" height="96" rx="28" fill="#0a1020" stroke="${hsl(hue, 45, 30)}" stroke-width="2"/>
        <g filter="url(#${id}-g)">${eyes({ sclera: glow, pupil: "#0a1020", highlight: "#e0f2fe", prx: 5, pry: 6 })}</g>
        ${brows(glow, 4)}${blush}
        <g filter="url(#${id}-g)">${mouth(glow)}</g>`;
    },
  },
  ada: {
    label: "Coder",
    build(hue, id) {
      const hair = hsl(hue + 200, 35, 23);
      const accent = hsl(hue, 85, 58);
      return `<defs><radialGradient id="${id}-s" cx=".5" cy=".42" r=".66"><stop offset="0" stop-color="#f4cba6"/><stop offset="1" stop-color="#dea47c"/></radialGradient></defs>
        <path d="M44 98 Q38 34 100 30 Q162 34 156 98 L161 152 Q150 172 132 160 L68 160 Q50 172 39 152 Z" fill="${hair}"/>
        <path d="M54 170 Q100 192 146 170 L156 200 L44 200 Z" fill="${accent}"/>
        <rect x="88" y="148" width="24" height="26" rx="9" fill="#dea47c"/>
        <ellipse cx="47" cy="106" rx="8" ry="12" fill="#dea47c"/><ellipse cx="153" cy="106" rx="8" ry="12" fill="#dea47c"/>
        <circle cx="46" cy="123" r="3.6" fill="${accent}"/><circle cx="154" cy="123" r="3.6" fill="${accent}"/>
        <ellipse cx="100" cy="104" rx="54" ry="60" fill="url(#${id}-s)"/>
        <path d="M46 94 Q50 42 100 40 Q150 42 154 94 Q134 66 106 61 Q74 63 46 94 Z" fill="${hair}"/>
        <path d="M70 51 Q100 40 130 51" stroke="${hsl(hue + 200, 40, 38)}" stroke-width="3" fill="none" stroke-linecap="round" opacity=".7"/>
        ${eyes({ sclera: "#ffffff", pupil: hsl(hue + 200, 45, 22), ring: "rgba(0,0,0,.08)" })}
        ${brows(hair, 4.5)}${blush}
        <path d="M100 108 Q95 122 101 124" stroke="#c98a62" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        ${mouth("#8a2a3b")}`;
    },
  },
  neo: {
    label: "Hacker",
    build(hue, id) {
      const hoodie = hsl(hue, 55, 34);
      return `<defs><radialGradient id="${id}-s" cx=".5" cy=".42" r=".66"><stop offset="0" stop-color="#cf9369"/><stop offset="1" stop-color="#ad744d"/></radialGradient></defs>
        <path d="M28 200 Q28 150 64 140 L136 140 Q172 150 172 200 Z" fill="${hoodie}"/>
        <path d="M36 122 Q30 38 100 28 Q170 38 164 122 Q160 162 136 150 L64 150 Q40 162 36 122 Z" fill="${hoodie}"/>
        <path d="M48 120 Q46 52 100 45 Q154 52 152 120" fill="none" stroke="${hsl(hue, 60, 46)}" stroke-width="5"/>
        <path d="M84 150 L80 178 M116 150 L120 178" stroke="#e5e7eb" stroke-width="3" stroke-linecap="round"/>
        <ellipse cx="100" cy="106" rx="48" ry="54" fill="url(#${id}-s)"/>
        <path d="M54 93 Q56 55 100 53 Q144 55 146 93 Q128 73 100 71 Q72 73 54 93 Z" fill="#1c1714"/>
        ${eyes({ sclera: "#ffffff", pupil: "#2b1d14" })}
        <g fill="${hsl(hue, 90, 70, 0.16)}" stroke="#0f172a" stroke-width="3.5"><rect x="51" y="83" width="42" height="34" rx="10"/><rect x="107" y="83" width="42" height="34" rx="10"/></g>
        <path d="M93 98 Q100 94 107 98" stroke="#0f172a" stroke-width="3" fill="none"/>
        ${brows("#1c1714", 4.5)}${blush}
        <path d="M100 110 Q95 124 102 126" stroke="#9b6440" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        ${mouth("#6b2130")}`;
    },
  },
  kit: {
    label: "Cat",
    build(hue) {
      const fur = hsl(hue, 45, 62);
      const dark = hsl(hue, 40, 42);
      return `<path d="M46 88 L38 24 L90 58 Z" fill="${fur}" stroke="${dark}" stroke-width="3" stroke-linejoin="round"/>
        <path d="M52 74 L48 40 L76 58 Z" fill="#f9a8d4"/>
        <path d="M154 88 L162 24 L110 58 Z" fill="${fur}" stroke="${dark}" stroke-width="3" stroke-linejoin="round"/>
        <path d="M148 74 L152 40 L124 58 Z" fill="#f9a8d4"/>
        <ellipse cx="100" cy="108" rx="62" ry="56" fill="${fur}" stroke="${dark}" stroke-width="3"/>
        <path d="M86 56 l4 11 M100 52 v13 M114 56 l-4 11" stroke="${dark}" stroke-width="3" stroke-linecap="round" opacity=".55"/>
        <ellipse cx="100" cy="135" rx="30" ry="20" fill="#fff7ed"/>
        ${eyes({ sclera: "#fef9c3", pupil: "#14532d", prx: 3.6, pry: 10 })}
        ${brows(dark, 4)}${blush}
        <path d="M94 122 L106 122 L100 129 Z" fill="#f472b6"/>
        <g stroke="${dark}" stroke-width="1.6" stroke-linecap="round" opacity=".8">
          <path d="M70 130 L38 124"/><path d="M70 136 L38 139"/><path d="M130 130 L162 124"/><path d="M130 136 L162 139"/></g>
        ${mouth("#7f1d1d")}`;
    },
  },
};
export const FACE_OPTIONS = Object.entries(FACES).map(([key, f]) => ({ key, label: f.label }));

// ---------------------------------------------------------------- text -> visemes
export function wordVisemes(word) {
  const w = String(word || "").toLowerCase().replace(/[^a-z]/g, "");
  const seq = [];
  for (let i = 0; i < w.length; i++) {
    const c = w[i];
    let v;
    if ("mbp".includes(c)) v = "closed";
    else if ("fv".includes(c)) v = "fv";
    else if (c === "o") v = "o";
    else if ("uwq".includes(c)) v = "u";
    else if (c === "a") v = "a";
    else if (c === "e") v = "e";
    else if ("iy".includes(c)) v = "i";
    else if (i % 2 === 0) v = "rest";
    if (v && seq[seq.length - 1] !== v) seq.push(v);
  }
  return seq.slice(0, 7).length ? seq.slice(0, 7) : ["a"];
}

export function textVisemes(text) {
  const out = [];
  for (const word of String(text).split(/\s+/).slice(0, 160)) {
    out.push(...wordVisemes(word), "closed");
    if (/[.!?]$/.test(word)) out.push("rest", "rest");
  }
  return out;
}

const BABBLE = ["a", "e", "o", "i", "rest", "closed", "u", "a", "e"];

export class Avatar {
  constructor(container, { face = "bot", hue = 190, interactive = true, name = "Byte" } = {}) {
    this.container = container;
    this.face = FACES[face] ? face : "bot";
    this.hue = Number.isFinite(Number(hue)) ? Number(hue) : 190;
    this.name = name;
    this.expression = "neutral";
    this.speaking = false;
    this.blinkFactor = 1;
    this.gaze = [0, 0];
    this.queue = [];
    this.timers = {};
    this.listeners = new Set();
    this.render();
    if (interactive) {
      this.onMove = (e) => this.track(e);
      window.addEventListener("pointermove", this.onMove, { passive: true });
    }
    this.scheduleBlink();
  }

  render() {
    const id = `av${++uid}`;
    this.container.innerHTML = `<div class="avatar-wrap" style="--hue:${this.hue}">
        <div class="avatar-ring"></div>
        <svg class="avatar-svg" viewBox="0 0 200 200" role="img" aria-label="${esc(this.name)} - ${esc(FACES[this.face].label)} avatar">
          ${FACES[this.face].build(this.hue, id)}${sparkles(hsl(this.hue, 95, 65))}
        </svg></div>`;
    this.wrap = this.container.firstElementChild;
    this.svg = this.wrap.querySelector("svg");
    this.eyes = Array.from(this.svg.querySelectorAll(".eye-group"));
    this.pupils = Array.from(this.svg.querySelectorAll(".pupil"));
    this.browL = this.svg.querySelector('[data-brow="l"]');
    this.browR = this.svg.querySelector('[data-brow="r"]');
    this.blushes = Array.from(this.svg.querySelectorAll(".blush"));
    this.mouth = this.svg.querySelector(".mouth");
    this.sparkles = this.svg.querySelector(".sparkles");
    this.apply();
  }

  setFace(face, hue) {
    if (FACES[face]) this.face = face;
    if (hue != null && Number.isFinite(Number(hue))) this.hue = Number(hue);
    this.render();
  }

  onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit() { this.listeners.forEach((fn) => fn({ expression: this.expression, speaking: this.speaking })); }

  setExpression(name) {
    this.expression = EXPRESSIONS[name] ? name : "neutral";
    this.apply();
    this.emit();
  }

  apply() {
    const ex = EXPRESSIONS[this.expression];
    const [ldy, lrot, rdy, rrot] = ex.brows;
    if (this.browL) this.browL.style.transform = `translate(0px, ${ldy}px) rotate(${lrot}deg)`;
    if (this.browR) this.browR.style.transform = `translate(0px, ${rdy}px) rotate(${rrot}deg)`;
    this.blushes.forEach((b) => b.setAttribute("opacity", String(ex.blush)));
    if (this.sparkles) this.sparkles.setAttribute("opacity", ex.sparkle ? "1" : "0");
    this.applyEyes();
    this.applyPupils();
    if (!this.speaking) this.setMouth(ex.mouth);
  }

  applyEyes() {
    const s = EXPRESSIONS[this.expression].eye;
    this.eyes.forEach((e) => (e.style.transform = `scale(${s}, ${s * this.blinkFactor})`));
  }

  applyPupils() {
    const fixed = EXPRESSIONS[this.expression].gaze;
    const [dx, dy] = fixed || this.gaze;
    this.pupils.forEach((p) => p.setAttribute("transform", `translate(${dx.toFixed(2)} ${dy.toFixed(2)})`));
  }

  setMouth(key) { if (this.mouth) this.mouth.setAttribute("d", MOUTHS[key] || MOUTHS.rest); }

  track(e) {
    if (!this.svg || EXPRESSIONS[this.expression].gaze) return;
    const r = this.svg.getBoundingClientRect();
    if (!r.width) return;
    const dx = e.clientX - (r.left + r.width / 2);
    const dy = e.clientY - (r.top + r.height / 2);
    const dist = Math.hypot(dx, dy) || 1;
    const k = Math.min(1, dist / 380);
    this.gaze = [(dx / dist) * 4.5 * k, (dy / dist) * 3.6 * k];
    this.applyPupils();
  }

  scheduleBlink() {
    clearTimeout(this.timers.blink);
    this.timers.blink = setTimeout(() => { this.blink(); this.scheduleBlink(); }, 1800 + Math.random() * 4200);
  }

  blink(again = true) {
    this.blinkFactor = 0.08;
    this.applyEyes();
    setTimeout(() => {
      this.blinkFactor = 1;
      this.applyEyes();
      if (again && Math.random() < 0.18) setTimeout(() => this.blink(false), 170);
    }, 120);
  }

  // ------------------------------------------------------------ lip sync
  startTalking() {
    this.speaking = true;
    this.wrap && this.wrap.classList.add("avatar-speaking");
    this.emit();
  }

  stopTalking() {
    this.speaking = false;
    this.queue = [];
    clearTimeout(this.timers.viseme);
    this.timers.viseme = null;
    this.babble(false);
    this.wrap && this.wrap.classList.remove("avatar-speaking");
    this.apply();
    this.emit();
  }

  /**
   * Audio-driven lip sync: `level` is the RMS loudness of the voice right now, `hint` the viseme of the word
   * being spoken. Loudness decides how far the mouth opens; the hint decides its shape.
   */
  lipSync(level, hint) {
    this.peak = Math.max((this.peak || 0.08) * 0.996, level, 0.04);
    const n = level / this.peak;
    let shape;
    if (n < 0.14) shape = "closed";
    else if (n < 0.34) shape = ["i", "fv", "u", "closed"].includes(hint) ? (hint === "closed" ? "rest" : hint) : "i";
    else if (n < 0.62) shape = ["e", "o", "u", "i"].includes(hint) ? hint : "e";
    else shape = hint === "o" || hint === "u" ? "o" : "a";
    if (shape !== this.lastShape) {
      this.lastShape = shape;
      this.setMouth(shape);
    }
  }

  playVisemes(seq, perMs = 80) {
    this.babble(false);
    this.queue.push(...seq, "rest");
    if (!this.timers.viseme) this.tick(perMs);
  }

  tick(perMs) {
    const next = this.queue.shift();
    if (!next) { this.timers.viseme = null; this.setMouth(this.speaking ? "rest" : EXPRESSIONS[this.expression].mouth); return; }
    this.setMouth(next);
    this.timers.viseme = setTimeout(() => this.tick(perMs), perMs);
  }

  babble(on) {
    clearInterval(this.timers.babble);
    this.timers.babble = null;
    if (on) this.timers.babble = setInterval(() => this.setMouth(BABBLE[Math.floor(Math.random() * BABBLE.length)]), 95);
  }

  silentTalk(text) {
    const seq = textVisemes(text);
    const total = Math.min(7000, Math.max(900, String(text).length * 42));
    const per = Math.max(55, Math.min(120, total / Math.max(1, seq.length)));
    this.startTalking();
    this.queue = [];
    this.playVisemes(seq.slice(0, Math.ceil(total / per)), per);
    clearTimeout(this.timers.silent);
    this.timers.silent = setTimeout(() => this.stopTalking(), total + 200);
  }

  destroy() {
    Object.values(this.timers).forEach((t) => { clearTimeout(t); clearInterval(t); });
    if (this.onMove) window.removeEventListener("pointermove", this.onMove);
    this.listeners.clear();
  }
}
