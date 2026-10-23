// Gantt: drag bars to move, ends to resize, the ● to link (this one → that one waits),
// click an arrow to remove a link. The server reschedules dependants; we reload to show it.
(() => {
  const root = document.querySelector('[data-gantt]');
  if (!root) return;
  const colW = +root.dataset.colW;
  const canEdit = root.dataset.canEdit === '1';
  const body = root.querySelector('.gt-body');
  const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  const toast = (m) => (window.LanesUI?.toast ? window.LanesUI.toast(m) : console.log(m));
  const post = (url, data) => fetch(url, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, 'X-Requested-With': 'fetch' },
    body: JSON.stringify(data) }).then(async r => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'That didn’t work.');
      return j;
    });
  const pad = n => String(n).padStart(2, '0');
  const shift = (iso, days) => { const d = new Date(iso + 'T12:00:00'); d.setDate(d.getDate() + days);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const reload = () => { const y = root.querySelector('.gt-scroll').scrollLeft; sessionStorage.setItem('gtScroll', y); location.reload(); };
  try { const y = sessionStorage.getItem('gtScroll'); if (y) { root.querySelector('.gt-scroll').scrollLeft = +y; sessionStorage.removeItem('gtScroll'); } } catch {}

  // keep the left table rows and the chart in step when the left side is clicked
  root.querySelector('[data-gt-critical]')?.addEventListener('change', e => root.classList.toggle('no-crit', !e.target.checked));
  root.querySelector('[data-gt-print]')?.addEventListener('click', () => window.print());

  // scroll so today is in view on first load
  const today = root.querySelector('.gt-today');
  if (today && !sessionStorage.getItem('gtScroll')) {
    const sc = root.querySelector('.gt-scroll');
    sc.scrollLeft = Math.max(0, today.offsetLeft - sc.clientWidth / 3);
  }

  if (!canEdit) return;

  let drag = null;
  body.addEventListener('pointerdown', e => {
    const bar = e.target.closest('[data-gt-bar]');
    if (!bar || e.button !== 0) return;
    e.preventDefault();
    const link = e.target.closest('[data-gt-link]');
    const handle = e.target.closest('[data-gt-h]');
    drag = { bar, x: e.clientX, y: e.clientY, mode: link ? 'link' : handle ? handle.dataset.gtH : 'move', days: 0, moved: false,
             left0: bar.offsetLeft, w0: bar.offsetWidth };
    if (drag.mode === 'link') {
      const r = body.getBoundingClientRect(), b = bar.getBoundingClientRect();
      drag.line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      drag.line.setAttribute('class', 'gt-linkline');
      drag.ox = b.right - r.left; drag.oy = b.top + b.height / 2 - r.top;
      ['x1', 'x2'].forEach(a => drag.line.setAttribute(a, drag.ox));
      ['y1', 'y2'].forEach(a => drag.line.setAttribute(a, drag.oy));
      body.querySelector('.gt-arrows').appendChild(drag.line);
    }
    bar.setPointerCapture(e.pointerId);
  });
  body.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 3 || Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
    if (drag.mode === 'link') {
      const r = body.getBoundingClientRect();
      drag.line.setAttribute('x2', e.clientX - r.left); drag.line.setAttribute('y2', e.clientY - r.top);
      body.querySelectorAll('.gt-target').forEach(x => x.classList.remove('gt-target'));
      const over = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-gt-bar]');
      if (over && over !== drag.bar) over.classList.add('gt-target');
      return;
    }
    drag.days = Math.round(dx / colW);
    const b = drag.bar;
    b.classList.add('dragging');
    if (drag.mode === 'move') b.style.transform = `translateX(${drag.days * colW}px)`;
    else if (drag.mode === 'r') b.style.width = `${Math.max(colW, drag.w0 + drag.days * colW)}px`;
    else if (drag.mode === 'l') {
      const d = Math.min(drag.days, Math.round(drag.w0 / colW) - 1);
      b.style.left = `${drag.left0 + d * colW}px`; b.style.width = `${drag.w0 - d * colW}px`;
    }
  });
  body.addEventListener('pointerup', async e => {
    if (!drag) return;
    const d = drag; drag = null;
    const id = d.bar.dataset.gtBar;
    if (d.mode === 'link') {
      d.line.remove();
      body.querySelectorAll('.gt-target').forEach(x => x.classList.remove('gt-target'));
      const over = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-gt-bar]');
      if (!over || over === d.bar) return;
      try { await post(`/api/cards/${over.dataset.gtBar}/dependencies/`, { depends_on: +id }); reload(); }
      catch (err) { toast(err.message); }
      return;
    }
    if (!d.moved) { window.LanesOpenCard ? window.LanesOpenCard(id) : (location.search = `?card=${id}`); return; }
    if (!d.days) { reload(); return; }
    let start = d.bar.dataset.start, end = d.bar.dataset.end;
    if (d.mode === 'move') { start = shift(start, d.days); end = shift(end, d.days); }
    else if (d.mode === 'r') { end = shift(end, d.days); if (end < start) end = start; }
    else if (d.mode === 'l') { start = shift(start, d.days); if (start > end) start = end; }
    const data = d.bar.dataset.ms ? { due_at: `${end}T17:00` } : { start_date: start, due_at: `${end}T17:00` };
    try { await post(`/api/cards/${id}/update/`, data); reload(); }
    catch (err) { toast(err.message); reload(); }
  });

  body.addEventListener('click', async e => {
    const a = e.target.closest('.gt-arrow');
    if (!a) return;
    const ok = window.LanesUI?.confirm ? await window.LanesUI.confirm('Remove this link?', { text: 'The later task will no longer wait for the earlier one.', ok: 'Remove' }) : confirm('Remove this link?');
    if (!ok) return;
    try { await post(`/api/cards/${a.dataset.depCard}/dependencies/`, { remove: +a.dataset.depOn }); reload(); }
    catch (err) { toast(err.message); }
  });
})();
