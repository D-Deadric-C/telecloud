import { defineConfig, loadEnv } from 'vite';

// The backend exposes several top-level path prefixes. In local dev we proxy them
// to the running API so the browser talks to the dev server same-origin (no CORS
// dance). In production VITE_API_BASE points straight at the deployed backend and
// these proxies are irrelevant.
// NOTE: '/s/' (public-share route) MUST keep its trailing slash — a bare '/s'
// prefix would also match Vite's own '/src/...' module requests and proxy them
// to the backend, breaking the dev server.
const API_PREFIXES = ['/auth', '/users', '/folders', '/files', '/shares', '/s/', '/jobs', '/health'];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  // Where the dev proxy forwards API calls. Override with DEV_API_TARGET if your
  // backend isn't on the default port.
  const target = env.DEV_API_TARGET || 'http://127.0.0.1:8000';

  const proxy = Object.fromEntries(
    API_PREFIXES.map((p) => [p, { target, changeOrigin: true, secure: false }])
  );

  const port = Number(process.env.PORT) || 5173;

  return {
    server: { port, proxy },
    preview: { port: Number(process.env.PORT) || 4173, proxy },
    build: { outDir: 'dist', sourcemap: true },
  };
});
