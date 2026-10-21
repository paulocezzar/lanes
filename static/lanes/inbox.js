/* Personal inbox — used on the Planner page and in the board page's slide-out panel. */
window.LanesInbox = (() => {
  const csrf = () => document.querySelector('meta[name=csrf-token]').content;
  const $ = (s, r = document) => r.querySelector(s);

  function toast(msg) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500);
  }

  async function post(url, data) {
    const res = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(data || {}),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(body.error || 'Something went wrong.'); throw new Error(body.error); }
    return body;
  }

  // opts: { onChange(listEl), sendToBoard(itemId, text) }
  function init(panel, opts = {}) {
    const listBox = $('[data-inbox-list]', panel);
    const setHtml = html => { listBox.innerHTML = html; opts.onChange?.(listBox); };

    $('[data-inbox-add]', panel).addEventListener('submit', async e => {
      e.preventDefault();
      const ta = e.target.text;
      if (!ta.value.trim()) return;
      const data = await post('/api/inbox/add/', { text: ta.value });
      ta.value = '';
      setHtml(data.html);
      if (data.added > 1) toast(`Added ${data.added} items.`);
      ta.focus();
    });
    $('[data-inbox-add] textarea', panel).addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.target.value.includes('\n')) { e.preventDefault(); e.target.form.requestSubmit(); }
    });

    const aiBtn = $('[data-inbox-ai]', panel);
    const aiDialog = document.getElementById('ai-dialog');
    if (aiBtn && aiDialog) {
      aiBtn.addEventListener('click', () => { aiDialog.showModal(); aiDialog.querySelector('textarea').focus(); });
      aiDialog.querySelector('[data-close]').addEventListener('click', () => aiDialog.close());
      aiDialog.querySelector('form').addEventListener('submit', async e => {
        e.preventDefault();
        const btn = e.target.querySelector('.btn.primary');
        btn.disabled = true; btn.textContent = 'Reading…';
        try {
          const data = await post('/api/inbox/capture/', { text: e.target.text.value });
          setHtml(data.html);
          e.target.text.value = '';
          aiDialog.close();
          toast(data.added ? `Added ${data.added} to-do${data.added === 1 ? '' : 's'} to your inbox.` : 'No to-dos found in that message.');
        } catch { /* toast already shown */ }
        finally { btn.disabled = false; btn.textContent = 'Capture'; }
      });
    }

    panel.addEventListener('change', async e => {
      const row = e.target.closest('[data-inbox-id]');
      if (!row) return;
      const id = row.dataset.inboxId;
      if (e.target.matches('[data-inbox-done]')) setHtml((await post(`/api/inbox/${id}/`, { is_done: e.target.checked })).html);
      if (e.target.matches('[data-inbox-date]')) setHtml((await post(`/api/inbox/${id}/`, { due_date: e.target.value })).html);
    });
    panel.addEventListener('click', async e => {
      const row = e.target.closest('[data-inbox-id]');
      if (!row) return;
      if (e.target.closest('[data-inbox-delete]')) setHtml((await post(`/api/inbox/${row.dataset.inboxId}/`, { delete: true })).html);
      if (e.target.closest('[data-inbox-send]')) {
        e.target.closest('details')?.removeAttribute('open');
        opts.sendToBoard?.(row.dataset.inboxId, $('.inbox-text', row).textContent.trim());
      }
    });
    return { setHtml, post, toast };
  }

  return { init, post, toast };
})();
