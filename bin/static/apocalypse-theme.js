// Apocalypse shared theme switcher.
// Applies persisted theme on load and wires the #themeSwitcher click handler.
// Pages that use this script must include a button row with id="themeSwitcher"
// containing buttons with data-set="<theme name>".
(function () {
  const THEMES = ['terminal', 'paper', 'forest', 'solarized', 'geocities'];

  function applyTheme(name) {
    if (!THEMES.includes(name)) name = 'terminal';
    document.body.dataset.theme = name;
    document.body.dataset.crt = (name === 'terminal') ? '1' : '0';
    document.querySelectorAll('#themeSwitcher button').forEach(b => {
      b.classList.toggle('active', b.dataset.set === name);
    });
    // Geocities-only DOM toggles, harmless on pages that lack these classes
    const showGeo = (name === 'geocities');
    document.querySelectorAll('.geocities-marquee, .geocities-construction, .geocities-footer').forEach(el => {
      el.style.display = showGeo ? '' : 'none';
    });
    try { localStorage.setItem('apocalypse-theme', name); } catch (e) {}
  }

  function init() {
    const saved = (() => {
      try { return localStorage.getItem('apocalypse-theme') || 'terminal'; }
      catch (e) { return 'terminal'; }
    })();
    applyTheme(saved);
    const sw = document.getElementById('themeSwitcher');
    if (sw) {
      sw.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-set]');
        if (btn) applyTheme(btn.dataset.set);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose so pages can re-apply manually if they inject content
  window.APOC_THEME = { apply: applyTheme, list: THEMES };
})();
