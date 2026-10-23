// Running timers: tick any [data-since] clock, and stop from the top-bar pill.
(() => {
  const csrf = document.querySelector('meta[name=csrf-token]')?.content || '';
  const fmt = s => { const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`; };
  const tick = () => document.querySelectorAll('[data-since]').forEach(el => {
    const base = +(el.dataset.base || 0) * 60;
    el.textContent = fmt(Math.max(0, Math.floor((Date.now() - Date.parse(el.dataset.since)) / 1000) + base));
  });
  tick(); setInterval(tick, 1000);
  // board.js calls this when a timer starts or stops on an open card
  window.LanesTimerPill = (info) => {
    document.querySelector('[data-timer-pill]')?.remove();
    if (!info) return;
    const right = document.querySelector('.topbar-right');
    if (!right) return;
    const pill = document.createElement('span');
    pill.className = 'timer-pill'; pill.dataset.timerPill = '';
    pill.title = `Timer running on ${info.title}`;
    const a = document.createElement('a'); a.href = info.href;
    a.innerHTML = '<i class="rec"></i><span data-since></span><small></small>';
    a.querySelector('[data-since]').dataset.since = info.since;
    a.querySelector('[data-since]').dataset.base = info.base || 0;
    a.querySelector('small').textContent = info.title.length > 24 ? info.title.slice(0, 23) + '…' : info.title;
    const b = document.createElement('button'); b.type = 'button'; b.dataset.timerStopTop = ''; b.textContent = '■';
    b.title = 'Stop and log the time'; b.setAttribute('aria-label', 'Stop timer');
    pill.append(a, b); right.prepend(pill); tick();
  };
  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-timer-stop-top]');
    if (!b) return;
    e.preventDefault();
    const r = await fetch('/api/time/stop/', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: '{}' });
    const j = await r.json().catch(() => ({}));
    document.querySelector('[data-timer-pill]')?.remove();
    if (j.card && window.LanesUI?.toast) window.LanesUI.toast(`Logged ${Math.floor(j.minutes / 60) ? Math.floor(j.minutes / 60) + 'h ' : ''}${j.minutes % 60}m on “${j.card}”.`);
    window.dispatchEvent(new CustomEvent('lanes:timer-stopped'));
  });
})();
