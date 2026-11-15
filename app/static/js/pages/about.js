// About: disclaimers (no liability, no warranty, MIT), student-grade / not medical / not an IQ test, privacy, credits.
import { esc, icon } from "../ui.js";

const MIT = `MIT License

Copyright (c) 2026 {author}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`;

export async function render(root, app) {
  const c = app.config || {};
  const card = (i8, la, title, html) => `<section class="card"><div class="section-title">${icon(i8, la)}${title}</div><div class="mt-3 space-y-2 text-sm text-2">${html}</div></section>`;
  root.innerHTML = `<div class="flex flex-col gap-6">
    <div class="card glow-border flex flex-col gap-3 sm:flex-row sm:items-center">
      <img src="/static/img/logo.svg" alt="" class="h-16 w-16">
      <div class="flex-1"><h1 class="text-2xl font-extrabold gradient-text">${esc(c.name || "Critical Thinking Enhancer")}</h1>
        <p class="text-sm muted">${esc(c.tagline || "")} · version ${esc(c.version || "")}</p></div>
      ${c.vercel_url ? `<a class="btn btn-primary" href="${esc(c.vercel_url)}" target="_blank" rel="noopener noreferrer"><i class="la la-globe"></i>${esc(c.author || "Author")}'s website</a>` : '<span class="text-xs muted">Website link: set app.vercel_url in config.json</span>'}</div>

    <div class="grid gap-6 lg:grid-cols-2">
      ${card("warning-shield", "exclamation-triangle", "Not medical. Not an IQ test.", `
        <p>This is a <b>student-grade project</b> built for learning and self-reflection.</p>
        <p>It is <b>not</b> a medical, psychological, psychometric or clinical tool, and it does <b>not</b> measure IQ. The "estimated IQ" is a playful, wide-range
        estimate derived from training behaviour with an AI - it can be wrong, it is not standardised and it must not be used for any decision about anyone.</p>
        <p>AI output can be inaccurate. Answers below the confidence bar are retried and, if still uncertain, shown with a red warning.</p>`)}
      ${card("shield", "user-shield", "Privacy - everything stays local", `
        <p><b>Everything happens and is stored locally on this machine.</b> Your profile, biography, SWOT, chats, projects, memories (pgvector) and cognitive profile
        live in the local PostgreSQL database; no analytics, no tracking, no external CDNs - fonts, icons and CSS are served from this app.</p>
        <p>The AI runs on the servers configured by the person who installed this app (local by default). If <i>you</i> choose to add a Claude or ChatGPT key, the text sent to
        the AI while that fallback is used is processed by that provider under its terms. Your keys are encrypted at rest.</p>
        <p>You can delete all of your data at any time in Settings.</p>`)}
      ${card("lock", "balance-scale", "No warranty · No liability", `
        <p>The software is provided <b>"as is", without warranty of any kind</b>, express or implied. In no event shall the authors or copyright holders be
        <b>liable</b> for any claim, damages or other liability arising from, out of or in connection with the software or its use.</p>
        <p>Use it at your own risk and always double-check important conclusions with a qualified human.</p>`)}
      ${card("idea", "heart", "Credits", `
        <p>Built with FastAPI, SQLAlchemy (async), PostgreSQL + pgvector, Redis, vLLM (Qwen2.5-Coder, MiniCPM-V), Tesseract, RapidOCR, Windows OCR / Apple Vision,
        fastembed and Tailwind CSS.</p>
        <p>Colour icons by <a class="underline" href="https://icons8.com" target="_blank" rel="noopener noreferrer">Icons8</a>; icon font
        <a class="underline" href="https://icons8.com/line-awesome" target="_blank" rel="noopener noreferrer">Line Awesome</a> by Icons8 (MIT / SIL OFL).
        Fonts: Inter and JetBrains Mono (SIL OFL).</p>`)}
    </div>
    <section class="card"><div class="section-title">${icon("source-code", "file-alt")}MIT License</div>
      <pre class="scroll-thin mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-xl bg-[color:var(--surface-2)] p-4 font-mono text-xs text-2">${esc(MIT.replace("{author}", c.author || "the authors"))}</pre></section></div>`;
}
