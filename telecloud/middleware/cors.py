"""CORS configuration for the frontend (SPEC.md §6.7).

The frontend is a single-file HTML/JS/CSS app (SPEC §2) that calls this API from
the browser, so the app must send the right CORS headers for the frontend's
origin. Origins are taken from ``config``.

**Contract note — flagged, not silently diverged (SPEC top matter):** ``config``
currently exposes no dedicated "allowed CORS origins" field (see
``config/settings.py``); its only browser-facing URL is ``app_base_url``. Rather
than reach past ``config`` to read an undeclared env var (``Settings`` uses
``extra="ignore"``, so it could not be read anyway), this module derives the
allowed origin from the existing :attr:`Settings.app_base_url` by default. If the
frontend is ever served from a *different* origin than the API, ``config`` should
gain an explicit ``cors_allowed_origins`` list — that is a ``config/`` change and
is flagged here, not made. Until then, callers may also pass ``origins=`` to
:func:`register_cors` / ``register_middleware`` to override.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from telecloud.config import Settings, get_settings


def _origin_of(url: str) -> str | None:
    """Reduce a URL to its ``scheme://host[:port]`` origin, or ``None`` if not a
    usable absolute URL."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


#: Response headers the browser's JS must be able to read cross-origin. The
#: frontend reads these off ``GET /files/{id}`` and the public ``GET /s/{token}``
#: to name downloads and show share sizes; cross-origin JS can only see them when
#: they're in ``Access-Control-Expose-Headers`` (SPEC §6.7 / brief §4).
EXPOSE_HEADERS = [
    "Content-Disposition",
    "Content-Range",
    "Content-Length",
    "Accept-Ranges",
]

#: Allow Vercel **preview** deployments (random ``*.vercel.app`` subdomains) in
#: addition to the explicitly-configured origins, so preview builds aren't blocked
#: by CORS. Production origins should still be listed via ``cors_allowed_origins``.
VERCEL_PREVIEW_ORIGIN_REGEX = r"https://.*\.vercel\.app"


def resolve_cors_origins(settings: Settings | None = None) -> list[str]:
    """The allowed browser origins for the frontend (SPEC §6.7).

    Unions the origin derived from :attr:`Settings.app_base_url` (the public app
    URL) with any explicit :attr:`Settings.cors_allowed_origins` (e.g. a
    separately-hosted Vercel frontend). Returns a sorted list so it slots straight
    into Starlette's :class:`CORSMiddleware`.
    """
    settings = settings or get_settings()
    origins: set[str] = set()
    base = _origin_of(settings.app_base_url)
    if base:
        origins.add(base)
    origins.update(getattr(settings, "cors_allowed_origins", []) or [])
    return sorted(origins)


def register_cors(
    app: FastAPI,
    *,
    origins: list[str] | None = None,
    settings: Settings | None = None,
) -> None:
    """Install :class:`CORSMiddleware` allowing the frontend origin(s) (SPEC §6.7).

    ``origins`` overrides the config-derived list when given (e.g. a separately
    hosted frontend). Credentials are allowed so the browser may send the bearer
    token; methods/headers are left open since this is a same-trust frontend.
    ``expose_headers`` lets the frontend read the download/streaming headers it
    needs (see :data:`EXPOSE_HEADERS`), and a ``*.vercel.app`` regex additionally
    permits Vercel preview deploys.
    """
    allowed = origins if origins is not None else resolve_cors_origins(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_origin_regex=VERCEL_PREVIEW_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSE_HEADERS,
    )
