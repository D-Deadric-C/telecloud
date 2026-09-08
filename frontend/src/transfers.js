// Transfer manager: concurrent uploads AND downloads, each with its own row in
// the floating panel, independent progress, speed/ETA, and a small concurrency
// cap so a batch never saturates the browser's connection pool (which would
// stall interactive clicks). Replaces the old single-file, single-XHR upload.

import { API_BASE } from './config.js';
import { session, refreshAccessToken, needsRefreshSoon } from './session.js';
import { Files } from './api.js';
import {
  showToast, formatBytes, getFileIconName, getFileIconClass, escapeHtml,
  getFilenameFromContentDisposition, renderIcons,
} from './ui.js';

const MAX_CONCURRENT = 3;

let seq = 0;
let active = 0;
const queue = [];
const jobs = new Map(); // id -> job

const panelEl = () => document.getElementById('upload-panel');
const listEl = () => document.getElementById('transfer-list');

function isRunning(j) { return j.status === 'queued' || j.status === 'running'; }

function updateHeader() {
  const running = [...jobs.values()].filter(isRunning).length;
  const title = document.getElementById('upload-panel-title');
  title.textContent = running > 0 ? `Transferring ${running} file${running > 1 ? 's' : ''}…` : 'Transfers';
  const dismiss = document.getElementById('upload-dismiss-btn');
  dismiss.disabled = running > 0;
  dismiss.style.opacity = running > 0 ? 0.5 : 1.0;
}

function createRow(job) {
  const item = document.createElement('div');
  item.className = 'transfer-item';
  item.dataset.key = job.id;
  item.innerHTML = `
    <div class="upload-file-info">
      <div class="item-icon ${getFileIconClass(job.name)}" data-role="icon"><i data-lucide="${getFileIconName(job.name)}" style="width:18px;height:18px;"></i></div>
      <div class="upload-file-details">
        <div class="upload-filename" data-role="name">${escapeHtml(job.name)}</div>
        <div class="upload-filesize" data-role="sub">${job.size ? formatBytes(job.size) : ''}</div>
      </div>
    </div>
    <div class="upload-bar-bg"><div class="upload-bar-fill" data-role="bar"></div></div>
    <div class="upload-progress-row">
      <span class="upload-progress-text" data-role="status">Queued…</span>
      <span class="upload-progress-percent" data-role="pct">0%</span>
    </div>`;
  listEl().prepend(item); // newest on top
  renderIcons(item);
  job.dom = {
    item,
    icon: item.querySelector('[data-role="icon"]'),
    name: item.querySelector('[data-role="name"]'),
    sub: item.querySelector('[data-role="sub"]'),
    bar: item.querySelector('[data-role="bar"]'),
    status: item.querySelector('[data-role="status"]'),
    pct: item.querySelector('[data-role="pct"]'),
  };
}

function relabel(job) {
  const d = job.dom;
  if (!d) return;
  d.name.textContent = job.name;
  d.icon.className = `item-icon ${getFileIconClass(job.name)}`;
  d.icon.innerHTML = `<i data-lucide="${getFileIconName(job.name)}" style="width:18px;height:18px;"></i>`;
  renderIcons(d.icon);
}

function etaText(job) {
  if (!job.size || !job.loaded || !job.startTime) return '';
  const elapsed = (performance.now() - job.startTime) / 1000;
  if (elapsed <= 0.2) return '';
  const speed = job.loaded / elapsed; // bytes/s
  if (speed <= 0) return '';
  const remain = Math.max(0, (job.size - job.loaded) / speed);
  const eta = remain >= 60 ? `${Math.ceil(remain / 60)}m` : `${Math.ceil(remain)}s`;
  return ` · ${formatBytes(speed)}/s · ${eta} left`;
}

function renderProgress(job) {
  const d = job.dom;
  if (!d) return;
  const pct = job.size ? Math.min(Math.round((job.loaded / job.size) * 100), 100) : 0;
  d.bar.style.width = `${pct}%`;
  d.pct.textContent = `${pct}%`;
  d.status.textContent = job.kind === 'upload' ? 'Uploading…' : 'Downloading…';
  d.sub.textContent = `${formatBytes(job.size || 0)}${etaText(job)}`;
}

function finish(job, ok, message) {
  job.status = ok ? 'done' : 'error';
  const d = job.dom;
  if (d) {
    if (ok) { d.bar.style.width = '100%'; d.pct.textContent = '100%'; }
    d.status.textContent = ok
      ? (job.kind === 'upload' ? 'Uploaded' : 'Downloaded')
      : (message || 'Failed');
    d.sub.textContent = formatBytes(job.size || 0);
    d.item.classList.toggle('transfer-error', !ok);
  }
  active = Math.max(0, active - 1);
  updateHeader();
  pump();
}

function enqueue(job) {
  jobs.set(job.id, job);
  createRow(job);
  panelEl().classList.add('active');
  updateHeader();
  queue.push(job);
  pump();
}

function pump() {
  while (active < MAX_CONCURRENT && queue.length) {
    const job = queue.shift();
    active += 1;
    job.status = 'running';
    job.startTime = performance.now();
    updateHeader();
    job.run();
  }
}

// ---- Public API ------------------------------------------------------------

export function startUpload(file, folderId, { onComplete } = {}) {
  const job = {
    id: `u${++seq}`, kind: 'upload', name: file.name,
    size: file.size, loaded: 0, status: 'queued', startTime: 0,
  };
  job.run = async () => {
    if (needsRefreshSoon()) { try { await refreshAccessToken(); } catch { /* upload may 401 */ } }
    const folderParam = folderId ? `&folder_id=${folderId}` : '';
    const url = `${API_BASE}/files?name=${encodeURIComponent(file.name)}${folderParam}`;
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    xhr.open('POST', url, true);
    xhr.setRequestHeader('Authorization', `Bearer ${session.access}`);
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      job.loaded = e.loaded;
      job.size = e.total;
      renderProgress(job);
    };
    xhr.onload = () => {
      if (xhr.status === 201) {
        finish(job, true);
        showToast(`Uploaded "${file.name}".`);
        onComplete && onComplete(true, file);
      } else {
        let msg = 'Upload failed.';
        try { msg = JSON.parse(xhr.responseText).error?.message || msg; } catch { /* keep default */ }
        if (xhr.status === 401) msg = 'Session expired during upload.';
        finish(job, false, msg);
        showToast(`${file.name}: ${msg}`, 'error');
        onComplete && onComplete(false, file);
      }
    };
    xhr.onerror = () => {
      finish(job, false, 'Network error');
      showToast(`Upload failed: ${file.name}`, 'error');
      onComplete && onComplete(false, file);
    };
    xhr.send(file);
  };
  enqueue(job);
}

export function startDownload(fileId) {
  const job = {
    id: `d${++seq}`, kind: 'download', fileId, name: 'Download',
    size: 0, loaded: 0, status: 'queued', startTime: 0,
  };
  job.run = async () => {
    try {
      if (needsRefreshSoon()) { try { await refreshAccessToken(); } catch { /* handled below */ } }
      const res = await Files.download(fileId); // auth + refresh-on-401 + retry, no timeout
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error?.message || `Download failed (${res.status}).`);
      }
      job.name = getFilenameFromContentDisposition(res.headers.get('content-disposition'));
      const total = parseInt(res.headers.get('content-length'), 10);
      job.size = Number.isNaN(total) ? 0 : total;
      relabel(job);

      // Stream the body so the panel shows live progress + ETA instead of a
      // silent wait. (Native browser-download-tab progress would require an
      // auth-free signed URL from the backend — see BACKEND_RENDER_BRIEF.md.)
      const chunks = [];
      if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          job.loaded += value.length;
          renderProgress(job);
        }
      } else {
        chunks.push(await res.blob()); // very old browsers: no streaming
        job.loaded = job.size;
      }

      const blob = new Blob(chunks);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = job.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      finish(job, true);
    } catch (err) {
      finish(job, false, err.message);
      showToast(err.message || 'Download failed.', 'error');
    }
  };
  enqueue(job);
}

// True while a given file has an in-progress (queued/running) download, so the UI
// can block deleting a file out from under an active transfer.
export function isFileTransferring(fileId) {
  for (const job of jobs.values()) {
    if (job.fileId === fileId && isRunning(job)) return true;
  }
  return false;
}

// Remove finished rows and hide the panel if nothing is running.
export function clearFinishedTransfers() {
  if ([...jobs.values()].some(isRunning)) return;
  jobs.forEach((job, id) => { job.dom?.item.remove(); jobs.delete(id); });
  panelEl().classList.remove('active');
}
