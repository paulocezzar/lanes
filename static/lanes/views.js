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

    // "+" on a day: quick-add a card into the first list, due that day
    const firstList = document.getElementById('first-list');
    cal.addEventListener('click', async e => {
      const btn = e.target.closest('[data-cal-add]');
      if (!btn || !firstList) return;
      const day = btn.closest('[data-day]').dataset.day;
      const title = prompt(`New card due ${day} (added to “${firstList.dataset.listTitle}”):`);
      if (!title || !title.trim()) return;
      const data = await L.post(`/api/lists/${firstList.dataset.listId}/cards/`, { title: title.trim() });
      await L.post(`/api/cards/${data.card_id}/update/`, { due_at: `${day}T17:00` });
      location.reload();
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
      list: r => +r.dataset.listPos,
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
    }));
  }
})();
