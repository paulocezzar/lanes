/* The bell in the top bar (every page) and @mention autocomplete in comments. */
(() => {
  const csrf = document.querySelector('meta[name=csrf-token]')?.content;
  const bell = document.getElementById('bell');
  const countEl = bell?.querySelector('[data-bell-count]');
  const panel = bell?.querySelector('[data-bell-panel]');
  const list = bell?.querySelector('[data-bell-list]');

  function setCount(n) {
    if (!countEl) return;
    countEl.textContent = n > 99 ? '99+' : n;
    countEl.hidden = !n;
    document.title = document.title.replace(/^\(\d+\+?\) /, '');
    if (n) document.title = `(${n > 99 ? '99+' : n}) ${document.title}`;
  }

  async function refresh(withHtml) {
    try {
      const res = await fetch(`/api/notifications/${withHtml ? '?html=1' : ''}`, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      setCount(data.unread);
      if (withHtml && list) list.innerHTML = data.html;
    } catch { /* offline — try again later */ }
  }

  async function markRead(id) {
    const res = await fetch('/api/notifications/read/', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(id ? { id } : { all: true }),
    });
    if (res.ok) setCount((await res.json()).unread);
  }

  if (bell) {
    bell.querySelector('[data-bell]').addEventListener('click', e => {
      e.stopPropagation();
      panel.hidden = !panel.hidden;
      if (!panel.hidden) refresh(true);
    });
    document.addEventListener('click', e => { if (!bell.contains(e.target)) panel.hidden = true; });
    setCount(+countEl.textContent || 0);
    setInterval(() => { if (!document.hidden) refresh(!panel.hidden); }, 30000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(false); });
  }
  document.querySelectorAll('[data-bell-readall]').forEach(b => b.addEventListener('click', async () => {
    await markRead();
    document.querySelectorAll('.notif.unread').forEach(n => n.classList.remove('unread'));
    document.querySelectorAll('.notif-dot').forEach(d => d.remove());
  }));

  // ---------- @mention autocomplete (works inside the card dialog, which is re-rendered) ----------
  let pop = null, field = null, people = [], start = -1, active = 0, matches = [];

  function close() { if (pop) pop.hidden = true; start = -1; }

  function render() {
    pop.innerHTML = matches.map((p, i) =>
      `<button type="button" class="mention-opt${i === active ? ' on' : ''}" data-i="${i}">` +
      `<span class="avatar sm" style="--h:${p.hue}">${p.initials}</span>${p.name.replace(/</g, '&lt;')}</button>`).join('');
    pop.hidden = !matches.length;
  }

  function pick(i) {
    const p = matches[i];
    if (!p || start < 0) return;
    const before = field.value.slice(0, start), after = field.value.slice(field.selectionStart);
    const insert = `@${p.name} `;
    field.value = before + insert + after;
    const pos = before.length + insert.length;
    field.setSelectionRange(pos, pos);
    field.focus();
    close();
  }

  document.addEventListener('input', e => {
    const ta = e.target.closest('[data-comment-new] textarea');
    if (!ta) return;
    const form = ta.closest('[data-comment-new]');
    field = ta;
    pop = form.querySelector('[data-mention-pop]');
    try { people = JSON.parse(form.dataset.people || '[]'); } catch { people = []; }
    const upto = ta.value.slice(0, ta.selectionStart);
    const m = upto.match(/(?:^|\s)@([\w.' -]{0,30})$/);
    if (!m) return close();
    start = upto.length - m[1].length - 1;
    const q = m[1].toLowerCase();
    matches = people.filter(p => p.name.toLowerCase().startsWith(q) ||
      p.name.toLowerCase().split(' ').some(w => w.startsWith(q))).slice(0, 6);
    active = 0;
    render();
  });

  document.addEventListener('keydown', e => {
    if (!pop || pop.hidden || !e.target.closest('[data-comment-new] textarea')) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); active = (active + 1) % matches.length; render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = (active - 1 + matches.length) % matches.length; render(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); pick(active); }
    else if (e.key === 'Escape') { e.stopPropagation(); close(); }
  }, true);

  document.addEventListener('mousedown', e => {
    const opt = e.target.closest('.mention-opt');
    if (opt) { e.preventDefault(); pick(+opt.dataset.i); }
  });
})();
