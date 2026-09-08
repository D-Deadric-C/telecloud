// UI primitives: scoped icon rendering, keyed DOM reconciliation, toasts, modals,
// view switching, formatting helpers, and a small declarative action dispatcher.

import { createIcons } from 'lucide';
import { appIcons as icons } from './icons.js';

// ---- Icons -----------------------------------------------------------------

// Render <i data-lucide> icons inside `root` ONLY. Scoping each pass to the
// container that changed avoids re-scanning the whole document on every update.
export function renderIcons(root) {
  try {
    if (root && root !== document && root.querySelectorAll) {
      createIcons({ icons, root });
    } else {
      createIcons({ icons });
    }
  } catch {
    /* never let an icon pass break a render */
  }
}

// ---- Keyed list reconciliation --------------------------------------------

// Patch `container`'s children in place to match `desired` instead of wiping +
// rebuilding via innerHTML. Each entry: { key, sig, html, tag?, className? }.
// Unchanged rows keep their DOM node, so scroll position, :hover, and CSS
// transitions survive updates. Icons are rendered scoped to changed nodes only.
export function reconcileList(container, desired) {
  const existing = new Map();
  for (const el of Array.from(container.children)) {
    if (el.dataset && el.dataset.key) existing.set(el.dataset.key, el);
  }

  const wanted = new Set(desired.map((d) => d.key));
  existing.forEach((el, key) => {
    if (!wanted.has(key)) { el.remove(); existing.delete(key); }
  });

  let prev = null;
  for (const d of desired) {
    let el = existing.get(d.key);
    if (!el) {
      el = document.createElement(d.tag || 'div');
      el.dataset.key = d.key;
      if (d.className !== undefined) el.className = d.className;
      el.innerHTML = d.html;
      el.dataset.sig = d.sig;
    } else if (el.dataset.sig !== d.sig) {
      if (d.className !== undefined && el.className !== d.className) el.className = d.className;
      el.innerHTML = d.html;
      el.dataset.sig = d.sig;
    }
    const ref = prev ? prev.nextSibling : container.firstChild;
    if (ref !== el) container.insertBefore(el, ref);
    prev = el;
  }

  // Render icons AFTER every row is attached to the document. Rendering into a
  // detached element (before insertBefore) is what intermittently left rows with
  // blank icon boxes. This single scoped pass only touches unrendered <i>
  // elements (already-rendered <svg> no longer carry data-lucide), so it stays cheap.
  renderIcons(container);
}

// ---- Toasts ----------------------------------------------------------------

export function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type === 'error' ? 'toast-error' : 'toast-success'}`;
  const iconName = type === 'error' ? 'alert-circle' : 'check-circle';
  toast.innerHTML = `
    <i data-lucide="${iconName}" style="width:18px;height:18px;color:${type === 'error' ? 'var(--destructive)' : '#16a34a'}"></i>
    <div class="toast-message">${escapeHtml(message)}</div>
    <i data-lucide="x" class="toast-close" data-action="dismiss-toast"></i>
  `;
  container.appendChild(toast);
  renderIcons(toast);

  setTimeout(() => {
    if (toast.parentElement) {
      toast.style.animation = 'fadeIn 0.2s reverse ease';
      setTimeout(() => toast.remove(), 200);
    }
  }, 5000);
}

// ---- Modals & views --------------------------------------------------------

export function openModal(id) { document.getElementById(id).classList.add('active'); }
export function closeModal(id) {
  document.getElementById(id).classList.remove('active');
}

// ---- Wakeup overlay (cold-start retry notice) -------------------------------

export function showWakeupOverlay() {
  document.getElementById('wakeup-overlay').classList.add('active');
}
export function hideWakeupOverlay() {
  document.getElementById('wakeup-overlay').classList.remove('active');
}

export function transitionToView(viewId) {
  document.querySelectorAll('.view-container').forEach((v) => v.classList.remove('active'));
  const view = document.getElementById(viewId);
  view.classList.add('active');
  renderIcons(view);
}

// ---- Mobile sidebar drawer --------------------------------------------------
// Below the mobile breakpoint the dashboard sidebar is an off-canvas drawer;
// `sidebar-open` on <body> slides it in over a backdrop (see style.css). On
// desktop the class has no effect — the drawer styles live in a media query.

export function toggleSidebar() { document.body.classList.toggle('sidebar-open'); }
export function closeSidebar() { document.body.classList.remove('sidebar-open'); }

// ---- Per-item overflow menus ------------------------------------------------
// On touch/narrow viewports the hover-revealed action buttons are unreachable,
// so each row/card gets a "..." trigger that toggles `menu-open` on its host.
// Only one menu is open at a time; any click elsewhere closes it (initDispatch).

export function toggleItemMenu(el) {
  const host = el.closest('tr, .grid-card');
  if (!host) return;
  const wasOpen = host.classList.contains('menu-open');
  closeItemMenus();
  if (!wasOpen) host.classList.add('menu-open');
}

export function closeItemMenus() {
  document.querySelectorAll('.menu-open').forEach((el) => el.classList.remove('menu-open'));
}

export function togglePasswordVisibility(id) {
  const input = document.getElementById(id);
  const eye = document.getElementById(`${id}-eye`);
  if (input.type === 'password') {
    input.type = 'text';
    eye.setAttribute('data-lucide', 'eye-off');
  } else {
    input.type = 'password';
    eye.setAttribute('data-lucide', 'eye');
  }
  renderIcons(eye.closest('.input-wrapper') || document);
}

// ---- Action dispatch -------------------------------------------------------
// Elements declare data-action="name" (click) or data-action-submit="name"
// (form submit). Handlers receive (element, event) and read params from dataset.
// This replaces inline onclick="" — names with quotes/specials no longer break.

const actions = {};
export function registerActions(map) { Object.assign(actions, map); }

export function initDispatch() {
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    // Any click that isn't the "..." trigger itself dismisses open item menus
    // (the trigger manages its own toggle, including closing siblings).
    if (!el || el.dataset.action !== 'item-menu') closeItemMenus();
    if (!el) return;
    if (el.tagName === 'A') e.preventDefault(); // action links never navigate
    const fn = actions[el.dataset.action];
    if (fn) fn(el, e);
  });
  document.addEventListener('submit', (e) => {
    const el = e.target.closest('[data-action-submit]');
    if (!el) return;
    const fn = actions[el.dataset.actionSubmit];
    if (fn) fn(el, e);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSidebar(); closeItemMenus(); }
  });
}

// ---- Formatting ------------------------------------------------------------

export function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 Bytes';
  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / k ** i).toFixed(1))} ${sizes[i]}`;
}

export function formatDate(isoString) {
  if (!isoString) return '—';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return '—';
  return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })} ${
    d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })}`;
}

// Extension → { icon, box } lookup shared by the table, grid, transfers panel,
// and public share page so every surface renders the same visual identity.
const FILE_KINDS = [
  { exts: ['pdf'], icon: 'file-text', box: 'icon-pdf-box' },
  { exts: ['doc', 'docx', 'rtf', 'odt'], icon: 'file-text', box: 'icon-text-box' },
  { exts: ['mp4', 'mov', 'avi', 'mkv', 'webm'], icon: 'video', box: 'icon-video-box' },
  { exts: ['txt', 'md', 'log'], icon: 'file-type', box: 'icon-text-box' },
  { exts: ['xlsx', 'xls', 'csv', 'tsv'], icon: 'table', box: 'icon-sheet-box' },
  { exts: ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico', 'heic'], icon: 'image', box: 'icon-image-box' },
  { exts: ['mp3', 'wav', 'flac', 'm4a', 'ogg', 'aac'], icon: 'music', box: 'icon-audio-box' },
  { exts: ['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'iso'], icon: 'archive', box: 'icon-zip-box' },
  { exts: ['js', 'ts', 'jsx', 'tsx', 'py', 'json', 'html', 'css', 'java', 'c', 'cpp', 'h', 'rs', 'go', 'sh', 'sql', 'yml', 'yaml', 'toml'], icon: 'file-code', box: 'icon-code-box' },
];

function fileKind(filename) {
  const ext = String(filename).split('.').pop().toLowerCase();
  return FILE_KINDS.find((k) => k.exts.includes(ext)) || { icon: 'file', box: 'icon-file-box' };
}

export function getFileIconName(filename) {
  return fileKind(filename).icon;
}

export function getFileIconClass(filename) {
  return `item-icon ${fileKind(filename).box}`;
}

export function getFilenameFromContentDisposition(header) {
  if (!header) return 'download';
  const utf8 = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8) return decodeURIComponent(utf8[1]);
  const std = header.match(/filename="?([^";]+)"?/i);
  if (std) return std[1];
  return 'download';
}

export function getSizeBytesFromHeaders(headers) {
  const contentRange = headers.get('content-range');
  if (contentRange) {
    const total = parseInt(contentRange.split('/')[1], 10);
    if (!Number.isNaN(total)) return total;
  }
  const len = parseInt(headers.get('content-length'), 10);
  return Number.isNaN(len) ? 0 : len;
}

export function escapeHtml(string) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return String(string).replace(/[&<>"']/g, (m) => map[m]);
}

export function copyToClipboard(text) {
  navigator.clipboard.writeText(text)
    .then(() => showToast('Link copied to clipboard!'))
    .catch(() => showToast('Failed to copy link.', 'error'));
}
