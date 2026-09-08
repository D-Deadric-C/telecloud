# `telecloud.shared` — common errors, models & pure helpers

The shared vocabulary the rest of TeleCloud builds on (SPEC.md §6.2, §5.1, §5.3).
Everything here is **pure**: no DB, Telegram, Redis, HTTP, or filesystem access.
It depends only on [`telecloud.config`](../config) (for `CHUNK_SIZE`).

## What's here

| Group       | Exports |
|-------------|---------|
| Error model | `TeleCloudError`, `ErrorCode`, `DEFAULT_STATUS` |
| Read models | `UserContext`, `FileMeta`, `ChunkMeta`, `FolderMeta`, `ShareMeta` |
| Enums       | `FileStatus`, `ChunkStatus` |
| Helpers     | `generate_token`, `format_size`, `compute_chunk_count`, `locate_byte` |

## Error model (SPEC §5.1)

One exception type carries everything `middleware/` needs to render the canonical
envelope `{ "error": { "code", "message" } }`:

```python
from telecloud.shared import TeleCloudError, ErrorCode

# Full control:
raise TeleCloudError(ErrorCode.NOT_FOUND, "No such file", 404)

# Or let the conventional status fill itself in:
raise TeleCloudError.from_code(ErrorCode.QUOTA_EXCEEDED, "Over your 500 MiB limit")

err.to_dict()       # {"error": {"code": "quota_exceeded", "message": "..."}}
err.http_status     # 413
```

`ErrorCode` is the reserved, stable code set from SPEC §5.1 — extend it **there**,
never with ad-hoc strings. `DEFAULT_STATUS` maps each code to its default HTTP
status (used by `from_code`).

## Read models (SPEC §4, §5.3)

Immutable (`frozen`) views mirroring the meaningful fields of the frozen Postgres
tables. They accept attribute-access objects (`from_attributes=True`), so a
`database/` repo can build one straight from a row:

```python
from telecloud.shared import FileMeta
meta = FileMeta.model_validate(db_row)   # ids are uuid.UUID, times are UTC datetime
```

Module-private request/response shapes do **not** live here (SPEC §5.3).
`ShareMeta` is the *internal* model and includes `owner_id`; the public share
download route must not leak owner identity (SPEC §6.13) and returns its own shape.

## Helpers (SPEC §6.2)

```python
from telecloud.shared import generate_token, format_size, compute_chunk_count, locate_byte

generate_token()              # URL-safe, ~256-bit unguessable string (verification + shares)
format_size(18 * 1024 * 1024) # "18.0 MiB"  (IEC binary units)

# Chunk math — default chunk size is config.CHUNK_SIZE (18 MiB, SPEC §1):
compute_chunk_count(size)     # ceil(size / CHUNK_SIZE); 0 bytes → 0 chunks (upload, §7.1)
locate_byte(start)            # (chunk_index, intra_chunk_offset) for Range (§7.2)
```

All helpers validate their inputs and raise `ValueError` on negative sizes/offsets
or a non-positive chunk size.

## Tests

```
python -m pytest telecloud/shared/tests -q
```

Covers the chunk-math and token helpers (the brief's focus) plus light checks of
the error envelope and model construction.

## Dependencies

- `pydantic` (v2) — the read models.
- `telecloud.config` — only for the `CHUNK_SIZE` constant.

No other TeleCloud module is imported (SPEC §6.2).
