// Trello-style automation builder: pick a trigger sentence, add action
// sentences with +, review, save. Posts the same fields the server forms expect.
(() => {
  const root = document.querySelector('[data-autobuilder]');
  const dataEl = document.getElementById('auto-data');
  if (!root || !dataEl) return;
  const D = JSON.parse(dataEl.textContent);
  const home = document.querySelector('[data-ab-home]');
  const form = document.querySelector('[data-ab-form]');
  const MODE = { rules: 'rule', due: 'due', scheduled: 'schedule', 'card-buttons': 'cardbtn', 'board-buttons': 'boardbtn' }[D.section];
  if (!MODE) return;

  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const pad = h => String(h).padStart(2, '0') + ':00';

  // ---------- option sets ----------
  const O = {
    list: D.lists.map(l => [l.id, l.title]),
    label: D.labels.map(l => [l.id, l.name]),
    person: D.people.map(p => [p.id, p.name]),
    personOpt: [['', 'nobody']].concat(D.people.map(p => [p.id, p.name])),
    field: D.fields.map(f => [f.id, f.name]),
    by: [['anyone', 'by anyone'], ['me', 'by me'], ['others', 'by anyone except me']],
    who: [['anyone', 'someone is'], ['me', 'I am']],
    hours: D.hours, after: D.after,
    weekday: D.weekdays.map(([i, n]) => [i, n]),
    month: D.months, nth: D.nths,
    mday: Array.from({ length: 31 }, (_, i) => [i + 1, ordinal(i + 1)]),
    clock: Array.from({ length: 24 }, (_, h) => [h, pad(h)]),
    sticker: D.stickers.map(s => [s, s]),
    order: [['asc', 'low to high'], ['desc', 'high to low']],
  };
  function ordinal(n) { const s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }

  // token helpers: T('text'), S(key, options, default), N(key, min, max, def), I(key, placeholder, required), A(key, placeholder)
  const T = v => ({ t: 'text', v });
  const S = (k, o, def) => ({ t: 'sel', k, o, def });
  const N = (k, min, max, def, blank) => ({ t: 'num', k, min, max, def, blank });
  const I = (k, ph, req = true) => ({ t: 'input', k, ph, req });
  const A = (k, ph) => ({ t: 'area', k, ph });

  // ---------- sentences ----------
  const RULE_TRIGGERS = [
    { tab: 'Card move', fixed: { trigger: 'card_added' }, tokens: [T('when a card is added to list'), S('trigger_list', 'list'), S('by_whom', 'by')] },
    { tab: 'Card move', fixed: { trigger: 'card_moved' }, tokens: [T('when a card is moved into list'), S('trigger_list', 'list'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'card_completed' }, tokens: [T('when a card is marked complete'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'label_added' }, tokens: [T('when the label'), S('trigger_label', 'label'), T('is added to a card'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'member_added' }, tokens: [T('when'), S('member_who', 'who'), T('added to a card')] },
    { tab: 'Checklists', fixed: { trigger: 'checklist_done' }, tokens: [T('when every checklist item on a card is done'), S('by_whom', 'by')] },
  ];
  const DUE_TRIGGERS = [
    { tab: 'Dates', fixed: { trigger: 'card_overdue', trigger_hours: 0 }, tokens: [T('the moment a card is due')] },
    { tab: 'Dates', fixed: { trigger: 'card_due_soon' }, tokens: [S('trigger_hours', 'hours', 24), T('before a card is due')] },
    { tab: 'Dates', fixed: { trigger: 'card_overdue' }, tokens: [S('trigger_hours', 'after', 24), T('a card is due')] },
  ];
  const at = [T('at'), S('hour', 'clock', 7)];
  const SCHED_TRIGGERS = [
    { tab: 'Schedule', fixed: { frequency: 'daily' }, tokens: [T('every day'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'weekdays' }, tokens: [T('every weekday (Mon–Fri)'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'weekly' }, tokens: [T('every'), S('weekday', 'weekday'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'month_nth' }, tokens: [T('every'), S('nth', 'nth'), S('weekday', 'weekday'), T('of the month'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'monthly' }, tokens: [T('every month on the'), S('month_day', 'mday'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'yearly' }, tokens: [T('every year on the'), S('month_day', 'mday'), T('of'), S('month', 'month'), ...at] },
  ];
  const CARD_ACTIONS = [
    { tab: 'Move', fixed: { type: 'move' }, tokens: [T('move the card to list'), S('list', 'list')] },
    { tab: 'Move', fixed: { type: 'move_top' }, tokens: [T('move the card to the top of its list')] },
    { tab: 'Move', fixed: { type: 'archive' }, tokens: [T('archive the card')] },
    { tab: 'Add/Remove', fixed: { type: 'add_label' }, tokens: [T('add the'), S('label', 'label'), T('label')] },
    { tab: 'Add/Remove', fixed: { type: 'remove_label' }, tokens: [T('remove the'), S('label', 'label'), T('label')] },
    { tab: 'Add/Remove', fixed: { type: 'add_sticker' }, tokens: [T('add the sticker'), S('sticker', 'sticker')] },
    { tab: 'Dates', fixed: { type: 'set_due' }, tokens: [T('set the due date'), N('days', 0, 365, 1), T('days from now')] },
    { tab: 'Dates', fixed: { type: 'set_due_workdays' }, tokens: [T('set the due date'), N('days', 0, 260, 5), T('working days from now')] },
    { tab: 'Dates', fixed: { type: 'clear_due' }, tokens: [T('remove the due date')] },
    { tab: 'Dates', fixed: { type: 'complete' }, tokens: [T('mark the card complete')] },
    { tab: 'Dates', fixed: { type: 'incomplete' }, tokens: [T('mark the card incomplete')] },
    { tab: 'Checklists', fixed: { type: 'add_checklist' }, tokens: [T('add a checklist called'), I('cltitle', 'Checklist'), T('with items'), A('clitems', 'One item per line (optional)')] },
    { tab: 'Checklists', fixed: { type: 'check_all' }, tokens: [T('tick every checklist item')] },
    { tab: 'Members', fixed: { type: 'add_member' }, tokens: [T('add'), S('user', 'person'), T('to the card')] },
    { tab: 'Members', fixed: { type: 'add_me' }, tokens: [T('add me (whoever presses it) to the card')], only: 'cardbtn' },
    { tab: 'Members', fixed: { type: 'clear_members' }, tokens: [T('remove all members from the card')] },
    { tab: 'Content', fixed: { type: 'comment' }, tokens: [T('post the comment'), I('text', 'Comment')] },
    { tab: 'Content', fixed: { type: 'notify' }, tokens: [T("notify the card's members"), I('text', 'with a message (optional)', false)] },
  ];
  const BOARD_ACTIONS = [
    { tab: 'Add card', fixed: { type: 'create_card' }, tokens: [T('create a card titled'), I('text', 'Card title'), T('in list'), S('list', 'list')], extras: MODE === 'schedule' },
    { tab: 'Move cards', fixed: { type: 'move_all' }, tokens: [T('move every card from list'), S('from_list', 'list'), T('to list'), S('list', 'list')] },
    { tab: 'Move cards', fixed: { type: 'move_random' }, tokens: [T('move'), N('count', 1, 50, 1), T('random cards from list'), S('from_list', 'list'), T('to list'), S('list', 'list')] },
    { tab: 'Lists', fixed: { type: 'create_list' }, tokens: [T('create a list called'), I('text', 'List name')] },
    { tab: 'Lists', fixed: { type: 'archive_list' }, tokens: [T('archive every card in list'), S('list', 'list')] },
    { tab: 'Sort', fixed: { type: 'sort_due' }, tokens: [T('sort list'), S('list', 'list'), T('by due date')] },
    { tab: 'Sort', fixed: { type: 'sort_field' }, tokens: [T('sort list'), S('list', 'list'), T('by'), S('field', 'field'), S('order', 'order')] },
    { tab: 'Other', fixed: { type: 'archive_done' }, tokens: [T('archive every complete card')] },
  ];

  const CFG = {
    rule: { title: 'Create a Rule', steps: true, triggers: RULE_TRIGGERS, actions: CARD_ACTIONS },
    due: { title: 'Create a due date automation', note: 'Due-date automations are checked every few minutes, once per due date. If you change a card’s due date, they can run again.', triggers: DUE_TRIGGERS, actions: CARD_ACTIONS },
    schedule: { title: 'Create a scheduled automation', triggers: SCHED_TRIGGERS, actions: BOARD_ACTIONS, triggerHead: 'Select a schedule' },
    cardbtn: { title: 'Create a Card Button', button: true, actions: CARD_ACTIONS },
    boardbtn: { title: 'Create a Board Button', button: true, actions: BOARD_ACTIONS },
  }[MODE];
  const ACTIONS = CFG.actions.filter(r => !r.only || r.only === MODE);
  const tabsOf = rows => [...new Set(rows.map(r => r.tab))];

  // ---------- sentence rendering ----------
  const optLabel = (key, v) => (O[key].find(([k]) => String(k) === String(v)) || [, '?'])[1];
  function describe(row, vals) {
    return row.tokens.map(tok => {
      if (tok.t === 'text') return esc(tok.v);
      const v = vals[tok.k];
      if (tok.t === 'sel') return `<b>${esc(optLabel(tok.o, v))}</b>`;
      if (tok.t === 'num') return `<b>${esc(v)}</b>`;
      if (tok.t === 'input') return v ? `<b>“${esc(v)}”</b>` : '';
      if (tok.t === 'area') { const n = (v || '').split('\n').filter(x => x.trim()).length; return `<b>${n} item${n === 1 ? '' : 's'}</b>`; }
      return '';
    }).filter(Boolean).join(' ') + (vals._extras || '');
  }
  function control(tok, val) {
    const v = val ?? tok.def;
    if (tok.t === 'text') return `<span class="ab-word">${esc(tok.v)}</span>`;
    if (tok.t === 'sel') {
      const opts = O[tok.o];
      if (!opts.length) return `<span class="ab-missing">no ${tok.o === 'field' ? 'custom fields' : tok.o + 's'} yet</span>`;
      return `<select data-k="${tok.k}">${opts.map(([k, l]) => `<option value="${esc(k)}"${String(k) === String(v) ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    }
    if (tok.t === 'num') return `<input type="number" data-k="${tok.k}" min="${tok.min}" max="${tok.max}" value="${esc(v ?? '')}" class="ab-num">`;
    if (tok.t === 'input') return `<input data-k="${tok.k}" placeholder="${esc(tok.ph)}" value="${esc(v ?? '')}" class="ab-text"${tok.req ? ' data-req' : ''}>`;
    if (tok.t === 'area') return `<textarea data-k="${tok.k}" rows="2" placeholder="${esc(tok.ph)}" class="ab-area">${esc(v ?? '')}</textarea>`;
    return '';
  }
  function rowHTML(row, i, kind) {
    const extras = row.extras ? `<details class="ab-extras"><summary>More options — only when this is the only action</summary>
      <div class="ab-line"><span class="ab-word">assign to</span>${control(S('member', 'personOpt'), '')}
      <span class="ab-word">due in</span><input type="number" data-k="due_in_days" min="0" max="365" class="ab-num" placeholder="—"><span class="ab-word">days</span></div>
      <textarea data-k="checklist" rows="2" class="ab-area" placeholder="Checklist — one item per line (optional)"></textarea></details>` : '';
    return `<div class="ab-row" data-row="${kind}:${i}"><div class="ab-line">${row.tokens.map(t => control(t)).join('')}</div>${extras}
      <button type="button" class="ab-plus" data-add="${kind}:${i}" title="Add" aria-label="Add">+</button></div>`;
  }

  // ---------- state ----------
  const st = { trigger: null, actions: [], pickTrigger: false, pickAction: true, tTab: null, aTab: null, name: '', icon: '' };

  function readRow(el, row) {
    const vals = {};
    let bad = null;
    el.querySelectorAll('[data-k]').forEach(c => {
      let v = c.value;
      if (c.type === 'number') v = c.value === '' ? '' : Number(c.value);
      if (c.hasAttribute('data-req') && !String(v).trim()) bad = c;
      vals[c.dataset.k] = typeof v === 'string' ? v.trim() : v;
    });
    if (row.tokens.some(t => t.t === 'sel' && !O[t.o].length)) return { error: 'There’s nothing to pick here yet.' };
    if (bad) { bad.focus(); bad.classList.add('ab-shake'); setTimeout(() => bad.classList.remove('ab-shake'), 500); return { error: 'Fill this in first.' }; }
    if (row.fixed.type === 'move_all' && vals.from_list === vals.list) return { error: 'Pick two different lists.' };
    return { vals };
  }

  // ---------- render ----------
  function render() {
    const c = CFG;
    const ready = (c.button ? st.name.trim() : st.trigger) && st.actions.length;
    const step = !st.trigger ? 1 : !st.actions.length ? 2 : 3;
    let h = `<header class="ab-head"><h2>${esc(c.title)}</h2>
      <div class="ab-head-btns"><button type="button" class="btn primary" data-ab-save ${ready ? '' : 'disabled'}>Save</button>
      <button type="button" class="btn ghost" data-ab-cancel>Cancel</button></div></header>`;
    if (c.steps) {
      const s = (n, t) => `<span class="ab-step${step > n ? ' done' : step === n ? ' on' : ''}"><i>${step > n ? '✓' : n}</i>${t}</span>`;
      h += `<div class="ab-steps">${s(1, 'Select trigger')}<b>›</b>${s(2, 'Select action')}<b>›</b>${s(3, 'Review and save')}</div>`;
    }
    if (c.note) h += `<p class="ab-note">ⓘ ${esc(c.note)}</p>`;

    if (c.button) {
      h += `<div class="ab-btnmeta"><label>Icon<select data-ab-icon>${D.icons.map(i => `<option value="${esc(i)}"${i === st.icon ? ' selected' : ''}>${i || '—'}</option>`).join('')}</select></label>
        <label class="grow">Title<input data-ab-name maxlength="40" value="${esc(st.name)}" placeholder="Button name, e.g. ${MODE === 'cardbtn' ? 'Delivered' : 'New week'}"></label></div>`;
    } else {
      h += `<section class="ab-sec"><h3>Trigger</h3>`;
      if (st.trigger) {
        h += `<div class="ab-chosen"><span>${st.trigger.html}</span><button type="button" class="icon-btn" data-del-trigger title="Remove trigger" aria-label="Remove trigger">🗑</button></div>`;
      } else if (!st.pickTrigger) {
        h += `<p class="ab-empty">Your automation doesn’t have a trigger yet. Select a trigger for the automation below.</p>
              <button type="button" class="ab-big" data-pick-trigger>+ Add trigger</button>`;
      } else {
        const tabs = tabsOf(c.triggers); st.tTab = st.tTab || tabs[0];
        h += `<h4>${esc(c.triggerHead || 'Select trigger')}</h4>`;
        if (tabs.length > 1) h += `<div class="ab-tabs">${tabs.map(t => `<button type="button" data-ttab="${esc(t)}" class="${t === st.tTab ? 'on' : ''}">${esc(t)}</button>`).join('')}</div>`;
        h += c.triggers.map((r, i) => r.tab === st.tTab ? rowHTML(r, i, 't') : '').join('');
      }
      h += `</section>`;
    }

    if (c.button || st.trigger) {
      h += `<section class="ab-sec"><h3>${c.button ? 'Actions' : st.actions.length ? 'Action' : 'Select action'}</h3>`;
      if (!st.actions.length && c.button) h += `<p class="ab-empty">Your button doesn’t do anything yet. Add some actions below.</p>`;
      st.actions.forEach((a, i) => {
        h += `<div class="ab-chosen"><span>${a.html}</span>
          <button type="button" class="icon-btn" data-del-action="${i}" title="Remove" aria-label="Remove">🗑</button>
          ${i ? `<button type="button" class="icon-btn" data-up-action="${i}" title="Move up" aria-label="Move up">↑</button>` : '<span class="ab-gap"></span>'}</div>`;
      });
      const room = st.actions.length < D.max_actions;
      if (st.pickAction && room) {
        const tabs = tabsOf(ACTIONS); st.aTab = st.aTab || tabs[0];
        if (st.actions.length) h += `<h4>Add another action</h4>`;
        h += `<div class="ab-tabs">${tabs.map(t => `<button type="button" data-atab="${esc(t)}" class="${t === st.aTab ? 'on' : ''}">${esc(t)}</button>`).join('')}</div>`;
        h += ACTIONS.map((r, i) => r.tab === st.aTab ? rowHTML(r, i, 'a') : '').join('');
      } else if (room) {
        h += `<button type="button" class="ab-another" data-pick-action>+ Add another action</button>`;
      }
      if (MODE === 'schedule' && st.actions.length > 1 && st.actions.some(a => a.data.type === 'create_card' && a.extras)) {
        h += `<p class="ab-note warn">A created card's assignee, due date and checklist are only used when it's the only action.</p>`;
      }
      h += `</section>`;
    }
    h += `<p class="ab-error" data-ab-error hidden></p>`;
    root.innerHTML = h;
  }
  const error = msg => { const e = root.querySelector('[data-ab-error]'); if (e) { e.textContent = msg; e.hidden = !msg; } };

  // ---------- events ----------
  root.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.matches('[data-ab-cancel]')) return close();
    if (b.matches('[data-ab-save]')) return save();
    if (b.matches('[data-pick-trigger]')) { st.pickTrigger = true; return render(); }
    if (b.matches('[data-pick-action]')) { st.pickAction = true; return render(); }
    if (b.dataset.ttab) { st.tTab = b.dataset.ttab; return render(); }
    if (b.dataset.atab) { st.aTab = b.dataset.atab; return render(); }
    if (b.matches('[data-del-trigger]')) { st.trigger = null; st.pickTrigger = true; return render(); }
    if (b.dataset.delAction != null) { st.actions.splice(+b.dataset.delAction, 1); if (!st.actions.length) st.pickAction = true; return render(); }
    if (b.dataset.upAction != null) { const i = +b.dataset.upAction; [st.actions[i - 1], st.actions[i]] = [st.actions[i], st.actions[i - 1]]; return render(); }
    if (b.dataset.add) {
      const [kind, idx] = b.dataset.add.split(':');
      const row = (kind === 't' ? CFG.triggers : ACTIONS)[+idx];
      const el = b.closest('.ab-row');
      const r = readRow(el, row);
      if (r.error) return error(r.error);
      add(kind, row, r.vals);
    }
  });
  root.addEventListener('input', e => {
    if (e.target.matches('[data-ab-name]')) { st.name = e.target.value; root.querySelector('[data-ab-save]').disabled = !(st.name.trim() && st.actions.length); }
  });
  root.addEventListener('change', e => { if (e.target.matches('[data-ab-icon]')) st.icon = e.target.value; });
  root.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target.matches('.ab-row input')) { e.preventDefault(); e.target.closest('.ab-row').querySelector('[data-add]').click(); }
  });

  function add(kind, row, vals) {
    const data = { ...row.fixed, ...vals };
    let extrasTxt = '', extras = false;
    if (row.extras) {
      const bits = [];
      if (vals.member) { bits.push(`assigned to <b>${esc(optLabel('personOpt', vals.member))}</b>`); extras = true; }
      if (vals.due_in_days !== '' && vals.due_in_days != null) { bits.push(`due in <b>${vals.due_in_days}</b> days`); extras = true; }
      const n = (vals.checklist || '').split('\n').filter(x => x.trim()).length;
      if (n) { bits.push(`with a <b>${n}-item</b> checklist`); extras = true; }
      if (bits.length) extrasTxt = ', ' + bits.join(', ');
    }
    const item = { data, extras, html: describe(row, { ...vals, _extras: extrasTxt }) };
    if (kind === 't') { st.trigger = item; st.pickTrigger = false; st.pickAction = true; }
    else { st.actions.push(item); st.pickAction = false; }
    error(''); render();
  }

  // ---------- save: build the POST the server already understands ----------
  function save() {
    const f = {};
    f.section = D.section;
    let url;
    const actionFields = (prefix, list) => list.forEach((a, i) => {
      Object.entries(a.data).forEach(([k, v]) => {
        if (k === 'type') f[`${prefix}action_${i}`] = v;
        else if (!['member', 'due_in_days', 'checklist'].includes(k)) f[`${prefix}${k}_${i}`] = v;
      });
    });
    if (MODE === 'rule' || MODE === 'due') {
      url = D.urls.rule;
      Object.assign(f, st.trigger.data);
      actionFields('', st.actions);
    } else if (MODE === 'schedule') {
      url = D.urls.schedule;
      Object.assign(f, st.trigger.data);
      const only = st.actions.length === 1 && st.actions[0].data.type === 'create_card' ? st.actions[0].data : null;
      if (only) {
        Object.assign(f, { kind: 'card', title: only.text, target_list: only.list, member: only.member || '',
                           due_in_days: only.due_in_days ?? '', checklist: only.checklist || '' });
      } else {
        f.kind = 'actions';
        actionFields('s', st.actions);
      }
    } else {
      url = D.urls.button;
      Object.assign(f, { kind: MODE === 'cardbtn' ? 'card' : 'board', name: st.name.trim(), icon: st.icon });
      actionFields(MODE === 'cardbtn' ? 'b' : 'bb', st.actions);
    }
    form.action = url;
    form.querySelectorAll('input:not([name=csrfmiddlewaretoken])').forEach(x => x.remove());
    Object.entries(f).forEach(([k, v]) => {
      const inp = document.createElement('input'); inp.type = 'hidden'; inp.name = k; inp.value = v ?? ''; form.appendChild(inp);
    });
    root.querySelector('[data-ab-save]').disabled = true;
    form.submit();
  }

  // ---------- open / close ----------
  function open(prefill) {
    Object.assign(st, { trigger: null, actions: [], pickTrigger: false, pickAction: true, tTab: null, aTab: null, name: '', icon: '' });
    if (prefill) applyPrefill(prefill);
    home.hidden = true; root.hidden = false;
    render();
    root.scrollIntoView({ block: 'start' });
    (root.querySelector('[data-ab-name]') || root.querySelector('[data-pick-trigger]') || root.querySelector('.ab-row select, .ab-row input'))?.focus();
  }
  function close() {
    root.hidden = true; home.hidden = false;
    if (location.search.match(/[?&](trigger|sort_list|new)=/)) history.replaceState(null, '', `?s=${D.section}`);
  }
  document.querySelector('[data-ab-open]')?.addEventListener('click', () => open());
  document.querySelectorAll('[data-example]').forEach(b => b.addEventListener('click', () => open(JSON.parse(b.dataset.example))));

  // ---------- examples & links from list menus → builder state ----------
  function resolveList(want) {
    if (want == null) return undefined;
    const L = D.lists;
    if (!L.length) return undefined;
    if (typeof want === 'number') return (L.find(l => l.id === want) || L[0]).id;
    const w = String(want).toLowerCase();
    if (w === 'first') return L[0].id;
    if (w === 'last') return L[L.length - 1].id;
    if (w === 'second') return (L[1] || L[0]).id;
    const hit = L.find(l => l.title.toLowerCase() === w) || L.find(l => l.title.toLowerCase().includes(w));
    return (hit || (w === 'done' ? L[L.length - 1] : L[0])).id;
  }
  function resolveLabel(want) {
    if (!D.labels.length) return undefined;
    const w = String(want).toLowerCase();
    return (D.labels.find(l => l.colour === w) || D.labels.find(l => l.name.toLowerCase().includes(w)) || D.labels[0]).id;
  }
  function itemFrom(rows, data, kind) {
    const row = rows.find(r => Object.entries(r.fixed).every(([k, v]) => data[k] === v || (k === 'trigger_hours' && Number(data[k] || 0) === v)));
    if (!row) return null;
    const vals = {};
    row.tokens.forEach(tok => {
      if (tok.t === 'text') return;
      let v = data[tok.k];
      if (tok.k === 'cltitle') v = data.title ?? data.cltitle;
      if (tok.k === 'clitems') v = data.text ?? data.clitems ?? '';
      if (tok.t === 'sel') {
        if (tok.o === 'list') v = resolveList(v ?? 'first');
        else if (tok.o === 'label') v = resolveLabel(v ?? '');
        else if (tok.o === 'person') v = v ?? (D.people[0] || {}).id;
        else if (tok.o === 'field') v = v ?? (D.fields[0] || {}).id;
        else if (v == null) v = tok.def ?? (O[tok.o][0] || [])[0];
        if (v === undefined || !O[tok.o].some(([k]) => String(k) === String(v))) v = tok.def ?? (O[tok.o][0] || [])[0];
      }
      if (tok.t === 'num' && v == null) v = tok.def;
      if (tok.t === 'input' && v == null) v = '';
      vals[tok.k] = v;
    });
    let extrasTxt = '', extras = false;
    if (row.extras && data.checklist) {
      vals.checklist = data.checklist; extras = true;
      const n = data.checklist.split('\n').filter(x => x.trim()).length;
      extrasTxt = `, with a <b>${n}-item</b> checklist`;
    }
    return { data: { ...row.fixed, ...vals }, extras, html: describe(row, { ...vals, _extras: extrasTxt }) };
  }
  function applyPrefill(d) {
    if (CFG.triggers) {
      let t = null;
      if (MODE === 'schedule') t = itemFrom(CFG.triggers, d, 't');
      else if (d.trigger) t = itemFrom(CFG.triggers, d, 't');
      if (t) st.trigger = t; else st.pickTrigger = true;
    }
    if (d.name) { st.name = d.name; st.icon = d.icon || ''; }
    let acts = d.actions || [];
    if (MODE === 'schedule' && d.kind === 'card') acts = [{ type: 'create_card', text: d.title, list: d.target_list, checklist: d.checklist }];
    acts.forEach(a => { const it = itemFrom(ACTIONS, a, 'a'); if (it) st.actions.push(it); });
    st.pickAction = !st.actions.length;
  }

  const qs = new URLSearchParams(location.search);
  if (MODE === 'rule' && qs.get('trigger')) open({ trigger: qs.get('trigger'), trigger_list: Number(qs.get('list')) || 'first', by_whom: 'anyone' });
  else if (MODE === 'schedule' && qs.get('sort_list')) {
    const monday = qs.get('every') === 'monday';
    open({ kind: 'actions', frequency: monday ? 'weekly' : 'daily', weekday: 0, hour: 6, actions: [{ type: 'sort_due', list: Number(qs.get('sort_list')) }] });
  } else if (qs.get('new') === '1') open();
})();
