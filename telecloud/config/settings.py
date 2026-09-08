"""Single typed settings layer for the whole TeleCloud app (SPEC.md §5.2, §6.1).

Every piece of configuration the system needs is read **here and only here** from
environment variables. No other module may touch ``os.environ`` directly; they
import :func:`get_settings` instead.

Two kinds of values live in this module:

* **Hard constants** — fixed by the design and never configurable
  (e.g. :data:`CHUNK_SIZE`, the unverified quota limits). They are module-level
  constants, *not* environment variables, so they can never drift per-deploy.
* **Secrets / per-deploy config** — Telegram tokens, Supabase keys, Redis,
  Resend, QStash, the app base URL. These are required environment variables and
  are validated at startup; a missing one fails fast with a clear message.
"""

from __future__ import annotations

import functools
from typing import Annotated

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ---------------------------------------------------------------------------
# Hard constants (NOT environment-configurable) — SPEC.md §1, §3
# ---------------------------------------------------------------------------

#: Fixed 18 MiB chunk size. Chosen because the Telegram Bot API ``getFile``
#: download path caps at 20 MB, and downloads stream back through the bot, so the
#: download cap is the binding limit. Do NOT make this dynamic (SPEC §1).
CHUNK_SIZE: int = 18 * 1024 * 1024

#: Total storage an **unverified** user may consume (500 MiB) — SPEC §3.
QUOTA_UNVERIFIED_BYTES: int = 500 * 1024 * 1024

#: Largest single file an **unverified** user may upload (30 MiB) — SPEC §3.
MAX_FILE_SIZE_UNVERIFIED: int = 30 * 1024 * 1024

#: Sentinel for "unlimited" — a **verified** user has no total quota and no
#: per-file size cap (SPEC §3). Quota code treats ``None`` as unlimited.
QUOTA_VERIFIED_BYTES: None = None
MAX_FILE_SIZE_VERIFIED: None = None


class Settings(BaseSettings):
    """All per-deploy configuration, loaded from environment variables.

    Field names map case-insensitively to ``UPPER_SNAKE_CASE`` env vars
    (e.g. ``supabase_url`` ← ``SUPABASE_URL``). In local development a ``.env``
    file at the repo root is also read; in production (Fly.io) the values come
    from the real environment. See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Telegram (round-robin bot pool + channels) — SPEC §6.8 ------------
    #: One or more bot tokens forming the round-robin send pool. Provide as a
    #: comma-separated list in ``TELEGRAM_BOT_TOKENS``.
    telegram_bot_tokens: Annotated[list[str], NoDecode]
    #: One or more private channel ids chunks may live in. Comma-separated in
    #: ``TELEGRAM_CHANNEL_IDS``. Channel ids are negative bigints (e.g. ``-100…``).
    telegram_channel_ids: Annotated[list[int], NoDecode]

    # -- Supabase (Postgres + Auth) — SPEC §2, §6.3 ------------------------
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    supabase_jwt_secret: str

    # -- Upstash Redis (rate limit + retry queue) — SPEC §6.6 --------------
    upstash_redis_rest_url: str
    upstash_redis_rest_token: str

    # -- Resend — DEPRECATED (email verification is Supabase-managed) -------
    #: Formerly used by the removed ``notifications/`` module. Email verification
    #: now goes through Supabase's built-in confirmation, so these are optional and
    #: unused; kept (nullable) only so existing ``.env`` files don't break.
    resend_api_key: str | None = None
    resend_from_email: str | None = None

    # -- QStash (cron / cleanup callbacks) — SPEC §6.14 --------------------
    #: Two signing keys are kept so QStash key rotation never drops a request:
    #: verify an incoming signature against *either* the current or next key.
    qstash_current_signing_key: str
    qstash_next_signing_key: str

    # -- App ----------------------------------------------------------------
    #: Public base URL used to build verification / share links
    #: (e.g. ``https://telecloud.fly.dev``). No trailing slash.
    app_base_url: str
    #: Extra browser origins allowed by CORS (comma-separated), e.g. the Vercel
    #: frontend URL(s). The origin derived from app_base_url is always allowed too.
    cors_allowed_origins: Annotated[list[str], NoDecode] = []
    #: Deploy environment label. Optional; informational only.
    app_env: str = "development"

    # -- Validators ---------------------------------------------------------

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip().rstrip("/") for v in value.split(",") if v.strip()]
        return value

    @field_validator("telegram_bot_tokens", "telegram_channel_ids", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept a comma-separated string (the env-var form) or a real list.

        Using ``NoDecode`` above disables pydantic-settings' default JSON
        decoding for these complex fields, so a plain ``a,b,c`` env value reaches
        us here as a string and we split it ourselves. An already-parsed list
        (e.g. when constructed in tests) passes through untouched.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("app_base_url", "supabase_url", "upstash_redis_rest_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an absolute http(s) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _require_non_empty_pools(self) -> "Settings":
        if not self.telegram_bot_tokens:
            raise ValueError(
                "TELEGRAM_BOT_TOKENS must contain at least one bot token"
            )
        if not self.telegram_channel_ids:
            raise ValueError(
                "TELEGRAM_CHANNEL_IDS must contain at least one channel id"
            )
        return self

    # -- Convenience --------------------------------------------------------

    @property
    def chunk_size(self) -> int:
        """The fixed chunk size, exposed on the settings object for convenience."""
        return CHUNK_SIZE


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing or invalid."""


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into a clear, actionable startup message."""
    lines: list[str] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "<root>"
        env_name = field.upper()
        if err["type"] == "missing":
            lines.append(f"  - {env_name}: required but not set")
        else:
            lines.append(f"  - {env_name}: {err['msg']}")
    joined = "\n".join(lines)
    return (
        "TeleCloud configuration is invalid. Fix the following environment "
        f"variable(s) (see .env.example):\n{joined}"
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton :class:`Settings`.

    Public entry point for the whole app (SPEC §6.1). The first call reads and
    validates the environment; every subsequent call returns the same instance
    thanks to :func:`functools.lru_cache`. If required configuration is missing
    or malformed, this raises :class:`ConfigError` with a clear message so the
    process fails fast at startup rather than deep inside a request.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
