// Robust API client.
//
// Every call goes through request(): it attaches the bearer token, proactively
// refreshes a near-expired token, retries on cold-start/transient failures
// (network errors, timeouts, 502/503/504 — important for Render free-tier
// spin-up), refreshes + retries once on a 401, and surfaces the backend's
// {error:{code,message}} envelope as a typed ApiError.

import { API_BASE } from './config.js';
import {
  session, refreshAccessToken, needsRefreshSoon, clearSession,
} from './session.js';

export class ApiError extends Error {
  constructor(message, { status = 0, code = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const RETRYABLE_STATUS = new Set([502, 503, 504]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Callbacks wired by main.js (kept here to avoid importing UI/view modules).
let authLostHandler = () => {};
let retryNoticeHandler = () => {};
let retryDoneHandler = () => {};
export function setAuthLostHandler(fn) { authLostHandler = fn; }
// `retryNoticeHandler` fires once a request starts retrying (e.g. cold-start);
// `retryDoneHandler` fires exactly when that same request's retry sequence
// resolves (success or final failure) — never on a blind timeout.
export function setRetryNoticeHandler(fn) { retryNoticeHandler = fn; }
export function setRetryDoneHandler(fn) { retryDoneHandler = fn; }

function buildUrl(path) {
  if (/^https?:\/\//.test(path)) return path;
  return `${API_BASE}${path}`;
}

// One bare fetch with an optional timeout (timeout=0 disables it, e.g. downloads).
// `priority` is a browser fetch hint ('high'|'low'|'auto') — we mark background
// scans 'low' so interactive requests jump the connection-pool queue.
async function bareFetch(path, { method = 'GET', headers = {}, body, auth = true, timeout = 30_000, priority } = {}) {
  const h = { ...headers };
  if (auth && session.access) h.Authorization = `Bearer ${session.access}`;

  const ctrl = new AbortController();
  const timer = timeout ? setTimeout(() => ctrl.abort(new DOMException('Timed out', 'TimeoutError')), timeout) : null;
  const init = { method, headers: h, body, signal: ctrl.signal };
  if (priority) init.priority = priority;
  try {
    return await fetch(buildUrl(path), init);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function isTransient(err) {
  return err && (err.name === 'TypeError' || err.name === 'TimeoutError' || err.name === 'AbortError');
}

// Core request. Returns the raw Response (use requestJson for parsed JSON).
export async function request(path, opts = {}) {
  const { json, retries = 2, auth = true, headers = {}, ...rest } = opts;
  const finalHeaders = { ...headers };
  let body = rest.body;
  if (json !== undefined) {
    finalHeaders['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }
  const fetchOpts = { ...rest, headers: finalHeaders, body, auth };

  // Proactive refresh so we rarely even hit a 401.
  if (auth && needsRefreshSoon()) {
    try { await refreshAccessToken(); } catch { /* the 401 path will handle it */ }
  }

  let attempt = 0;
  let noticeFired = false;
  try {
    for (;;) {
      try {
        let res = await bareFetch(path, fetchOpts);

        if (res.status === 401 && auth) {
          if (session.refresh) {
            try {
              await refreshAccessToken();
              res = await bareFetch(path, fetchOpts);
            } catch {
              clearSession();
              authLostHandler();
              throw new ApiError('Your session expired. Please log in again.', { status: 401 });
            }
          }
          if (res.status === 401) {
            clearSession();
            authLostHandler();
            throw new ApiError('Your session expired. Please log in again.', { status: 401 });
          }
        }

        if (RETRYABLE_STATUS.has(res.status) && attempt < retries) {
          if (attempt === 0) { retryNoticeHandler(); noticeFired = true; }
          await sleep(800 * 2 ** attempt);
          attempt += 1;
          continue;
        }
        return res;
      } catch (err) {
        if (err instanceof ApiError) throw err;
        if (isTransient(err) && attempt < retries) {
          if (attempt === 0) { retryNoticeHandler(); noticeFired = true; }
          await sleep(800 * 2 ** attempt);
          attempt += 1;
          continue;
        }
        const msg = err.name === 'TimeoutError'
          ? 'The request timed out — the server may be waking up. Please try again.'
          : 'Network error. Check your connection and that the server is reachable.';
        throw new ApiError(msg, {});
      }
    }
  } finally {
    if (noticeFired) retryDoneHandler();
  }
}

// Parse JSON; raise ApiError on non-2xx using the error envelope.
export async function requestJson(path, opts = {}) {
  const res = await request(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty/non-JSON body */ }
  if (!res.ok) {
    throw new ApiError(data?.error?.message || `Request failed (${res.status}).`, {
      status: res.status,
      code: data?.error?.code || null,
    });
  }
  return data;
}

// ---- Endpoint helpers ------------------------------------------------------

export const Auth = {
  login: (email, password) => requestJson('/auth/login', { method: 'POST', json: { email, password }, auth: false }),
  signup: (email, password) => requestJson('/auth/signup', { method: 'POST', json: { email, password }, auth: false }),
  resend: (email) => request('/auth/resend-confirmation', { method: 'POST', json: { email }, auth: false }),
  logout: (refresh_token) => request('/auth/logout', { method: 'POST', json: { refresh_token }, retries: 0 }),
  me: () => requestJson('/users/me'),
};

export const Folders = {
  list: (folderId, opts = {}) => requestJson(folderId ? `/folders/${folderId}` : '/folders', opts),
  create: (name, parent_id) => requestJson('/folders', { method: 'POST', json: { name, parent_id } }),
  rename: (id, name) => requestJson(`/folders/${id}`, { method: 'PATCH', json: { name } }),
  move: (id, new_parent_id) => requestJson(`/folders/${id}/move`, { method: 'POST', json: { new_parent_id } }),
  remove: (id) => request(`/folders/${id}`, { method: 'DELETE' }),
};

export const Files = {
  rename: (id, name) => requestJson(`/files/${id}`, { method: 'PATCH', json: { name } }),
  move: (id, folder_id) => requestJson(`/files/${id}/move`, { method: 'POST', json: { folder_id } }),
  remove: (id) => request(`/files/${id}`, { method: 'DELETE' }),
  // Download streams a blob; no client timeout (large files), with auth + refresh.
  download: (id) => request(`/files/${id}`, { timeout: 0 }),
};

export const Shares = {
  list: (fileId) => requestJson(`/shares?file_id=${fileId}`),
  create: (payload) => requestJson('/shares', { method: 'POST', json: payload }),
  revoke: (id) => request(`/shares/${id}/revoke`, { method: 'POST' }),
  // Public, unauthenticated metadata probe for the share page.
  publicHead: (token) => request(`/s/${token}`, { auth: false, headers: { Range: 'bytes=0-0' } }),
};
