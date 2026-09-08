"""TeleCloud FastAPI Application Entrypoint (Assembly Glue)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from telecloud.config import get_settings
from telecloud.middleware import register_middleware
from telecloud.auth import router as auth_router
from telecloud.auth.supabase_auth import SupabaseAuth
import telecloud.auth.service
import telecloud.auth
from telecloud.users import router as users_router
from telecloud.folders import router as folders_router
from telecloud.files import router as files_router
from telecloud.sharing import router as sharing_router, public_router as sharing_public_router
from telecloud.jobs import router as jobs_router, register as register_jobs
import telecloud.database
import telecloud.rate_limit
import telecloud.telegram

logger = logging.getLogger("telecloud.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan manager for the TeleCloud application.

    Performs startup config validation, dependency injection/wiring, and
    eager resource client initialization, followed by proper resource
    disposal on shutdown.
    """
    logger.info("Starting TeleCloud application...")

    # 1. Validate required config (fail-fast)
    settings = get_settings()
    logger.info("Configuration validated successfully. Environment: %s", settings.app_env)

    # 2. Wire jobs/ enqueuer seam into files/
    register_jobs()
    logger.info("Jobs enqueuer successfully registered with files module.")

    # 3. Eagerly initialize and cache singletons to warm connection pools
    # Supabase service-role client (RLS bypass)
    await telecloud.database.get_service_db()
    logger.info("Supabase service-role client initialized.")

    # Pooled user-scoped Supabase clients (keep-alive reuse across requests)
    await telecloud.database.warm_db_pool()
    logger.info("Supabase user-client pool initialized.")

    # Upstash Redis REST client
    telecloud.rate_limit.get_redis()
    logger.info("Upstash Redis connection pool initialized.")

    # Telegram bot pool
    telecloud.telegram.get_pool()
    logger.info("Telegram bot pool initialized.")

    # Supabase auth adapter singleton injection
    auth_adapter = await SupabaseAuth.from_settings()
    telecloud.auth.service.set_auth(auth_adapter)
    logger.info("Supabase Auth adapter successfully injected.")

    yield

    logger.info("Shutting down TeleCloud application...")

    # 4. Dispose resources gracefully in reverse dependency order
    # Auth client HTTP resources
    await telecloud.auth.close()

    # Telegram bot pool HTTP resources
    await telecloud.telegram.close()

    # Redis REST client HTTP resources
    await telecloud.rate_limit.close()

    # Supabase service role database client connections
    await telecloud.database.close_service_db()

    # Pooled user-scoped Supabase clients (keep-alive connection reuse)
    await telecloud.database.close_db_pool()

    # JWKS verification HTTP client
    await telecloud.auth.close_jwks_client()

    logger.info("All resources cleaned up and shutdown complete.")


def create_app() -> FastAPI:
    """FastAPI application factory (creates and wires the app)."""
    app = FastAPI(
        title="TeleCloud",
        description="A cloud storage system that uses Telegram's Bot API as the storage backend.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Register middleware pipeline (errors, logging, rate limit, CORS)
    register_middleware(app)

    # Mount all module routers
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(folders_router)
    app.include_router(files_router)
    app.include_router(sharing_router)
    app.include_router(sharing_public_router)
    app.include_router(jobs_router)

    # Register health check endpoint
    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Simple health check endpoint."""
        return {"status": "ok"}

    return app


# Module-level app instance for ASGI servers like uvicorn
app = create_app()
