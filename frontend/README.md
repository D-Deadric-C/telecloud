# TeleCloud Frontend

Maintained by **Suryansh Sharma**.

[Repository](https://github.com/D-Deadric-C/telecloud) · [LinkedIn](https://www.linkedin.com/in/suryansh-sharma-a76b52324/) · [MIT License](../LICENSE)

The TeleCloud web client — an installable Vite + vanilla-JS progressive web app.
It works with any HTTPS-hosted TeleCloud API.

## What changed vs. the old single `index.html`

- **No more silent session death.** The access token is refreshed automatically —
  proactively before it expires and reactively on a `401` (single-flight, against
  Supabase's token endpoint). Sessions no longer "randomly fail" after ~1 hour.
- **Fast reconnects.** Requests retry one transient failure with a short backoff
  and show a reconnecting notice without holding the UI for long periods.
- **Installable and offline-ready.** A web app manifest and service worker provide
  home-screen installation and cache the application shell.
- **Configurable API URL** via `VITE_API_BASE` (no hardcoded `127.0.0.1:8000`).
- **Snappier rendering.** Scoped icon rendering, keyed DOM diffing (scroll/hover
  survive updates), and a parallel folder-tree scan instead of N+1 sequential
  fetches.
- **No more frozen clicks.** The background quota scan is concurrency-capped (3)
  and marked low-priority so it can't hog the browser's ~6 connections and stall
  your clicks; quota updates locally after upload/delete instead of re-scanning.
- **Concurrent transfers panel.** Multiple uploads run independently (capped at 3
  at a time) with per-file progress; downloads stream with live progress + ETA
  instead of a silent wait. (A literal browser-download-tab download for owner
  files needs an auth-free signed URL from the backend — see the brief.)
- **No CDN dependency.** `lucide` is bundled and tree-shaken (44 KB total, ~13 KB
  gzipped) instead of a 410 KB render-blocking `<script>`.
- **Robust event handling.** Declarative `data-action` dispatch replaces inline
  `onclick`, so file/folder names containing quotes no longer break buttons.

## Project layout

```
frontend/
  index.html            # markup (data-action attributes, no inline JS/CSS)
  src/
    main.js             # bootstrap, routing, action wiring
    config.js           # reads VITE_* env vars
    session.js          # token storage + refresh (single-flight)
    api.js              # fetch client (retry, refresh-on-401, error envelope) + endpoints
    ui.js               # icons, DOM reconciliation, toasts, modals, formatting, dispatch
    icons.js            # tree-shaken lucide icon set
    state.js            # shared app state
    style.css           # design tokens + components (unchanged design)
    views/
      auth.js           # login/signup, verify, Supabase redirect, logout
      files.js          # dashboard: table, sidebar, breadcrumbs, quota, CRUD, upload
      share.js          # share modal + public share page
  vite.config.js        # dev proxy to the backend
  vercel.json           # Vercel build + SPA rewrite
```

## Local development

```bash
cd frontend
npm install
cp .env.example .env.local      # fill in VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY
npm run dev                      # http://localhost:5173
```

Leave `VITE_API_BASE` **empty** for local dev: the Vite dev server proxies API
paths (`/auth`, `/folders`, `/files`, `/shares`, `/s/`, `/users`, `/health`) to the
backend, so the browser talks same-origin and there's no CORS to configure. The
proxy target defaults to `http://127.0.0.1:8000` (override with `DEV_API_TARGET`).

Run the backend separately (see [the root quickstart](../README.md#quickstart)).

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `VITE_API_BASE` | prod only | Backend base URL, e.g. `https://api.telecloud.example`. Empty = same-origin (dev proxy). |
| `VITE_SUPABASE_URL` | yes* | Supabase project URL — used for client-side token refresh. |
| `VITE_SUPABASE_ANON_KEY` | yes* | Supabase anon (public) key — gated by RLS, safe to ship to browsers. |
| `DEV_API_TARGET` | no | Dev-proxy target when `VITE_API_BASE` is empty. |

\* Without the two Supabase vars the app still works, but an expired session just
bounces the user to the login screen instead of refreshing seamlessly.

## Deploying to Vercel

1. Import the repo in Vercel and set **Root Directory** to `frontend/`.
   (Framework preset auto-detects as **Vite**; build `npm run build`, output `dist`.)
2. Add the env vars above (`VITE_API_BASE` = your API URL, plus the two
   Supabase values) for Production (and Preview, if you use preview deploys).
3. Deploy. `vercel.json` rewrites all routes to `index.html` (SPA).

After the frontend URL is live, the **backend** must:
- allow that origin in CORS, and
- set `APP_BASE_URL` to the Vercel URL (used for share links and the Supabase
  email-confirmation redirect),
- and that URL must be in the Supabase **Redirect URLs** allow-list.

All of that is spelled out in [the deployment instructions](../README.md#deploying-and-scheduling-cleanup).

## Build

```bash
npm run build     # -> dist/
npm run preview   # serve the production build locally (also proxies the API)
```
