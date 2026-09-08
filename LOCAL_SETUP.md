# TeleCloud local setup

Maintained by Suryansh Sharma.

- Repository: https://github.com/D-Deadric-C/telecloud
- LinkedIn: https://www.linkedin.com/in/suryansh-sharma-a76b52324/

## Backend terminal

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Fill `.env` with your own Telegram, Supabase and Upstash credentials. Keep
`APP_BASE_URL=http://localhost:5173` and
`CORS_ALLOWED_ORIGINS=http://localhost:5173`. Apply the five Supabase SQL migrations
in order as described in README.md. Allow `http://localhost:5173/index.html` in
Supabase Auth redirect URLs.

```bash
python -m uvicorn telecloud.main:app --reload --host 127.0.0.1 --port 8000
```

## Frontend terminal

From the project root:

```bash
cd frontend
npm ci
cp -n .env.example .env.local
```

Fill in your Supabase project URL and public anon key in `.env.local`, matching
the backend project. Leave `VITE_API_BASE` empty to use the local API proxy.

```bash
npm run dev
```

Open http://localhost:5173. Local development still requires the configured
Telegram, Supabase and Upstash services; it is not an offline storage mode.

## Commit and publish your changes

```bash
git remote set-url origin https://github.com/D-Deadric-C/telecloud.git
git add .env.example DEPLOY_GUIDE.html README.md LOCAL_SETUP.md LICENSE TeleCloud_Engineering_Deep_Dive.docx frontend/README.md frontend/index.html frontend/package.json frontend/package-lock.json index.html
git commit -m "Update TeleCloud branding and links for Suryansh Sharma"
git push -u origin main
```

## LinkedIn post draft

I’m sharing my version of TeleCloud, a cloud storage project that uses Telegram
for file storage, FastAPI for the API, Supabase for authentication and metadata,
and a Vite frontend. It supports chunked uploads, folders, and shareable links.

I’ve updated the branding and local setup and will deploy it next.

Explore the code: https://github.com/D-Deadric-C/telecloud
Connect with me: https://www.linkedin.com/in/suryansh-sharma-a76b52324/

#Python #FastAPI #JavaScript #Supabase #OpenSource
