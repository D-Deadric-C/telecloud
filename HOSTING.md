# TeleCloud hosting

TeleCloud is provider-neutral. The frontend is an installable static PWA and the
backend is a portable Docker service.

## Lowest-latency layout

Keep the API, Supabase project, and Upstash Redis in the same region. For users
primarily in India and South Asia, use Singapore when every provider offers it.
Use an always-on API instance: hosts that suspend idle containers add cold-start
delay that application code cannot remove.

```text
Browser / installed PWA
        |
        | HTTPS
        v
Always-on API  ── same region ── Supabase + Upstash
        |
        v
Telegram Bot API
```

## Backend container

Build and test locally:

```bash
docker build -t telecloud-api .
docker run --rm -p 8000:8000 --env-file .env telecloud-api
curl --fail http://127.0.0.1:8000/health
```

Deploy this repository's `Dockerfile` on your chosen container platform. Set all
variables from `.env.example` as platform secrets. Use at least two workers and
keep one instance continuously running.

## Frontend PWA

```bash
cd frontend
npm ci
npm run build
```

Deploy `frontend/dist/` to any HTTPS static host. Set these build variables:

```dotenv
VITE_API_BASE=https://your-api.example
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your-public-anon-key
```

The service worker is deliberately excluded from long-lived HTTP caching so new
releases activate quickly. API responses and file data are never stored in the
offline cache.

## Final wiring

Set the backend values to the final frontend origin without a trailing slash:

```dotenv
APP_BASE_URL=https://your-app.example
CORS_ALLOWED_ORIGINS=https://your-app.example
APP_ENV=production
```

Add `https://your-app.example/index.html` to the Supabase Auth redirect allow-list.
Schedule QStash POST requests to:

```text
https://your-api.example/jobs/sweep-orphans
https://your-api.example/jobs/deferred-delete
```

## Production checks

```bash
curl --fail https://your-api.example/health
curl --fail --head https://your-app.example/
```

Test installation, sign-in, folder navigation, upload, ranged download, sharing,
and an offline reload of the application shell before announcing the release.
