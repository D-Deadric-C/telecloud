"""The **unauthenticated** public share-download route — ``GET /s/{token}`` (SPEC §7.3).

A separate ``APIRouter`` (no auth dependency, ``/s`` prefix) so the token-download
surface is unmistakably distinct from the authed ``/shares`` management routes. It
delegates the trust-sensitive work to :func:`telecloud.sharing.service.open_share_download`
(service-role token resolve + ``revoked`` / ``expires_at`` / ``download_limit``
enforcement + counter bump) and is responsible only for HTTP framing.

Framing mirrors ``files/``'s authenticated download route (SPEC §6.9, §7.2): it
reflects ``storage``'s 200-vs-206 decision and its ``Content-Length`` /
``Content-Range`` / ``Accept-Ranges`` headers onto the wire and adds
``Content-Disposition``. The ``Content-Disposition`` logic is replicated here
(rather than imported from ``files/``'s private router) to keep the module boundary
clean. The response leaks nothing about the owner — only the file's bytes, type,
and name (SPEC §6.13).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from telecloud.sharing import service

public_router = APIRouter(prefix="/s", tags=["sharing"])


def _content_disposition(filename: str) -> str:
    """Build a ``Content-Disposition`` header for an attachment download.

    Provides both a sanitized ASCII ``filename`` (quotes/backslashes/control chars
    stripped) and an RFC 5987 ``filename*`` so non-ASCII names survive intact.
    Replicated from ``files/``'s router (SPEC §6.9) to avoid reaching across a
    module boundary.
    """
    ascii_name = "".join(
        ch for ch in filename if 32 <= ord(ch) < 127 and ch not in '"\\'
    ).strip()
    fallback = ascii_name or "download"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


@public_router.get("/{token}")
async def download_shared_file(token: str, request: Request) -> StreamingResponse:
    """Stream a shared file by its public token — no auth (SPEC §7.3).

    Honors an optional ``Range`` header (``206`` + ``Content-Range`` for a range,
    else ``200`` with the full ``Content-Length``; always advertises
    ``Accept-Ranges: bytes``). The share gates (revoked / expired / over-limit) and
    the download-counter bump are enforced in the service. No owner information is
    exposed.
    """
    range_header = request.headers.get("range")
    download, filename = await service.open_share_download(token, range_=range_header)
    # storage hands us Content-Type/Length/(Range)/Accept-Ranges; we own
    # Content-Disposition. Content-Type is set via media_type to avoid duplication.
    headers = {k: v for k, v in download.headers.items() if k != "Content-Type"}
    headers["Content-Disposition"] = _content_disposition(filename)
    return StreamingResponse(
        download.stream,
        status_code=download.status_code,
        headers=headers,
        media_type=download.mime_type,
    )
