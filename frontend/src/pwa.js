let deferredInstallPrompt = null;
let notify = () => {};

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
}

function isAndroidContainer() {
  return typeof window.TeleCloudAndroid !== 'undefined';
}

async function removeBrowserAppCaches() {
  const tasks = [];
  if ('serviceWorker' in navigator) {
    tasks.push(
      navigator.serviceWorker.getRegistrations()
        .then((registrations) => Promise.all(registrations.map((registration) => registration.unregister()))),
    );
  }
  if ('caches' in window) {
    tasks.push(
      caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))),
    );
  }
  await Promise.allSettled(tasks);
}

function isIos() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

function syncInstallButtons() {
  const canInstall = !isAndroidContainer()
    && !isStandalone()
    && (Boolean(deferredInstallPrompt) || isIos());
  document.querySelectorAll('.install-app-link').forEach((button) => {
    button.hidden = !canInstall;
  });
}

export function initPwa(onMessage = () => {}) {
  notify = onMessage;

  // The Android APK already provides the installable app shell. Keeping a web
  // service worker inside its WebView can preserve an old JavaScript bundle
  // across APK upgrades, so remove browser-level app caches in the container.
  if (isAndroidContainer()) {
    removeBrowserAppCaches();
    syncInstallButtons();
    return;
  }

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
