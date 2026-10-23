// Workload (capacity dialog) and project costs (add / remove).
(() => {
  const csrf = document.querySelector('meta[name=csrf-token]')?.content || document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  const toast = (m) => (window.LanesUI?.toast ? window.LanesUI.toast(m) : alert(m));
  const post = (url, data) => fetch(url, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data) })
    .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || 'That didn’t work.'); return j; });

  // capacity + rate
  const dlg = document.querySelector('[data-cap-dialog]');
  if (dlg) {
    const f = dlg.querySelector('[data-cap-form]');
    let who = null;
    document.addEventListener('click', e => {
      const b = e.target.closest('[data-cap-edit]');
      if (!b) return;
      who = b.dataset.capEdit;
      dlg.querySelector('[data-cap-title]').textContent = b.dataset.name;
      f.capacity_hours.value = b.dataset.cap;
      const hasRate = 'rate' in b.dataset;
      dlg.querySelector('[data-rate-wrap]').hidden = !hasRate;
      f.hourly_rate.value = hasRate ? b.dataset.rate : '';
      dlg.showModal();
    });
    f.addEventListener('submit', async e => {
      if (e.submitter?.value !== 'save') return;
      e.preventDefault();
      const data = { capacity_hours: f.capacity_hours.value };
      if (!dlg.querySelector('[data-rate-wrap]').hidden) data.hourly_rate = f.hourly_rate.value;
      try { await post(`/api/users/${who}/capacity/`, data); location.reload(); }
      catch (err) { toast(err.message); }
    });
  }

  // costs
  const root = document.querySelector('[data-costs]');
  if (root) {
    const id = root.dataset.costs;
    root.querySelector('[data-cost-add]')?.addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      try {
        await post(`/api/boards/${id}/costs/`, { description: f.description.value, category: f.category.value,
          amount: f.amount.value, day: f.day.value, card: f.card.value || null });
        location.reload();
      } catch (err) { toast(err.message); }
    });
    root.addEventListener('click', async e => {
      const x = e.target.closest('[data-cost-remove]');
      if (!x) return;
      const ok = window.LanesUI?.confirm ? await window.LanesUI.confirm('Remove this cost?', { ok: 'Remove' }) : confirm('Remove this cost?');
      if (!ok) return;
      try { await post(`/api/boards/${id}/costs/`, { remove: +x.dataset.costRemove }); location.reload(); }
      catch (err) { toast(err.message); }
    });
  }
})();
