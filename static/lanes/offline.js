/* Offline support in the page: register the service worker, show when you're offline, and keep
   changes made offline (ticks, amounts, new items, comments, time) in a queue that is sent, in order,
   as soon as the connection is back. */
(() => {
  const KEY = 'lanes-offline-queue';
  const csrf = () => document.querySelector('meta[name=csrf-token]')?.content || '';
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; } };
  const write = q => { try { localStorage.setItem(KEY, JSON.stringify(q)); } catch { /* storage full or blocked */ } };
  const QUEUEABLE = /^\/api\/(items\/\d+|checklists\/\d+\/items|cards\/\d+\/(comments|time|update|amount|fields))\/$/;

  // banner
  const bar = document.createElement('div');
  bar.className = 'offline-bar'; bar.hidden = true; bar.setAttribute('role', 'status');
  document.body.appendChild(bar);
  function paint() {
    const n = read().length;
    document.documentElement.classList.toggle('is-offline', !navigator.onLine);
    const dlg = document.getElementById('card-dialog');      // an open card sits above the banner, so say it there too
    if (dlg) {
      let strip = dlg.querySelector(':scope > .offline-strip');
      if (!strip) { strip = document.createElement('div'); strip.className = 'offline-strip'; dlg.prepend(strip); }
      strip.hidden = navigator.onLine;
      strip.textContent = `Offline — what you tick or record is kept on this device and sent when you're back online.${n ? ` (${n} waiting)` : ''}`;
    }
    if (!navigator.onLine) {
      bar.hidden = false;
      bar.textContent = `You're offline — boards and cards you've opened still work.${n ? ` ${n} change${n === 1 ? '' : 's'} will be sent when you're back online.` : ' Ticks and amounts you record are kept and sent later.'}`;
    } else if (n) {
      bar.hidden = false; bar.textContent = `Sending ${n} change${n === 1 ? '' : 's'} made offline…`;
    } else bar.hidden = true;
  }

  async function flush() {
    if (!navigator.onLine) return paint();
    let q = read(), sent = 0, dropped = 0;
    while (q.length) {
      const job = q[0];
      let res;
      try {
        res = await fetch(job.url, { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(job.data) });
      } catch { break; }                       // still offline — try again later
      q.shift(); write(q);
      if (res.ok) sent++; else dropped++;
    }
    paint();
    if (sent || dropped) {
      window.LanesUI?.toast?.(`Back online — ${sent} change${sent === 1 ? '' : 's'} saved${dropped ? `, ${dropped} couldn't be (the card may have changed)` : ''}.`);
      window.dispatchEvent(new CustomEvent('lanes:synced'));
    }
  }

  window.LanesOffline = {
    canQueue: url => QUEUEABLE.test(new URL(url, location.origin).pathname),
    queue(url, data) { const q = read(); q.push({ url, data, at: Date.now() }); write(q); paint(); },
    pending: () => read().length,
    flush,
  };

  addEventListener('online', flush);
  addEventListener('offline', paint);
  paint();
  if (navigator.onLine && read().length) flush();

  // signing out: forget saved pages and anything queued on this device
  document.addEventListener('submit', e => {
    if (e.target.matches('form[action$="/logout/"]')) {
      write([]);
      navigator.serviceWorker?.controller?.postMessage({ type: 'forget' });
    }
  }, true);

  if ('serviceWorker' in navigator && document.body.dataset.sw === '1') {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
  }
})();
