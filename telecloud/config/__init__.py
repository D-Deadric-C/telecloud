"""``telecloud.config`` — the single typed settings layer (SPEC.md §6.1).

Public surface:

* :func:`get_settings` — cached singleton accessor (the primary entry point).
* :class:`Settings` — the settings type, for annotations.
* :class:`ConfigError` — raised on missing/invalid config at startup.
* The hard constants: :data:`CHUNK_SIZE`, :data:`QUOTA_UNVERIFIED_BYTES`,
  :data:`MAX_FILE_SIZE_UNVERIFIED`, and the ``*_VERIFIED`` "unlimited" sentinels.

This package has **no** dependencies on any other TeleCloud module (SPEC §6.1).
"""

from telecloud.config.settings import (
    CHUNK_SIZE,
    MAX_FILE_SIZE_UNVERIFIED,
    MAX_FILE_SIZE_VERIFIED,
    QUOTA_UNVERIFIED_BYTES,
    QUOTA_VERIFIED_BYTES,
    ConfigError,
    Settings,
    get_settings,
)

__all__ = [
    "get_settings",
    "Settings",
    "ConfigError",
    "CHUNK_SIZE",
    "QUOTA_UNVERIFIED_BYTES",
    "MAX_FILE_SIZE_UNVERIFIED",
    "QUOTA_VERIFIED_BYTES",
    "MAX_FILE_SIZE_VERIFIED",
]
