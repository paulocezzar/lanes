/* Planner page: drag cards, checklist items and inbox items onto days. */
(() => {
  const planner = document.getElementById('planner');
  if (!planner) return;
  const panel = planner.querySelector('[data-inbox-panel]');
  const sendDialog = document.getElementById('send-dialog');
  let sendingId = null;

  const inbox = LanesInbox.init(panel, {
    sendToBoard(id, text) {
      sendingId = id;
      sendDialog.querySelector('[data-send-what]').textContent = `“${text}” becomes a card, assigned to you.`;
      sendDialog.showModal();
    },
  });
  sendDialog.querySelector('[data-close]').addEventListener('click', () => sendDialog.close());
  sendDialog.querySelector('form').addEventListener('submit', async e => {
    e.preventDefault();
    const data = await inbox.post(`/api/inbox/${sendingId}/to-card/`, { list_id: +e.target.list_id.value });
    sendDialog.close();
    location.href = `/b/${data.board_id}/?card=${data.card_id}`;
  });

  // ---------- drag onto a day ----------
  let dragged = null;
  planner.addEventListener('dragstart', e => {
    const el = e.target.closest('[data-plan-kind], [data-inbox-id]');
    if (!el) return;
    dragged = el;
    el.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'lanes');
  });
  planner.addEventListener('dragend', () => {
    dragged?.classList.remove('dragging');
    planner.querySelectorAll('.drop-over').forEach(el => el.classList.remove('drop-over'));
    dragged = null;
  });
  planner.addEventListener('dragover', e => {
    const day = e.target.closest('[data-plan-day]');
    if (!day || !dragged) return;
    e.preventDefault();
    planner.querySelectorAll('.drop-over').forEach(el => el !== day && el.classList.remove('drop-over'));
    day.classList.add('drop-over');
  });
  planner.addEventListener('drop', async e => {
    const day = e.target.closest('[data-plan-day]');
    if (!day || !dragged) return;
    e.preventDefault();
    const date = day.dataset.planDay;
    const el = dragged;
    const kind = el.dataset.planKind || 'inbox';
    const id = el.dataset.planId || el.dataset.inboxId;
    const at = date ? `${date}T${el.dataset.time || '17:00'}` : '';
    try {
      if (kind === 'card') await inbox.post(`/api/cards/${id}/update/`, { due_at: at });
      else if (kind === 'item') await inbox.post(`/api/items/${id}/`, { due_at: at });
      else await inbox.post(`/api/inbox/${id}/`, { due_date: date });
      location.reload();
    } catch { /* toast shown */ }
  });
})();
