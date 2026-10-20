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
    return { before_id: prev ? +prev.dataset[selector === '.list' ? 'listId' : 'cardId'] : null,
             after_id: next ? +next.dataset[selector === '.list' ? 'listId' : 'cardId'] : null };
  }

  function refreshCounts() {
    $$('.list').forEach(l => {
      const c = $('.list-count', l);
      if (c) c.textContent = $$('.card-tile', l).length;
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
          const data = await post(`/api/cards/${tile.dataset.cardId}/move/`,
            { list_id: +listEl.dataset.listId, ...neighbours(tile, '.card-tile') });
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
    const old = $(`.card-tile[data-card-id="${data.card_id}"]`, listsEl);
    if (data.archived) {
      old?.remove();
    } else {
      const tile = fromHTML(data.tile);
      const target = $(`.list[data-list-id="${data.list_id}"] [data-list-cards]`, listsEl);
      if (old && old.closest('.list')?.dataset.listId === String(data.list_id)) old.replaceWith(tile);
      else { old?.remove(); target?.appendChild(tile); }
    }
    refreshCounts();
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
      return openPop === name ? hidePops() : showPop(name);
    }
    if (!t.closest('.pop')) hidePops();

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
  $('[data-join]')?.addEventListener('click', async () => {
    await post(`/api/boards/${boardId}/join/`);
    location.reload();
  });

  // Share dialog
  const share = document.getElementById('share-dialog');
  $$('[data-open-share]').forEach(b => b.addEventListener('click', () => share.showModal()));
  $('[data-close]', share).addEventListener('click', () => share.close());
  share.addEventListener('click', e => { if (e.target === share) share.close(); });
  const shareMembers = document.getElementById('share-members');
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
  const drawer = document.getElementById('drawer');
  $('[data-open-drawer]').addEventListener('click', () => (drawer.hidden = !drawer.hidden));
  $('[data-close-drawer]').addEventListener('click', () => (drawer.hidden = true));
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

  setInterval(poll, 4000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // Any element with data-card-open="<id>" opens that card (used by the other views)
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-card-open]');
    if (el && !e.defaultPrevented) { e.preventDefault(); openCard(el.dataset.cardOpen); }
  });
  window.LanesBoard = { openCard, post, toast, boardId, setDragging: v => (dragging = v) };

  // Deep link: /b/1/?card=5 opens that card
  if (boardEl.dataset.openCard) openCard(boardEl.dataset.openCard);
})();
