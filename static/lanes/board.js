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
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

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
      toast('Connection lost — your change was not saved.');
      throw new Error('network');
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(body.error || 'Something went wrong.');
      throw new Error(body.error || res.status);
    }
    return body;
  }

  function fromHTML(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
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
      filter: '.list-title, .menu, input, textarea',
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
    } else if (e.target.closest('[data-list-colour]')) {
      await post(`/api/lists/${listEl.dataset.listId}/`, { colour: e.target.closest('[data-list-colour]').dataset.listColour });
      e.target.closest('details')?.removeAttribute('open');
      forceRefresh();
    } else if (e.target.closest('[data-list-limit]')) {
      e.target.closest('details')?.removeAttribute('open');
      const cur = listEl.dataset.limit || '';
      const v = prompt('Maximum number of cards in this list (leave empty for no limit):', cur);
      if (v === null) return;
      await post(`/api/lists/${listEl.dataset.listId}/`, { card_limit: v.trim() });
      forceRefresh();
    } else if (e.target.closest('[data-collapse-list]')) {
      e.target.closest('details')?.removeAttribute('open');
      toggleCollapsed(listEl.dataset.listId, true);
    } else if (e.target.closest('[data-expand-list]')) {
      toggleCollapsed(listEl.dataset.listId, false);
    } else if (e.target.closest('[data-archive-list]')) {
      if (!confirm('Archive this list and hide its cards?')) return;
      await post(`/api/lists/${listEl.dataset.listId}/`, { archive: true });
      listEl.remove();
    } else {
      const tile = e.target.closest('.card-tile');
      if (tile) openCard(tile.dataset.cardId);
    }
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
    const data = await post(`/api/lists/${listEl.dataset.listId}/cards/`, { title });
    const cards = $('[data-list-cards]', listEl);
    cards.appendChild(fromHTML(data.tile));
    cards.scrollTop = cards.scrollHeight;
    ta.value = '';
    ta.focus();
    refreshCounts();
  });

  function openAddCard(listEl) {
    $('[data-add-card]', listEl).hidden = true;
    const form = $('[data-add-card-form]', listEl);
    form.hidden = false;
    $('textarea', form).focus();
  }
  function closeAddCard(listEl) {
    $('[data-add-card-form]', listEl).hidden = true;
    $('[data-add-card]', listEl).hidden = false;
  }

  // ---------- card dialog ----------
  let openCardId = null;
  let openPop = null;

  async function openCard(id) {
    const res = await fetch(`/cards/${id}/`, { credentials: 'same-origin' });
    if (!res.ok) { toast('That card could not be opened.'); return; }
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

  const cardPost = (path, payload) => post(`/api/cards/${openCardId}/${path}/`, payload).then(applyCard);

  function showPop(name) {
    $$('[data-pop-panel]', dialogBody).forEach(p => (p.hidden = p.dataset.popPanel !== name));
    openPop = name;
  }
  function hidePops() {
    $$('[data-pop-panel]', dialogBody).forEach(p => (p.hidden = true));
    openPop = null;
  }

  function wireModal() {
    const root = $('.cm', dialogBody);
    if (!root) return;
    const title = $('[data-card-title]', root);
    if (title) inlineEdit(title, v => cardPost('update', { title: v }));

    const desc = $('[data-description]', root);
    if (desc) {
      const ta = $('textarea', desc), saveRow = $('.save-row', desc);
      let original = ta.value;
      ta.addEventListener('focus', () => (saveRow.hidden = false));
      $('[data-cancel-desc]', desc).addEventListener('click', () => { ta.value = original; saveRow.hidden = true; });
      desc.addEventListener('submit', e => {
        e.preventDefault();
        cardPost('update', { description: ta.value }).then(() => (original = ta.value));
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
      showPop(name);
      if (popBtn.hasAttribute('data-suggest-steps')) loadSuggestions();
      return;
    }
    if (!t.closest('.pop')) hidePops();

    if (t.closest('[data-watch-card]')) return cardPost('watch');
    const press = t.closest('[data-press-card]');
    if (press) {
      press.disabled = true;
      return post(`/api/cards/${openCardId}/buttons/${press.dataset.pressCard}/`).then(data => {
        applyCard(data);
        toast(data.summary);
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
    const hide = t.closest('[data-hide-checked]');
    if (hide) return post(`/api/checklists/${hide.closest('[data-checklist]').dataset.checklist}/`,
      { hide_checked: hide.dataset.hideChecked === '1' }).then(applyCard);
    if (t.closest('[data-delete-checklist]')) {
      if (!confirm('Delete this checklist?')) return;
      return post(`/api/checklists/${t.closest('[data-checklist]').dataset.checklist}/`, { delete: true }).then(applyCard);
    }
    const delItem = t.closest('[data-delete-item]');
    if (delItem) return post(`/api/items/${delItem.dataset.deleteItem}/`, { delete: true }).then(applyCard);
    const rmMirror = t.closest('[data-remove-mirror]');
    if (rmMirror) {
      if (!confirm('Remove the card from that board? The original stays where it is.')) return;
      return post(`/api/mirrors/${rmMirror.dataset.removeMirror}/`, { delete: true }).then(data => {
        $(`.card-tile[data-mirror-id="${rmMirror.dataset.removeMirror}"]`, listsEl)?.remove();
        refreshCounts();
        applyCard(data);
      });
    }
    const cc = t.closest('[data-cover-colour]');
    if (cc) return cardPost('update', { cover_colour: cc.dataset.coverColour });
    const st = t.closest('[data-sticker]');
    if (st) return cardPost('update', { sticker: st.dataset.sticker });
    const tplBtn = t.closest('[data-template-toggle]');
    if (tplBtn) return cardPost('update', { is_template: tplBtn.dataset.templateToggle === '1' });
    const cover = t.closest('[data-cover]');
    if (cover) return post(`/api/attachments/${cover.dataset.cover}/`, { cover: cover.dataset.on === '1' }).then(applyCard);
    const delAtt = t.closest('[data-delete-attachment]');
    if (delAtt) {
      if (!confirm('Delete this attachment?')) return;
      return post(`/api/attachments/${delAtt.dataset.deleteAttachment}/`, { delete: true }).then(applyCard);
    }
    const delComment = t.closest('[data-delete-comment]');
    if (delComment) {
      if (!confirm('Delete this comment?')) return;
      return post(`/api/comments/${delComment.dataset.deleteComment}/`, { delete: true }).then(applyCard);
    }
  });

  dialogBody.addEventListener('change', e => {
    const t = e.target;
    if (t.matches('[data-label]')) return cardPost('labels', { label_id: +t.dataset.label });
    if (t.matches('[data-item]')) return post(`/api/items/${t.dataset.item}/`, { is_done: t.checked }).then(applyCard);
    if (t.matches('[data-move-list]')) return cardPost('move', { list_id: +t.value });
    if (t.matches('[data-attach]') && t.files.length) return uploadFile(t.files[0]);
    if (t.matches('[data-field]')) {
      const value = t.type === 'checkbox' ? t.checked : t.value;
      return cardPost('fields', { field_id: +t.dataset.field, value });
    }
  });

  dialogBody.addEventListener('submit', e => {
    const f = e.target;
    if (f.matches('[data-dates]')) {
      e.preventDefault();
      hidePops();
      return cardPost('update', { due_at: f.due_at.value, start_date: f.start_date.value });
    }
    if (f.matches('[data-checklist-new]')) {
      e.preventDefault();
      hidePops();
      return cardPost('checklists', { title: f.title.value }).then(() => {
        const inputs = $$('[data-item-new] input', dialogBody);
        inputs[inputs.length - 1]?.focus();   // straight into "Add an item"
      });
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
      return post(`/api/items/${f.dataset.itemMeta}/`,
        { assignee_id: f.assignee_id.value ? +f.assignee_id.value : null, due_at: f.due_at.value }).then(applyCard);
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
    const data = await post(`/api/boards/${boardId}/star/`);
    e.currentTarget.classList.toggle('on', data.starred);
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
  $$('[data-open-share]').forEach(b => b.addEventListener('click', () => share.showModal()));
  $('[data-close]', share)?.addEventListener('click', () => share.close());
  share.addEventListener('click', e => { if (e.target === share) share.close(); });
  const shareMembers = document.getElementById('share-members') || document.createElement('div');
  const updateMembers = data => {
    shareMembers.innerHTML = data.html;
    document.getElementById('member-count').textContent = data.count;
  };
  document.getElementById('share-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target;
    const data = await post(`/api/boards/${boardId}/share/`, { email: f.email.value, role: f.role.value });
    updateMembers(data);
    f.email.value = '';
    toast('Added to the board. Reload to assign them to cards.');
  });
  shareMembers.addEventListener('change', async e => {
    const s = e.target.closest('[data-member-role]');
    if (!s) return;
    try { updateMembers(await post(`/api/boards/${boardId}/members/${s.dataset.memberRole}/`, { role: s.value })); }
    catch { location.reload(); }
  });
  shareMembers.addEventListener('click', async e => {
    const b = e.target.closest('[data-member-remove]');
    if (!b || !confirm('Remove from this board?')) return;
    updateMembers(await post(`/api/boards/${boardId}/members/${b.dataset.memberRemove}/`, { remove: true }));
  });

  // Drawer (menu): activity, archived, settings
  const drawer = document.getElementById('drawer') || document.createElement('aside');
  $('[data-open-drawer]')?.addEventListener('click', () => (drawer.hidden = !drawer.hidden));
  $('[data-close-drawer]')?.addEventListener('click', () => (drawer.hidden = true));
  $$('[data-tab]', drawer).forEach(tab => tab.addEventListener('click', () => {
    $$('[data-tab]', drawer).forEach(t => t.classList.toggle('on', t === tab));
    $$('[data-pane]', drawer).forEach(p => (p.hidden = p.dataset.pane !== tab.dataset.tab));
  }));
  drawer.addEventListener('click', async e => {
    const link = e.target.closest('[data-card-link]');
    if (link) { e.preventDefault(); return openCard(link.dataset.cardLink); }
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
      if (!confirm('Archive this board? It disappears for everyone until an admin restores it.')) return;
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
  drawer.addEventListener('change', e => {
    const show = e.target.closest('[data-field-show]');
    if (show) post(`/api/boards/${boardId}/fields/`,
      { field_id: +show.closest('[data-field-row]').dataset.fieldRow, show_on_card: show.checked });
  });
  drawer.addEventListener('click', async e => {
    const del = e.target.closest('[data-field-delete]');
    if (!del || !confirm('Delete this field and every value stored in it?')) return;
    await post(`/api/boards/${boardId}/fields/`, { field_id: +del.closest('[data-field-row]').dataset.fieldRow, delete: true });
    del.closest('[data-field-row]').remove();
  });

  $('[data-board-collection]', drawer)?.addEventListener('change', async e => {
    let value = e.target.value;
    if (value === '__new') {
      const name = prompt('Name the new collection (e.g. Christmas 2026):');
      if (!name || !name.trim()) { location.reload(); return; }
      value = `new:${name.trim()}`;
    }
    await post(`/api/boards/${boardId}/update/`, { collection: value });
    location.reload();
  });
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
    const templates = $$('.card-tile.is-template', listsEl);
    if (!templates.length) {
      toast('No templates yet. Open a card and click “Make template” to create one.');
      return;
    }
    const choice = prompt('Create a card from which template?\n\n' +
      templates.map((t, i) => `${i + 1}. ${t.dataset.title}`).join('\n') + '\n\nType a number:');
    const tpl = templates[parseInt(choice, 10) - 1];
    if (!tpl) return;
    const listEl = btn.closest('.list');
    const data = await post(`/api/lists/${listEl.dataset.listId}/from-template/`, { template_id: +tpl.dataset.cardId });
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
      || openPop !== null || !!dialogBody.querySelector('details.item-more[open]');
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
    $('[data-toggle-inbox]').addEventListener('click', async () => {
      inboxDrawer.hidden = !inboxDrawer.hidden;
      boardEl.classList.toggle('with-inbox', !inboxDrawer.hidden);
      if (!inboxDrawer.hidden && !loaded) {
        const res = await fetch('/api/inbox/', { credentials: 'same-origin' });
        if (res.ok) { inbox.setHtml((await res.json()).html); loaded = true; }
      }
    });
    $('[data-close-inbox]', inboxDrawer).addEventListener('click', () => {
      inboxDrawer.hidden = true; boardEl.classList.remove('with-inbox');
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
             l: fd.getAll('l'), hide_done: !!fd.get('hide_done') };
  }
  function applyFilter() {
    const f = readFilter();
    if (!f) return;
    const me = boardEl.dataset.userId;
    const now = Date.now() / 1000;
    const active = f.q || f.m.length || f.d.length || f.l.length || f.hide_done;
    let shown = 0;
    $$('.card-tile', listsEl).forEach(t => {
      const members = (t.dataset.members || '').trim().split(/\s+/).filter(Boolean);
      const labels = (t.dataset.labels || '').trim().split(/\s+/).filter(Boolean);
      const due = t.dataset.dueTs ? +t.dataset.dueTs : null;
      const done = t.dataset.complete === '1';
      const ok = (!f.q || t.textContent.toLowerCase().includes(f.q))
        && (!f.m.length || f.m.some(v => v === 'me' ? members.includes(me) : v === 'none' ? !members.length : members.includes(v)))
        && (!f.l.length || f.l.some(v => labels.includes(v)))
        && (!f.d.length || f.d.some(v => v === 'none' ? due === null
              : v === 'overdue' ? (due !== null && due < now && !done)
              : v === 'day' ? (due !== null && due >= now && due <= now + 86400)
              : (due !== null && due >= now && due <= now + 7 * 86400)))
        && !(f.hide_done && done);
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
          cb.checked = cb.name === 'hide_done' ? !!saved.hide_done : (saved[cb.name] || []).includes(cb.value);
        });
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

  // Deep link: /b/1/?card=5 opens that card
  if (boardEl.dataset.openCard) openCard(boardEl.dataset.openCard);
})();
