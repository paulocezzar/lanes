/* Calendar and Table view interactions. Card dialogs, live updates and the
   API helper come from board.js via window.LanesBoard. */
(() => {
  const L = window.LanesBoard;
  if (!L) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const canEdit = document.getElementById('board').dataset.canEdit === '1';

  // ---------- Calendar ----------
  const cal = $('[data-calendar]');
  if (cal && canEdit) {
    let dragged = null;
    cal.addEventListener('dragstart', e => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      dragged = chip;
      chip.classList.add('dragging');
      cal.classList.add('view-busy');            // hold live refresh while dragging
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', chip.dataset.cardId);
    });
    cal.addEventListener('dragend', () => {
      dragged?.classList.remove('dragging');
      cal.classList.remove('view-busy');
      $$('.drop-over', cal).forEach(el => el.classList.remove('drop-over'));
    });
    cal.addEventListener('dragover', e => {
      const target = e.target.closest('[data-day]');
      if (!target || !dragged) return;
      e.preventDefault();
      $$('.drop-over', cal).forEach(el => el !== target && el.classList.remove('drop-over'));
      target.classList.add('drop-over');
    });
    cal.addEventListener('drop', async e => {
      const target = e.target.closest('[data-day]');
      if (!target || !dragged) return;
      e.preventDefault();
      const day = target.dataset.day;
      const chip = dragged;
      const due = day ? `${day}T${chip.dataset.time || '17:00'}` : '';
      // move it straight away, then save
      (target.matches('.cal-chips') ? target : $('.cal-chips', target)).appendChild(chip);
      try {
        await L.post(`/api/cards/${chip.dataset.cardId}/update/`, { due_at: due });
        chip.dataset.time = day ? (chip.dataset.time || '17:00') : '';
      } catch { location.reload(); }
    });

    // "+ Add" (bottom-left) → Card / List, and "+" on a day → Add card due that day (like Trello)
    const menu = $('[data-cal-add-menu]'), cardForm = $('[data-cal-card-form]'), listForm = $('[data-cal-list-form]');
    const closeAll = () => [menu, cardForm, listForm].forEach(el => el && (el.hidden = true));
    const openCardForm = day => {
      closeAll();
      cardForm.reset();
      cardForm.due.value = day || new Date().toISOString().slice(0, 10);
      cardForm.start.disabled = true;
      cardForm.hidden = false;
      cardForm.title.focus();
    };
    document.addEventListener('click', e => {
      if (e.target.closest('[data-cal-add-toggle]')) { const open = menu.hidden; closeAll(); menu.hidden = !open; return; }
      const nw = e.target.closest('[data-cal-new]');
      if (nw) {
        if (nw.dataset.calNew === 'card') return openCardForm();
        closeAll(); listForm.reset(); listForm.position.selectedIndex = listForm.position.options.length - 1;
        listForm.hidden = false; return listForm.title.focus();
      }
      if (e.target.closest('[data-cal-close]')) return closeAll();
      const plus = e.target.closest('[data-cal-add]');
      if (plus && cardForm) { e.preventDefault(); return openCardForm(plus.closest('[data-day]').dataset.day); }
      if (!e.target.closest('.cal-addbar')) closeAll();
    });
    if (cardForm) {
      cardForm.has_start.addEventListener('change', () => { cardForm.start.disabled = !cardForm.has_start.checked; });
      cardForm.has_due.addEventListener('change', () => { cardForm.due.disabled = cardForm.due_time.disabled = !cardForm.has_due.checked; });
      cardForm.addEventListener('submit', async e => {
        e.preventDefault();
        const title = cardForm.title.value.trim();
        if (!title) return;
        try {
          const data = await L.post(`/api/lists/${cardForm.list.value}/cards/`, { title });
          const upd = {};
          if (cardForm.has_due.checked && cardForm.due.value) upd.due_at = `${cardForm.due.value}T${cardForm.due_time.value || '17:00'}`;
          if (cardForm.has_start.checked && cardForm.start.value) upd.start_date = cardForm.start.value;
          if (Object.keys(upd).length) await L.post(`/api/cards/${data.card_id}/update/`, upd);
          location.reload();
        } catch (err) { L.toast(err.message || 'Could not add the card.'); }
      });
      listForm.addEventListener('submit', async e => {
        e.preventDefault();
        const title = listForm.title.value.trim();
        if (!title) return;
        try {
          await L.post(`/api/boards/${L.boardId}/lists/`, { title, position: +listForm.position.value });
          L.toast(`List “${title}” added.`);
          setTimeout(() => location.reload(), 500);
        } catch (err) { L.toast(err.message || 'Could not add the list.'); }
      });
    }
  }

  // Calendar toolbar (everyone): month picker, Month/Week, No due date panel, Sync link
  if (cal) {
    cal.addEventListener('click', e => {
      const more = e.target.closest('[data-cal-more]');
      if (!more) return;
      e.preventDefault();
      const box = more.closest('.cal-chips');
      box.classList.toggle('expanded');
      more.textContent = box.classList.contains('expanded') ? 'Show less' : `+${box.querySelectorAll('.chip').length - 3} more`;
    });
    const qs = new URLSearchParams(location.search);
    $('[data-cal-month]')?.addEventListener('change', e => {
      qs.delete('v'); qs.delete('d'); qs.set('m', e.target.value); location.search = qs.toString();
    });
    $('[data-cal-mode]')?.addEventListener('change', e => {
      if (e.target.value === 'week') { qs.set('v', 'week'); } else { qs.delete('v'); qs.delete('d'); }
      location.search = qs.toString();
    });
    const side = $('[data-cal-unsched]');
    const key = 'lanes-cal-unsched';
    try { if (localStorage.getItem(key) === '1' && side) side.hidden = false; } catch {}
    $('[data-cal-unsched-toggle]')?.addEventListener('click', () => {
      side.hidden = !side.hidden;
      try { localStorage.setItem(key, side.hidden ? '0' : '1'); } catch {}
    });
    const sync = $('[data-cal-sync]');
    $('[data-cal-sync-toggle]')?.addEventListener('click', e => { e.stopPropagation(); sync.hidden = !sync.hidden; });
    document.addEventListener('click', e => { if (sync && !sync.hidden && !e.target.closest('.cal-sync-pop, [data-cal-sync-toggle]')) sync.hidden = true; });
    $('[data-copy-ical]')?.addEventListener('click', async () => {
      const input = $('[data-ical-url]');
      try { await navigator.clipboard.writeText(input.value); L.toast('Calendar link copied.'); }
      catch { input.select(); L.toast('Press Ctrl+C to copy.'); }
    });
  }

  // ---------- "+ Add card" on the Table and Timeline views ----------
  const addForm = $('[data-view-add-form]');
  if (addForm) {
    document.addEventListener('click', e => {
      if (e.target.closest('[data-view-add-toggle]')) {
        addForm.hidden = !addForm.hidden;
        if (!addForm.hidden) addForm.title.focus();
      } else if (!addForm.hidden && !e.target.closest('.view-add')) addForm.hidden = true;
    });
    addForm.addEventListener('submit', async e => {
      e.preventDefault();
      const title = addForm.title.value.trim();
      if (!title) return;
      try {
        const data = await L.post(`/api/lists/${addForm.list.value}/cards/`, { title });
        if (addForm.due.value) await L.post(`/api/cards/${data.card_id}/update/`, { due_at: `${addForm.due.value}T17:00` });
        const loc = addForm.location;
        if (loc && loc.value.trim()) await L.post(`/api/cards/${data.card_id}/fields/`, { field_id: +loc.dataset.fieldId, value: loc.value.trim() });
        location.reload();
      } catch (err) { L.toast(err.message || 'Could not add the card.'); }
    });
  }

  // ---------- Table ----------
  const table = $('[data-table]');
  if (table) {
    const tbody = $('tbody', table);
    const rows = $$('tr[data-card-open]', tbody);
    const search = $('[data-table-search]', table);
    const listSel = $('[data-table-list]', table);
    const personSel = $('[data-table-person]', table);
    const hideDone = $('[data-table-hide-done]', table);
    const count = $('[data-table-count]', table);

    function filter() {
      const q = search.value.trim().toLowerCase();
      let shown = 0;
      rows.forEach(r => {
        const ok = (!q || r.dataset.title.includes(q) || r.dataset.peopleName.toLowerCase().includes(q))
          && (!listSel.value || r.dataset.list === listSel.value)
          && (!personSel.value || r.dataset.people.split(' ').includes(personSel.value))
          && (!hideDone.checked || r.dataset.done !== '1');
        r.hidden = !ok;
        if (ok) shown++;
      });
      count.textContent = `${shown} card${shown === 1 ? '' : 's'}`;
    }
    [search, listSel, personSel, hideDone].forEach(el => el.addEventListener('input', filter));

    let sortKey = null, asc = true;
    const keyFns = {
      title: r => r.dataset.title,
      list: r => (isNaN(+r.dataset.listPos) ? r.dataset.listPos.toLowerCase() : +r.dataset.listPos),
      people: r => r.dataset.peopleName || '~',
      due: r => +r.dataset.due,
      items: r => +r.dataset.items,
      status: r => +r.dataset.status,
    };
    $$('th[data-sort]', table).forEach(th => th.addEventListener('click', () => {
      const key = th.dataset.sort;
      asc = sortKey === key ? !asc : true;
      sortKey = key;
      $$('th', table).forEach(h => h.classList.remove('asc', 'desc'));
      th.classList.add(asc ? 'asc' : 'desc');
      const fn = keyFns[key];
      rows.sort((a, b) => {
        const x = fn(a), y = fn(b);
        return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
      }).forEach(r => tbody.appendChild(r));
      saveState();
    }));

    // Keep filters and sort when the page refreshes itself (live updates reload the table)
    const KEY = `lanes-table-${location.pathname}`;
    function saveState() {
      try { sessionStorage.setItem(KEY, JSON.stringify({ q: search.value, list: listSel.value, person: personSel.value,
                                                          hide: hideDone.checked, sortKey, asc })); } catch {}
    }
    [search, listSel, personSel, hideDone].forEach(el => el.addEventListener('input', saveState));
    try {
      const st = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      if (st) {
        search.value = st.q || ''; listSel.value = st.list || ''; personSel.value = st.person || ''; hideDone.checked = !!st.hide;
        filter();
        if (st.sortKey) {
          const th = $(`th[data-sort="${st.sortKey}"]`, table);
          if (th) { sortKey = st.sortKey; asc = !st.asc; th.click(); }   // click flips asc back to the saved one
        }
      }
    } catch {}
  }
})();
