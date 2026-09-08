// Share-link management (authenticated modal) + the public share download page.

import { Shares, ApiError } from '../api.js';
import { state } from '../state.js';
import {
  showToast, openModal, renderIcons, formatDate, formatBytes,
  getFileIconName, getFilenameFromContentDisposition, getSizeBytesFromHeaders,
} from '../ui.js';
import { API_BASE } from '../config.js';

// ---- Authenticated share modal --------------------------------------------

export async function openShareModal(fileId, filename) {
  state.activeModalItem = { type: 'file', id: fileId, name: filename };
  document.getElementById('share-modal-title').textContent = `Share: ${filename}`;
  document.getElementById('share-expires').value = '';
  document.getElementById('share-limit').value = '';
  await fetchFileShares(fileId);
  openModal('share-modal');
}

export async function fetchFileShares(fileId) {
  const container = document.getElementById('active-shares-list-container');
  container.innerHTML = '<p style="color:var(--text-subdued);font-size:12px;">Loading shares...</p>';
  try {
    const data = await Shares.list(fileId);
    container.innerHTML = '';
    const active = (data.shares || []).filter((s) => !s.revoked);
    if (active.length === 0) {
      container.innerHTML = '<p style="color:var(--text-subdued);font-size:12px;">No active public share links.</p>';
      return;
    }
    const origin = window.location.origin + window.location.pathname;
    active.forEach((share) => {
      const item = document.createElement('div');
      item.className = 'share-history-item';
      const shareUrl = `${origin}?s=${share.token}`;
      const expiryText = share.expires_at ? `Expires: ${formatDate(share.expires_at)}` : 'Never expires';
      const limitText = share.download_limit
        ? `Limit: ${share.download_count}/${share.download_limit} downloads`
        : `Downloads: ${share.download_count}`;
      item.innerHTML = `
        <div class="share-history-info">
          <span class="share-history-url">${shareUrl}</span>
          <span>${expiryText} · ${limitText}</span>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn-icon" title="Copy Link" data-action="copy" data-text="${shareUrl}"><i data-lucide="copy" style="width:14px;height:14px;"></i></button>
          <button class="btn-icon danger-hover" title="Revoke Share" data-action="revoke-share" data-id="${share.id}" data-file="${fileId}"><i data-lucide="slash" style="width:14px;height:14px;"></i></button>
        </div>`;
      container.appendChild(item);
    });
    renderIcons(container);
  } catch (err) {
    container.innerHTML = `<p style="color:var(--destructive);font-size:12px;">${err.message}</p>`;
  }
}

export async function submitCreateShare(event) {
  event.preventDefault();
  const item = state.activeModalItem;
  if (!item) return;
  const expiresInput = document.getElementById('share-expires').value;
  const limitInput = document.getElementById('share-limit').value;
  const payload = { file_id: item.id };
  if (expiresInput) payload.expires_at = new Date(expiresInput).toISOString();
  if (limitInput) payload.download_limit = parseInt(limitInput, 10);
  try {
    await Shares.create(payload);
    showToast('Public share link created successfully!');
    fetchFileShares(item.id);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

export async function revokeShare(shareId, fileId) {
  if (!confirm('Revoke this public share? Anyone with the link will lose download access immediately.')) return;
  try {
    const res = await Shares.revoke(shareId);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(data.error?.message || 'Failed to revoke share link.', { status: res.status });
    }
    showToast('Share link revoked.');
    fetchFileShares(fileId);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ---- Public share download page -------------------------------------------

export async function loadPublicFileDetails(shareToken) {
  state.publicShareToken = shareToken;
  const loading = document.getElementById('public-loading');
  const errorDiv = document.getElementById('public-error');
  const content = document.getElementById('public-content');
  loading.style.display = 'block';
  errorDiv.style.display = 'none';
  content.style.display = 'none';

  try {
    const res = await Shares.publicHead(shareToken);
    if (!res.ok && res.status !== 206) {
      let msg = res.status === 404 ? 'File not found or share link revoked.' : 'Share link unavailable.';
      try { msg = (await res.json()).error?.message || msg; } catch { /* keep default */ }
      throw new ApiError(msg, { status: res.status });
    }
    const sizeBytes = getSizeBytesFromHeaders(res.headers);
    const filename = getFilenameFromContentDisposition(res.headers.get('content-disposition'));
    const mimeType = res.headers.get('content-type') || 'application/octet-stream';
    state.publicFileDetails = { filename, sizeBytes, mimeType };

    document.getElementById('public-filename').textContent = filename;
    document.getElementById('public-filesize').textContent = formatBytes(sizeBytes);

    const iconName = getFileIconName(filename);
    const iconBox = document.getElementById('public-file-icon');
    const iconInner = document.getElementById('public-file-icon-inner');
    iconBox.className = 'public-file-icon';
    iconInner.setAttribute('data-lucide', iconName);
    if (iconName === 'video') iconBox.classList.add('video');

    loading.style.display = 'none';
    content.style.display = 'block';
    renderIcons(content);
  } catch (err) {
    loading.style.display = 'none';
    document.getElementById('public-error-message').textContent = err.message;
    errorDiv.style.display = 'block';
    renderIcons(errorDiv);
  }
}

export function downloadPublicFile() {
  if (!state.publicShareToken) return;
  window.location.href = `${API_BASE}/s/${state.publicShareToken}`;
}
