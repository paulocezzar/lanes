/* Lanes board page. The server owns all state and returns rendered HTML;
   this file wires up drag-and-drop, inline editing and the card dialog. */
(() => {
  const boardEl = document.getElementById('board');
  if (!boardEl) return;
  const boardId = boardEl.dataset.boardId;
  const view = boardEl.dataset.view || 'board';
  const canEdit = boardEl.dataset.canEdit === '1';
  const csrf = document.querySelector('meta[name=csrf-token]').content;
  const listsEl = document.getElementById('lists') || document.createElement('div');  // other views have no lists
  const dialog = document.getElementById('card-dialog');
  const dialogBody = document.getElementById('card-dialog-body');
  // A missing root (e.g. the Set up dialog, which only the Board view has) finds nothing rather than throwing.
  const $ = (sel, root = document) => (root ? root.querySelector(sel) : null);
  const $$ = (sel, root = document) => (root ? [...root.querySelectorAll(sel)] : []);

  // ---------- helpers ----------
  const toastEl = document.getElementById('toast');
  let toastTimer;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.hidden = true), 3500);
  }

  async function post(url, data = {}) {
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify(data),
        credentials: 'same-origin',
      });
    } catch {
      if (window.LanesOffline?.canQueue(url)) {      // keep it on this device; it's sent when we're back online
        window.LanesOffline.queue(url, data);
        const msg = 'Saved on this device — it will be sent when you\'re back online.';
        toast(msg);
        const err = new Error(msg); err.queued = true; throw err;
      }
      toast(navigator.onLine ? 'Connection lost — your change was not saved.' : 'You\'re offline — this needs a connection.');
      throw new Error('network');
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 409 && body.conflict) {           // someone else saved first — caller decides
        const err = new Error('conflict'); err.conflict = body; throw err;
      }
      toast(body.error || 'Something went wrong.');
      throw new Error(body.error || res.status);
    }
    return body;
  }

  function fromHTML(html) {
    const t = document.createElement('template');
    t.innerHTML = String(html ?? '').trim();
    return t.content.firstElementChild;
  }

  function neighbours(el, selector) {
    let prev = el.previousElementSibling, next = el.nextElementSibling;
    while (prev && !prev.matches(selector)) prev = prev.previousElementSibling;
    while (next && !next.matches(selector)) next = next.nextElementSibling;
    if (selector === '.list') {
      return { before_id: prev ? +prev.dataset.listId : null, after_id: next ? +next.dataset.listId : null };
    }
    // cards: tokens so mirrored cards ('m12') and cards ('c34') can sit side by side
    return { before: prev ? prev.dataset.token : null, after: next ? next.dataset.token : null };
  }

  function refreshCounts() {
    $$('.list').forEach(l => {
      const c = $('.list-count', l);
      if (!c) return;
      const n = $$('.card-tile', l).length, lim = +l.dataset.limit || 0;
      c.textContent = lim ? `${n}/${lim}` : n;
      c.classList.toggle('over', lim > 0 && n > lim);
    });
  }

  // Inline edit for contenteditable titles: Enter saves, Escape reverts.
  function inlineEdit(el, save) {
    let original = el.textContent;
    el.addEventListener('focus', () => (original = el.textContent));
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
      if (e.key === 'Escape') { el.textContent = original; el.blur(); }
    });
    el.addEventListener('blur', async () => {
      const value = el.textContent.trim();
      if (!value) { el.textContent = original; return; }
      if (value === original.trim()) return;
      try { await save(value); original = value; } catch { el.textContent = original; }
    });
  }

  // ---------- drag and drop ----------
  let dragging = false;           // live updates wait while a drag is in progress
  function makeCardsSortable(container) {
    if (!canEdit) return;
    Sortable.create(container, {
      group: 'cards',
      animation: 150,
      forceFallback: true,        // same drag feel in every browser, incl. iPad
      fallbackOnBody: true,
      fallbackTolerance: 4,
      delay: 120,                 // small hold on touch screens so scrolling still works
      delayOnTouchOnly: true,
      ghostClass: 'sortable-ghost',
      dragClass: 'sortable-drag',
      emptyInsertThreshold: 40,   // lets you drop into an empty list
      onStart: () => (dragging = true),
      onEnd: async evt => {
        dragging = false;
        const tile = evt.item;
        const listEl = tile.closest('.list');
        if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
        refreshCounts();
        try {
          const url = tile.dataset.mirrorId ? `/api/mirrors/${tile.dataset.mirrorId}/move/`
                                            : `/api/cards/${tile.dataset.cardId}/move/`;
          const data = await post(url, { list_id: +listEl.dataset.listId, ...neighbours(tile, '.card-tile') });
          tile.replaceWith(fromHTML(data.tile));
        } catch {
          location.reload();      // server refused: snap back to the truth
        }
      },
    });
  }

  function wireList(listEl) {
    makeCardsSortable($('[data-list-cards]', listEl));
    const title = $('[data-list-title]', listEl);
    if (title) inlineEdit(title, v => post(`/api/lists/${listEl.dataset.listId}/`, { title: v }));
  }

  $$('.list', listsEl).forEach(wireList);

  if (canEdit) {
    Sortable.create(listsEl, {
      animation: 150,
      handle: '.list-head',
      draggable: '.list',
      forceFallback: true,
      fallbackOnBody: true,
      fallbackTolerance: 4,
      filter: '.list-title, .menu, .list-collapse, input, textarea',
      preventOnFilter: false,
      ghostClass: 'sortable-ghost',
      delay: 150,
      delayOnTouchOnly: true,
      onStart: () => (dragging = true),
      onEnd: async evt => {
        dragging = false;
        if (evt.oldIndex === evt.newIndex) return;
        const l = evt.item;
        try { await post(`/api/lists/${l.dataset.listId}/move/`, neighbours(l, '.list')); }
        catch { location.reload(); }
      },
    });
  }

  // ---------- add list / add card ----------
  const addList = document.getElementById('add-list');
  if (addList) {
    const btn = $('[data-add-list]', addList), form = $('[data-add-list-form]', addList), input = $('input', form);
    btn.addEventListener('click', () => { btn.hidden = true; form.hidden = false; input.focus(); });
    $('[data-cancel]', form).addEventListener('click', () => { form.hidden = true; btn.hidden = false; });
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const title = input.value.trim();
      if (!title) return;
      const data = await post(`/api/boards/${boardId}/lists/`, { title });
      const el = fromHTML(data.html);
      listsEl.insertBefore(el, addList);
      wireList(el);
      input.value = '';
      input.focus();
      listsEl.scrollLeft = listsEl.scrollWidth;
    });
  }

  listsEl.addEventListener('click', async e => {
    const listEl = e.target.closest('.list');
    if (!listEl) return;
    if (e.target.closest('[data-add-card], [data-add-card-top]')) {
      e.target.closest('details')?.removeAttribute('open');
      openAddCard(listEl);
    } else if (e.target.closest('[data-cancel]') && e.target.closest('[data-add-card-form]')) {
      closeAddCard(listEl);
    } else if (e.target.closest('[data-list-pin]')) {
      const on = e.target.closest('[data-list-pin]').dataset.listPin === '1';
      await post(`/api/lists/${listEl.dataset.listId}/`, { is_pinned: on });
      e.target.closest('details')?.removeAttribute('open');
      toast(on ? '📌 Pinned — this list stays on the left as you scroll.' : 'Unpinned.');
      forceRefresh();
    } else if (e.target.closest('[data-list-colour]')) {
      await post(`/api/lists/${listEl.dataset.listId}/`, { colour: e.target.closest('[data-list-colour]').dataset.listColour });
      e.target.closest('details')?.removeAttribute('open');
      forceRefresh();
    } else if (e.target.closest('[data-list-limit]')) {
      e.target.closest('details')?.removeAttribute('open');
      const cur = listEl.dataset.limit || '';
      const v = await LanesUI.ask('Card limit for this list', { value: cur, type: 'number', hint: 'The count turns red when the list has more cards than this. Leave empty for no limit.' });
      if (v === null) return;
      await post(`/api/lists/${listEl.dataset.listId}/`, { card_limit: v.trim() });
      forceRefresh();
    } else if (e.target.closest('[data-collapse-list]')) {
      e.target.closest('details')?.removeAttribute('open');
      toggleCollapsed(listEl.dataset.listId, true);
    } else if (e.target.closest('[data-expand-list]')) {
      toggleCollapsed(listEl.dataset.listId, false);
    } else if (e.target.closest('[data-new-product]')) {
      e.target.closest('details')?.removeAttribute('open');
      openProductDialog(listEl.dataset.listId);
    } else if (e.target.closest('[data-list-copy]')) {
      e.target.closest('details')?.removeAttribute('open');
      const t = await LanesUI.ask('Copy list', { value: `${$('.list-title', listEl).textContent.trim()} (copy)`, ok: 'Copy', hint: 'All its cards are copied too.' });
      if (t === null) return;
      await post(`/api/lists/${listEl.dataset.listId}/copy/`, { title: t });
      forceRefresh(); toast('List copied.');
    } else if (e.target.closest('[data-list-move-board]')) {
      e.target.closest('details')?.removeAttribute('open');
      const res = await fetch('/api/boards/mine/', { credentials: 'same-origin' });
      const boards = (await res.json()).boards.filter(b => b.can_edit && String(b.id) !== String(boardId));
      if (!boards.length) return toast("There's no other board you can move it to.");
      const to = await LanesUI.choose('Move this list to which board?',
        boards.map(b => ({ label: `${b.starred ? '★ ' : ''}${b.title} · ${b.dept}`, value: b.id })),
        { text: 'Its cards come too. Labels and fields are matched by name on the new board.' });
      if (to === null) return;
      const d = await post(`/api/lists/${listEl.dataset.listId}/move-board/`, { board_id: to });
      listEl.remove(); refreshCounts();
      toast(`Moved to ${d.board} with ${d.moved} card${d.moved === 1 ? '' : 's'}.`);
    } else if (e.target.closest('[data-list-move-cards]')) {
      e.target.closest('details')?.removeAttribute('open');
      const others = $$('.list', listsEl).filter(l => l !== listEl)
        .map(l => ({ label: $('.list-title', l).textContent.trim(), value: l.dataset.listId }));
      if (!others.length) return toast('Add another list first.');
      const to = await LanesUI.choose('Move every card in this list to…', others);
      if (to === null) return;
      const d = await post(`/api/lists/${listEl.dataset.listId}/move-cards/`, { list_id: +to });
      forceRefresh(); toast(`Moved ${d.moved} card${d.moved === 1 ? '' : 's'}.`);
    } else if (e.target.closest('[data-list-sort]')) {
      e.target.closest('details')?.removeAttribute('open');
      const by = await LanesUI.choose('Sort this list by…', [
        { label: '🕒 Due date (soonest first)', value: 'due' }, { label: '🆕 Newest first', value: 'newest' },
        { label: '📜 Oldest first', value: 'oldest' }, { label: '🔤 Card name (A–Z)', value: 'name' }]);
      if (by === null) return;
      await post(`/api/lists/${listEl.dataset.listId}/sort/`, { by });
      forceRefresh();
    } else if (e.target.closest('[data-archive-cards]')) {
      e.target.closest('details')?.removeAttribute('open');
      const n = $$('.card-tile:not([data-mirror-id])', listEl).length;
      if (!n) return toast('There are no cards in this list.');
      if (!await LanesUI.confirm(`Archive all ${n} card${n === 1 ? '' : 's'} in this list?`, { text: 'You can bring them back from Menu → Archived.', ok: 'Archive cards', danger: true })) return;
      const d = await post(`/api/lists/${listEl.dataset.listId}/archive-cards/`);
      forceRefresh(); toast(`Archived ${d.archived} card${d.archived === 1 ? '' : 's'}.`);
    } else if (e.target.closest('[data-archive-list]')) {
      if (!await LanesUI.confirm('Archive this list?', { text: 'Its cards are hidden too. You can bring it back from Menu → Archived.', ok: 'Archive', danger: true })) return;
      await post(`/api/lists/${listEl.dataset.listId}/`, { archive: true });
      listEl.remove();
    } else {
      const tile = e.target.closest('.card-tile');
      if (tile) openCard(tile.dataset.cardId);
    }
  });

  // Spot a date while typing a new card's title ("tomorrow", "next Friday 3pm", "12 June")
  let smartTimer;
  listsEl.addEventListener('input', e => {
    const ta = e.target.closest('[data-add-card-form] textarea');
    if (!ta) return;
    const chip = $('[data-smart-due]', ta.form);
    clearTimeout(smartTimer);
    smartTimer = setTimeout(async () => {
      const q = ta.value.trim();
      if (q.length < 3) { chip.hidden = true; return; }
      try {
        const res = await fetch(`/api/parse-date/?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' });
        const d = await res.json();
        if (!d.due) { chip.hidden = true; return; }
        if (chip.dataset.off && chip.dataset.for === d.words) return;   // user dismissed this one
        delete chip.dataset.off;
        chip.dataset.for = d.words;
        $('[data-smart-label]', chip).textContent = `${d.label} (“${d.words}”)`;
        chip.hidden = false;
      } catch { chip.hidden = true; }
    }, 250);
  });
  listsEl.addEventListener('click', e => {
    const off = e.target.closest('[data-smart-off]');
    if (!off) return;
    const chip = off.closest('[data-smart-due]');
    chip.dataset.off = '1'; chip.hidden = true;
    off.closest('form').querySelector('textarea').focus();
  });

  listsEl.addEventListener('keydown', e => {
    const tile = e.target.closest('.card-tile');
    if (tile && e.key === 'Enter') openCard(tile.dataset.cardId);
    const ta = e.target.closest('[data-add-card-form] textarea');
    if (ta && e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ta.form.requestSubmit(); }
    if (ta && e.key === 'Escape') closeAddCard(ta.closest('.list'));
  });

  listsEl.addEventListener('submit', async e => {
    const form = e.target.closest('[data-add-card-form]');
    if (!form) return;
    e.preventDefault();
    const listEl = form.closest('.list');
    const ta = $('textarea', form);
    const title = ta.value.trim();
    if (!title) return;
    const chip = $('[data-smart-due]', form);
    const smart = !chip.hidden && !chip.dataset.off;
    const data = await post(`/api/lists/${listEl.dataset.listId}/cards/`, { title, smart_due: smart });
    chip.hidden = true; delete chip.dataset.off; delete chip.dataset.for;
    const cards = $('[data-list-cards]', listEl);
    cards.appendChild(fromHTML(data.tile));
    cards.scrollTop = cards.scrollHeight;
    ta.value = '';
    ta.focus();
    refreshCounts();
  });

  function openAddCard(listEl) {
    $('.foot-row', listEl).hidden = true;
    const form = $('[data-add-card-form]', listEl);
    form.hidden = false;
    $('textarea', form).focus();
  }
  function closeAddCard(listEl) {
    $('[data-add-card-form]', listEl).hidden = true;
    $('.foot-row', listEl).hidden = false;
  }

  // ---------- card dialog ----------
  let openCardId = null;
  let openPop = null;

  window.LanesOpenCard = id => openCard(id);
  // the bottom bar can be tucked away (remembered on this device)
  const dock = $('.dock');
  function setDockMini(mini) {
    if (!dock) return;
    dock.classList.toggle('mini', mini);
    document.body.classList.toggle('dock-mini', mini);
    const show = $('[data-dock-show]', dock); if (show) show.hidden = !mini;
    try { localStorage.setItem('lanes-dock-mini', mini ? '1' : ''); } catch { /* private mode */ }
  }
  try { if (localStorage.getItem('lanes-dock-mini')) setDockMini(true); } catch { /* ignore */ }
  dock?.addEventListener('click', e => {
    if (e.target.closest('[data-dock-hide]')) setDockMini(true);
    else if (e.target.closest('[data-dock-show]')) setDockMini(false);
  });
  let hintTimer = null;
  dialogBody.addEventListener('input', e => {
    const input = e.target.closest('[data-item-new] input');
    if (!input) return;
    const box = input.closest('[data-checklist]'), hint = $('[data-item-hint]', box);
    if (!hint) return;
    hint.dataset.tip ??= hint.innerHTML;
    clearTimeout(hintTimer);
    if (!input.value.trim()) { hint.innerHTML = hint.dataset.tip; hint.classList.remove('live'); return; }
    hintTimer = setTimeout(async () => {
      try {
        const d = await post(`/api/checklists/${box.dataset.checklist}/items/`, { text: input.value, preview: true });
        hint.innerHTML = `→ ${esc(d.hint)}`; hint.classList.add('live');
      } catch { /* keep the old hint */ }
    }, 200);
  });
  dialogBody.addEventListener('paste', e => {
    const input = e.target.closest('[data-item-new] input');
    const text = e.clipboardData?.getData('text') || '';
    if (!input || !/\n/.test(text.trim())) return;
    e.preventDefault();
    const clId = input.closest('[data-checklist]').dataset.checklist;
    post(`/api/checklists/${clId}/items/`, { text }).then(d => { applyCard(d); toast(`Added ${text.trim().split(/\n+/).length} items.`); })
      .catch(err => toast(err.message || 'Could not add those.'));
  });

  // ---------- set up cards: describe / paste anything (AI) or type several cards at once ----------
  const setupDlg = document.getElementById('setup-dialog');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let previewTimer = null, shots = [];
  function suTab(name) {
    $$('[data-su-tab]', setupDlg).forEach(b => b.classList.toggle('on', b.dataset.suTab === name));
    $$('[data-su-pane]', setupDlg).forEach(p => { p.hidden = p.dataset.suPane !== name; });
    $('[data-su-create]', setupDlg).hidden = name !== 'type';
    (name === 'type' ? $('textarea[name=text]', setupDlg) : $('textarea[name=source]', setupDlg))?.focus();
  }
  function openSetup(listId) {
    if (!setupDlg) return;
    const f = $('[data-setup-form]', setupDlg);
    if (listId) f.list_id.value = listId;
    setupDlg.showModal();
    suTab($('[data-su-pane="ai"] textarea', setupDlg) && !f.text.value.trim() ? 'ai' : 'type');
  }
  function openProductDialog(listId) { openSetup(listId); }
  window.LanesSetupCards = openSetup;
  document.addEventListener('click', e => { if (e.target.closest('[data-setup-board]')) { $('[data-close-drawer]')?.click(); openSetup(); } });
  setupDlg?.addEventListener('click', e => {
    const tab = e.target.closest('[data-su-tab]');
    if (tab) return suTab(tab.dataset.suTab);
    const x = e.target.closest('[data-su-shot-x]');
    if (x) { shots.splice(+x.dataset.suShotX, 1); renderShots(); }
  });
  // screenshots: shrink in the browser so they upload quickly
  function shrink(file) {
    return new Promise((ok, bad) => {
      const img = new Image();
      img.onload = () => {
        const k = Math.min(1, 1600 / Math.max(img.width, img.height));
        const c = document.createElement('canvas'); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        const url = c.toDataURL('image/jpeg', 0.85); URL.revokeObjectURL(img.src);
        ok({ media_type: 'image/jpeg', data: url.split(',')[1], preview: url });
      };
      img.onerror = () => bad(new Error('That image could not be read.'));
      img.src = URL.createObjectURL(file);
    });
  }
  function renderShots() {
    const box = $('[data-su-shots]', setupDlg);
    if (box) box.innerHTML = shots.map((s, i) => `<span class="su-shot"><img src="${s.preview}" alt=""><button type="button" data-su-shot-x="${i}" aria-label="Remove">×</button></span>`).join('');
  }
  async function addShots(files) {
    for (const f of files) {
      if (!f.type.startsWith('image/')) continue;
      if (shots.length >= 4) { toast('Up to 4 screenshots.'); break; }
      try { shots.push(await shrink(f)); } catch (err) { toast(err.message); }
    }
    renderShots();
  }
  $('[data-su-file]', setupDlg)?.addEventListener('change', e => { addShots([...e.target.files]); e.target.value = ''; });
  $('textarea[name=source]', setupDlg)?.addEventListener('paste', e => {
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
    if (files.length) { e.preventDefault(); addShots(files); }
  });
  $('[data-su-draft]', setupDlg)?.addEventListener('click', async e => {
    const btn = e.currentTarget, f = $('[data-setup-form]', setupDlg);
    const text = f.source.value.trim();
    if (!text && !shots.length) return toast('Paste or describe what you want to set up first.');
    btn.disabled = true; const label = btn.textContent; btn.textContent = 'Drafting… (up to a minute)';
    try {
      const d = await post(`/api/boards/${boardId}/setup/draft/`, { text, images: shots.map(s => ({ media_type: s.media_type, data: s.data })) });
      f.text.value = d.text; suTab('type'); preview();
      toast('Drafted — check it, change anything, then Create cards.');
    } catch (err) { toast(err.message || 'Could not draft that.'); }
    finally { btn.disabled = false; btn.textContent = label; }
  });
  async function preview() {
    const f = $('[data-setup-form]', setupDlg), box = $('[data-su-preview]', setupDlg), go = $('[data-su-create]', setupDlg);
    if (!f.text.value.trim()) { box.innerHTML = ''; go.disabled = true; go.textContent = 'Create cards'; return; }
    try {
      const d = await post(`/api/boards/${boardId}/setup/`, { text: f.text.value, preview: true });
      go.disabled = !d.cards.length; go.textContent = `Create ${d.cards.length} card${d.cards.length === 1 ? '' : 's'}`;
      box.innerHTML = d.cards.map(p => `<div class="su-card">${p.list ? `<div class="muted small">→ ${esc(p.list)}</div>` : ''}<h4>${esc(p.title)}${p.planned ? ` <span class="muted">· ${esc(p.planned)} ${esc(p.unit)}</span>` : ''}</h4>` +
        (p.fields.length ? `<div class="np-fields">${p.fields.map(x => `<span title="Saved as a field on the card">🏷 <b>${esc(x.name)}</b> ${esc(x.value)}</span>`).join('')}</div>` : '') +
        (p.notes ? `<p class="muted small">${esc(p.notes)}</p>` : '') +
        (!p.planned && p.sections.some(s => s.items.some(i => i.kind === 'per')) ? `<p class="su-warn">Lines with “x” need an amount on the first line, e.g. “${esc(p.title)} — 200 each”.</p>` : '') +
        p.sections.map(s => `<div class="np-sec"><b>${esc(s.title)}</b>${s.items.map(i =>
          `<div class="np-item${i.done ? ' done' : ''}"><span>${i.kind === 'tick' ? '☐' : '＃'} ${esc(i.text)}</span><span class="muted">${esc(i.explain)}</span></div>`).join('')}</div>`).join('') + '</div>').join('');
    } catch (err) { box.innerHTML = `<p class="muted small">${esc(err.message)}</p>`; go.disabled = true; }
  }
  const EXAMPLES = {
    event: `Summer fair — 200 guests
Deadline 4 July

Setup:
1 x Chair
0.125 x Table
Food:
30 kg Salad
1 x Plate
Book the band`,
    production: `Gift hampers — 300 hampers
Deadline 1 December

Packaging:
1 x Wicker basket
2 x Tissue paper sheet
1 x Label
Make:
[x] 60 kg Fudge
1 x Fill hamper
Finish:
1 x Wrap
1 x Pack`,
    order: `Weekly stock order
Due 30 October

Dry goods:
25 kg Flour
10 kg Sugar
Dairy:
40 l Milk
Check the delivery against the invoice`,
    many: `Mince pies — 2000 packs
6 x Bake pies
1 x Pack
---
Stollen — 600 loaves
1 x Bake
1 x Glaze & pack
---
Order more boxes`,
    todo: `Open the new site
Deadline 15 November

Book the electrician
Order signage
Train the team
Invite the press`,
  };
  setupDlg?.addEventListener('click', e => {
    const ex = e.target.closest('[data-su-example]');
    if (ex) {
      const f = $('[data-setup-form]', setupDlg);
      f.text.value = EXAMPLES[ex.dataset.suExample] || '';
      f.text.focus(); preview();
      return;
    }
    const ai = e.target.closest('[data-su-ai-example]');
    if (ai) { const f = $('[data-setup-form]', setupDlg); f.source.value = ai.dataset.suAiExample; f.source.focus(); }
  });
  $('textarea[name=text]', setupDlg)?.addEventListener('input', () => { clearTimeout(previewTimer); previewTimer = setTimeout(preview, 300); });
  $('[data-setup-form]', setupDlg)?.addEventListener('submit', async e => {
    if (e.submitter?.value !== 'create') return;
    e.preventDefault();
    const f = e.target, btn = e.submitter; btn.disabled = true;
    try {
      const d = await post(`/api/boards/${boardId}/setup/`, { text: f.text.value, list_id: +f.list_id.value });
      setupDlg.close(); f.text.value = ''; if (f.source) f.source.value = ''; shots = []; renderShots();
      $('[data-su-preview]', setupDlg).innerHTML = '';
      toast(`Created ${d.created} card${d.created === 1 ? '' : 's'}.`);
      forceRefresh();
      if (d.created === 1) openCard(d.card_ids[0]);
    } catch (err) { toast(err.message || 'Could not create those.'); btn.disabled = false; }
  });

  window.addEventListener('lanes:timer-stopped', () => { if (openCardId) openCard(openCardId); });
  window.addEventListener('lanes:synced', () => { forceRefresh(); if (openCardId) openCard(openCardId); });
  // keep this board's cards on the device so they open offline too
  (async () => {
    if (!navigator.serviceWorker || !navigator.onLine) return;
    const reg = await navigator.serviceWorker.ready.catch(() => null);
    const ids = $$('.card-tile[data-card-id]', listsEl).map(t => `/cards/${t.dataset.cardId}/`);
    setTimeout(() => reg?.active?.postMessage({ type: 'keep', urls: [...new Set(ids)] }), 3000);
  })();
  async function openCard(id) {
    let res;
    try { res = await fetch(`/cards/${id}/`, { credentials: 'same-origin' }); }
    catch { res = { ok: false, status: 503 }; }
    if (!res.ok) { toast(res.status === 503 ? 'This card isn\'t saved for offline use yet — open it once while you have signal.' : 'That card could not be opened.'); return; }
    openCardId = String(id);
    openPop = null;
    dialogBody.innerHTML = await res.text();
    if (!dialog.open) dialog.showModal();
    history.replaceState(null, '', `?card=${id}`);
    wireModal();
  }

  function closeCard() {
    dialog.close();
  }
  dialog.addEventListener('close', () => {
    openCardId = null;
    history.replaceState(null, '', location.pathname);
  });
  dialog.addEventListener('click', e => { if (e.target === dialog) closeCard(); });

  // Apply a card payload from the server: update the tile and the open dialog.
  function applyCard(data) {
    if (data.next_card) {
      toast(`🔁 Next one made — due ${data.next_card.due}.`);
      setTimeout(forceRefresh, 300);
    }
    const old = $(`.card-tile[data-card-id="${data.card_id}"]:not([data-mirror-id])`, listsEl);
    // mirrors of this card on this board: update their faces in place
    $$(`.card-tile[data-card-id="${data.card_id}"][data-mirror-id]`, listsEl).forEach(m => {
      const fresh = fromHTML(data.tile);
      fresh.classList.add('is-mirror');
      fresh.dataset.mirrorId = m.dataset.mirrorId;
      fresh.dataset.token = m.dataset.token;
      const flag = m.querySelector('.mirror-flag');
      if (flag) fresh.prepend(flag.cloneNode(true));
      m.replaceWith(fresh);
    });
    if (data.archived) {
      old?.remove();
    } else {
      const tile = fromHTML(data.tile);
      const target = $(`.list[data-list-id="${data.list_id}"] [data-list-cards]`, listsEl);
      if (old && old.closest('.list')?.dataset.listId === String(data.list_id)) old.replaceWith(tile);
      else { old?.remove(); target?.appendChild(tile); }
    }
    refreshCounts();
    window.LanesBoard?.applyFilter?.();
    if (dialog.open && openCardId === String(data.card_id)) {
      const scroller = $('.cm-main', dialogBody), side = $('.cm-side', dialogBody);
      const scroll = [scroller?.scrollTop, side?.scrollTop];
      dialogBody.innerHTML = data.modal;
      wireModal();
      if (scroll[0]) $('.cm-main', dialogBody).scrollTop = scroll[0];
      if (scroll[1]) $('.cm-side', dialogBody).scrollTop = scroll[1];
      if (openPop) showPop(openPop);
    }
  }

  const cardPost = (path, payload) => post(`/api/cards/${openCardId}/${path}/`, payload).then(d => { addNewTile(d.new_card); applyCard(d); return d; });

  // A card made from inside another (a part, or a checklist item turned into a card): show its tile.
  function addNewTile(nc) {
    if (!nc || !nc.tile) return;
    const target = $(`.list[data-list-id="${nc.list_id}"] [data-list-cards]`, listsEl);
    if (target && !$(`.card-tile[data-card-id="${nc.id}"]`, listsEl)) { target.appendChild(fromHTML(nc.tile)); refreshCounts(); }
  }

  // "⊂ Part of…": find the bigger card this one belongs to.
  let parentTimer;
  dialogBody.addEventListener('input', e => {
    if (!e.target.matches('[data-parent-search]')) return;
    clearTimeout(parentTimer);
    const q = e.target.value.trim(), box = $('[data-parent-results]', dialogBody);
    parentTimer = setTimeout(async () => {
      const res = await fetch(`/api/cards/lookup/?q=${encodeURIComponent(q)}&exclude=${openCardId}`, { credentials: 'same-origin' });
      const { cards = [] } = await res.json().catch(() => ({}));
      const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
      box.innerHTML = cards.length ? cards.map(c => `<button type="button" class="parent-opt" data-parent-set="${c.id}"><b>${esc(c.title)}</b><small>${esc(c.where)}</small></button>`).join('')
        : '<p class="muted small">No cards match.</p>';
    }, 200);
  });

  function showPop(name) {
    $$('[data-pop-panel]', dialogBody).forEach(p => (p.hidden = p.dataset.popPanel !== name));
    openPop = name;
  }
  function hidePops() {
    $$('[data-pop-panel]', dialogBody).forEach(p => (p.hidden = true));
    openPop = null;
  }

  // "List ▾" chip at the top of a card → Move card: Board, List, Position (like Trello)
  function wireMoveForm(root) {
    const form = $('[data-move-form]', root);
    if (!form) return;
    const tree = JSON.parse($('[data-move-tree]', form).textContent);
    const bSel = $('[data-mv-board]', form), lSel = $('[data-mv-list]', form), pSel = $('[data-mv-pos]', form);
    const opt = (v, t, sel) => `<option value="${v}"${sel ? ' selected' : ''}>${String(t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))}</option>`;
    const board = () => tree.boards.find(b => b.id === +bSel.value) || tree.boards[0];
    function fillPos() {
      const l = board().lists.find(x => x.id === +lSel.value);
      const same = l && l.id === tree.list;
      const n = l ? l.n + (same ? 0 : 1) : 1;
      pSel.innerHTML = Array.from({ length: Math.max(n, 1) }, (_, i) => opt(i + 1, i + 1, same ? i + 1 === tree.position : i + 1 === n)).join('');
    }
    function fillLists() {
      const b = board();
      lSel.innerHTML = b.lists.map(l => opt(l.id, l.title, l.id === tree.list)).join('') || opt('', 'No lists');
      fillPos();
    }
    bSel.innerHTML = tree.boards.map(b => opt(b.id, b.title, b.id === tree.board)).join('');
    fillLists();
    bSel.addEventListener('change', fillLists);
    lSel.addEventListener('change', fillPos);
    form.addEventListener('submit', async e => {
      e.preventDefault();
      if (!lSel.value) return;
      const otherBoard = +bSel.value !== tree.board;
      if (otherBoard && !await LanesUI.confirm(`Move this card to ${board().title}?`, { text: 'Comments, checklists and attachments come too. Labels and fields that don\'t exist on that board are dropped.', ok: 'Move' })) return;
      hidePops();
      try {
        const data = await post(`/api/cards/${openCardId}/move/`, { list_id: +lSel.value, position: +pSel.value });
        if (otherBoard) {
          $$(`.card-tile[data-card-id="${data.card_id}"]:not([data-mirror-id])`, listsEl).forEach(el => el.remove());
          refreshCounts(); closeCard();
          return toast(`Moved to ${board().title}.`);
        }
        applyCard(data); forceRefresh();
        toast('Card moved.');
      } catch (err) { toast(err.message || 'Could not move the card.'); }
    });
  }

  function wireModal() {
    const root = $('.cm', dialogBody);
    if (!root) return;
    setTimeout(() => showSummary && showSummary());
    const title = $('[data-card-title]', root);
    if (title) inlineEdit(title, v => cardPost('update', { title: v }));
    wireMoveForm(root);

    const desc = $('[data-description]', root);
    if (desc) {
      const ta = $('textarea', desc), saveRow = $('.save-row', desc);
      let original = ta.value;
      ta.addEventListener('focus', () => (saveRow.hidden = false));
      $('[data-cancel-desc]', desc).addEventListener('click', () => {
        ta.value = original; saveRow.hidden = true;
        const view = $('[data-desc-view]', root);
        if (view) { view.hidden = false; desc.hidden = true; }
      });
      desc.addEventListener('submit', e => {
        e.preventDefault();
        const mine = ta.value;
        cardPost('update', { description: mine, description_base: original }).then(() => (original = mine))
          .catch(async err => {
            if (!err.conflict) return;
            const theirs = err.conflict.current || '';
            const keepMine = await LanesUI.confirm('Someone else changed this description while you were editing.', {
              text: `Their version:\n\n${theirs.slice(0, 600) || '(empty)'}\n\nReplace it with yours, or keep theirs?`,
              ok: 'Replace with mine', cancel: 'Keep theirs' });
            if (keepMine) cardPost('update', { description: mine }).then(() => (original = mine));
            else { original = theirs; openCard(openCardId); }
          });
      });
    }

    const comment = $('[data-comment-new]', root);
    if (comment) {
      const ta = $('textarea', comment), btn = $('button', comment);
      ta.addEventListener('focus', () => (btn.hidden = false));
      ta.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) comment.requestSubmit(); });
      comment.addEventListener('submit', e => {
        e.preventDefault();
        if (!ta.value.trim()) return;
        post(`/api/cards/${openCardId}/comments/`, { body: ta.value }).then(applyCard);
      });
    }
  }

  dialogBody.addEventListener('click', async e => {
    const t = e.target;
    if (t.closest('[data-close-card]')) return closeCard();
    const popBtn = t.closest('[data-pop]');
    if (popBtn) {
      const name = popBtn.dataset.pop;
      if (openPop === name) return hidePops();
      // Open the panel next to whichever button asked for it (e.g. the date block in the card body).
      const wrap = popBtn.closest('.pop-wrap');
      const panel = $(`[data-pop-panel="${name}"]`, dialogBody);
      if (wrap && panel && !wrap.contains(panel)) wrap.appendChild(panel);
      showPop(name);
      if (popBtn.hasAttribute('data-suggest-steps')) loadSuggestions();
      return;
    }
    if (!t.closest('.pop')) hidePops();

    if (t.closest('[data-summarise]')) return loadSummary();
    if (t.closest('[data-write-desc]')) return writeDescription(t.closest('[data-write-desc]'));
    if (t.closest('[data-watch-card]')) return cardPost('watch');
    if (t.closest('[data-vote]')) return cardPost('vote');
    const react = t.closest('[data-react]');
    if (react) {
      const cid = react.closest('[data-comment]').dataset.comment;
      return post(`/api/comments/${cid}/react/`, { emoji: react.dataset.react }).then(applyCard);
    }
    const dv = t.closest('[data-desc-view]');
    if (dv && !t.closest('a')) {
      const form = $('[data-description]', dialogBody);
      dv.hidden = true; form.hidden = false;
      const ta = $('textarea', form); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
      return;
    }
    const press = t.closest('[data-press-card]');
    if (press) {
      press.disabled = true;
      const closeAfter = press.hasAttribute('data-close-after');
      return post(`/api/cards/${openCardId}/buttons/${press.dataset.pressCard}/`).then(data => {
        applyCard(data);
        toast(data.summary);
        if (closeAfter) closeCard();
      }).finally(() => (press.disabled = false));
    }
    if (t.closest('[data-toggle-complete]')) {
      return cardPost('update', { is_complete: !t.closest('[data-toggle-complete]').classList.contains('on') });
    }
    const arch = t.closest('[data-archive-card]');
    if (arch) {
      const archive = arch.dataset.archiveCard === '1';
      await cardPost('update', { archive });
      if (archive) { closeCard(); toast('Card archived. Restore it from the board menu.'); }
      return;
    }
    const person = t.closest('[data-member]');
    if (person) return cardPost('members', { user_id: +person.dataset.member });
    if (t.closest('[data-clear-dates]')) return cardPost('update', { due_at: '', start_date: '' });
    const readAmt = t.closest('[data-read-amounts]');
    if (readAmt) return post(`/api/checklists/${readAmt.dataset.readAmounts}/read-amounts/`).then(d => {
      applyCard(d); toast(d.converted ? `${d.converted} item${d.converted === 1 ? '' : 's'} now count up — type how many are done next to each.` : 'Nothing to change.');
    });
    if (t.closest('[data-focus-qty]')) {
      const det = t.closest('details.item-more');
      setTimeout(() => { if (det?.open) det.querySelector('input[name="qty_target"]')?.focus(); }, 30);
      return;
    }
    const edit = t.closest('[data-item-edit]');
    if (edit && !edit.querySelector('input')) return editItemText(edit);
    const finish = t.closest('[data-finish-card]');
    if (finish) {
      finish.disabled = true;
      await cardPost('update', { is_complete: true });
      if (finish.dataset.finishCard) await cardPost('move', { list_id: +finish.dataset.finishCard });
      return;
    }
    const hide = t.closest('[data-hide-checked]');
    if (hide) return post(`/api/checklists/${hide.closest('[data-checklist]').dataset.checklist}/`,
      { hide_checked: hide.dataset.hideChecked === '1' }).then(applyCard);
    if (t.closest('[data-delete-checklist]')) {
      if (!await LanesUI.confirm('Delete this checklist?', { ok: 'Delete', danger: true })) return;
      return post(`/api/checklists/${t.closest('[data-checklist]').dataset.checklist}/`, { delete: true }).then(applyCard);
    }
    const clr = t.closest('[data-item-clear]');
    if (clr) return post(`/api/items/${clr.dataset.itemClear}/`, { due_at: '' }).then(applyCard);
    const delItem = t.closest('[data-delete-item]');
    if (delItem) return post(`/api/items/${delItem.dataset.deleteItem}/`, { delete: true }).then(applyCard);
    const rmMirror = t.closest('[data-remove-mirror]');
    if (rmMirror) {
      if (!await LanesUI.confirm('Remove the mirror from that board?', { text: 'The original card stays where it is.', ok: 'Remove', danger: true })) return;
      return post(`/api/mirrors/${rmMirror.dataset.removeMirror}/`, { delete: true }).then(data => {
        $(`.card-tile[data-mirror-id="${rmMirror.dataset.removeMirror}"]`, listsEl)?.remove();
        refreshCounts();
        applyCard(data);
      });
    }
    const cc = t.closest('[data-cover-colour]');
    if (cc) return cardPost('update', { cover_colour: cc.dataset.coverColour });
    const cf = t.closest('[data-cover-full]');
    if (cf) return cardPost('update', { cover_full: cf.dataset.coverFull === '1' });
    const st = t.closest('[data-sticker]');
    if (st) return cardPost('update', { sticker: st.dataset.sticker });
    const tplBtn = t.closest('[data-template-toggle]');
    if (tplBtn) return cardPost('update', { is_template: tplBtn.dataset.templateToggle === '1' });
    const cover = t.closest('[data-cover]');
    if (cover) return post(`/api/attachments/${cover.dataset.cover}/`, { cover: cover.dataset.on === '1' }).then(applyCard);
    const rev = t.closest('[data-reveal]');
    if (rev) {
      const sec = $(`[data-opt="${rev.dataset.reveal}"]`, dialogBody);
      if (sec) { sec.hidden = false; rev.hidden = true; sec.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        $('input:not([type=hidden]), select, textarea', sec)?.focus(); }
      return;
    }
    if (t.closest('[data-amount-show]')) {
      const f = $('[data-amount-form]', dialogBody);
      if (f) { f.hidden = false; f.amount.focus(); }
      return;
    }
    const tAct = t.closest('[data-time-act]');
    if (tAct) {
      const act = tAct.dataset.timeAct, title = dialogBody.querySelector('.cm-title, [data-title]')?.textContent?.trim() || 'this card';
      return cardPost('time', { action: act }).then(() => {
        const clock = dialogBody.querySelector('.tt-stop [data-since]');
        if (act === 'start' && clock) window.LanesTimerPill?.({ since: clock.dataset.since, base: clock.dataset.base, title,
          href: `${location.pathname}?card=${openCardId}` });
        if (act === 'stop') window.LanesTimerPill?.(null);
      }).catch(err => toast(err.message || 'Could not do that.'));
    }
    const tDel = t.closest('[data-time-del]');
    if (tDel) return cardPost('time', { action: 'delete', id: +tDel.dataset.timeDel }).catch(err => toast(err.message));
    const depX = t.closest('[data-dep-remove]');
    if (depX) return cardPost('dependencies', { remove: +depX.dataset.depRemove });
    const toCard = t.closest('[data-item-to-card]');
    if (toCard) {
      toCard.disabled = true;
      return post(`/api/items/${toCard.dataset.itemToCard}/to-card/`, {}).then(data => {
        addNewTile(data.new_card); applyCard(data);
        toast('Made it a card. The item ticks itself when that card is done.');
      }).catch(err => toast(err.message || 'Could not do that.')).finally(() => (toCard.disabled = false));
    }
    const pSet = t.closest('[data-parent-set]');
    if (pSet) {
      hidePops();
      return cardPost('parent', { parent_id: pSet.dataset.parentSet ? +pSet.dataset.parentSet : null })
        .then(() => toast(pSet.dataset.parentSet ? 'Added as a part.' : 'Taken out of that card.')).catch(() => {});
    }
    const soAct = t.closest('[data-signoff-act]');
    if (soAct) {
      const act = soAct.dataset.signoffAct;
      if (act === 'cancel' && !await LanesUI.confirm('Cancel this sign-off?', { ok: 'Cancel sign-off', cancel: 'Keep it', danger: true })) return;
      return post(`/api/signoffs/${soAct.dataset.signoff}/`, { action: act }).then(d => {
        applyCard(d); if (act === 'remind') toast('Reminder sent.');
      }).catch(() => {});
    }
    const soAgain = t.closest('[data-signoff-again]');
    if (soAgain) return cardPost('signoffs', { approver_id: +soAgain.dataset.signoffAgain }).then(() => toast('Asked again.')).catch(() => {});
    const attC = t.closest('[data-att-comment]');
    if (attC) {
      hidePops();
      const ta = $('textarea[name=body]', dialogBody);
      if (ta) {
        ta.value = `${ta.value ? ta.value.trimEnd() + ' ' : ''}[${attC.dataset.attName}](${location.origin}${attC.dataset.attComment}) `;
        ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
        ta.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
      return;
    }
    const delAtt = t.closest('[data-delete-attachment]');
    if (delAtt) {
      if (!await LanesUI.confirm('Delete this attachment?', { ok: 'Delete', danger: true })) return;
      return post(`/api/attachments/${delAtt.dataset.deleteAttachment}/`, { delete: true }).then(applyCard);
    }
    const delComment = t.closest('[data-delete-comment]');
    if (delComment) {
      if (!await LanesUI.confirm('Delete this comment?', { ok: 'Delete', danger: true })) return;
      return post(`/api/comments/${delComment.dataset.deleteComment}/`, { delete: true }).then(applyCard);
    }
  });

  // Click a checklist item's words to change them. "430 Christmas Cake" becomes an amount that counts up.
  function editItemText(span) {
    const id = span.dataset.itemEdit, was = span.textContent.trim();
    const box = document.createElement('input');
    box.className = 'item-edit'; box.value = was; box.maxLength = 250;
    span.textContent = ''; span.appendChild(box); box.focus(); box.select();
    let done = false;
    const finish = save => {
      if (done) return; done = true;
      const v = box.value.trim();
      if (!save || !v || v === was) { span.textContent = was; return; }
      span.textContent = v;
      post(`/api/items/${id}/`, { text: v, reparse: true }).then(applyCard).catch(() => (span.textContent = was));
    };
    box.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    box.addEventListener('blur', () => finish(true));
  }

  // Every item ticked: offer to mark the card complete and move it to the Done list.
  const DONE_WORDS = ['done', 'complete', 'finished', 'closed', 'delivered', 'shipped'];
  function offerFinish() {
    setTimeout(() => {
      const boxes = $$('[data-item]', dialogBody);
      if (!boxes.length || boxes.some(b => !b.checked) || $('[data-finish-bar]', dialogBody)) return;
      if ($('.complete-toggle.on', dialogBody)) return;
      const lists = $$('.list[data-list-id]', listsEl).map(l => ({ value: l.dataset.listId, textContent: $('.list-title', l)?.textContent || '' }));
      const opt = lists.find(o => DONE_WORDS.some(w => o.textContent.toLowerCase().includes(w)));
      const tile = $(`.card-tile[data-card-id="${openCardId}"]:not([data-mirror-id])`, listsEl);
      const here = opt && tile?.closest('.list')?.dataset.listId === String(opt.value);
      const bar = document.createElement('div');
      bar.className = 'finish-bar'; bar.dataset.finishBar = '';
      bar.innerHTML = `<span>✅ Everything's ticked off.</span><button type="button" class="btn primary sm" data-finish-card="${opt && !here ? opt.value : ''}">Mark complete${opt && !here ? ' and move to ' + opt.textContent.trim().replace(/[<&>]/g, '') : ''}</button><button type="button" class="btn ghost sm" data-finish-later>Not yet</button>`;
      bar.querySelector('[data-finish-later]').addEventListener('click', () => bar.remove());
      const cl = boxes[boxes.length - 1].closest('[data-checklist]');
      (cl || dialogBody).prepend(bar);
    }, 250);
  }

  dialogBody.addEventListener('change', async e => {
    const t = e.target;
    if (t.matches('[data-label]')) return cardPost('labels', { label_id: +t.dataset.label });
    if (t.matches('[data-item]')) return post(`/api/items/${t.dataset.item}/`, { is_done: t.checked }).then(d => { applyCard(d); if (d.refresh_board && typeof forceRefresh === 'function') forceRefresh(); if (t.checked) offerFinish(); })
      .catch(err => { if (err.queued) t.closest('.item')?.classList.toggle('done', t.checked); else t.checked = !t.checked; });
    if (t.matches('[data-move-list]')) return cardPost('move', { list_id: +t.value });
    if (t.matches('[data-move-board]')) {
      const opt = t.selectedOptions[0];
      if (!t.value || !await LanesUI.confirm(`Move this card to ${opt.parentElement.label} › ${opt.textContent}?`, { text: 'Comments, checklists and attachments come too. Labels and fields that don\'t exist on that board are dropped.', ok: 'Move' })) { t.value = ''; return; }
      return post(`/api/cards/${openCardId}/move/`, { list_id: +t.value }).then(data => {
        $$(`.card-tile[data-card-id="${data.card_id}"]:not([data-mirror-id])`, listsEl).forEach(el => el.remove());
        refreshCounts();
        closeCard();
        toast(`Moved to ${opt.parentElement.label}.`);
      });
    }
    if (t.matches('[data-attach]') && t.files.length) return uploadFile(t.files[0]);
    if (t.matches('[data-field]')) {
      const value = t.type === 'checkbox' ? t.checked : t.value;
      return cardPost('fields', { field_id: +t.dataset.field, value });
    }
  });

  dialogBody.addEventListener('submit', e => {
    const f = e.target;
    if (f.matches('[data-time-add]')) {
      e.preventDefault();
      return cardPost('time', { action: 'add', time: f.time.value, day: f.day.value, note: f.note.value })
        .catch(err => toast(err.message || 'Could not log that.'));
    }
    if (f.matches('[data-signoff-new]')) {
      e.preventDefault();
      if (!f.approver_id.value) return toast('Choose who signs it off.');
      hidePops();
      return cardPost('signoffs', { approver_id: +f.approver_id.value, ask: f.ask.value })
        .then(() => toast('Asked — they\'ve been notified.')).catch(() => {});
    }
    if (f.matches('[data-signoff-decide]')) {
      e.preventDefault();
      const action = e.submitter?.value || 'approve';
      return post(`/api/signoffs/${f.dataset.signoffDecide}/`, { action, note: f.note.value }).then(d => {
        applyCard(d); toast(action === 'approve' ? 'Signed off.' : 'Sent back with your note.');
      }).catch(() => {});
    }
    if (f.matches('[data-child-new]')) {
      e.preventDefault();
      const title = f.title.value.trim();
      if (!title) return;
      return cardPost('children', { title, list_id: +f.list_id.value }).then(() => {
        $('[data-child-new] input[name=title]', dialogBody)?.focus();
      }).catch(() => {});
    }
    if (f.matches('[data-dep-add]')) {
      e.preventDefault();
      if (!f.depends_on.value) return;
      return cardPost('dependencies', { depends_on: +f.depends_on.value, lag_days: +f.lag_days.value || 0 })
        .catch(err => toast(err.message || 'Could not add that.'));
    }
    if (f.matches('[data-att-edit]')) {
      e.preventDefault();
      const data = { name: f.name.value };
      if (f.url) data.url = f.url.value;
      return post(`/api/attachments/${f.dataset.attEdit}/`, data).then(applyCard).catch(err => toast(err.message || 'Could not save that.'));
    }
    if (f.matches('[data-attach-link]')) {
      e.preventDefault();
      hidePops();
      return cardPost('links', { url: f.url.value, name: f.name.value }).catch(err => toast(err.message || 'Could not add that link.'));
    }
    if (f.matches('[data-dates]')) {
      e.preventDefault();
      hidePops();
      return cardPost('update', { due_at: f.due_at.value, start_date: f.start_date.value,
                                  recur: f.recur.value, reminder_minutes: +f.reminder_minutes.value,
                                  estimate_hours: f.estimate_hours ? f.estimate_hours.value : '',
                                  is_milestone: f.is_milestone ? f.is_milestone.checked : false });
    }
    if (f.matches('[data-checklist-new]')) {
      e.preventDefault();
      hidePops();
      return cardPost('checklists', { title: f.title.value, copy_from: f.copy_from?.value || '' }).then(() => {
        const inputs = $$('[data-item-new] input', dialogBody);
        inputs[inputs.length - 1]?.focus();   // straight into "Add an item"
      });
    }
    if (f.matches('[data-amount-form]')) {
      e.preventDefault();
      return cardPost('amount', { amount: f.amount.value, unit: f.unit.value })
        .then(() => toast('Amount set — lines “per one” updated.'))
        .catch(err => toast(err.message || 'Could not set that.'));
    }
    if (f.matches('[data-item-new]')) {
      e.preventDefault();
      const text = f.text.value.trim();
      if (!text) return;
      const clId = f.closest('[data-checklist]').dataset.checklist;
      return post(`/api/checklists/${clId}/items/`, { text }).then(data => {
        applyCard(data);
        $(`[data-checklist="${clId}"] [data-item-new] input`, dialogBody)?.focus();
      });
    }
    if (f.matches('[data-mirror-card]')) {
      e.preventDefault();
      hidePops();
      return post(`/api/cards/${openCardId}/mirrors/`, { list_id: +f.list_id.value }).then(data => {
        applyCard(data);
        toast('Mirrored. It now shows in both places.');
      });
    }
    if (f.matches('[data-copy-card]')) {
      e.preventDefault();
      hidePops();
      return post(`/api/cards/${openCardId}/copy/`, {
        title: f.title.value, list_id: +f.list_id.value,
        checklists: f.checklists.checked, labels: f.labels.checked, members: f.members.checked,
      }).then(data => {
        const target = $(`.list[data-list-id="${data.list_id}"] [data-list-cards]`, listsEl);
        target?.appendChild(fromHTML(data.tile));
        refreshCounts();
        toast('Copy created.');
        openCard(data.card_id);
      });
    }
    if (f.matches('[data-item-meta]')) {
      e.preventDefault();
      const meta = { assignee_id: f.assignee_id.value ? +f.assignee_id.value : null, due_at: f.due_at.value,
          reminder_minutes: +f.reminder_minutes.value };
      if (f.qty_target) {
        Object.assign(meta, { qty_target: f.qty_target.value, qty_unit: f.qty_unit.value, per_unit: f.per_unit.value });
        if (f.qty_done.value !== '' && (f.qty_target.value || f.per_unit.value)) meta.qty_done = f.qty_done.value;
      }
      return post(`/api/items/${f.dataset.itemMeta}/`, meta)
        .then(d => { applyCard(d); toast(f.assignee_id.value ? 'Saved — they\'ve been notified.' : 'Saved.'); })
        .catch(err => toast(err.message || 'Could not save that.'));
    }
    if (f.matches('[data-item-qty]')) {
      e.preventDefault();
      const n = f.n.value.trim();
      if (!n) return;
      return post(`/api/items/${f.dataset.itemQty}/`, { qty_add: n }).then(applyCard)
        .catch(err => {
          if (!err.queued) return toast(err.message || 'Could not record that.');
          const b = f.closest('.item-qty')?.querySelector('b');    // show it straight away; the server catches up later
          if (b) b.textContent = (parseFloat(b.textContent.replace(/,/g, '')) + parseFloat(n)).toLocaleString('en-GB');
          f.n.value = '';
        });
    }
    if (f.matches('[data-label-new]')) {
      e.preventDefault();
      return post(`/api/boards/${boardId}/labels/`,
        { name: f.name.value, colour: f.colour.value, card_id: +openCardId }).then(applyCard);
    }
  });

  // ---------- board header ----------
  const boardTitle = $('[data-board-title]');
  if (boardTitle) inlineEdit(boardTitle, v => post(`/api/boards/${boardId}/update/`, { title: v }));

  $('[data-star]')?.addEventListener('click', async e => {
    const btn = e.currentTarget;            // currentTarget is gone after the await
    const data = await post(`/api/boards/${boardId}/star/`);
    btn.classList.toggle('on', data.starred);
  });
  $$('[data-press-board]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const data = await post(`/api/boards/${boardId}/buttons/${b.dataset.pressBoard}/`);
      toast(data.summary);
      if (view === 'board') forceRefresh(); else location.reload();
    } finally { b.disabled = false; }
  }));
  $('[data-watch-board]')?.addEventListener('click', async e => {
    const btn = e.currentTarget;
    const data = await post(`/api/boards/${boardId}/watch/`);
    btn.classList.toggle('on', data.watching);
    btn.title = data.watching ? 'Watching — you hear about every change on this board' : 'Watch this board';
    toast(data.watching ? 'Watching this board. Changes will show in your 🔔.' : 'Stopped watching this board.');
  });
  $('[data-join]')?.addEventListener('click', async () => {
    await post(`/api/boards/${boardId}/join/`);
    location.reload();
  });

  // Share dialog
  const share = document.getElementById('share-dialog') || document.createElement('dialog');
  $$('[data-open-share]').forEach(b => b.addEventListener('click', e => { if (!e.target.closest('[data-member-card]')) share.showModal(); }));

  // Click someone's avatar in the board header: who they are, their role, and their cards here.
  const memberPop = $('[data-member-pop]');
  const memberData = (() => { try { return JSON.parse($('#member-data')?.textContent || '{}'); } catch { return {}; } })();
  const escHTML = v => String(v ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  $$('[data-member-card]').forEach(btn => btn.addEventListener('click', e => {
    e.stopPropagation();
    const id = btn.dataset.memberCard, m = memberData[id];
    if (!m || !memberPop) return;
    if (!memberPop.hidden && memberPop.dataset.for === id) { memberPop.hidden = true; return; }
    memberPop.dataset.for = id;
    memberPop.innerHTML = `<div class="mc-top"><span class="avatar xl" style="--h:${m.hue}">${escHTML(m.initials)}</span>
        <div><b>${escHTML(m.name)}</b>${m.title ? `<span class="mc-role">${escHTML(m.title)}</span>` : ''}<small>${escHTML(m.email)}</small>${m.role ? `<span class="mc-role">Board ${escHTML(m.role.toLowerCase())}</span>` : ''}</div>
        <button type="button" class="icon-btn xs mc-x" data-mc-close aria-label="Close">×</button></div>
      <p class="mc-count">${m.open ? `${m.open} open card${m.open === 1 ? '' : 's'} on this board` : 'No open cards on this board'}</p>
      <a class="mc-item" href="/people/${id}/">View profile</a>
      ${filterForm ? `<button type="button" class="mc-item" data-mc-filter="${id}">Show only their cards here</button>` : ''}
      <a class="mc-item" href="/search/?q=${encodeURIComponent('@' + m.first + ' is:open')}">Their open cards on every board</a>
      <a class="mc-item" href="/workload/">See their workload</a>`;
    const r = btn.getBoundingClientRect();
    memberPop.style.top = `${r.bottom + 8 + window.scrollY}px`;
    memberPop.style.left = `${Math.max(8, Math.min(r.left - 120, window.innerWidth - 300))}px`;
    memberPop.hidden = false;
  }));
  memberPop?.addEventListener('click', e => {
    if (e.target.closest('[data-mc-close]')) { memberPop.hidden = true; return; }
    const f = e.target.closest('[data-mc-filter]');
    if (f && filterForm) {
      $$('input[name=m]', filterForm).forEach(cb => (cb.checked = cb.value === f.dataset.mcFilter || (cb.value === 'me' && +f.dataset.mcFilter === +boardEl.dataset.userId)));
      filterForm.dispatchEvent(new Event('input', { bubbles: true }));
      memberPop.hidden = true;
      toast('Showing only their cards — clear it from Filter.');
    }
  });
  document.addEventListener('click', e => { if (memberPop && !memberPop.hidden && !memberPop.contains(e.target)) memberPop.hidden = true; });
  $('[data-close]', share)?.addEventListener('click', () => share.close());
  share.addEventListener('click', e => { if (e.target === share) share.close(); });
  const shareMembers = document.getElementById('share-members') || document.createElement('div');
  const updateMembers = data => {
    shareMembers.innerHTML = data.html;
    document.getElementById('member-count').textContent = data.count;
  };
  const guestBox = document.getElementById('guest-invite');
  const accountBox = document.getElementById('guest-account');
  const shareForm = document.getElementById('share-form');
  shareForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target;
    accountBox.hidden = true;
    const data = await post(`/api/boards/${boardId}/share/`, { email: f.email.value, role: f.role.value });
    if (data.needs_guest) {
      guestBox.hidden = false;
      $('[data-guest-email]', guestBox).textContent = data.email;
      $('[data-guest-first]', guestBox).focus();
      return;
    }
    updateMembers(data);
    f.email.value = '';
    toast('Added to the board. Reload to assign them to cards.');
  });
  $('[data-guest-cancel]', guestBox || document.createElement('div'))?.addEventListener('click', () => (guestBox.hidden = true));
  $('[data-invite-guest]', guestBox || document.createElement('div'))?.addEventListener('click', async () => {
    const data = await post(`/api/boards/${boardId}/share/`, {
      email: shareForm.email.value, role: shareForm.role.value, invite_guest: true,
      first_name: $('[data-guest-first]', guestBox).value, last_name: $('[data-guest-last]', guestBox).value,
    });
    updateMembers(data);
    guestBox.hidden = true;
    shareForm.email.value = '';
    if (data.new_account) {
      const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
      accountBox.innerHTML = `<b>Guest account created.</b> ${data.new_account.emailed ? 'We\'ve emailed them an invite. If it doesn\'t arrive, send' : 'Send'} them these details — the password is shown only once:
        <div class="cred"><span>Sign in at</span><code>${esc(location.origin)}/login/</code></div>
        <div class="cred"><span>Email</span><code>${esc(data.new_account.email)}</code></div>
        <div class="cred"><span>Temporary password</span><code>${esc(data.new_account.password)}</code></div>
        <small class="muted">They can change it from their profile menu.</small>`;
      accountBox.hidden = false;
    }
  });
  shareMembers.addEventListener('change', async e => {
    const s = e.target.closest('[data-member-role]');
    if (!s) return;
    try { updateMembers(await post(`/api/boards/${boardId}/members/${s.dataset.memberRole}/`, { role: s.value })); }
    catch { location.reload(); }
  });
  shareMembers.addEventListener('click', async e => {
    const b = e.target.closest('[data-member-remove]');
    if (!b || !await LanesUI.confirm('Remove this person from the board?', { ok: 'Remove', danger: true })) return;
    updateMembers(await post(`/api/boards/${boardId}/members/${b.dataset.memberRemove}/`, { remove: true }));
  });

  // Drawer (menu): activity, archived, settings
  const drawer = document.getElementById('drawer') || document.createElement('aside');
  drawer.addEventListener('click', async e => {
    const t = e.target.closest('[data-act-tab]');
    if (t) {
      $$('[data-act-tab]', drawer).forEach(b => b.classList.toggle('on', b === t));
      $$('[data-act-list]', drawer).forEach(l => { l.hidden = l.dataset.actList !== t.dataset.actTab; });
      return;
    }
    if (e.target.closest('[data-leave-board]')) {
      if (!await LanesUI.confirm('Leave this board? You can be added back by a board admin.', { ok: 'Leave', danger: true })) return;
      try { await post(`/api/boards/${boardId}/members/${boardEl.dataset.userId}/`, { remove: true }); location.href = '/'; }
      catch (err) { toast(err.message || 'Could not leave the board.'); }
    }
  });
  // Activity and Archived can change after the page loaded: refresh them each time the menu opens.
  async function refreshDrawer() {
    try {
      const res = await fetch(location.pathname, { credentials: 'same-origin' });
      if (!res.ok) return;
      const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
      ['activity', 'archived'].forEach(name => {
        const fresh = doc.querySelector(`#drawer [data-pane="${name}"]`);
        const mine = $(`[data-pane="${name}"]`, drawer);
        if (fresh && mine) mine.innerHTML = fresh.innerHTML;
      });
    } catch { /* keep what's there */ }
  }
  // The menu is a list (Trello-style); each item opens its own panel with a back arrow.
  function drawerGo(pane, title) {
    $$('[data-pane]', drawer).forEach(p => (p.hidden = p.dataset.pane !== pane));
    const t = $('[data-drawer-title]', drawer);
    if (t) t.textContent = pane === 'home' ? 'Menu' : (title || 'Menu');
    const back = $('[data-drawer-back]', drawer);
    if (back) back.hidden = pane === 'home';
    drawer.scrollTop = 0;
  }
  window.LanesDrawerGo = (pane, title) => { drawer.hidden = false; drawerGo(pane, title); };
  $('[data-open-drawer]')?.addEventListener('click', () => {
    drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) { drawerGo('home'); refreshDrawer(); }
  });
  $('[data-close-drawer]')?.addEventListener('click', () => (drawer.hidden = true));
  $('[data-drawer-back]', drawer)?.addEventListener('click', () => drawerGo('home'));
  drawer.addEventListener('click', async e => {
    const go = e.target.closest('[data-goto]');
    if (go) return drawerGo(go.dataset.goto, go.dataset.title);
    if (e.target.closest('[data-menu-share]')) { drawer.hidden = true; return share.showModal(); }
    if (e.target.closest('[data-print-board]')) { drawer.hidden = true; return setTimeout(() => window.print(), 50); }
    if (e.target.closest('[data-menu-star]')) {
      const data = await post(`/api/boards/${boardId}/star/`);
      $('[data-star]')?.classList.toggle('on', data.starred);
      $('[data-menu-star-label]', drawer).textContent = data.starred ? 'Unstar' : 'Star';
      return toast(data.starred ? '⭐ Starred — it shows at the top of your Boards page.' : 'Unstarred.');
    }
    if (e.target.closest('[data-menu-watch]')) {
      const data = await post(`/api/boards/${boardId}/watch/`);
      $('[data-watch-board]')?.classList.toggle('on', data.watching);
      $('[data-menu-watch-label]', drawer).textContent = data.watching ? 'Watching ✓' : 'Watch';
      return toast(data.watching ? 'Watching this board. Changes will show in your 🔔.' : 'Stopped watching this board.');
    }
  });
  drawer.addEventListener('click', async e => {
    const link = e.target.closest('[data-card-link]');
    if (link) { e.preventDefault(); return openCard(link.dataset.cardLink); }
    const restoreList = e.target.closest('[data-restore-list]');
    if (restoreList) {
      await post(`/api/lists/${restoreList.dataset.restoreList}/`, { archive: false });
      return location.reload();
    }
    const del = e.target.closest('[data-delete-card]');
    if (del) {
      if (!await LanesUI.confirm('Delete this card for good?', { text: 'Its comments, checklists and attachments go too. This can\'t be undone.', ok: 'Delete for good', danger: true })) return;
      await post(`/api/cards/${del.dataset.deleteCard}/delete/`);
      del.closest('.archived-row').remove();
      return toast('Card deleted.');
    }
    const restore = e.target.closest('[data-restore]');
    if (restore) {
      await post(`/api/cards/${restore.dataset.restore}/update/`, { archive: false });
      return location.reload();
    }
    const bg = e.target.closest('[data-bg]');
    if (bg) {
      await post(`/api/boards/${boardId}/update/`, { background: bg.dataset.bg });
      boardEl.className = boardEl.className.replace(/bg-\w+/, `bg-${bg.dataset.bg}`);
      $$('[data-bg]', drawer).forEach(s => s.classList.toggle('on', s === bg));
      return;
    }
    if (e.target.closest('[data-archive-board]')) {
      if (!await LanesUI.confirm('Archive this board?', { text: 'It disappears for everyone until an admin restores it from the Boards page.', ok: 'Archive board', danger: true })) return;
      await post(`/api/boards/${boardId}/update/`, { archive: true });
      location.href = '/';
    }
  });
  // Custom fields (menu → Fields)
  const fieldNew = $('[data-field-new]', drawer);
  if (fieldNew) {
    const kindSel = fieldNew.kind, opts = fieldNew.options;
    kindSel.addEventListener('change', () => { opts.hidden = kindSel.value !== 'dropdown'; });
    fieldNew.addEventListener('submit', async e => {
      e.preventDefault();
      await post(`/api/boards/${boardId}/fields/`,
        { name: fieldNew.name.value, kind: kindSel.value, options: opts.value });
      location.reload();
    });
  }
  drawer.addEventListener('click', async e => {
    const sug = e.target.closest('[data-field-suggest]');
    if (!sug) return;
    try { await post(`/api/boards/${boardId}/fields/`, JSON.parse(sug.dataset.fieldSuggest)); location.reload(); }
    catch (err) { toast(err.message || 'Could not add that field.'); }
  });
  drawer.addEventListener('change', e => {
    const show = e.target.closest('[data-field-show]');
    if (show) post(`/api/boards/${boardId}/fields/`,
      { field_id: +show.closest('[data-field-row]').dataset.fieldRow, show_on_card: show.checked });
  });
  drawer.addEventListener('click', async e => {
    const del = e.target.closest('[data-field-delete]');
    if (!del || !await LanesUI.confirm('Delete this field?', { text: 'Every value stored in it on every card goes too.', ok: 'Delete', danger: true })) return;
    await post(`/api/boards/${boardId}/fields/`, { field_id: +del.closest('[data-field-row]').dataset.fieldRow, delete: true });
    del.closest('[data-field-row]').remove();
  });

  $('[data-board-workspace]', drawer)?.addEventListener('change', async e => {
    const name = e.target.selectedOptions[0].textContent;
    if (!await LanesUI.confirm(`Move this board to ${name}?`, { text: 'Everything on it moves too. People in that department can then see it (unless the board is private).', ok: 'Move' })) { location.reload(); return; }
    try { await post(`/api/boards/${boardId}/update/`, { workspace: e.target.value }); location.reload(); }
    catch (err) { toast(err.message); location.reload(); }
  });
  $('[data-board-collection]', drawer)?.addEventListener('change', async e => {
    let value = e.target.value;
    if (value === '__new') {
      const name = await LanesUI.ask('New collection', { placeholder: 'e.g. 2026 projects', ok: 'Create' });
      if (!name || !name.trim()) { location.reload(); return; }
      value = `new:${name.trim()}`;
    }
    await post(`/api/boards/${boardId}/update/`, { collection: value });
    location.reload();
  });
  $('[data-public-toggle]', drawer)?.addEventListener('change', async e => {
    const data = await post(`/api/boards/${boardId}/update/`, { public: e.target.checked });
    const box = $('[data-public-link]', drawer);
    box.hidden = !data.public_token;
    $('[data-public-url]', box).value = data.public_token ? `${location.origin}/p/${data.public_token}/` : '';
    toast(data.public_token ? 'Public link is on. Anyone with it can view this board.' : 'Public link turned off.');
  });
  $('[data-copy-public]', drawer)?.addEventListener('click', async () => {
    const input = $('[data-public-url]', drawer);
    try { await navigator.clipboard.writeText(input.value); toast('Link copied.'); }
    catch { input.select(); toast('Press Ctrl+C to copy.'); }
  });
  $$('[data-board-flag]', drawer).forEach(cb => cb.addEventListener('change', async () => {
    await post(`/api/boards/${boardId}/update/`, { [cb.dataset.boardFlag]: cb.checked });
    location.reload();
  }));
  $('[data-project-budget]', drawer)?.addEventListener('submit', async e => {
    e.preventDefault();
    try { await post(`/api/boards/${boardId}/update/`, { budget: e.target.budget.value }); toast('Budget saved.'); }
    catch (err) { toast(err.message || 'Could not save that.'); }
  });
  $('[data-project-status]', drawer)?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target;
    await post(`/api/boards/${boardId}/status/`, { rag: f.rag.value, status_note: f.status_note.value });
    toast('Status updated.');
  });
  $$('[data-board-select]', drawer).forEach(sel => sel.addEventListener('change', async () => {
    await post(`/api/boards/${boardId}/update/`, { [sel.dataset.boardSelect]: sel.value });
    toast('Saved.');
  }));
  $('[data-board-template]', drawer)?.addEventListener('change', e =>
    post(`/api/boards/${boardId}/update/`, { is_template: e.target.checked })
      .then(() => toast(e.target.checked ? 'This board is now a template.' : 'No longer a template.')));

  $('[data-visibility]', drawer)?.addEventListener('change', async e => {
    await post(`/api/boards/${boardId}/update/`, { visibility: e.target.value });
    location.reload();
  });

  // Close any open <details> menu when clicking elsewhere
  document.addEventListener('click', e => {
    $$('details.menu[open]').forEach(d => { if (!d.contains(e.target)) d.removeAttribute('open'); });
  });

  // ---------- ✨ summary and writing help ----------
  const summaries = {};          // card id → html, so a live refresh doesn't lose it
  const escHtml = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function showSummary() {
    const box = $('[data-summary-box]', dialogBody);
    if (!box || !summaries[openCardId]) return;
    box.innerHTML = summaries[openCardId];
    box.hidden = false;
  }
  async function loadSummary() {
    const box = $('[data-summary-box]', dialogBody);
    if (!box) return;
    box.hidden = false;
    box.innerHTML = '<p class="muted small">✨ Reading the card…</p>';
    const id = openCardId;
    try {
      const d = await post(`/api/cards/${id}/summarise/`);
      const status = d.status ? `<span class="ai-status s-${d.status.replace(' ', '-')}">${escHtml(d.status)}</span>` : '';
      summaries[id] = `<div class="ai-summary-head"><b>✨ Summary</b>${status}<button class="icon-btn xs" data-close-summary title="Hide">×</button></div>
        <p>${escHtml(d.summary)}</p>
        ${d.next ? `<p><b>Next:</b> ${escHtml(d.next)}</p>` : ''}
        ${d.questions.length ? `<p class="muted small">Open questions:</p><ul>${d.questions.map(q => `<li>${escHtml(q)}</li>`).join('')}</ul>` : ''}`;
      if (id === openCardId) showSummary();
    } catch { box.innerHTML = '<p class="muted small">Couldn\'t summarise right now.</p>'; }
  }
  dialogBody.addEventListener('click', e => {
    if (!e.target.closest('[data-close-summary]')) return;
    delete summaries[openCardId];
    const box = $('[data-summary-box]', dialogBody); box.hidden = true; box.innerHTML = '';
  });
  async function writeDescription(btn) {
    const ta = $('[data-description] textarea', dialogBody);
    if (!ta) return;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = '✨ Writing…';
    try {
      const d = await post(`/api/cards/${openCardId}/write-description/`, { notes: ta.value });
      ta.value = d.text;
      ta.focus();
      toast('Draft written — check it, then Save. Cancel puts back what was there.');
    } catch { /* toast already shown */ }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  // ---------- ✨ suggest steps ----------
  async function loadSuggestions() {
    const box = $('[data-suggest-body]', dialogBody);
    if (!box) return;
    box.innerHTML = '<p class="muted small">Thinking… (a few seconds)</p>';
    const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    try {
      const data = await post(`/api/cards/${openCardId}/suggest-steps/`);
      box.innerHTML = `<form data-suggest-form>
        <label>Checklist title<input name="title" value="${esc(data.title)}" maxlength="120"></label>
        ${data.steps.map(s => `<label class="check-inline dark suggest-step"><input type="checkbox" name="step" value="${esc(s)}" checked> ${esc(s)}</label>`).join('')}
        <p class="muted small">Untick anything you don't want. AI can get things wrong, so check before adding.</p>
        <div class="row"><button class="btn primary sm">Add checklist</button><button type="button" class="btn ghost sm" data-suggest-again>Try again</button></div>
      </form>`;
    } catch {
      box.innerHTML = '<p class="muted small">Couldn\'t get suggestions right now.</p>';
    }
  }
  dialogBody.addEventListener('submit', async e => {
    const f = e.target.closest('[data-suggest-form]');
    if (!f) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const items = [...f.querySelectorAll('input[name=step]:checked')].map(i => i.value);
    if (!items.length) return toast('Tick at least one step.');
    hidePops();
    applyCard(await post(`/api/cards/${openCardId}/checklists/`, { title: f.title.value, items }));
  }, true);
  dialogBody.addEventListener('click', e => { if (e.target.closest('[data-suggest-again]')) loadSuggestions(); });

  // ---------- attachments ----------
  async function uploadFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    toast(`Uploading ${file.name}…`);
    try {
      const res = await fetch(`/api/cards/${openCardId}/attachments/`, {
        method: 'POST', body: fd, headers: { 'X-CSRFToken': csrf }, credentials: 'same-origin' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(data.error || 'Upload failed.'); return; }
      applyCard(data);
      toast('Attached.');
    } catch { toast('Upload failed — check your connection.'); }
  }
  // Drop a file anywhere on an open card to attach it
  dialog.addEventListener('dragover', e => { if (canEdit && e.dataTransfer?.types.includes('Files')) e.preventDefault(); });
  dialog.addEventListener('drop', e => {
    if (!canEdit || !e.dataTransfer?.files.length) return;
    e.preventDefault();
    uploadFile(e.dataTransfer.files[0]);
  });

  // ---------- card templates ----------
  listsEl.addEventListener('click', async e => {
    const btn = e.target.closest('[data-from-template]');
    if (!btn) return;
    const res = await fetch(`/api/boards/${boardId}/templates/`, { credentials: 'same-origin' });
    const { templates = [], department = '' } = res.ok ? await res.json() : {};
    if (!templates.length) {
      toast(`No templates yet in ${department || 'this department'}. Open a card and click “Make template” — it can then be used on any board in the department.`);
      return;
    }
    const pick = await LanesUI.choose('Create a card from which template?',
      templates.map((t, i) => ({ label: t.here ? t.title : `${t.title} · from ${t.board}`, value: i })));
    const tpl = pick === null ? null : templates[pick];
    if (!tpl) return;
    const listEl = btn.closest('.list');
    const body = { template_id: tpl.id };
    if (tpl.per_one) {                 // the template scales: ask how many
      const n = await LanesUI.ask('How many?', { type: 'number', ok: 'Create', hint: 'Every “per one” line is worked out from this. Leave empty to keep the template’s amount.' });
      if (n === null) return;
      if (n.trim()) body.amount = n.trim();
    }
    const data = await post(`/api/lists/${listEl.dataset.listId}/from-template/`, body);
    $('[data-list-cards]', listEl).appendChild(fromHTML(data.tile));
    refreshCounts();
    openCard(data.card_id);
  });

  // ---------- collapsed lists (personal, remembered in this browser) ----------
  const collapseKey = `lanes-collapsed-${boardId}`;
  function collapsedSet() {
    try { return new Set(JSON.parse(localStorage.getItem(collapseKey) || '[]')); } catch { return new Set(); }
  }
  function applyCollapsed() {
    const set = collapsedSet();
    $$('.list', listsEl).forEach(l => {
      const on = set.has(l.dataset.listId);
      l.classList.toggle('collapsed', on);
      if (on && !$('[data-expand-list]', l)) {
        const b = document.createElement('button');
        b.className = 'expand-btn'; b.dataset.expandList = ''; b.title = 'Expand list';
        b.innerHTML = `<span>${$('.list-title', l).textContent.trim()}</span><b>${$('.list-count', l).textContent}</b>`;
        l.prepend(b);
      }
      if (!on) $('[data-expand-list]', l)?.remove();
    });
  }
  function toggleCollapsed(id, on) {
    const set = collapsedSet();
    on ? set.add(id) : set.delete(id);
    try { localStorage.setItem(collapseKey, JSON.stringify([...set])); } catch { /* private mode */ }
    applyCollapsed();
  }
  applyCollapsed();
  function forceRefresh() { version = -1; poll(); }

  // ---------- live updates ----------
  // Poll a tiny version stamp; only when it moves do we pull fresh HTML.
  // Updates wait while you're dragging, typing in a list, or adding a card.
  let version = boardEl.dataset.version;
  let polling = false;

  function userIsBusy() {
    if (dragging) return true;
    const a = document.activeElement;
    if (a && listsEl.contains(a) && (a.isContentEditable || a.matches('input, textarea'))) return true;
    return $$('[data-add-card-form]', listsEl).some(f => !f.hidden);
  }

  function modalIsBusy() {
    const a = document.activeElement;
    return !!(a && dialogBody.contains(a) && (a.isContentEditable || a.matches('input, textarea')))
      || openPop !== null || !!dialogBody.querySelector('details[open]');
  }

  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const lite = view === 'board' ? '' : '&lite=1';
      const res = await fetch(`/api/boards/${boardId}/state/?v=${version}${lite}`, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      if (data.archived) { location.href = '/'; return; }
      if (String(data.version) === String(version)) return;
      if (view !== 'board') {                 // other views: just re-render the page when idle
        if (!dialog.open && !dragging && !document.querySelector('.view-busy')) location.reload();
        return;
      }
      if (!data.lists_html) return;
      if (userIsBusy()) return;              // try again next tick
      const scroll = listsEl.scrollLeft;
      const cardScroll = new Map($$('.list', listsEl).map(l => [l.dataset.listId, $('[data-list-cards]', l).scrollTop]));
      $$('.list', listsEl).forEach(l => l.remove());
      const tpl = document.createElement('template');
      tpl.innerHTML = data.lists_html;
      const anchor = document.getElementById('add-list');
      [...tpl.content.children].forEach(el => {
        listsEl.insertBefore(el, anchor);
        wireList(el);
        const top = cardScroll.get(el.dataset.listId);
        if (top) $('[data-list-cards]', el).scrollTop = top;
      });
      listsEl.scrollLeft = scroll;
      version = data.version;
      window.LanesBoard.applyFilter?.();
      applyCollapsed();
      if (dialog.open && openCardId && !modalIsBusy()) refreshOpenCard();
    } catch { /* offline for a moment — next tick will retry */ }
    finally { polling = false; }
  }

  async function refreshOpenCard() {
    const id = openCardId;
    const res = await fetch(`/cards/${id}/`, { credentials: 'same-origin' });
    if (!res.ok || id !== openCardId || modalIsBusy()) return;
    const scroller = $('.cm-main', dialogBody), side = $('.cm-side', dialogBody);
    const scroll = [scroller?.scrollTop, side?.scrollTop];
    dialogBody.innerHTML = await res.text();
    wireModal();
    if (scroll[0]) $('.cm-main', dialogBody).scrollTop = scroll[0];
    if (scroll[1]) $('.cm-side', dialogBody).scrollTop = scroll[1];
  }

  if (boardId) setInterval(poll, 4000);   // the All-boards pages don't poll a single board
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // ---------- inbox panel (drag inbox items onto a list) ----------
  const inboxDrawer = document.getElementById('inbox-drawer');
  if (inboxDrawer && window.LanesInbox) {
    let loaded = false;
    const wireInbox = box => {
      const items = box.querySelector('[data-inbox-items]');
      if (!items) return;
      items.querySelectorAll('[draggable]').forEach(el => el.removeAttribute('draggable'));
      Sortable.create(items, {
        group: { name: 'cards', pull: true, put: false },
        sort: false, animation: 150, forceFallback: true, fallbackOnBody: true, fallbackTolerance: 4,
        delay: 120, delayOnTouchOnly: true, draggable: '.inbox-item:not(.done)',
        filter: 'input, details, .inbox-done', preventOnFilter: false,
        onStart: () => (dragging = true),
        onEnd: async evt => {
          dragging = false;
          const el = evt.item;
          const target = evt.to.closest('.list');
          if (!target || evt.to === evt.from) return;
          try {
            const data = await post(`/api/inbox/${el.dataset.inboxId}/to-card/`,
              { list_id: +target.dataset.listId, ...neighbours(el, '.card-tile') });
            el.replaceWith(fromHTML(data.tile));
            inbox.setHtml(data.inbox_html);
            refreshCounts();
            toast('Card created from your inbox.');
          } catch { location.reload(); }
        },
      });
    };
    const inbox = LanesInbox.init(inboxDrawer.querySelector('[data-inbox-panel]'), {
      onChange: wireInbox,
      sendToBoard: () => toast('Drag the item onto a list to put it on this board.'),
    });
    window.LanesOpenInbox = async () => { if (inboxDrawer.hidden) $('[data-toggle-inbox]')?.click(); };
    window.LanesInboxReload = async () => {
      const res = await fetch('/api/inbox/', { credentials: 'same-origin' });
      if (res.ok) { inbox.setHtml((await res.json()).html); loaded = true; }
    };
    $('[data-toggle-inbox]').addEventListener('click', async () => {
      inboxDrawer.hidden = !inboxDrawer.hidden;
      boardEl.classList.toggle('with-inbox', !inboxDrawer.hidden);
      $('[data-toggle-inbox]').classList.toggle('on', !inboxDrawer.hidden);
      $('[data-toggle-inbox]').setAttribute('aria-pressed', !inboxDrawer.hidden);
      saveDock();
      if (!inboxDrawer.hidden && !loaded) {
        const res = await fetch('/api/inbox/', { credentials: 'same-origin' });
        if (res.ok) { inbox.setHtml((await res.json()).html); loaded = true; }
      }
    });
    $('[data-close-inbox]', inboxDrawer).addEventListener('click', () => {
      inboxDrawer.hidden = true; boardEl.classList.remove('with-inbox');
      $('[data-toggle-inbox]').classList.remove('on'); saveDock();
    });
  }

  // ---------- bottom dock (Inbox · Planner · Board · Switch boards · voice), like Trello ----------
  const plannerDrawer = document.getElementById('planner-drawer');
  const DOCK_KEY = 'lanes-dock';
  function saveDock() {
    try {
      localStorage.setItem(DOCK_KEY, JSON.stringify({
        inbox: !!inboxDrawer && !inboxDrawer.hidden, planner: !!plannerDrawer && !plannerDrawer.hidden,
        hideBoard: boardEl.classList.contains('board-hidden') }));
    } catch {}
  }
  async function loadPlanner() {
    try {
      const res = await fetch('/api/planner/panel/', { credentials: 'same-origin' });
      if (res.ok) $('[data-planner-body]', plannerDrawer).innerHTML = (await res.json()).html;
    } catch {}
  }
  function setPlanner(open) {
    if (!plannerDrawer) return;
    plannerDrawer.hidden = !open;
    boardEl.classList.toggle('with-planner', open);
    $('[data-toggle-planner]')?.classList.toggle('on', open);
    $('[data-toggle-planner]')?.setAttribute('aria-pressed', open);
    if (open) loadPlanner();
    saveDock();
  }
  $('[data-toggle-planner]')?.addEventListener('click', () => setPlanner(plannerDrawer.hidden));
  $('[data-close-planner]')?.addEventListener('click', () => setPlanner(false));
  $('[data-toggle-boardview]')?.addEventListener('click', e => {
    const btn = e.currentTarget;
    const hide = !boardEl.classList.contains('board-hidden');
    // hiding the board only makes sense when a panel is open — otherwise open the planner
    if (hide && (!inboxDrawer || inboxDrawer.hidden) && plannerDrawer.hidden) setPlanner(true);
    boardEl.classList.toggle('board-hidden', hide);
    btn.classList.toggle('on', !hide); btn.setAttribute('aria-pressed', !hide);
    saveDock();
  });
  $('[data-dock-switch]')?.addEventListener('click', () => window.LanesBoard_switch?.());
  // keep the planner fresh when the board changes
  setInterval(() => { if (plannerDrawer && !plannerDrawer.hidden && !document.hidden) loadPlanner(); }, 60000);
  try {
    const s = JSON.parse(localStorage.getItem(DOCK_KEY) || '{}');
    if (s.inbox && inboxDrawer) $('[data-toggle-inbox]')?.click();
    if (s.planner) setPlanner(true);
    if (s.hideBoard && (s.inbox || s.planner)) $('[data-toggle-boardview]')?.click();
  } catch {}

  // Voice capture: say a to-do and it lands in your Inbox (browser speech recognition, Chrome/Edge/Safari)
  const voiceBtn = $('[data-dock-voice]');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (voiceBtn && SR) {
    voiceBtn.hidden = false;
    let rec = null;
    voiceBtn.addEventListener('click', () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = document.documentElement.lang === 'en' ? 'en-GB' : (navigator.language || 'en-GB');
      rec.interimResults = false; rec.maxAlternatives = 1;
      voiceBtn.classList.add('listening');
      toast('🎙 Listening… say your to-do.');
      rec.onresult = async ev => {
        const text = ev.results[0][0].transcript.trim();
        if (!text) return;
        try {
          await post('/api/inbox/add/', { text: text.charAt(0).toUpperCase() + text.slice(1) });
          toast(`Added to your Inbox: “${text}”`);
          await window.LanesInboxReload?.();
          window.LanesOpenInbox?.();
        } catch (err) { toast(err.message || 'Could not add that.'); }
      };
      rec.onerror = ev => toast(ev.error === 'not-allowed' ? 'Allow the microphone to use voice capture.' : 'Didn\'t catch that — try again.');
      rec.onend = () => { voiceBtn.classList.remove('listening'); rec = null; };
      rec.start();
    });
  }

  // Any element with data-card-open="<id>" opens that card (used by the other views)
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-card-open]');
    if (el && !e.defaultPrevented) { e.preventDefault(); openCard(el.dataset.cardOpen); }
  });
  window.LanesBoard = { openCard, post, toast, boardId, setDragging: v => (dragging = v) };

  // ---------- filter bar ----------
  const filterForm = $('[data-filter-form]');
  const filterKey = `lanes-filter-${boardId}`;
  function readFilter() {
    if (!filterForm) return null;
    const fd = new FormData(filterForm);
    return { q: (fd.get('q') || '').trim().toLowerCase(), m: fd.getAll('m'), d: fd.getAll('d'),
             l: fd.getAll('l'), s: fd.getAll('s'), a: fd.getAll('a'), match: fd.get('match') || 'any' };
  }
  function applyFilter() {
    const f = readFilter();
    if (!f) return;
    const me = boardEl.dataset.userId;
    const now = Date.now() / 1000;
    const active = f.q || f.m.length || f.d.length || f.l.length || f.s.length || f.a.length;
    let shown = 0;
    $$('.card-tile', listsEl).forEach(t => {
      const members = (t.dataset.members || '').trim().split(/\s+/).filter(Boolean);
      const labels = (t.dataset.labels || '').trim().split(/\s+/).filter(Boolean);
      const due = t.dataset.dueTs ? +t.dataset.dueTs : null;
      const done = t.dataset.complete === '1';
      const updated = +(t.dataset.updatedTs || 0);
      const within = days => due !== null && due >= now && due <= now + days * 86400;
      const groups = [];
      if (f.m.length) groups.push(f.m.some(v => v === 'me' ? members.includes(me) : v === 'none' ? !members.length : members.includes(v)));
      if (f.l.length) groups.push(f.l.some(v => v === 'none' ? !labels.length : labels.includes(v)));
      if (f.d.length) groups.push(f.d.some(v => v === 'none' ? due === null
              : v === 'overdue' ? (due !== null && due < now && !done)
              : within({ day: 1, week: 7, month: 31 }[v] || 7)));
      if (f.s.length) groups.push(f.s.some(v => v === 'done' ? done : !done));
      if (f.a.length) groups.push(f.a.some(v => +v > 0 ? updated >= now - +v * 86400 : updated < now + +v * 86400));
      const ok = (!f.q || t.textContent.toLowerCase().includes(f.q))
        && (!groups.length || (f.match === 'all' ? groups.every(Boolean) : groups.some(Boolean)));
      t.hidden = !ok;
      if (ok) shown++;
    });
    const count = $('[data-filter-count]');
    if (count) { count.hidden = !active; count.textContent = active ? ` · ${shown}` : ''; }
    try { localStorage.setItem(filterKey, JSON.stringify(f)); } catch { /* private mode */ }
  }
  if (filterForm) {
    try {
      const saved = JSON.parse(localStorage.getItem(filterKey) || 'null');
      if (saved) {
        filterForm.q.value = saved.q || '';
        $$('input[type=checkbox]', filterForm).forEach(cb => {
          cb.checked = (saved[cb.name] || []).includes(cb.value);
        });
        if (saved.match) filterForm.match.value = saved.match;
      }
    } catch { /* ignore */ }
    filterForm.addEventListener('input', applyFilter);
    filterForm.addEventListener('submit', e => e.preventDefault());
    $('[data-toggle-filter]').addEventListener('click', e => {
      e.stopPropagation();
      filterForm.hidden = !filterForm.hidden;
      if (!filterForm.hidden) filterForm.q.focus();
    });
    document.addEventListener('click', e => {
      if (!filterForm.hidden && !e.target.closest('.filter-wrap')) filterForm.hidden = true;
    });
    $('[data-filter-clear]', filterForm).addEventListener('click', () => { filterForm.reset(); applyFilter(); });
    applyFilter();
  }
  window.LanesBoard.applyFilter = applyFilter;

  // ---------- board header: visibility, switch boards ----------
  const visPop = $('[data-vis-pop]');
  $('[data-vis-toggle]')?.addEventListener('click', e => { e.stopPropagation(); visPop.hidden = !visPop.hidden; });
  document.addEventListener('click', e => { if (visPop && !visPop.hidden && !e.target.closest('.vis-wrap')) visPop.hidden = true; });
  visPop?.addEventListener('click', async e => {
    const opt = e.target.closest('[data-vis]');
    if (opt && !opt.disabled && !opt.classList.contains('on')) {
      await post(`/api/boards/${boardId}/update/`, { visibility: opt.dataset.vis });
      return location.reload();
    }
    if (e.target.closest('[data-vis-public]')) {
      visPop.hidden = true;
      window.LanesDrawerGo('settings', 'Settings');
    }
  });

  async function switchBoards() {
    const res = await fetch('/api/boards/mine/', { credentials: 'same-origin' });
    const boards = (await res.json()).boards;
    const pick = await LanesUI.choose('Switch to…', boards.map(b => ({
      label: `${b.starred ? '★ ' : ''}${b.title} · ${b.dept}${String(b.id) === String(boardId) ? ' (this board)' : ''}`, value: b.id })));
    if (pick !== null && String(pick) !== String(boardId)) location.href = `/b/${pick}/`;
  }
  $('[data-switch-boards]')?.addEventListener('click', switchBoards);
  window.LanesBoard_switch = switchBoards;

  // ---------- menu: about, tools, labels ----------
  $('[data-board-about]', drawer)?.addEventListener('submit', async e => {
    e.preventDefault();
    await post(`/api/boards/${boardId}/update/`, { description: e.target.description.value });
    toast('Saved.');
  });
  drawer.addEventListener('click', async e => {
    if (e.target.closest('[data-collapse-all]')) {
      $$('.list', listsEl).forEach(l => toggleCollapsed(l.dataset.listId, true));
    } else if (e.target.closest('[data-expand-all]')) {
      $$('.list', listsEl).forEach(l => toggleCollapsed(l.dataset.listId, false));
    } else if (e.target.closest('[data-copy-board-link]')) {
      try { await navigator.clipboard.writeText(`${location.origin}/b/${boardId}/`); toast('Link copied.'); }
      catch { toast(`${location.origin}/b/${boardId}/`); }
    } else if (e.target.closest('[data-label-delete]')) {
      const row = e.target.closest('[data-label-edit]');
      if (!await LanesUI.confirm('Delete this label?', { text: 'It comes off every card that has it.', ok: 'Delete', danger: true })) return;
      await post(`/api/boards/${boardId}/labels/`, { label_id: +row.dataset.labelEdit, delete: true });
      row.remove(); forceRefresh();
    }
  });
  drawer.addEventListener('change', async e => {
    const row = e.target.closest('[data-label-edit]');
    if (!row) return;
    await post(`/api/boards/${boardId}/labels/`, { label_id: +row.dataset.labelEdit, name: row.name.value, colour: row.colour.value });
    $('[data-swatch]', row).className = `label-swatch lc-${row.colour.value}`;
    forceRefresh();
  });
  drawer.addEventListener('submit', async e => {
    const f = e.target.closest('[data-label-create]');
    if (f) {
      e.preventDefault();
      await post(`/api/boards/${boardId}/labels/`, { name: f.name.value, colour: f.colour.value });
      location.reload();
    }
    const row = e.target.closest('[data-label-edit]');
    if (row) { e.preventDefault(); row.querySelector('input').blur(); }
  });

  // ---------- quick-edit menu on a card (✎ on hover, or right-click) ----------
  const quick = document.createElement('div');
  quick.className = 'quick-menu';
  quick.hidden = true;
  document.body.appendChild(quick);
  const QUICK = [
    ['open', '🗂', 'Open card'], ['labels', '🏷', 'Edit labels', 1], ['members', '👤', 'Change members', 1],
    ['look', '🎨', 'Change cover', 1], ['dates', '🕒', 'Edit dates', 1], ['move', '↗', 'Move', 1],
    ['copy', '⧉', 'Copy card', 1], ['link', '🔗', 'Copy link'], ['mirror', '🪞', 'Mirror', 1], ['archive', '🗄', 'Archive', 1],
  ];
  function showQuick(tile, x, y) {
    setCurrent(tile);
    quick.dataset.cardId = tile.dataset.cardId;
    quick.innerHTML = QUICK.filter(q => canEdit || !q[3])
      .map(([k, icon, label]) => `<button data-q="${k}"><span>${icon}</span>${label}</button>`).join('');
    quick.hidden = false;
    const r = quick.getBoundingClientRect();
    quick.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
    quick.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
    quick.querySelector('button')?.focus();
  }
  const hideQuick = () => (quick.hidden = true);
  listsEl.addEventListener('click', e => {
    const b = e.target.closest('[data-quick-edit]');
    if (!b) return;
    e.stopImmediatePropagation(); e.preventDefault();
    const r = b.closest('.card-tile').getBoundingClientRect();
    showQuick(b.closest('.card-tile'), r.right + 6, r.top);
  }, true);
  // Round tick on the card front (slides in on hover, like Trello) → toggle complete without opening the card.
  listsEl.addEventListener('click', async e => {
    const b = e.target.closest('[data-tile-complete]');
    if (!b) return;
    e.stopImmediatePropagation(); e.preventDefault();
    if (boardEl.dataset.canEdit !== '1') return;
    const tile = b.closest('.card-tile');
    const done = !b.classList.contains('on');
    b.classList.toggle('on', done); b.classList.add('pop');           // instant feedback; server copy replaces it
    tile.classList.toggle('is-complete', done);
    try { await onCard(tile.dataset.cardId, 'update', { is_complete: done }); }
    catch (err) { b.classList.toggle('on', !done); tile.classList.toggle('is-complete', !done); toast(err.message || 'Could not update the card.'); }
  }, true);
  listsEl.addEventListener('contextmenu', e => {
    const tile = e.target.closest('.card-tile');
    if (!tile || e.target.closest('input, textarea')) return;
    e.preventDefault();
    showQuick(tile, e.clientX, e.clientY);
  });
  document.addEventListener('click', e => { if (!quick.hidden && !quick.contains(e.target)) hideQuick(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !quick.hidden) { e.stopPropagation(); hideQuick(); } }, true);
  // Escape inside a card: close the open popover first, only then the card (like Trello)
  const popIsOpen = () => openPop || $$('[data-pop-panel]:not([hidden])', dialogBody).length || $('details[open]', dialogBody);
  const closePopsOnly = () => { hidePops(); $$('details[open]', dialogBody).forEach(d => d.removeAttribute('open')); };
  dialog.addEventListener('keydown', e => {
    if (e.key === 'Escape' && popIsOpen()) { e.preventDefault(); e.stopPropagation(); closePopsOnly(); }
  }, true);
  dialog.addEventListener('cancel', e => { if (popIsOpen()) { e.preventDefault(); closePopsOnly(); } });
  quick.addEventListener('click', async e => {
    const b = e.target.closest('[data-q]');
    if (!b) return;
    const id = quick.dataset.cardId, k = b.dataset.q;
    hideQuick();
    if (k === 'open') return openCard(id);
    if (k === 'link') {
      const url = `${location.origin}/b/${boardId}/?card=${id}`;
      try { await navigator.clipboard.writeText(url); toast('Link copied.'); } catch { toast(url); }
      return;
    }
    if (k === 'archive') {
      try { await onCard(id, 'update', { archive: true }); toast('Card archived. Restore it from Menu → Archived.'); } catch {}
      return;
    }
    if (k === 'move') {
      await openCard(id);
      if ($('[data-pop="movecard"]', dialogBody)) { showPop('movecard'); $('[data-mv-list]', dialogBody)?.focus(); }
      return;
    }
    return openWithPop(id, k);
  });

  // ---------- Ctrl/⌘ + C / X / V: copy or cut a card under the mouse, paste onto the list under the mouse ----------
  // Works between boards (the clip lives in localStorage). Pasting ordinary text onto a list makes cards from it.
  const CLIP = 'lanes-card-clip';
  let hoverList = null;
  listsEl.addEventListener('mouseover', e => { hoverList = e.target.closest('.list') || hoverList; });
  listsEl.addEventListener('mouseleave', () => { hoverList = null; });
  const editing = el => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  document.addEventListener('keydown', async e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || editing(document.activeElement)) return;
    if (dialog.open || String(window.getSelection() || '')) return;
    const key = e.key.toLowerCase();
    if ((key === 'c' || key === 'x') && kbTile && document.body.contains(kbTile) && kbTile.matches(':hover, :focus, .kb-current')) {
      if (boardEl.dataset.canEdit !== '1') return;     // observers can't paste anyway
      e.preventDefault();
      const marker = `${location.origin}/b/${boardId}/?card=${kbTile.dataset.cardId}`;
      try { localStorage.setItem(CLIP, JSON.stringify({ id: kbTile.dataset.cardId, mode: key === 'x' ? 'move' : 'copy', marker, at: Date.now() })); } catch {}
      try { await navigator.clipboard.writeText(marker); } catch { /* clipboard blocked: the local clip still works */ }
      const title = kbTile.dataset.title || 'Card';
      toast(`${key === 'x' ? 'Cut' : 'Copied'} “${title.slice(0, 40)}”. Point at a list and press ${navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}+V.`);
    }
  });
  document.addEventListener('paste', async e => {
    if (editing(e.target) || editing(document.activeElement) || dialog.open || !hoverList) return;
    if (boardEl.dataset.canEdit !== '1') return;
    const text = (e.clipboardData?.getData('text/plain') || '').trim();
    let clip = null;
    try { clip = JSON.parse(localStorage.getItem(CLIP) || 'null'); } catch {}
    const listId = hoverList.dataset.listId;
    if (!listId) return;
    e.preventDefault();
    try {
      if (clip && (!text || text === clip.marker) && Date.now() - clip.at < 6 * 3600e3) {
        if (clip.mode === 'move') {
          await post(`/api/cards/${clip.id}/move/`, { list_id: +listId });
          localStorage.removeItem(CLIP);
          toast('Card moved.');
        } else {
          await post(`/api/cards/${clip.id}/copy/`, { list_id: +listId });
          toast('Card copied.');
        }
      } else if (text) {
        const lines = text.split(/\r?\n/).map(s => s.replace(/^[-*•\d.)\s]+/, '').trim()).filter(Boolean).slice(0, 20);
        for (const title of lines) await post(`/api/lists/${listId}/cards/`, { title: title.slice(0, 250) });
        toast(lines.length === 1 ? 'Card added from your clipboard.' : `${lines.length} cards added from your clipboard.`);
      } else return;
      forceRefresh();
    } catch (err) { toast(err.message || 'Could not paste there.'); }
  });

  // ---------- keyboard shortcuts (press ? for the list) ----------
  let kbTile = null;                       // the card shortcuts act on when no card is open
  function setCurrent(tile, byKey) {
    kbTile?.classList.remove('kb-current', 'kb-key');
    if (byKey) tile?.classList.add('kb-key');
    kbTile = tile;
    tile?.classList.add('kb-current');
  }
  listsEl.addEventListener('mouseover', e => { const t = e.target.closest('.card-tile'); if (t) { const f = document.activeElement; if (f?.classList?.contains('card-tile') && f !== t) f.blur(); setCurrent(t); t.classList.remove('kb-key'); } });
  listsEl.addEventListener('focusin', e => { const t = e.target.closest('.card-tile'); if (t && t !== kbTile) setCurrent(t); });
  const visibleTiles = list => $$('.card-tile', list).filter(t => !t.hidden);
  function moveCurrent(dx, dy) {
    const lists = $$('.list', listsEl).filter(l => !l.classList.contains('collapsed') && visibleTiles(l).length);
    if (!lists.length) return;
    if (!kbTile || !document.body.contains(kbTile)) { setCurrent(visibleTiles(lists[0])[0], true); kbTile.focus(); return; }
    let li = lists.indexOf(kbTile.closest('.list'));
    let ti = visibleTiles(lists[li] || lists[0]).indexOf(kbTile);
    if (li < 0) { li = 0; ti = 0; }
    if (dx) { li = Math.max(0, Math.min(lists.length - 1, li + dx)); ti = Math.min(ti, visibleTiles(lists[li]).length - 1); }
    if (dy) ti = Math.max(0, Math.min(visibleTiles(lists[li]).length - 1, ti + dy));
    const t = visibleTiles(lists[li])[ti];
    setCurrent(t, true); t.focus(); t.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  const targetCardId = () => (dialog.open ? openCardId : kbTile && document.body.contains(kbTile) ? kbTile.dataset.cardId : null);
  async function onCard(id, path, payload) {
    const data = await post(`/api/cards/${id}/${path}/`, payload);
    applyCard(data);
    return data;
  }
  async function openWithPop(id, pop) {
    if (!dialog.open || openCardId !== String(id)) await openCard(id);
    const btn = $(`[data-pop="${pop}"]`, dialogBody);
    if (btn) { showPop(pop); $(`[data-pop-panel="${pop}"] input, [data-pop-panel="${pop}"] button`, dialogBody)?.focus(); }
    else toast("You can't change this card.");
  }
  document.addEventListener('keydown', async e => {
    if (e.defaultPrevented || window.LanesKeys?.typing(e)) return;
    const k = e.key;
    const otherDialog = [...document.querySelectorAll('dialog[open]')].some(d => d !== dialog);
    if (otherDialog) return;
    if (!dialog.open && view === 'board') {
      const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
                       h: [-1, 0], l: null, j: [0, 1], k: [0, -1] };
      if (k in arrows && arrows[k]) { e.preventDefault(); return moveCurrent(...arrows[k]); }
      if (k === 'n' && canEdit) {
        e.preventDefault();
        const list = kbTile?.closest('.list') || $('.list', listsEl);
        return list && openAddCard(list);
      }
      if (k === 'f' && $('[data-toggle-filter]')) { e.preventDefault(); return $('[data-toggle-filter]').click(); }
      if (k === 'q' && filterForm) {
        e.preventDefault();
        const me = $('input[name=m][value=me]', filterForm);
        me.checked = !me.checked; applyFilter();
        return toast(me.checked ? 'Showing only your cards (q to undo).' : 'Showing everyone\'s cards.');
      }
      if (k === 's') { e.preventDefault(); return switchBoards(); }
      if (k === 'x' && filterForm) { e.preventDefault(); filterForm.reset(); applyFilter(); return toast('Filters cleared.'); }
    }
    const id = targetCardId();
    if (!id) return;
    if (k === 'Enter' && !dialog.open && !e.target.closest('.card-tile')) { e.preventDefault(); return openCard(id); }
    if (k === ' ') {
      e.preventDefault();
      try { await onCard(id, 'members', { user_id: +boardEl.dataset.userId }); toast('Updated your membership of the card.'); } catch {}
      return;
    }
    if (k === 'l') { e.preventDefault(); return openWithPop(id, 'labels'); }
    if (k === 'd') { e.preventDefault(); return openWithPop(id, 'dates'); }
    if (k === 'm') { e.preventDefault(); return openWithPop(id, 'members'); }
    if (k === 't') {
      e.preventDefault();
      if (!dialog.open || openCardId !== String(id)) await openCard(id);
      const title = $('[data-card-title]', dialogBody);
      if (title) { title.focus(); document.getSelection().selectAllChildren(title); }
      return;
    }
    if (k === 'w') { e.preventDefault(); try { await onCard(id, 'watch'); toast('Watch toggled.'); } catch {} return; }
    if (k === 'v') { e.preventDefault(); try { await onCard(id, 'vote'); } catch {} return; }
    if (k === 'c' && canEdit) {
      e.preventDefault();
      try { await onCard(id, 'update', { archive: true }); if (dialog.open) closeCard(); toast('Card archived. Restore it from the board menu.'); } catch {}
      return;
    }
    if (/^[1-9]$/.test(k)) {
      const ids = JSON.parse(boardEl.dataset.labelIds || '[]');
      const lid = ids[+k - 1];
      if (!lid) return;
      e.preventDefault();
      try { await onCard(id, 'labels', { label_id: lid }); } catch {}
    }
  });

  // Deep link: /b/1/?card=5 opens that card
  if (boardEl.dataset.openCard) openCard(boardEl.dataset.openCard);
})();
