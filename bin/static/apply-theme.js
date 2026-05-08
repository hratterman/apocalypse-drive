/* apply-theme.js set <html data-theme> from /api/status, with localStorage cache.
   Drop on any page that uses themes.css. Persists across reloads even
   if the shim is briefly unreachable. */
(function () {
  const STORAGE_KEY = 'apocalypse-theme';
  const VALID = ['terminal', 'paper', 'forest', 'solarized', 'geocities'];

  // Apply cached theme immediately so we don't flash the default.
  const cached = localStorage.getItem(STORAGE_KEY);
  if (cached && VALID.indexOf(cached) !== -1) {
    document.documentElement.setAttribute('data-theme', cached);
  }

  // Then refresh from the server. If status returns a different theme,
  // re-apply (e.g. user just changed it on the landing page).
  fetch('/api/status')
    .then(r => r.json())
    .then(d => {
      const t = d && d.theme;
      if (t && VALID.indexOf(t) !== -1) {
        if (t !== cached) document.documentElement.setAttribute('data-theme', t);
        localStorage.setItem(STORAGE_KEY, t);
      }
    })
    .catch(() => {/* offline / no shim keep cached */});
})();
