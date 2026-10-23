// Trello-style automation builder: pick a trigger sentence, add action
// sentences with +, review, save. Posts the same fields the server forms expect.
(() => {
  const root = document.querySelector('[data-autobuilder]');
  const dataEl = document.getElementById('auto-data');
  if (!root || !dataEl) return;
  const D = JSON.parse(dataEl.textContent);
  const home = document.querySelector('[data-ab-home]');
  const form = document.querySelector('[data-ab-form]');
  const SECTION = D.mode_section || D.section;
  const MODE = { rules: 'rule', due: 'due', scheduled: 'schedule', 'card-buttons': 'cardbtn', 'board-buttons': 'boardbtn' }[SECTION];
  if (!MODE) return;

  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const pad = h => String(h).padStart(2, '0') + ':00';

  // ---------- option sets ----------
  const O = {
    list: D.lists.map(l => [l.id, l.title]),
    listNew: [['new', 'the new list']].concat(D.lists.map(l => [l.id, l.title])),
    pos: [['bottom', 'bottom'], ['top', 'top']],
    sortBy: D.sort_by,
    unique: [['', 'new'], ['1', 'unique']],
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
  const W = k => ({ t: 'days', k });
  const DAY = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  // ---------- sentences ----------
  const RULE_TRIGGERS = [
    { tab: 'Card move', fixed: { trigger: 'card_added' }, tokens: [T('when a card is added to list'), S('trigger_list', 'list'), S('by_whom', 'by')] },
    { tab: 'Card move', fixed: { trigger: 'card_moved' }, tokens: [T('when a card is moved into list'), S('trigger_list', 'list'), S('by_whom', 'by')] },
    { tab: 'Card move', fixed: { trigger: 'board_added' }, tokens: [T('when a card is added to the board'), S('by_whom', 'by')], hint: '“Added” means created, copied, moved in from another board or made from a template.' },
    { tab: 'Card move', fixed: { trigger: 'card_archived' }, tokens: [T('when a card is archived'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'card_completed' }, tokens: [T('when a card is marked complete'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'label_added' }, tokens: [T('when the label'), S('trigger_label', 'label'), T('is added to a card'), S('by_whom', 'by')] },
    { tab: 'Card changes', fixed: { trigger: 'member_added' }, tokens: [T('when'), S('member_who', 'who'), T('added to a card')] },
    { tab: 'Card changes', fixed: { trigger: 'attachment_added' }, tokens: [T('when an attachment is added to a card'), S('by_whom', 'by')] },
    { tab: 'Dates', fixed: { trigger: 'due_set' }, tokens: [T('when a due date is set on a card'), S('by_whom', 'by')] },
    { tab: 'Dates', fixed: { trigger: 'due_removed' }, tokens: [T('when the due date is removed from a card'), S('by_whom', 'by')] },
    { tab: 'Checklists', fixed: { trigger: 'checklist_done' }, tokens: [T('when every checklist item on a card is done'), S('by_whom', 'by')] },
    { tab: 'Card content', fixed: { trigger: 'comment_added' }, tokens: [T('when a comment is posted to a card'), S('by_whom', 'by')] },
    { tab: 'Card content', fixed: { trigger: 'mentioned' }, tokens: [T('when'), S('member_who', 'who'), T('mentioned in a comment'), S('by_whom', 'by')] },
    { tab: 'Fields', fixed: { trigger: 'field_set' }, tokens: [T('when the custom field'), S('trigger_field', 'field'), T('is set on a card'), S('by_whom', 'by')] },
    { tab: 'Lists', fixed: { trigger: 'list_count' }, tokens: [T('when list'), S('trigger_list', 'list'), T('has exactly'), N('trigger_count', 0, 500, 1), T('cards')], hint: 'Checked whenever a card is added to or moved into that list.' },
    { tab: 'Lists', fixed: { trigger: 'list_created' }, tokens: [T('when a list is created'), S('by_whom', 'by')], hint: 'Actions for this one work on the board — “the new list” means the list just made.' },
  ];
  const DUE_TRIGGERS = [
    { tab: 'Dates', fixed: { trigger: 'card_overdue', trigger_hours: 0 }, tokens: [T('the moment a card is due')] },
    { tab: 'Dates', fixed: { trigger: 'card_due_soon' }, tokens: [S('trigger_hours', 'hours', 24), T('before a card is due')] },
    { tab: 'Dates', fixed: { trigger: 'card_overdue' }, tokens: [S('trigger_hours', 'after', 24), T('a card is due')] },
    { tab: 'Dates', fixed: { trigger: 'due_weekday' }, tokens: [T('on the'), S('trigger_weekday', 'weekday', 0), T('before a card is due, at'), S('trigger_hours', 'clock', 9)] },
  ];
  const at = [T('at'), S('hour', 'clock', 7)];
  const SCHED_TRIGGERS = [
    { tab: 'Schedule', fixed: { frequency: 'daily' }, tokens: [T('every day'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'weekdays' }, tokens: [T('every weekday (Mon–Fri)'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'weekly', interval: 1 }, tokens: [T('every'), W('weekdays'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'weekly' }, tokens: [T('every'), N('interval', 2, 52, 2), T('weeks on'), W('weekdays'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'month_nth' }, tokens: [T('every'), S('nth', 'nth'), S('weekday', 'weekday'), T('of the month'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'monthly' }, tokens: [T('every month on the'), S('month_day', 'mday'), ...at] },
    { tab: 'Schedule', fixed: { frequency: 'yearly' }, tokens: [T('every year on the'), S('month_day', 'mday'), T('of'), S('month', 'month'), ...at] },
  ];
  const FILTERS = [
    { tab: 'Filters', fixed: { type: 'label' }, tokens: [T('with the'), S('label', 'label'), T('label')] },
    { tab: 'Filters', fixed: { type: 'no_label' }, tokens: [T('without the'), S('label', 'label'), T('label')] },
    { tab: 'Filters', fixed: { type: 'has_due' }, tokens: [T('with a due date')] },
    { tab: 'Filters', fixed: { type: 'no_due' }, tokens: [T('without a due date')] },
    { tab: 'Filters', fixed: { type: 'member' }, tokens: [T('with'), S('user', 'person'), T('on the card')] },
    { tab: 'Filters', fixed: { type: 'complete' }, tokens: [T('that is complete')] },
    { tab: 'Filters', fixed: { type: 'incomplete' }, tokens: [T("that isn't complete")] },
    { tab: 'Filters', fixed: { type: 'in_list' }, tokens: [T('in list'), S('list', 'list')] },
  ];
  const CARD_ACTIONS = [
    { tab: 'Move', fixed: { type: 'move' }, tokens: [T('move the card to the'), S('position', 'pos'), T('of list'), S('list', 'list')] },
    { tab: 'Move', fixed: { type: 'move_top' }, tokens: [T('move the card to the top of its list')] },
    { tab: 'Move', fixed: { type: 'move_bottom' }, tokens: [T('move the card to the bottom of its list')] },
    { tab: 'Move', fixed: { type: 'copy' }, tokens: [T('copy the card to the'), S('position', 'pos'), T('of list'), S('list', 'list')] },
    { tab: 'Move', fixed: { type: 'archive' }, tokens: [T('archive the card')] },
    { tab: 'Add/Remove', fixed: { type: 'add_label' }, tokens: [T('add the'), S('label', 'label'), T('label')] },
    { tab: 'Add/Remove', fixed: { type: 'remove_label' }, tokens: [T('remove the'), S('label', 'label'), T('label')] },
    { tab: 'Add/Remove', fixed: { type: 'add_sticker' }, tokens: [T('add the sticker'), S('sticker', 'sticker')] },
    { tab: 'Dates', fixed: { type: 'set_due' }, tokens: [T('set the due date'), N('days', 0, 365, 1), T('days from now')] },
    { tab: 'Dates', fixed: { type: 'set_due_workdays' }, tokens: [T('set the due date'), N('days', 0, 260, 5), T('working days from now')] },
    { tab: 'Dates', fixed: { type: 'set_start' }, tokens: [T('set the start date'), N('days', 0, 365, 0), T('days from now')] },
    { tab: 'Dates', fixed: { type: 'clear_due' }, tokens: [T('remove the due date')] },
    { tab: 'Dates', fixed: { type: 'complete' }, tokens: [T('mark the card complete')] },
    { tab: 'Dates', fixed: { type: 'incomplete' }, tokens: [T('mark the card incomplete')] },
    { tab: 'Checklists', fixed: { type: 'add_checklist' }, tokens: [T('add a checklist called'), I('cltitle', 'Checklist'), T('with items'), A('clitems', 'One item per line (optional)')] },
    { tab: 'Checklists', fixed: { type: 'check_all' }, tokens: [T('tick every checklist item')] },
    { tab: 'Members', fixed: { type: 'add_member' }, tokens: [T('add'), S('user', 'person'), T('to the card')] },
    { tab: 'Members', fixed: { type: 'add_me' }, tokens: [T('add me (whoever presses it) to the card')], only: 'cardbtn' },
    { tab: 'Members', fixed: { type: 'remove_member' }, tokens: [T('remove'), S('user', 'person'), T('from the card')] },
    { tab: 'Members', fixed: { type: 'clear_members' }, tokens: [T('remove all members from the card')] },
    { tab: 'Content', fixed: { type: 'comment' }, tokens: [T('post the comment'), I('text', 'Comment')] },
    { tab: 'Content', fixed: { type: 'notify' }, tokens: [T("notify the card's members"), I('text', 'with a message (optional)', false)] },
    { tab: 'Content', fixed: { type: 'rename' }, tokens: [T('rename the card to'), I('text', 'New name — {cardname} keeps the old one')] },
    { tab: 'Content', fixed: { type: 'set_description' }, tokens: [T('set the description to'), A('text', 'Description — {cardname} and {date} work here')] },
    { tab: 'Fields', fixed: { type: 'set_field' }, tokens: [T('set custom field'), S('field', 'field'), T('to'), I('value', 'Value (leave empty to clear)', false)] },
    { tab: 'Sort', fixed: { type: 'sort_list' }, tokens: [T("sort the card's list by"), S('by', 'sortBy')] },
    { tab: 'Cascade', fixed: { type: 'cascade' }, tokens: [T("turn the card's unticked checklist items into new cards in list"), S('list', 'list')] },
  ];
  const BOARD_ACTIONS = [
    { tab: 'Add card', fixed: { type: 'create_card' }, tokens: [T('create a'), S('unique', 'unique'), T('card titled'), I('text', 'Card title'), T('in list'), S('list', 'list')], extras: MODE === 'schedule', hint: 'A “unique” card is only made if there isn’t already one with that title in the list.' },
    { tab: 'Add card', fixed: { type: 'copy_named' }, tokens: [T('find the card'), I('text', 'Card name, e.g. a template'), T('and copy it to list'), S('list', 'list')] },
    { tab: 'Move cards', fixed: { type: 'move_all' }, tokens: [T('move every card from list'), S('from_list', 'list'), T('to list'), S('list', 'list')] },
    { tab: 'Move cards', fixed: { type: 'move_random' }, tokens: [T('move'), N('count', 1, 50, 1), T('random cards from list'), S('from_list', 'list'), T('to list'), S('list', 'list')] },
    { tab: 'Lists', fixed: { type: 'create_list' }, tokens: [T('create a list called'), I('text', 'List name')] },
    { tab: 'Lists', fixed: { type: 'archive_list' }, tokens: [T('archive every card in list'), S('list', 'list')] },
    { tab: 'Sort', fixed: { type: 'sort_due' }, tokens: [T('sort list'), S('list', 'list'), T('by due date')] },
    { tab: 'Sort', fixed: { type: 'sort_field' }, tokens: [T('sort list'), S('list', 'list'), T('by'), S('field', 'field'), S('order', 'order')] },
    { tab: 'Other', fixed: { type: 'archive_done' }, tokens: [T('archive every complete card')] },
  ];
  // "When a list is created" runs board actions; their lists can be "the new list".
  const NEW_LIST_ACTIONS = BOARD_ACTIONS.filter(r => r.fixed.type !== 'move_random')
    .map(r => ({ ...r, extras: false, tokens: r.tokens.map(t => t.t === 'sel' && t.k === 'list' ? { ...t, o: 'listNew', def: 'new' } : t) }));

  const CFG = {
    rule: { title: 'Create a Rule', steps: true, triggers: RULE_TRIGGERS, actions: CARD_ACTIONS },
    due: { title: 'Create a due date automation', note: 'Due-date automations are checked every few minutes, once per due date. If you change a card’s due date, they can run again.', triggers: DUE_TRIGGERS, actions: CARD_ACTIONS },
    schedule: { title: 'Create a scheduled automation', triggers: SCHED_TRIGGERS, actions: BOARD_ACTIONS, triggerHead: 'Select a schedule' },
    cardbtn: { title: 'Create a Card Button', button: true, actions: CARD_ACTIONS },
    boardbtn: { title: 'Create a Board Button', button: true, actions: BOARD_ACTIONS },
  }[MODE];
  const BASE_ACTIONS = CFG.actions.filter(r => !r.only || r.only === MODE);
  const actionsNow = () => (st.trigger && st.trigger.data.trigger === 'list_created' ? NEW_LIST_ACTIONS : BASE_ACTIONS);
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
      if (tok.t === 'area') {
        if (row.fixed.type === 'set_description') return v ? `<b>“${esc(v.length > 40 ? v.slice(0, 40) + '…' : v)}”</b>` : '<b>(empty)</b>';
        const n = (v || '').split('\n').filter(x => x.trim()).length; return `<b>${n} item${n === 1 ? '' : 's'}</b>`;
      }
      if (tok.t === 'days') {
        const ds = String(v || '').split(',').filter(x => x !== '').map(Number);
        const names = ds.map(d => D.weekdays[d][1]);
        return `<b>${esc(names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names.slice(-1) : names[0] || '?')}</b>`;
      }
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
    if (tok.t === 'days') {
      const on = String(v ?? '0').split(',');
      return `<span class="ab-days" data-k="${tok.k}" data-days>${DAY.map((d, i) => `<label><input type="checkbox" value="${i}"${on.includes(String(i)) ? ' checked' : ''}><span>${d}</span></label>`).join('')}</span>`;
    }
    return '';
  }
  function rowHTML(row, i, kind) {
    const extras = row.extras ? `<details class="ab-extras"><summary>More options — only when this is the only action</summary>
      <div class="ab-line"><span class="ab-word">assign to</span>${control(S('member', 'personOpt'), '')}
      <span class="ab-word">due in</span><input type="number" data-k="due_in_days" min="0" max="365" class="ab-num" placeholder="—"><span class="ab-word">days</span></div>
      <textarea data-k="checklist" rows="2" class="ab-area" placeholder="Checklist — one item per line (optional)"></textarea></details>` : '';
    const hint = row.hint ? `<p class="ab-hint">${esc(row.hint)}</p>` : '';
    return `<div class="ab-row" data-row="${kind}:${i}"><div class="ab-line">${row.tokens.map(t => control(t)).join('')}</div>${hint}${extras}
      <button type="button" class="ab-plus" data-add="${kind}:${i}" title="Add" aria-label="Add">+</button></div>`;
  }

  // ---------- state ----------
  const FRESH = () => ({ trigger: null, actions: [], filters: [], pickTrigger: false, pickAction: true, pickFilter: false,
                         tTab: null, aTab: null, name: '', icon: '', enabled: true, closeCard: false, editId: null });
  const st = FRESH();

  function readRow(el, row) {
    const vals = {};
    let bad = null;
    el.querySelectorAll('[data-k]').forEach(c => {
      if (c.hasAttribute('data-days')) {
        const v = [...c.querySelectorAll('input:checked')].map(x => x.value).join(',');
        if (!v) bad = c.querySelector('input');
        vals[c.dataset.k] = v;
        return;
      }
      let v = c.value;
      if (c.type === 'number') v = c.value === '' ? '' : Number(c.value);
      if (c.hasAttribute('data-req') && !String(v).trim()) bad = c;
      vals[c.dataset.k] = typeof v === 'string' ? v.trim() : v;
    });
    if (row.tokens.some(t => t.t === 'sel' && !O[t.o].length)) return { error: 'There’s nothing to pick here yet.' };
    if (bad) {
      const box = bad.closest('.ab-days') || bad;
      bad.focus(); box.classList.add('ab-shake'); setTimeout(() => box.classList.remove('ab-shake'), 500);
      return { error: bad.closest('.ab-days') ? 'Pick at least one day.' : 'Fill this in first.' };
    }
    if (row.fixed.type === 'move_all' && vals.from_list === vals.list) return { error: 'Pick two different lists.' };
    return { vals };
  }

  // ---------- render ----------
  const isListCreated = () => st.trigger && st.trigger.data.trigger === 'list_created';
  function render() {
    const c = CFG;
    const ready = (c.button ? st.name.trim() : st.trigger) && st.actions.length;
    const step = !st.trigger ? 1 : !st.actions.length ? 2 : 3;
    const title = st.editId ? c.title.replace(/^Create an? /, 'Edit ').replace('Edit Rule', 'Edit rule') : c.title;
    let h = `<header class="ab-head"><h2>${esc(title)}</h2>
      <div class="ab-head-btns"><button type="button" class="btn primary" data-ab-save ${ready ? '' : 'disabled'}>Save</button>
      <button type="button" class="btn ghost" data-ab-cancel>Cancel</button></div></header>`;
    if (c.steps) {
      const sp = (n, t) => `<span class="ab-step${step > n ? ' done' : step === n ? ' on' : ''}"><i>${step > n ? '✓' : n}</i>${t}</span>`;
      h += `<div class="ab-steps">${sp(1, 'Select trigger')}<b>›</b>${sp(2, 'Select action')}<b>›</b>${sp(3, 'Review and save')}</div>`;
    }
    if (c.note) h += `<p class="ab-note">ⓘ ${esc(c.note)}</p>`;

    if (c.button) {
      h += `<div class="ab-btnmeta"><label>Icon<select data-ab-icon>${D.icons.map(i => `<option value="${esc(i)}"${i === st.icon ? ' selected' : ''}>${i || '—'}</option>`).join('')}</select></label>
        <label class="grow">Title<input data-ab-name maxlength="40" value="${esc(st.name)}" placeholder="Button name, e.g. ${MODE === 'cardbtn' ? 'Delivered' : 'New week'}"></label></div>
        <div class="ab-opts"><label><input type="checkbox" data-ab-enabled${st.enabled ? ' checked' : ''}> Enabled — show it ${MODE === 'cardbtn' ? 'on every card' : 'in the board header'}</label>
        ${MODE === 'cardbtn' ? `<label><input type="checkbox" data-ab-close${st.closeCard ? ' checked' : ''}> Close the card after pressing</label>` : ''}</div>`;
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
      // Advanced: filters on card triggers
      if (st.trigger && MODE !== 'schedule' && !isListCreated()) {
        st.filters.forEach((f, i) => {
          h += `<div class="ab-chosen"><span>only for cards ${f.html}</span><button type="button" class="icon-btn" data-del-filter="${i}" title="Remove filter" aria-label="Remove filter">🗑</button><span class="ab-gap"></span></div>`;
        });
        if (st.pickFilter) {
          h += `<h4>Only run for cards…</h4>` + FILTERS.map((r, i) => rowHTML(r, i, 'f')).join('');
        } else if (st.filters.length < 6) {
          h += `<button type="button" class="ab-link" data-pick-filter>+ Add a filter (Advanced)</button>`;
        }
      }
      h += `</section>`;
    }

    if (c.button || st.trigger) {
      const acts = actionsNow();
      h += `<section class="ab-sec"><h3>${c.button ? 'Actions' : st.actions.length ? 'Action' : 'Select action'}</h3>`;
      if (!st.actions.length && c.button) h += `<p class="ab-empty">Your button doesn’t do anything yet. Add some actions below.</p>`;
      st.actions.forEach((a, i) => {
        h += `<div class="ab-chosen"><span>${a.html}</span>
          <button type="button" class="icon-btn" data-del-action="${i}" title="Remove" aria-label="Remove">🗑</button>
          ${i ? `<button type="button" class="icon-btn" data-up-action="${i}" title="Move up" aria-label="Move up">↑</button>` : '<span class="ab-gap"></span>'}</div>`;
      });
      const room = st.actions.length < D.max_actions;
      if (st.pickAction && room) {
        const tabs = tabsOf(acts);
        if (!tabs.includes(st.aTab)) st.aTab = tabs[0];
        if (st.actions.length) h += `<h4>Add another action</h4>`;
        h += `<div class="ab-tabs">${tabs.map(t => `<button type="button" data-atab="${esc(t)}" class="${t === st.aTab ? 'on' : ''}">${esc(t)}</button>`).join('')}</div>`;
        h += acts.map((r, i) => r.tab === st.aTab ? rowHTML(r, i, 'a') : '').join('');
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
  const saveBtn = () => root.querySelector('[data-ab-save]');

  // ---------- events ----------
  root.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.matches('[data-ab-cancel]')) return close();
    if (b.matches('[data-ab-save]')) return save();
    if (b.matches('[data-pick-trigger]')) { st.pickTrigger = true; return render(); }
    if (b.matches('[data-pick-action]')) { st.pickAction = true; return render(); }
    if (b.matches('[data-pick-filter]')) { st.pickFilter = true; return render(); }
    if (b.dataset.ttab) { st.tTab = b.dataset.ttab; return render(); }
    if (b.dataset.atab) { st.aTab = b.dataset.atab; return render(); }
    if (b.matches('[data-del-trigger]')) { st.trigger = null; st.pickTrigger = true; return render(); }
    if (b.dataset.delFilter != null) { st.filters.splice(+b.dataset.delFilter, 1); return render(); }
    if (b.dataset.delAction != null) { st.actions.splice(+b.dataset.delAction, 1); if (!st.actions.length) st.pickAction = true; return render(); }
    if (b.dataset.upAction != null) { const i = +b.dataset.upAction; [st.actions[i - 1], st.actions[i]] = [st.actions[i], st.actions[i - 1]]; return render(); }
    if (b.dataset.add) {
      const [kind, idx] = b.dataset.add.split(':');
      const row = (kind === 't' ? CFG.triggers : kind === 'f' ? FILTERS : actionsNow())[+idx];
      const r = readRow(b.closest('.ab-row'), row);
      if (r.error) return error(r.error);
      add(kind, row, r.vals);
    }
  });
  root.addEventListener('input', e => {
    if (e.target.matches('[data-ab-name]')) { st.name = e.target.value; saveBtn().disabled = !(st.name.trim() && st.actions.length); }
  });
  root.addEventListener('change', e => {
    if (e.target.matches('[data-ab-icon]')) st.icon = e.target.value;
    if (e.target.matches('[data-ab-enabled]')) st.enabled = e.target.checked;
    if (e.target.matches('[data-ab-close]')) st.closeCard = e.target.checked;
  });
  root.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target.matches('.ab-row input:not([type=checkbox])')) { e.preventDefault(); e.target.closest('.ab-row').querySelector('[data-add]').click(); }
  });

  function extrasOf(row, vals) {
    if (!row.extras) return { txt: '', on: false };
    const bits = [];
    if (vals.member) bits.push(`assigned to <b>${esc(optLabel('personOpt', vals.member))}</b>`);
    if (vals.due_in_days !== '' && vals.due_in_days != null) bits.push(`due in <b>${vals.due_in_days}</b> days`);
    const n = (vals.checklist || '').split('\n').filter(x => x.trim()).length;
    if (n) bits.push(`with a <b>${n}-item</b> checklist`);
    return { txt: bits.length ? ', ' + bits.join(', ') : '', on: bits.length > 0 };
  }
  function add(kind, row, vals) {
    const ex = extrasOf(row, vals);
    const item = { data: { ...row.fixed, ...vals }, extras: ex.on, html: describe(row, { ...vals, _extras: ex.txt }) };
    if (kind === 't') {
      const wasNew = isListCreated();
      st.trigger = item; st.pickTrigger = false; st.pickAction = true;
      if (wasNew !== isListCreated()) st.actions = [];        // card actions ↔ board actions
      if (isListCreated()) st.filters = [];
    } else if (kind === 'f') { st.filters.push(item); st.pickFilter = false; }
    else { st.actions.push(item); st.pickAction = false; }
    error(''); render();
  }

  // ---------- save: build the POST the server understands ----------
  function save() {
    const f = { section: SECTION };
    if (st.editId) f.edit_id = st.editId;
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
      f.filters = JSON.stringify(st.filters.map(x => x.data));
      actionFields('', st.actions);
    } else if (MODE === 'schedule') {
      url = D.urls.schedule;
      Object.assign(f, st.trigger.data);
      const only = st.actions.length === 1 && st.actions[0].data.type === 'create_card' && !st.actions[0].data.unique ? st.actions[0].data : null;
      if (only) {
        Object.assign(f, { kind: 'card', title: only.text, target_list: only.list, member: only.member || '',
                           due_in_days: only.due_in_days ?? '', checklist: only.checklist || '' });
      } else {
        f.kind = 'actions';
        actionFields('s', st.actions);
      }
    } else {
      url = D.urls.button;
      Object.assign(f, { kind: MODE === 'cardbtn' ? 'card' : 'board', name: st.name.trim(), icon: st.icon,
                         enabled: st.enabled ? '1' : '0', close_card: st.closeCard ? '1' : '0' });
      actionFields(MODE === 'cardbtn' ? 'b' : 'bb', st.actions);
    }
    form.action = url;
    form.querySelectorAll('input:not([name=csrfmiddlewaretoken])').forEach(x => x.remove());
    Object.entries(f).forEach(([k, v]) => {
      const inp = document.createElement('input'); inp.type = 'hidden'; inp.name = k; inp.value = v ?? ''; form.appendChild(inp);
    });
    saveBtn().disabled = true;
    form.submit();
  }

  // ---------- open / close ----------
  function open(prefill) {
    Object.assign(st, FRESH());
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
  document.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => open(JSON.parse(b.dataset.edit))));

  // ---------- examples, edits and list-menu links → builder state ----------
  function resolveList(want) {
    if (want == null) return undefined;
    if (want === 'new') return 'new';
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
    if (typeof want === 'number') return (D.labels.find(l => l.id === want) || D.labels[0]).id;
    const w = String(want).toLowerCase();
    return (D.labels.find(l => l.colour === w) || D.labels.find(l => l.name.toLowerCase().includes(w)) || D.labels[0]).id;
  }
  const DEFAULTS = { trigger_hours: 0, interval: 1 };
  function itemFrom(rows, data) {
    const row = rows.find(r => Object.entries(r.fixed).every(([k, v]) => String(data[k] ?? DEFAULTS[k]) === String(v)));
    if (!row) return null;
    if (row.tokens.some(t => t.t === 'sel' && !O[t.o].length)) return null;
    const vals = {};
    row.tokens.forEach(tok => {
      if (tok.t === 'text') return;
      let v = data[tok.k];
      if (tok.k === 'cltitle') v = data.title ?? data.cltitle;
      if (tok.k === 'clitems') v = data.text ?? data.clitems ?? '';
      if (tok.k === 'unique') v = data.unique ? '1' : '';
      if (tok.k === 'trigger_hours' && tok.o === 'clock' && v == null) v = 9;
      if (tok.t === 'days') v = data.weekdays || String(data.weekday ?? 0);
      if (tok.t === 'sel') {
        if (tok.o === 'list' || tok.o === 'listNew') v = resolveList(v ?? (tok.def || 'first'));
        else if (tok.o === 'label') v = resolveLabel(v ?? '');
        if (v === undefined || v === null || !O[tok.o].some(([k]) => String(k) === String(v))) v = tok.def ?? (O[tok.o][0] || [])[0];
      }
      if (tok.t === 'num' && (v == null || v === '')) v = tok.def;
      if ((tok.t === 'input' || tok.t === 'area') && v == null) v = '';
      vals[tok.k] = v;
    });
    if (row.extras) ['member', 'due_in_days', 'checklist'].forEach(k => { if (data[k] != null && data[k] !== '') vals[k] = data[k]; });
    const ex = extrasOf(row, vals);
    return { data: { ...row.fixed, ...vals }, extras: ex.on, html: describe(row, { ...vals, _extras: ex.txt }) };
  }
  function applyPrefill(d) {
    st.editId = d.id || null;
    if (CFG.triggers) {
      const t = (MODE === 'schedule' || d.trigger) ? itemFrom(CFG.triggers, d) : null;
      if (t) st.trigger = t; else st.pickTrigger = true;
    }
    (d.filters || []).forEach(f => { const it = itemFrom(FILTERS, f); if (it) st.filters.push(it); });
    if (d.name) { st.name = d.name; st.icon = d.icon || ''; }
    if (d.enabled === false) st.enabled = false;
    if (d.close_card) st.closeCard = true;
    let acts = d.actions || [];
    if (MODE === 'schedule' && d.kind === 'card') {
      acts = [{ type: 'create_card', text: d.title, list: d.target_list, checklist: d.checklist, member: d.member, due_in_days: d.due_in_days }];
    }
    const rows = actionsNow();
    acts.forEach(a => { const it = itemFrom(rows, a); if (it) st.actions.push(it); });
    st.pickAction = !st.actions.length;
  }

  const qs = new URLSearchParams(location.search);
  if (MODE === 'rule' && qs.get('trigger')) open({ trigger: qs.get('trigger'), trigger_list: Number(qs.get('list')) || 'first', by_whom: 'anyone' });
  else if (MODE === 'schedule' && qs.get('sort_list')) {
    const monday = qs.get('every') === 'monday';
    open({ kind: 'actions', frequency: monday ? 'weekly' : 'daily', weekday: 0, hour: 6, actions: [{ type: 'sort_due', list: Number(qs.get('sort_list')) }] });
  } else if (qs.get('new') === '1') open();
})();
