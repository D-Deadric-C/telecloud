// Session/token lifecycle: persistence + proactive & reactive refresh.
//
// The backend has no /auth/refresh endpoint, but the issued tokens are standard
// Supabase tokens, so we renew them directly against Supabase's GoTrue endpoint
// using the public anon key (exactly what the official Supabase JS client does).
// This is what keeps long sessions from "randomly failing" after the ~1h access
// token TTL expires.

import { SUPABASE_URL, SUPABASE_ANON_KEY, CAN_REFRESH } from './config.js';

const K_ACCESS = 'access_token';
const K_REFRESH = 'refresh_token';
const K_EXPIRY = 'token_expiry'; // epoch ms
const K_USER = 'user'; // cached PublicUser, so a refresh can show the app instantly

function readUser() {
  try { return JSON.parse(localStorage.getItem(K_USER) || 'null'); } catch { return null; }
}

export const session = {
  access: localStorage.getItem(K_ACCESS) || null,
  refresh: localStorage.getItem(K_REFRESH) || null,
  expiresAt: Number(localStorage.getItem(K_EXPIRY)) || 0,
  user: readUser(), // PublicUser {id, email, email_verified}, restored across reloads
};

export function isLoggedIn() {
  return Boolean(session.access);
}

// Persist the current user so the next page load can render the app immediately
// without waiting on (or flashing the login screen during) a profile fetch.
export function setUser(user) {
  session.user = user || null;
  if (user) localStorage.setItem(K_USER, JSON.stringify(user));
  else localStorage.removeItem(K_USER);
}

export function setSession({ access_token, refresh_token, expires_in, user }) {
  if (access_token !== undefined) session.access = access_token;
  if (refresh_token) session.refresh = refresh_token;
  if (expires_in) session.expiresAt = Date.now() + Number(expires_in) * 1000;
  if (user) setUser(user);

  if (session.access) localStorage.setItem(K_ACCESS, session.access);
  if (session.refresh) localStorage.setItem(K_REFRESH, session.refresh);
  if (session.expiresAt) localStorage.setItem(K_EXPIRY, String(session.expiresAt));
}

export function clearSession() {
  session.access = null;
  session.refresh = null;
  session.expiresAt = 0;
  session.user = null;
  localStorage.removeItem(K_ACCESS);
  localStorage.removeItem(K_REFRESH);
  localStorage.removeItem(K_EXPIRY);
  localStorage.removeItem(K_USER);
}

// True when the access token is within 60s of expiry (or already expired).
export function needsRefreshSoon() {
  return CAN_REFRESH && session.refresh && session.expiresAt > 0 &&
    Date.now() > session.expiresAt - 60_000;
}

// "Try Demo": mint a brand-new anonymous Supabase session. POSTing /auth/v1/signup
// with an empty body (and anonymous sign-ins enabled on the project) returns a
// full session — no email, no password, no confirmation. The account has no
// email, so the profile trigger creates it unverified and it lands in the capped
// tier automatically.
export async function signInAnonymously() {
  if (!CAN_REFRESH) throw new Error('Demo sign-in is not configured.');

  const res = await fetch(`${SUPABASE_URL}/auth/v1/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', apikey: SUPABASE_ANON_KEY },
    body: JSON.stringify({}),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty/non-JSON body */ }

  if (!res.ok || !data?.access_token) {
    if (data?.error_code === 'anonymous_provider_disabled') {
      throw new Error('Demo mode is currently disabled (anonymous sign-ins are off for this project).');
    }
    throw new Error(data?.msg || data?.error_description || 'Demo sign-in failed. Please try again.');
  }

  setSession({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_in: data.expires_in,
  });
}

let inFlight = null;

// Renew the access token. Single-flight: concurrent callers share one request.
export async function refreshAccessToken() {
  if (inFlight) return inFlight;
  if (!CAN_REFRESH) throw new Error('Token refresh is not configured.');
  if (!session.refresh) throw new Error('No refresh token available.');

  inFlight = (async () => {
    const res = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', apikey: SUPABASE_ANON_KEY },
      body: JSON.stringify({ refresh_token: session.refresh }),
    });
    if (!res.ok) throw new Error('Session refresh failed.');
    const data = await res.json();
    if (!data.access_token) throw new Error('Session refresh returned no token.');
    setSession({
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_in: data.expires_in,
    });
    return data.access_token;
  })();

  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}
