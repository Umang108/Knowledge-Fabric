// Sign-in helpers. Sessions live on the server: the browser only carries an HttpOnly cookie that
// JavaScript cannot read, so nothing here stores tokens.
import { api } from "./api.js";

let config = null;

export async function authConfig() {
  if (!config) config = await (await fetch("/api/auth/config")).json();
  return config;
}

export async function currentUser() {
  const r = await fetch("/api/auth/me", { credentials: "same-origin" });
  return r.ok ? r.json() : null;
}

// {user} when signed in; {expired: true} when the browser had a session that timed out or was signed out.
export async function sessionState() {
  const r = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (r.ok) return { user: await r.json() };
  const body = await r.json().catch(() => ({}));
  return { user: null, expired: body?.detail === "Your session has ended" };
}

export async function localLogin(userId, password) {
  const { user } = await api("/auth/login", { method: "POST", json: { user_id: userId, password }, anonymous: true });
  return user;
}

// Keycloak: the backend runs the whole sign-in and sends the browser back to `next`.
export function keycloakLogin(next = "/workspace") {
  window.location.assign(`/api/auth/login?next=${encodeURIComponent(next)}`);
}

export async function signOut() {
  try {
    await api("/auth/logout", { method: "POST", anonymous: true });
  } finally {
    window.location.assign("/login");
  }
}
