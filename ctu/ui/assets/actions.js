// POST helpers for /api/v2/actions/<name>, settings, lazy topics, and in-page
// toasts for failures. Every action resolves to the parsed JSON body or throws
// an ActionError with a message written for the user.

import { levelIcon, icon } from "./icons.js";
import { esc } from "./format.js";

export class ActionError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

const OFFLINE = "Can't reach the tracker. It may still be starting.";

const CODES = {
  unreachable: "Can't reach the team service right now. Check your connection and try again.",
  signed_out: "You're signed out of the team pool.",
  not_admin: "Only team admins can do that.",
  admin_only: "Only team admins can do that.",
  rate_limited: "Too many tries. Wait a minute and try again.",
};
function messageFrom(data, res) {
  if (data && typeof data === "object") {
    const m = data.message || data.detail || data.error;
    if (typeof m === "string" && m) return CODES[m] || m;
  }
  if (res && res.status === 404) return "This version of the tracker doesn't support that yet.";
  if (res && res.status >= 500) return "The tracker hit an error. Details are in its log.";
  return "Something went wrong.";
}

/** fetch + JSON with friendly errors. */
export async function api(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      cache: "no-store",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ActionError(OFFLINE, 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {}
  if (!res.ok || (data && data.ok === false)) throw new ActionError(messageFrom(data, res), res.status, data);
  return data ?? {};
}

export const getJSON = (url) => api("GET", url);
export const getSettings = () => api("GET", "/api/v2/settings");
export const patchSettings = (partial) => api("PATCH", "/api/v2/settings", partial);

/** POST /api/v2/actions/<name> with a JSON body. */
export function post(name, body = {}) {
  return api("POST", "/api/v2/actions/" + encodeURIComponent(name), body);
}

const FAIL_TITLE = {
  refresh: "Couldn't refresh",
  login: "Couldn't open Claude sign-in",
  update: "Couldn't start the update",
  check_update: "Couldn't check for updates",
  mute: "Couldn't change alerts",
  overlay: "Couldn't change the overlay",
  open_dashboard: "Couldn't open the dashboard",
  remote_pair: "Couldn't start pairing",
  remote_rotate: "Couldn't rotate the phone key",
  remote_unpair: "Couldn't unpair the phone",
  team_login_start: "Couldn't send the code",
  team_login_verify: "Couldn't verify the code",
  team_logout: "Couldn't sign out of the pool",
  team_claim_device: "Couldn't link this PC",
  team_connector_token: "Couldn't create a token",
  install_statusline: "Couldn't install the statusline",
  open_logs: "Couldn't open the log folder",
};

/**
 * Run an action with UI feedback: the button is disabled (aria-busy) while
 * pending; failures become an error toast unless `onError` handles them.
 * Returns the response body, or null on failure.
 */
export async function act(name, body = {}, { button = null, success = null, onError = null } = {}) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-busy");
  }
  try {
    const data = await post(name, body);
    if (success) toast({ title: typeof success === "function" ? success(data) : success, kind: "success" });
    return data;
  } catch (e) {
    if (onError) onError(e);
    else toast({ title: FAIL_TITLE[name] || "That didn't work", detail: e.message, kind: "error" });
    return null;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.classList.remove("is-busy");
    }
  }
}

/* ---------------- toasts ---------------- */

function host() {
  let el = document.getElementById("toasts");
  if (!el) {
    el = document.createElement("div");
    el.id = "toasts";
    el.className = "toasts";
    document.body.appendChild(el);
  }
  return el;
}

/** In-page notification (never a system toast). kind: info | success | error. */
export function toast({ title, detail = "", kind = "info", timeout = 6000 }) {
  const box = host();
  const t = document.createElement("div");
  t.className = "toast is-" + kind;
  t.setAttribute("role", kind === "error" ? "alert" : "status");
  const ic = kind === "error" ? levelIcon("error", 16) : kind === "success" ? levelIcon("success", 16) : levelIcon("svc-unknown", 16);
  t.innerHTML = `${ic}<div class="toast-body"><div class="toast-title">${esc(title)}</div>${
    detail ? `<div class="toast-detail">${esc(detail)}</div>` : ""
  }</div><button type="button" class="btn btn-subtle btn-icon btn-sm" aria-label="Dismiss">${icon("close", { size: 12 })}</button>`;
  const close = () => t.remove();
  t.querySelector("button").addEventListener("click", close);
  box.appendChild(t);
  while (box.children.length > 3) box.firstElementChild.remove();
  let timer = setTimeout(close, kind === "error" ? Math.max(timeout, 9000) : timeout);
  t.addEventListener("mouseenter", () => clearTimeout(timer));
  t.addEventListener("mouseleave", () => (timer = setTimeout(close, 4000)));
  t.addEventListener("focusin", () => clearTimeout(timer));
  return t;
}
