"""``telecloud.files`` — file lifecycle & orchestration (SPEC.md §6.12).

The orchestrator that ties ``quota`` + ``storage`` + ``folders`` together and
builds the actual HTTP responses — the only file-domain module that produces
FastAPI responses (SPEC §6.12). It owns the upload route (``quota.check_can_upload``
→ ``storage.store_upload`` → ``quota.add_usage``, SPEC §7.1), the range-aware
authenticated download route (``storage.open_download``, SPEC §7.2), list / rename /
move, and the **soft-delete** entrypoint (mark ``deleting`` → decrement quota →
enqueue a deferred Telegram-deletion job, SPEC §6.12, §7.4).

Public surface (what other modules import):

* :func:`upload_file`, :func:`open_file_download`, :func:`list_files`,
  :func:`rename_file`, :func:`move_file` — the service functions (the read helpers
  are reused by ``sharing/``).
* :func:`soft_delete_file` — the public deletion entrypoint also called by
  ``folders/``'s cascade (registered with ``folders.set_file_deleter`` below).
* :func:`validate_file_name` — file-name validation (exported for reuse/tests).
* :data:`router` — the ``files`` ``APIRouter`` to mount on the FastAPI app.
* :func:`set_deletion_enqueuer` / :func:`get_deletion_enqueuer` /
  :class:`DeletionEnqueuer` — the seam ``jobs/`` (module 14) uses to plug in the
  deferred Telegram-deletion enqueuer (FLAGGED; see ``files/README.md``).

**Boundaries (SPEC §6.12):** does NOT talk to Telegram directly (always via
``storage``/``telegram``) and does NOT reimplement quota math or chunking.

**Composition.** Importing this package registers :func:`soft_delete_file` as the
file-deletion entrypoint ``folders/``'s cascade resolves through its
``folders.ports`` seam (``folders.set_file_deleter``). ``files/`` depends on
``folders/`` (SPEC §6.12), so this import direction is legal and ``folders/`` never
imports ``files/`` — the dependency is inverted, not circular.
"""

from telecloud import folders

from telecloud.files.ports import (
    DeletionEnqueuer,
    get_deletion_enqueuer,
    set_deletion_enqueuer,
)
from telecloud.files.router import router
from telecloud.files.service import (
    list_files,
    move_file,
    open_file_download,
    rename_file,
    soft_delete_file,
    upload_file,
    validate_file_name,
)

# Wire the file-deletion entrypoint into folders/'s cascade seam (SPEC §6.11,
# §6.12). soft_delete_file matches folders.ports.FileDeleter exactly
# ((user, file_id, *, access_token)), so it registers with no adapter.
folders.set_file_deleter(soft_delete_file)

__all__ = [
    # service
    "upload_file",
    "open_file_download",
    "list_files",
    "rename_file",
    "move_file",
    "soft_delete_file",
    "validate_file_name",
    # router
    "router",
    # jobs-enqueue seam
    "DeletionEnqueuer",
    "set_deletion_enqueuer",
    "get_deletion_enqueuer",
]
