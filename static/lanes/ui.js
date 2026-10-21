/* In-app replacements for the browser's prompt()/confirm() boxes, which look
   out of place (and show "lanes.onrender.com says"). All return Promises.
     await LanesUI.confirm('Archive this list?', { ok: 'Archive', danger: true })  -> true/false
     await LanesUI.ask('Card limit', { value: '5', hint: '…' })                     -> string or null
     await LanesUI.choose('Which template?', [{ label, value }])                     -> value or null
   Forms with data-confirm="Question?" ask before submitting. */
(() => {
  let dlg;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function open(html, setup) {
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.className = 'dialog ui-dialog';
      document.body.appendChild(dlg);
    }
    dlg.innerHTML = html;
    return new Promise(resolve => {
      let done = false;
      const finish = v => { if (done) return; done = true; dlg.close(); resolve(v); };
      dlg.onclose = () => finish(null);
      dlg.onclick = e => { if (e.target === dlg) finish(null); };
      setup(finish);
      dlg.showModal();
    });
  }
  const head = (title, text) =>
    `<h3 class="ui-title">${esc(title)}</h3>${text ? `<p class="ui-text">${esc(text).replace(/\n/g, '<br>')}</p>` : ''}`;

  window.LanesUI = {
    confirm(message, { ok = 'OK', cancel = 'Cancel', danger = false, text = '' } = {}) {
      return open(`<form method="dialog">${head(message, text)}
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>${esc(cancel)}</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-yes autofocus>${esc(ok)}</button></div></form>`,
      finish => {
        dlg.querySelector('[data-no]').onclick = () => finish(false);
        dlg.querySelector('form').onsubmit = e => { e.preventDefault(); finish(true); };
      }).then(v => v === true);
    },
    ask(message, { value = '', placeholder = '', ok = 'Save', hint = '', type = 'text' } = {}) {
      return open(`<form method="dialog">${head(message, hint)}
        <input class="ui-input" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autofocus>
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>Cancel</button>
        <button class="btn primary">${esc(ok)}</button></div></form>`,
      finish => {
        const input = dlg.querySelector('input');
        setTimeout(() => { input.focus(); input.select(); });
        dlg.querySelector('[data-no]').onclick = () => finish(null);
        dlg.querySelector('form').onsubmit = e => { e.preventDefault(); finish(input.value); };
      });
    },
    choose(message, options, { text = '' } = {}) {
      return open(`<div>${head(message, text)}<div class="ui-choices">${options.map((o, i) =>
        `<button class="ui-choice" data-i="${i}">${esc(o.label)}</button>`).join('')}</div>
        <div class="dialog-actions"><button type="button" class="btn ghost" data-no>Cancel</button></div></div>`,
      finish => {
        dlg.querySelector('[data-no]').onclick = () => finish(null);
        dlg.querySelectorAll('[data-i]').forEach(b => (b.onclick = () => finish(options[+b.dataset.i].value)));
        setTimeout(() => dlg.querySelector('[data-i]')?.focus());
      });
    },
  };

  // <form data-confirm="Delete this rule?" data-confirm-ok="Delete">
  document.addEventListener('submit', async e => {
    const f = e.target.closest('form[data-confirm]');
    if (!f || f.dataset.confirmed) return;
    e.preventDefault();
    const ok = await LanesUI.confirm(f.dataset.confirm, { ok: f.dataset.confirmOk || 'OK', danger: 'danger' in f.dataset });
    if (ok) { f.dataset.confirmed = '1'; f.requestSubmit(e.submitter || undefined); }
  }, true);
})();
