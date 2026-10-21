/* Keyboard shortcuts that work on every page. Board and card shortcuts live
   in board.js (they need the board's internals). Press ? to see them all. */
(() => {
  const dlg = document.getElementById('shortcuts-dialog');
  window.LanesKeys = {
    typing(e) {
      const t = e.target;
      return e.ctrlKey || e.metaKey || e.altKey || !!(t && (t.isContentEditable ||
        t.closest('input, textarea, select, [contenteditable]')));
    },
  };
  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || window.LanesKeys.typing(e)) return;
    if (e.key === '?') { e.preventDefault(); if (dlg && !dlg.open) dlg.showModal(); return; }
    if (e.key === '/') {
      const s = document.querySelector('.top-search input');
      if (s && s.offsetParent) { e.preventDefault(); s.focus(); s.select(); }
      return;
    }
    if (document.querySelector('dialog[open]')) return;   // let the open dialog handle its own keys
    const go = { b: '/', p: '/planner/', a: '/my-cards/' }[e.key];
    if (go) { e.preventDefault(); location.href = go; }
  });
  dlg?.addEventListener('click', e => { if (e.target === dlg || e.target.closest('[data-close]')) dlg.close(); });
})();
