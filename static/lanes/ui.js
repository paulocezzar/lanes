/* In-app replacements for the browser's prompt()/confirm() boxes, which look
   out of place (and show "lanes.onrender.com says"). All return Promises.
     await LanesUI.confirm('Archive this list?', { ok: 'Archive', danger: true })  -> true/false
     await LanesUI.ask('Card limit', { value: '5', hint: '…' })                     -> string or null
     await LanesUI.choose('Which template?', [{ label, value }])                     -> value or null
   Forms with data-confirm="Question?" ask before submitting. */
(() => {
  let dlg;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function open(html, setup) {
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.className = 'dialog ui-dialog';
      document.body.appendChild(dlg);
    }
    dlg.innerHTML = html;
    return new Promise(resolve => {
      let done = false;
      const finish = v => { if (done) return; done = true; dlg.close(); resolve(v); };
      dlg.onclose = () => finish(null);
      dlg.onclick = e => { if (e.target === dlg) finish(null); };
      setup(finish);
      dlg.showModal();
    });
  }
  const head = (title, text) =>
    `<h3 class="ui-title">${esc(title)}</h3>${text ? `<p class="ui-text">${esc(text).replace(/\n/g, '<br>')}</p>` : ''}`;

  window.LanesUI = {
    confirm(message, { ok = 'OK', cancel = 'Cancel', danger = false, text = '' } = {}) {
      return open(`<form method="dialog">${head(message, text)}
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>${esc(cancel)}</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-yes autofocus>${esc(ok)}</button></div></form>`,
      finish => {
        dlg.querySelector('[data-no]').onclick = () => finish(false);
        dlg.querySelector('form').onsubmit = e => { e.preventDefault(); finish(true); };
      }).then(v => v === true);
    },
    ask(message, { value = '', placeholder = '', ok = 'Save', hint = '', type = 'text' } = {}) {
      return open(`<form method="dialog">${head(message, hint)}
        <input class="ui-input" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autofocus>
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>Cancel</button>
        <button class="btn primary">${esc(ok)}</button></div></form>`,
      finish => {
        const input = dlg.querySelector('input');
        setTimeout(() => { input.focus(); input.select(); });
        dlg.querySelector('[data-no]').onclick = () => finish(null);
        dlg.querySelector('form').onsubmit = e => { e.preventDefault(); finish(input.value); };
      });
    },
    choose(message, options, { text = '' } = {}) {
      return open(`<div>${head(message, text)}<div class="ui-choices">${options.map((o, i) =>
        `<button class="ui-choice" data-i="${i}">${esc(o.label)}</button>`).join('')}</div>
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>Cancel</button></div></div>`,
      finish => {
        dlg.querySelector('[data-no]').onclick = () => finish(null);
        dlg.querySelectorAll('[data-i]').forEach(b => (b.onclick = () => finish(options[+b.dataset.i].value)));
        setTimeout(() => dlg.querySelector('[data-i]')?.focus());
      });
    },
  };

  // <form data-confirm="Delete this rule?" data-confirm-ok="Delete">
  document.addEventListener('submit', async e => {
    const f = e.target.closest('form[data-confirm]');
    if (!f || f.dataset.confirmed) return;
    e.preventDefault();
    const ok = await LanesUI.confirm(f.dataset.confirm, { ok: f.dataset.confirmOk || 'OK', danger: 'danger' in f.dataset });
    if (ok) { f.dataset.confirmed = '1'; f.requestSubmit(e.submitter || undefined); }
  }, true);
})();

// Colour-blind friendly labels (any page): <button data-colourblind="1|0">
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-colourblind]');
  if (!b) return;
  const on = b.dataset.colourblind === '1';
  const fd = new FormData(); fd.append('colourblind', on ? '1' : '0');
  await fetch('/account/theme/', { method: 'POST', body: fd, credentials: 'same-origin',
    headers: { 'X-CSRFToken': document.querySelector('meta[name=csrf-token]')?.content } });
  document.documentElement.classList.toggle('cb', on);
  document.querySelectorAll('[data-colourblind]').forEach(x => {
    x.dataset.colourblind = on ? '0' : '1';
    const label = x.querySelector('[data-cb-label]') || x;
    label.textContent = on ? 'Turn off colour-blind friendly mode' : 'Enable colour-blind friendly mode';
  });
});

// Top search: recent boards when empty; as you type, cards, boards, people, departments and templates.
(() => {
  const input = document.querySelector('[data-top-search]');
  const drop = document.querySelector('[data-search-drop]');
  if (!input || !drop) return;
  let recent = null, timer = null, seq = 0, active = -1;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const all = q => '/search/?q=' + encodeURIComponent(q);
  const open = html => { drop.innerHTML = html; drop.hidden = false; input.setAttribute('aria-expanded', 'true'); active = -1; };
  const close = () => { drop.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const mark = (t, q) => {
    let out = esc(t);
    q.split(/\s+/).filter(w => w.length > 1 && !w.includes(':')).forEach(w => {
      out = out.replace(new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/[&<>"]/g, '') + ')', 'ig'), '<mark>$1</mark>');
    });
    return out;
  };
  const ICON = {
    card: '<span class="sd-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 15h18"/></svg></span>',
  };
  const row = (r, q, lead) => `<a class="sd-row" role="option" href="${esc(r.url)}">${lead}<span><b>${mark(r.title, q)}</b><small>${esc(r.where)}</small></span></a>`;
  async function showRecent() {
    if (!recent) {
      try { recent = (await (await fetch('/api/boards/recent/', { credentials: 'same-origin' })).json()).boards || []; }
      catch { recent = []; }
    }
    if (input.value.trim()) return;
    open((recent.length ? '<div class="sd-head">Recent boards</div>' + recent.map(b =>
      `<a class="sd-row" role="option" href="/b/${b.id}/"><span class="sd-thumb bg-${esc(b.bg)}"></span><span><b>${esc(b.title)}</b><small>${esc(b.where)}</small></span></a>`).join('') : '')
      + '<div class="sd-foot">Type to search cards, boards, people and templates · <a href="/search/">Advanced search</a></div>');
  }
  async function suggest() {
    const q = input.value.trim();
    if (!q) return showRecent();
    if (q.length < 2) return close();
    const mine = ++seq;
    let d;
    try {
      const r = await fetch('/api/search/quick/?q=' + encodeURIComponent(q), { credentials: 'same-origin' });
      if (!r.ok) throw new Error(r.status);
      d = await r.json();
    } catch {
      if (mine === seq) open(`<div class="sd-empty">Couldn't search just now.</div><a class="sd-all" role="option" href="${all(q)}">Try the full search for “${esc(q)}”</a>`);
      return;
    }
    if (mine !== seq || input.value.trim() !== q) return;
    const parts = [];
    if (d.cards.length) parts.push('<div class="sd-head">Cards</div>' + d.cards.map(c => row(c, q, ICON.card).replace('class="sd-row"', 'class="sd-row' + (c.done ? ' done' : '') + '"')).join(''));
    if (d.boards.length) parts.push('<div class="sd-head">Boards</div>' + d.boards.map(b => row(b, q, `<span class="sd-thumb bg-${esc(b.bg)}"></span>`)).join(''));
    if (d.people.length) parts.push('<div class="sd-head">People</div>' + d.people.map(p => row(p, q, `<span class="avatar sm" style="--h:${+p.hue || 0}">${esc(p.initials)}</span>`)).join(''));
    if (d.departments.length) parts.push('<div class="sd-head">Departments</div>' + d.departments.map(w => row(w, q, `<span class="ws-badge sm">${esc(w.initial)}</span>`)).join(''));
    if (d.templates.length) parts.push('<div class="sd-head">Templates</div>' + d.templates.map(t => row(t, q, `<span class="sd-thumb bg-${esc(t.bg)}"></span>`)).join(''));
    const none = parts.length ? '' : `<div class="sd-empty">Nothing matches “${esc(q)}” by name. Press Enter to look inside descriptions, checklists and comments too.</div>`;
    open(parts.join('') + none
      + `<a class="sd-all" role="option" href="${all(q)}">View all results for “${esc(q)}”</a>`
      + `<a class="sd-adv" role="option" href="/search/"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>Advanced search<kbd>↵</kbd></a>`);
  }
  const opts = () => [...drop.querySelectorAll('[role="option"]')];
  function move(by) {
    const o = opts(); if (!o.length) return;
    active = (active + by + o.length) % o.length;
    o.forEach((el, i) => el.classList.toggle('on', i === active));
    o[active].scrollIntoView({ block: 'nearest' });
  }
  input.addEventListener('focus', () => (input.value.trim() ? suggest() : showRecent()));
  input.addEventListener('input', () => {
    clearTimeout(timer);
    if (input.value.trim().length >= 2 && drop.querySelector('.sd-head')?.textContent === 'Recent boards') open('<div class="sd-empty">Searching…</div>');
    timer = setTimeout(suggest, 160);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { close(); input.blur(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); if (drop.hidden) suggest(); else move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); opts()[active].click(); }
  });
  document.addEventListener('click', e => { if (!e.target.closest('.top-search')) close(); });
})();

// Search box inside a popover: <input data-pop-filter=".row-selector"> hides rows that don't match.
document.addEventListener('input', e => {
  const f = e.target.closest?.('[data-pop-filter]');
  if (!f) return;
  const q = f.value.trim().toLowerCase();
  const scope = f.closest('.pop, [data-pop-panel]') || f.parentElement;
  scope.querySelectorAll(f.dataset.popFilter).forEach(row => { row.hidden = !!q && !row.textContent.toLowerCase().includes(q); });
});

// The logo goes home; on the home page it goes back to wherever you were before (a toggle, like Trello).
(() => {
  const brand = document.querySelector('.topbar .brand');
  if (!brand) return;
  const KEY = 'lanes:last-page';
  const homePath = new URL(brand.href, location.href).pathname;
  const atHome = location.pathname === homePath && !location.search;
  try {
    if (!atHome) sessionStorage.setItem(KEY, location.pathname + location.search + location.hash);
  } catch {}
  if (!atHome) return;
  let back = null;
  try { back = sessionStorage.getItem(KEY); } catch {}
  if (!back || back === homePath) return;
  brand.title = 'Back to where you were';
  brand.addEventListener('click', e => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    location.href = back;
  });
})();
