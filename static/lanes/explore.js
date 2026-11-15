/* "Explore this board": a small bar of tips along the bottom. Each tip points at the real control;
   "Got it" ticks it off. Progress is remembered in this browser, per person. */
(() => {
  const bar = document.getElementById('explore-bar');
  if (!bar) return;
  const TIPS = {
    board: [
      ['add', 'Add a card', '[data-add-card]', 'A card is one job. Click “Add a card” at the bottom of a list, type what needs doing and press Enter.'],
      ['open', 'Open a card', '.card-tile', 'Click any card to give it a due date, people, a checklist or notes.'],
      ['move', 'Move a card', '.card-tile', 'Drag a card to another list as the work moves on, for example from To do to Doing.'],
      ['views', 'Try List or Calendar', '.view-tabs', 'The same cards as rows in a list, or on a calendar. Nothing changes except how you see it.'],
      ['share', 'Add your team', '.btn[data-open-share]', 'Click Share to invite the people who work on this board with you.'],
    ],
    list: [
      ['tick', 'Tick a card off', '[data-lv-tick]', 'Click the circle when the job is done.'],
      ['due', 'Set a due date', '[data-lv-edit="due"]', 'Click a row’s Due cell and pick the day. No need to open the card.'],
      ['people', 'Choose who does it', '[data-lv-edit="people"]', 'Click a row’s People cell and tick the names.'],
      ['add', 'Add a card', '.lv-add input', 'Type in “Add a card…” under any list and press Enter. Keep typing to add more.'],
      ['fold', 'Fold a list away', '[data-lv-fold]', 'Click the arrow beside a list’s name to hide its rows, and again to bring them back.'],
    ],
    calendar: [
      ['move', 'Change a due date', '.cal-chips > *', 'Drag a card to another day to move its due date.'],
      ['add', 'Add a card on a day', '.cal-day.today', 'Point at a day and click the + in its corner to add a card due that day.'],
      ['mode', 'Week or month', '[data-cal-mode]', 'Switch between a whole month and a single week.'],
      ['nodate', 'Cards with no date', '[data-cal-unsched-toggle]', 'Cards without a due date are kept here. Drag one onto a day to give it a date.'],
    ],
  };
  const view = bar.dataset.view, tips = TIPS[view];
  if (!tips) return;
  const KEY = `lanes-explore-u${bar.dataset.user}`;
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
  const write = s => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* private window: tips just won't be remembered */ } };
  let state = read();
  if (state.off) return;
  const done = () => new Set((state.done || {})[view] || []);
  if (done().size >= tips.length) return;

  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let pop = null, target = null;
  const closeTip = () => { pop?.remove(); pop = null; target?.classList.remove('explore-target'); target = null; bar.querySelectorAll('.on').forEach(b => b.classList.remove('on')); };
  const draw = () => {
    const d = done(), order = [...tips.filter(t => !d.has(t[0])), ...tips.filter(t => d.has(t[0]))];
    bar.innerHTML = `<div class="explore-top"><strong>Explore this ${view === 'board' ? 'board' : view + ' view'}</strong>
        <span class="explore-meter"><i style="width:${Math.round(100 * d.size / tips.length)}%"></i></span>
        <span class="explore-count">${d.size} of ${tips.length} done</span>
        <button type="button" class="icon-btn explore-close" data-explore-close title="Hide these tips" aria-label="Hide these tips">×</button></div>
      <div class="explore-tips">${order.map(t => `<button type="button" class="explore-tip${d.has(t[0]) ? ' done' : ''}" data-explore-tip="${t[0]}"><i aria-hidden="true">${d.has(t[0]) ? '✓' : '○'}</i>${esc(t[1])}</button>`).join('')}</div>`;
    bar.hidden = false;
  };
  const visible = el => el.offsetParent !== null && el.getBoundingClientRect().width > 0;
  const openTip = (tip, chip) => {
    closeTip();
    chip.classList.add('on');
    target = [...document.querySelectorAll(tip[2])].find(visible) || null;
    target?.classList.add('explore-target');
    target?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    pop = document.createElement('div');
    pop.className = 'explore-pop'; pop.setAttribute('role', 'dialog');
    pop.innerHTML = `<h4>${esc(tip[1])}</h4><p>${esc(tip[3])}</p><button type="button" class="btn primary block" data-explore-got="${tip[0]}">Got it</button>`;
    document.body.appendChild(pop);
    const r = (target || chip).getBoundingClientRect(), barTop = bar.getBoundingClientRect().top;
    let top = r.bottom + 10;
    if (!target || top + pop.offsetHeight > barTop - 8) top = Math.max(8, Math.min(r.top, barTop) - pop.offsetHeight - 10);
    pop.style.top = `${top}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8))}px`;
    pop.querySelector('button').focus();
  };
  bar.addEventListener('click', e => {
    if (e.target.closest('[data-explore-close]')) { closeTip(); state = read(); state.off = true; write(state); bar.hidden = true; return; }
    const chip = e.target.closest('[data-explore-tip]');
    if (!chip) return;
    if (chip.classList.contains('on')) return closeTip();
    openTip(tips.find(t => t[0] === chip.dataset.exploreTip), chip);
  });
  document.addEventListener('click', e => {
    const got = e.target.closest('[data-explore-got]');
    if (got) {
      state = read(); state.done = state.done || {};
      state.done[view] = [...new Set([...(state.done[view] || []), got.dataset.exploreGot])];
      write(state); closeTip(); draw();
      if (done().size >= tips.length) setTimeout(() => { bar.hidden = true; }, 1800);
      return;
    }
    if (pop && !e.target.closest('.explore-pop') && !e.target.closest('#explore-bar')) closeTip();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && pop) closeTip(); });
  draw();
})();
