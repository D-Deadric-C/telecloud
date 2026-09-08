"""``telecloud.folders`` — the virtual folder hierarchy (SPEC.md §6.11).

Owns the adjacency-list folder tree (``public.folders``, SPEC §4.2): create under
an optional parent, list a folder's contents (subfolders + files), rename, move
(re-parent with cycle rejection), and soft-delete cascading to descendant folders
and their files. Single-user-per-account: folders are never shared. Every
operation is owner-scoped and RLS-enforced (SPEC §6.3).

Public surface (what other modules import):

* :func:`create_folder`, :func:`list_contents`, :func:`rename_folder`,
  :func:`move_folder`, :func:`soft_delete_folder` — the service functions.
* :func:`validate_name` — folder-name validation (exported for reuse/tests).
* :data:`router` — the ``folders`` ``APIRouter`` to mount on the FastAPI app.
* :func:`set_file_deleter` / :func:`get_file_deleter` / :class:`FileDeleter` — the
  seam ``files/`` (module 13) uses to plug in the file-deletion entrypoint the
  cascade calls (see ``folders/README.md``).

**Boundaries (SPEC §6.11):** does NOT manage file bytes, talk to Telegram, or
compute quota. The cascade hands files to ``files/``'s deletion path via the
:mod:`telecloud.folders.ports` seam. Dependencies: ``config``, ``shared``,
``database``, ``auth``.
"""

from telecloud.folders.ports import FileDeleter, get_file_deleter, set_file_deleter
from telecloud.folders.router import router
from telecloud.folders.service import (
    create_folder,
    list_contents,
    move_folder,
    rename_folder,
    soft_delete_folder,
    validate_name,
)

__all__ = [
    # service
    "create_folder",
    "list_contents",
    "rename_folder",
    "move_folder",
    "soft_delete_folder",
    "validate_name",
    # router
    "router",
    # files-deletion seam
    "FileDeleter",
    "set_file_deleter",
    "get_file_deleter",
]
