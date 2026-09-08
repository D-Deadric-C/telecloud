// Dashboard: folder contents, files table, sidebar tree, breadcrumbs, quota,
// and all file/folder mutations (create, rename, move, delete, upload, download).

import { session } from '../session.js';
import { Folders, Files, ApiError } from '../api.js';
import { state } from '../state.js';
import {
  showToast, openModal, closeModal, reconcileList, renderIcons,
  formatBytes, formatDate, getFileIconName, getFileIconClass, escapeHtml,
  closeSidebar,
} from '../ui.js';
import {
  startUpload, startDownload, clearFinishedTransfers, isFileTransferring,
} from '../transfers.js';

// ---- Load + fetch ----------------------------------------------------------

export async function loadDashboard() {
  const user = session.user;
  const isDemo = Boolean(user && !user.email); // anonymous "Try Demo" account
  document.getElementById('user-email').textContent = user?.email || (isDemo ? 'Demo account' : '');
  const avatar = document.getElementById('user-avatar');
  if (avatar) avatar.textContent = (user?.email || '?').charAt(0);

  const badge = document.getElementById('user-verified-badge');
  const banner = document.getElementById('unverified-banner');
  if (user?.email_verified) {
    badge.textContent = 'Verified';
    badge.className = 'user-badge';
    banner.style.display = 'none';
  } else {
    badge.textContent = isDemo ? 'Demo' : 'Unverified';
    badge.className = 'user-badge unverified';
    banner.style.display = 'flex';
    // A demo account has no email to verify — reword the banner and hide "Resend".
    banner.querySelector('span').innerHTML = isDemo
      ? '<i data-lucide="sparkles" style="width:15px;height:15px;"></i> Demo sandbox — storage is limited to 500 MiB total and 30 MiB per file.'
      : '<i data-lucide="mail-warning" style="width:15px;height:15px;"></i> Email unverified — storage is limited to 500 MiB total and 30 MiB per file.';
    banner.querySelector('button').style.display = isDemo ? 'none' : '';
    renderIcons(banner);
  }

  await fetchCurrentFolderContents();
  populateSidebarFolders();

  // Quota + the move-dropdown folder list both need the whole tree; do ONE
  // parallel scan in the background and feed both.
  fetchFolderTree()
    .then(({ folders, totalBytes }) => {
      state.allWorkspaceFolders = folders;
      state.estimatedStorageUsed = totalBytes;
      updateQuotaUI();
    })
    .catch((err) => console.error('Workspace tree scan failed', err));
}

function setNavLoading(on) {
  document.getElementById('nav-progress')?.classList.toggle('active', on);
}

export async function fetchCurrentFolderContents() {
  setNavLoading(true);
  try {
    const data = await Folders.list(state.currentFolderId);
    state.folderContents = data;
    renderFilesTable();
    renderBreadcrumbs();
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    setNavLoading(false);
  }
}

// Move the sidebar highlight locally — no network request. The sidebar's folder
// SET only changes on create/rename/move/delete (which call populateSidebarFolders);
// plain navigation just changes which item is active.
export function updateSidebarActive() {
  document.getElementById('tree-root-item')
    .classList.toggle('active', state.currentFolderId === null);
  document.querySelectorAll('#sidebar-subfolders .tree-item').forEach((a) => {
    a.classList.toggle('active', a.dataset.id === state.currentFolderId);
  });
}

export function navigateToFolder(folderId) {
  if (folderId === null) state.navigationHistory = [];
  state.currentFolderId = folderId;
  clearSearch({ rerender: false }); // a stale query must not filter the next folder
  closeSidebar();                 // mobile drawer closes when you pick a folder
  updateSidebarActive();          // instant highlight, no extra request
  fetchCurrentFolderContents();   // the one request a folder-open actually needs
}

// ---- Search (client-side filter of the current folder) ---------------------

export function setSearchQuery(query) {
  state.searchQuery = query;
  renderFilesTable();
}

export function clearSearch({ rerender = true } = {}) {
  state.searchQuery = '';
  const input = document.getElementById('search-input');
  if (input) input.value = '';
  if (rerender) renderFilesTable();
}

// ---- View mode (list / grid) ------------------------------------------------

export function setViewMode(mode) {
  state.viewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('view_mode', state.viewMode);
  document.querySelectorAll('#view-toggle .seg-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === state.viewMode);
  });
  // Only re-render when the dashboard is actually showing (this also runs at boot).
  if (document.getElementById('dashboard-view').classList.contains('active')) {
    renderFilesTable();
  }
}

export function handleFolderClick(folderId, folderName) {
  state.navigationHistory.push({ id: folderId, name: folderName });
  navigateToFolder(folderId);
}

// ---- Parallel folder-tree scan (quota + move list) -------------------------

// Simple concurrency limiter — caps how many requests a run may have in flight.
function pLimit(max) {
  let running = 0;
  const waiting = [];
  const next = () => {
    if (running >= max || waiting.length === 0) return;
    running += 1;
    const { fn, resolve, reject } = waiting.shift();
    fn().then(resolve, reject).finally(() => { running -= 1; next(); });
  };
  return (fn) => new Promise((resolve, reject) => { waiting.push({ fn, resolve, reject }); next(); });
}

// Walk the whole tree fetching siblings in parallel, but capped at a few requests
// at a time and marked low-priority. The browser allows only ~6 connections per
// host (HTTP/1.1); an uncapped scan would hog them all and stall interactive
// clicks (new folder, navigate, delete) for seconds. Capping leaves headroom.
export async function fetchFolderTree() {
  const limit = pLimit(3);
  const flat = [];
  let totalBytes = 0;

  async function visit(folderId, depth, pathPrefix) {
    let data;
    try {
      data = await limit(() => Folders.list(folderId, { priority: 'low', retries: 1 }));
    } catch {
      return; // skip unreachable subtree; quota stays a best-effort estimate
    }
    for (const f of data.files || []) totalBytes += f.size_bytes;

    const pending = (data.folders || []).map((sub) => {
      const displayPath = pathPrefix ? `${pathPrefix} / ${sub.name}` : sub.name;
      flat.push({ id: sub.id, name: sub.name, path: displayPath, depth });
      return visit(sub.id, depth + 1, displayPath);
    });
    await Promise.all(pending);
  }

  await visit(null, 0, '');
  flat.sort((a, b) => a.path.localeCompare(b.path)); // stable tree pre-order
  return { folders: flat, totalBytes };
}

// Adjust the quota estimate locally (instant) when we already know the byte delta
// — avoids a full tree re-scan after every upload/file delete.
export function adjustQuota(deltaBytes) {
  state.estimatedStorageUsed = Math.max(0, state.estimatedStorageUsed + deltaBytes);
  updateQuotaUI();
}

let quotaTimer = null;
// Full re-scan, debounced + low-priority, for cases where we can't compute the
// delta locally (e.g. deleting a folder of unknown aggregate size).
export function refreshQuotaSoon() {
  clearTimeout(quotaTimer);
  quotaTimer = setTimeout(async () => {
    try {
      const { totalBytes } = await fetchFolderTree();
      state.estimatedStorageUsed = totalBytes;
    } catch (err) {
      console.error('Quota scan failed', err);
    }
    updateQuotaUI();
  }, 600);
}

export function updateQuotaUI() {
  const percentageText = document.getElementById('quota-percentage');
  const barFill = document.getElementById('quota-bar-fill');
  const text = document.getElementById('quota-text');
  const used = formatBytes(state.estimatedStorageUsed);

  if (session.user?.email_verified) {
    percentageText.textContent = '—';
    barFill.style.width = '30%';
    text.innerHTML = `<i data-lucide="database" style="width:12px;height:12px;"></i> ${used} used — Unlimited`;
  } else {
    const limit = 500 * 1024 * 1024;
    const pct = Math.min(Math.round((state.estimatedStorageUsed / limit) * 100), 100);
    percentageText.textContent = `${pct}%`;
    barFill.style.width = `${pct}%`;
    text.innerHTML = `<i data-lucide="database" style="width:12px;height:12px;"></i> ${used} / 500 MB`;
  }
  renderIcons(text);
}

export async function loadAllWorkspaceFolders() {
  try {
    const { folders } = await fetchFolderTree();
    state.allWorkspaceFolders = folders;
  } catch (err) {
    console.error('Workspace folders fetch error', err);
    state.allWorkspaceFolders = [];
  }
}

// ---- Sidebar ---------------------------------------------------------------

export async function populateSidebarFolders() {
  const container = document.getElementById('sidebar-subfolders');
  try {
    const data = await Folders.list(null);
    const desired = (data.folders || []).map((sub) => {
      const isActive = state.currentFolderId === sub.id;
      return {
        key: sub.id,
        sig: `${sub.name}|${isActive ? 1 : 0}`,
        className: 'tree-node',
        html: `
          <a href="#" class="tree-item ${isActive ? 'active' : ''}" data-action="nav-folder" data-id="${sub.id}">
            <i data-lucide="folder" style="width:16px;height:16px;"></i> ${escapeHtml(sub.name)}
          </a>`,
      };
    });
    reconcileList(container, desired);
  } catch (e) {
    console.error('Sidebar update failed', e);
  }
}

// ---- Breadcrumbs -----------------------------------------------------------

export function renderBreadcrumbs() {
  const breadcrumbs = document.getElementById('breadcrumbs');
  breadcrumbs.innerHTML = '';

  const rootLink = document.createElement('a');
  rootLink.className = `breadcrumb-item ${state.currentFolderId === null ? 'active' : ''}`;
  rootLink.textContent = 'My Files';
  rootLink.href = '#';
  rootLink.dataset.action = 'nav-crumb';
  rootLink.dataset.id = '';
  breadcrumbs.appendChild(rootLink);

  if (state.currentFolderId === null) return;

  const histIndex = state.navigationHistory.findIndex((h) => h.id === state.currentFolderId);
  if (histIndex !== -1) {
    state.navigationHistory = state.navigationHistory.slice(0, histIndex + 1);
  } else {
    const found = state.allWorkspaceFolders.find((f) => f.id === state.currentFolderId);
    state.navigationHistory.push({ id: state.currentFolderId, name: found ? found.name : 'Folder' });
  }

  state.navigationHistory.forEach((h, index) => {
    const sep = document.createElement('span');
    sep.className = 'breadcrumb-separator';
    sep.textContent = '/';
    breadcrumbs.appendChild(sep);

    const isLast = index === state.navigationHistory.length - 1;
    if (isLast) {
      const item = document.createElement('span');
      item.className = 'breadcrumb-item active';
      item.textContent = h.name;
      breadcrumbs.appendChild(item);
    } else {
      const link = document.createElement('a');
      link.className = 'breadcrumb-item';
      link.textContent = h.name;
      link.href = '#';
      link.dataset.action = 'nav-crumb';
      link.dataset.id = h.id;
      breadcrumbs.appendChild(link);
    }
  });
}

// ---- Files table -----------------------------------------------------------

// The "..." trigger shown instead of the hover-revealed action row on
// touch/narrow viewports (hidden on desktop via CSS). Toggling it opens the
// row's/card's .actions-cell / .grid-card-actions as a popover.
function buildMenuBtnHtml() {
  return `<button class="btn-icon item-menu-btn" title="More actions" aria-label="More actions" data-action="item-menu"><i data-lucide="ellipsis-vertical" style="width:18px;height:18px;"></i></button>`;
}

// Name cell contents: icon + name, with a secondary line (size · date) that is
// only shown in the mobile card layout.
function buildNameHtml(iconClass, iconName, name, sub) {
  return `
    <div class="item-icon ${iconClass}"><i data-lucide="${iconName}" style="width:16px;height:16px;"></i></div>
    <div class="name-text"><span class="row-name">${name}</span><span class="row-sub">${sub}</span></div>`;
}

function buildFolderRowHtml(folder) {
  const name = escapeHtml(folder.name);
  if (folder._pending) {
    return `
    <td>
      <div class="name-cell" style="opacity:0.55;cursor:default;">
        ${buildNameHtml('icon-folder-box', 'folder', name, 'Creating…')}
      </div>
    </td>
    <td>—</td>
    <td><span style="color:var(--text-subdued);font-size:13px;">Creating…</span></td>
    <td><div class="actions-cell"><div class="spinner" style="width:16px;height:16px;"></div></div></td>`;
  }
  return `
    <td>
      <a href="#" class="name-cell" data-action="open-folder" data-id="${folder.id}" data-name="${name}">
        ${buildNameHtml('icon-folder-box', 'folder', name, `Folder · ${shortDate(folder.created_at)}`)}
      </a>
    </td>
    <td>—</td>
    <td>${formatDate(folder.created_at)}</td>
    <td>
      <div class="actions-cell">
        <button class="btn-icon" title="Rename" data-action="rename" data-type="folder" data-id="${folder.id}" data-name="${name}"><i data-lucide="edit-2" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon" title="Move" data-action="move" data-type="folder" data-id="${folder.id}" data-name="${name}"><i data-lucide="folder-input" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon danger-hover" title="Delete" data-action="delete-folder" data-id="${folder.id}"><i data-lucide="trash-2" style="width:16px;height:16px;"></i></button>
      </div>
      ${buildMenuBtnHtml()}
    </td>`;
}

function buildFileRowHtml(file) {
  const name = escapeHtml(file.name);
  return `
    <td>
      <div class="name-cell file-node">
        ${buildNameHtml(getFileIconClass(file.name), getFileIconName(file.name), name, `${formatBytes(file.size_bytes)} · ${shortDate(file.created_at)}`)}
      </div>
    </td>
    <td>${formatBytes(file.size_bytes)}</td>
    <td>${formatDate(file.created_at)}</td>
    <td>
      <div class="actions-cell">
        <button class="btn-icon" title="Download" data-action="download" data-id="${file.id}"><i data-lucide="download" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon" title="Share" data-action="share" data-id="${file.id}" data-name="${name}"><i data-lucide="link-2" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon" title="Rename" data-action="rename" data-type="file" data-id="${file.id}" data-name="${name}"><i data-lucide="edit-2" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon" title="Move" data-action="move" data-type="file" data-id="${file.id}" data-name="${name}"><i data-lucide="folder-input" style="width:16px;height:16px;"></i></button>
        <button class="btn-icon danger-hover" title="Delete" data-action="delete-file" data-id="${file.id}"><i data-lucide="trash-2" style="width:16px;height:16px;"></i></button>
      </div>
      ${buildMenuBtnHtml()}
    </td>`;
}

// Short date for grid-card metadata (the full date+time is too wide for a card).
function shortDate(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function buildFolderCardHtml(folder) {
  const name = escapeHtml(folder.name);
  if (folder._pending) {
    return `
      <div class="item-icon icon-folder-box"><i data-lucide="folder"></i></div>
      <div class="grid-card-name">${name}</div>
      <div class="grid-card-meta">Creating…</div>`;
  }
  return `
    <a href="#" class="grid-card-link" data-action="open-folder" data-id="${folder.id}" data-name="${name}" aria-label="Open folder ${name}"></a>
    ${buildMenuBtnHtml()}
    <div class="grid-card-actions">
      <button class="btn-icon" title="Rename" data-action="rename" data-type="folder" data-id="${folder.id}" data-name="${name}"><i data-lucide="edit-2"></i></button>
      <button class="btn-icon" title="Move" data-action="move" data-type="folder" data-id="${folder.id}" data-name="${name}"><i data-lucide="folder-input"></i></button>
      <button class="btn-icon danger-hover" title="Delete" data-action="delete-folder" data-id="${folder.id}"><i data-lucide="trash-2"></i></button>
    </div>
    <div class="item-icon icon-folder-box"><i data-lucide="folder"></i></div>
    <div class="grid-card-name" title="${name}">${name}</div>
    <div class="grid-card-meta">Folder · ${shortDate(folder.created_at)}</div>`;
}

function buildFileCardHtml(file) {
  const name = escapeHtml(file.name);
  return `
    ${buildMenuBtnHtml()}
    <div class="grid-card-actions">
      <button class="btn-icon" title="Download" data-action="download" data-id="${file.id}"><i data-lucide="download"></i></button>
      <button class="btn-icon" title="Share" data-action="share" data-id="${file.id}" data-name="${name}"><i data-lucide="link-2"></i></button>
      <button class="btn-icon" title="Rename" data-action="rename" data-type="file" data-id="${file.id}" data-name="${name}"><i data-lucide="edit-2"></i></button>
      <button class="btn-icon" title="Move" data-action="move" data-type="file" data-id="${file.id}" data-name="${name}"><i data-lucide="folder-input"></i></button>
      <button class="btn-icon danger-hover" title="Delete" data-action="delete-file" data-id="${file.id}"><i data-lucide="trash-2"></i></button>
    </div>
    <div class="${getFileIconClass(file.name)}"><i data-lucide="${getFileIconName(file.name)}"></i></div>
    <div class="grid-card-name" title="${name}">${name}</div>
    <div class="grid-card-meta">${formatBytes(file.size_bytes)} · ${shortDate(file.created_at)}</div>`;
}

export function renderFilesTable() {
  const tbody = document.getElementById('files-table-body');
  const grid = document.getElementById('files-grid');
  const emptyState = document.getElementById('empty-state');
  const tableContainer = document.querySelector('.table-container');

  const query = state.searchQuery.trim().toLowerCase();
  const matches = (item) => !query || item.name.toLowerCase().includes(query);
  const byName = (a, b) => a.name.localeCompare(b.name);
  const folders = (state.folderContents.folders || []).filter(matches).sort(byName);
  const files = (state.folderContents.files || []).filter(matches).sort(byName);

  if (!folders.length && !files.length) {
    document.getElementById('empty-title').textContent = query ? 'No matches' : 'This folder is empty';
    document.getElementById('empty-desc').textContent = query
      ? `Nothing here matches “${state.searchQuery.trim()}”.`
      : 'Drag files anywhere on this window, or use the Upload button.';
    emptyState.style.display = 'flex';
    tableContainer.style.display = 'none';
    grid.style.display = 'none';
    tbody.replaceChildren();
    grid.replaceChildren();
    return;
  }
  emptyState.style.display = 'none';

  const isGrid = state.viewMode === 'grid';
  tableContainer.style.display = isGrid ? 'none' : 'block';
  grid.style.display = isGrid ? 'grid' : 'none';

  const desired = [];
  if (isGrid) {
    tbody.replaceChildren();
    folders.forEach((f) => desired.push({
      key: `folder:${f.id}`,
      sig: `g|${f.name}|${f.created_at}|${f._pending ? 1 : 0}`,
      className: `grid-card${f._pending ? ' pending' : ''}`,
      html: buildFolderCardHtml(f),
    }));
    files.forEach((f) => desired.push({
      key: `file:${f.id}`,
      sig: `g|${f.name}|${f.size_bytes}|${f.created_at}`,
      className: 'grid-card',
      html: buildFileCardHtml(f),
    }));
    reconcileList(grid, desired);
  } else {
    grid.replaceChildren();
    folders.forEach((f) => desired.push({ key: `folder:${f.id}`, sig: `${f.name}|${f.created_at}|${f._pending ? 1 : 0}`, tag: 'tr', html: buildFolderRowHtml(f) }));
    files.forEach((f) => desired.push({ key: `file:${f.id}`, sig: `${f.name}|${f.size_bytes}|${f.created_at}`, tag: 'tr', html: buildFileRowHtml(f) }));
    reconcileList(tbody, desired);
  }
}

// ---- Create folder ---------------------------------------------------------

export function openCreateFolderModal() {
  document.getElementById('new-folder-name').value = '';
  openModal('create-folder-modal');
}

export async function submitCreateFolder(event) {
  event.preventDefault();
  const name = document.getElementById('new-folder-name').value.trim();
  if (!name) return;
  closeModal('create-folder-modal');

  // Optimistic: show the folder (as a pending row) immediately so creation feels
  // instant despite backend latency. The server response then replaces it.
  const temp = { id: `temp-${Date.now()}`, name, created_at: new Date().toISOString(), _pending: true };
  state.folderContents.folders = [...(state.folderContents.folders || []), temp];
  renderFilesTable();

  try {
    await Folders.create(name, state.currentFolderId);
    showToast(`Folder "${name}" created successfully.`);
    fetchCurrentFolderContents(); // replaces the pending row with the real one
    populateSidebarFolders();
  } catch (err) {
    showToast(err.message, 'error');
    fetchCurrentFolderContents(); // rolls back the pending row
  }
}

// ---- Rename ----------------------------------------------------------------

export function openRenameModal(type, id, name) {
  state.activeModalItem = { type, id, name };
  document.getElementById('rename-modal-title').textContent = `Rename ${type === 'folder' ? 'Folder' : 'File'}`;
  document.getElementById('rename-item-name').value = name;
  openModal('rename-modal');
}

export async function submitRename(event) {
  event.preventDefault();
  const item = state.activeModalItem;
  if (!item) return;
  const newName = document.getElementById('rename-item-name').value.trim();
  if (!newName || newName === item.name) { closeModal('rename-modal'); return; }
  try {
    if (item.type === 'folder') await Folders.rename(item.id, newName);
    else await Files.rename(item.id, newName);
    showToast('Item renamed successfully.');
    closeModal('rename-modal');
    fetchCurrentFolderContents();
    populateSidebarFolders();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ---- Move ------------------------------------------------------------------

export async function openMoveModal(type, id, name) {
  state.activeModalItem = { type, id, name };
  const select = document.getElementById('move-destination-select');
  select.innerHTML = '';
  const rootOpt = document.createElement('option');
  rootOpt.value = 'root';
  rootOpt.textContent = 'My Files (Root)';
  select.appendChild(rootOpt);

  await loadAllWorkspaceFolders();
  state.allWorkspaceFolders.forEach((f) => {
    if (type === 'folder' && (f.id === id || f.path.startsWith(`${name} /`) || f.path === name)) return;
    const opt = document.createElement('option');
    opt.value = f.id;
    opt.textContent = `${'  '.repeat(f.depth)}└─ ${f.name}`;
    select.appendChild(opt);
  });
  openModal('move-modal');
}

export async function submitMove(event) {
  event.preventDefault();
  const item = state.activeModalItem;
  if (!item) return;
  const dest = document.getElementById('move-destination-select').value;
  const destinationFolderId = dest === 'root' ? null : dest;
  try {
    if (item.type === 'folder') await Folders.move(item.id, destinationFolderId);
    else await Files.move(item.id, destinationFolderId);
    showToast('Item moved successfully.');
    closeModal('move-modal');
    fetchCurrentFolderContents();
    populateSidebarFolders();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ---- Delete ----------------------------------------------------------------

export async function deleteFile(fileId) {
  // Don't pull a file out from under an in-progress transfer.
  if (isFileTransferring(fileId)) {
    showToast('This file is still transferring. Wait for it to finish before deleting.', 'error');
    return;
  }
  if (!confirm('Are you sure you want to delete this file? This action is irreversible.')) return;
  const known = (state.folderContents.files || []).find((f) => f.id === fileId);

  // Optimistic: drop the row immediately; restore from the server on failure.
  state.folderContents.files = (state.folderContents.files || []).filter((f) => f.id !== fileId);
  renderFilesTable();

  try {
    const res = await Files.remove(fileId);
    if (res.status === 204) {
      showToast('File soft-deleted. Space will reclaim shortly.');
      if (known) adjustQuota(-known.size_bytes); // instant, no re-scan
    } else {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(data.error?.message || 'Failed to delete file.', { status: res.status });
    }
  } catch (err) {
    showToast(err.message, 'error');
    fetchCurrentFolderContents(); // roll back
  }
}

export async function deleteFolder(folderId) {
  if (!confirm('Are you sure you want to delete this folder? ALL contents inside it (subfolders and files) will be deleted!')) return;

  // Optimistic removal from the table + sidebar; restore on failure.
  state.folderContents.folders = (state.folderContents.folders || []).filter((f) => f.id !== folderId);
  renderFilesTable();
  updateSidebarActive();

  try {
    const res = await Folders.remove(folderId);
    if (res.status === 204) {
      showToast('Folder deleted successfully.');
      refreshQuotaSoon(); // unknown aggregate size — debounced full re-scan
      populateSidebarFolders();
    } else {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(data.error?.message || 'Failed to delete folder.', { status: res.status });
    }
  } catch (err) {
    showToast(err.message, 'error');
    fetchCurrentFolderContents(); // roll back
    populateSidebarFolders();
  }
}

// ---- Download (streamed, with progress + ETA in the transfers panel) --------

export function downloadFile(fileId) {
  startDownload(fileId);
}

// ---- Upload (multiple files, concurrent, via the transfers manager) --------

export function triggerUpload() {
  document.getElementById('file-uploader').click();
}

export function dismissUploadPanel() {
  clearFinishedTransfers();
}

// Shared by the file picker and drag & drop. Enforces the client-side 30 MiB
// per-file cap for unverified users (the server enforces it too, FRONTEND_BRIEF §3).
export function queueUploads(files) {
  if (!files.length) return;

  const unverified = !session.user?.email_verified;
  const folderId = state.currentFolderId;

  for (const file of files) {
    if (unverified && file.size > 30 * 1024 * 1024) {
      showToast(`"${file.name}" skipped — unverified accounts can't upload files over 30 MiB.`, 'error');
      continue;
    }
    startUpload(file, folderId, {
      onComplete: (ok) => {
        if (!ok) return;
        fetchCurrentFolderContents();
        adjustQuota(file.size); // instant quota bump, no full re-scan
      },
    });
  }
}

export function handleFilesSelected(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = ''; // allow re-selecting the same file(s) later
  queueUploads(files);
}

// ---- Drag & drop upload ------------------------------------------------------

// Full-window drop target: dragging files anywhere over the dashboard shows the
// overlay; dropping uploads into the current folder. A depth counter is needed
// because dragenter/dragleave fire for every child element crossed.
export function initDropZone() {
  const overlay = document.getElementById('drop-overlay');
  const sub = document.getElementById('drop-overlay-sub');
  let depth = 0;

  const isFileDrag = (e) =>
    document.getElementById('dashboard-view').classList.contains('active') &&
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');

  const hide = () => { depth = 0; overlay.classList.remove('active'); };

  window.addEventListener('dragenter', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    depth += 1;
    if (depth === 1) {
      const here = state.currentFolderId
        ? state.navigationHistory[state.navigationHistory.length - 1]?.name
        : null;
      sub.textContent = here ? `Files will upload to “${here}”` : 'Files will upload to My Files';
      overlay.classList.add('active');
    }
  });
  window.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // required, or the browser navigates to the file on drop
  });
  window.addEventListener('dragleave', (e) => {
    if (!isFileDrag(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) overlay.classList.remove('active');
  });
  window.addEventListener('drop', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    hide();
    queueUploads(Array.from(e.dataTransfer.files || []));
  });
}
