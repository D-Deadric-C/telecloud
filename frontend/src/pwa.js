let deferredInstallPrompt = null;
let notify = () => {};

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
}

function isIos() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

function syncInstallButtons() {
  const canInstall = !isStandalone() && (Boolean(deferredInstallPrompt) || isIos());
  document.querySelectorAll('.install-app-link').forEach((button) => {
    button.hidden = !canInstall;
  });
}

export function initPwa(onMessage = () => {}) {
  notify = onMessage;

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    syncInstallButtons();
  });

  window.addEventListener('appinstalled', () => {
    deferredInstallPrompt = null;
    syncInstallButtons();
    notify('TeleCloud installed.');
  });

  if ('serviceWorker' in navigator && import.meta.env.PROD) {
    navigator.serviceWorker.register('/service-worker.js')
      .catch(() => notify('Offline support could not be enabled.', 'error'));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncInstallButtons, { once: true });
  } else {
    syncInstallButtons();
  }
}

export async function promptInstall() {
  if (isStandalone()) return 'TeleCloud is already installed.';

  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    const { outcome } = await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    syncInstallButtons();
    return outcome === 'accepted' ? 'Installing TeleCloud…' : '';
  }

  if (isIos()) {
    return 'On iPhone or iPad, tap Share, then Add to Home Screen.';
  }

  return 'Use your browser menu and choose Install TeleCloud.';
}
