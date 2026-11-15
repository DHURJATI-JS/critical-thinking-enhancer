// Tiny API client: CSRF token, JSON handling, normalized errors, job polling.
export class ApiError extends Error {
  constructor(status, detail) {
    const message = typeof detail === "string" ? detail
      : (detail && (detail.message || detail.detail)) || (Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : "Request failed");
    super(message);
    this.status = status;
    this.detail = detail;
    this.code = detail && typeof detail === "object" && !Array.isArray(detail) ? detail.code : undefined;
  }
}

const listeners = new Set();
export function onApiError(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// CSRF: the page carries this browser's token in <meta name="csrf-token">; every state-changing call sends it back.
let csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function refreshCsrf() {
  try {
    const res = await fetch("/api/csrf", { credentials: "same-origin", cache: "no-store" });
    if (res.ok) csrfToken = (await res.json()).token || "";
  } catch { /* offline: the caller reports the original error */ }
}

/** fetch() for this app: adds the CSRF token to POST/PUT/PATCH/DELETE and, if the server issued a new
 *  CSRF cookie meanwhile (cookies cleared, another tab), fetches the new token and retries once. */
export async function secureFetch(path, init = {}) {
  const method = (init.method || "GET").toUpperCase();
  const send = () => fetch(path, {
    credentials: "same-origin", ...init,
    headers: { ...(init.headers || {}), ...(UNSAFE.has(method) ? { "X-CSRF-Token": csrfToken } : {}) },
  });
  let res = await send();
  if (res.status === 403 && UNSAFE.has(method)) {
    const detail = await res.clone().json().then((d) => d && d.detail).catch(() => null);
    if (detail && detail.code === "CSRF_FAILED") {
      await refreshCsrf();
      res = await send();
    }
  }
  return res;
}

export async function api(path, { method = "GET", body, form, quiet = false, signal, background = false } = {}) {
  const headers = {};
  if (background) headers["X-CTE-Background"] = "1"; // automatic polling: does not count as activity
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await secureFetch(path, { method, headers, body: payload, signal });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    const err = new ApiError(0, "Cannot reach the app server - is it still running?");
    if (!quiet) listeners.forEach((fn) => fn(err));
    throw err;
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { detail: text.slice(0, 300) }; }
  }
  if (!res.ok) {
    const err = new ApiError(res.status, data && data.detail !== undefined ? data.detail : res.statusText);
    if (!quiet) listeners.forEach((fn) => fn(err));
    throw err;
  }
  return data;
}

export async function pollJob(id, { onUpdate, interval = 700, timeout = 420000, signal } = {}) {
  const started = Date.now();
  for (;;) {
    if (signal && signal.aborted) throw new DOMException("aborted", "AbortError");
    const job = await api(`/api/jobs/${id}`, { quiet: true, background: true });
    if (onUpdate) onUpdate(job);
    if (job.status === "succeeded" || job.status === "failed") return job;
    if (Date.now() - started > timeout) throw new ApiError(408, "Timed out waiting for the AI.");
    await sleep(interval);
  }
}
