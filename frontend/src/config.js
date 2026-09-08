// Runtime configuration, read from Vite env vars at build time.
// All values are public (see .env.example). Empty API_BASE => same-origin (dev proxy).

export const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');
export const SUPABASE_URL = (import.meta.env.VITE_SUPABASE_URL || '').replace(/\/+$/, '');
export const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

// Whether client-side token refresh is wired up. Without it, an expired session
// just bounces the user to the login screen (still correct, just less seamless).
export const CAN_REFRESH = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
