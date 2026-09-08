// Light/dark theme toggle. The initial theme is applied synchronously by an
// inline script in index.html (before this module loads) so the page never
// flashes the wrong theme; this module only handles the toggle interaction.

import { renderIcons } from './ui.js';

const STORAGE_KEY = 'theme';

function updateToggleIcon(theme) {
  // There can be several toggles (floating button + sidebar) — update them all.
  document.querySelectorAll('.theme-icon').forEach((icon) => {
    icon.setAttribute('data-lucide', theme === 'dark' ? 'sun' : 'moon');
    renderIcons(icon.parentElement);
  });
}

export function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem(STORAGE_KEY, next);
  updateToggleIcon(next);
}
