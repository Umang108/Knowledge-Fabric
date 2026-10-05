// Thin fetch wrapper: sends the session cookie and the CSRF header, turns error responses into Error(message).
export class ApiError extends Error {
  constructor(status, message, details) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

function messageFrom(body, status) {
  const d = body && body.detail;
  if (!d) return `Request failed (${status})`;
  if (typeof d === "string") return d;
  if (d.message) return d.errors ? `${d.message}: ${d.errors.join("; ")}` : d.message;
  if (Array.isArray(d)) return d.map((e) => e.msg || JSON.stringify(e)).join("; ");
  return JSON.stringify(d);
}

// anonymous: the call is allowed without a session (sign-in / sign-out), so a 401 is an error, not a redirect.
export async function api(path, { method = "GET", json, form, anonymous = false } = {}) {
  const headers = { "X-Requested-With": "graphbase" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  const r = await fetch(`/api${path}`, { method, headers, body, credentials: "same-origin" });
  const data = await r.json().catch(() => null);
  if (r.status === 401 && !anonymous) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    throw new ApiError(401, "Your session has expired");
  }
  if (!r.ok) throw new ApiError(r.status, messageFrom(data, r.status), data && data.detail);
  return data;
}

export function formatDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  return withTime ? `${date}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : date;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export const STATUS_TEXT = {
  draft: "Draft", extracting: "Extracting", awaiting_review: "Awaiting review", building: "Building graph",
  ingesting: "Indexing", ready: "Ready", failed: "Failed",
};
