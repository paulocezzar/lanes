/* Today (the phone home): capture into the Inbox, tick off what's due. */
(() => {
  const page = document.querySelector('[data-inbox-panel].today');
  if (!page || !window.LanesInbox) return;
  const inbox = window.LanesInbox.init(page);
  const { post, toast } = inbox;

  // Tick a card complete / a checklist item done, straight from the list.
  page.addEventListener('click', async e => {
    const btn = e.target.closest('[data-today-done]');
    if (!btn) return;
    const row = btn.closest('.today-row');
    btn.disabled = true;
    try {
      if (row.dataset.todayCard) await post(`/api/cards/${row.dataset.todayCard}/update/`, { is_complete: true });
      else await post(`/api/items/${row.dataset.todayItem}/`, { is_done: true });
      row.classList.add('done');
      toast('Done ✓');
      setTimeout(() => row.remove(), 600);
    } catch { btn.disabled = false; }
  });

  // Say it: speech → the capture box → Add.
  const mic = page.querySelector('[data-today-mic]');
  const box = page.querySelector('[data-today-text]');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (mic && SR) {
    mic.hidden = false;
    let rec = null;
    mic.addEventListener('click', () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = navigator.language || 'en-GB';
      rec.interimResults = false; rec.maxAlternatives = 1;
      mic.classList.add('listening');
      rec.onresult = ev => {
        const said = (ev.results[0] && ev.results[0][0].transcript || '').trim();
        if (!said) return;
        box.value = box.value.trim() ? box.value.trim() + '\n' + said : said;
        box.form.requestSubmit();
      };
      rec.onerror = ev => { toast(ev.error === 'not-allowed' ? 'Allow the microphone for Lanes in your browser settings.' : 'Didn’t catch that — try again or type it.'); };
      rec.onend = () => { mic.classList.remove('listening'); rec = null; };
      rec.start();
    });
  }
  if (location.hash === '#inbox') document.getElementById('inbox')?.scrollIntoView();
})();
