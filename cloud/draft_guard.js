// Fixed application code only. No source text or credentials are interpolated.
(() => {
  const resetToken = '__RESET_TOKEN__';
  const serverDirty = __SERVER_DIRTY__;
  if (!window.__skbDraftGuard) {
    const state = window.__skbDraftGuard = { dirty: false, token: '' };
    window.addEventListener('beforeunload', (event) => {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
    document.addEventListener('input', (event) => {
      const target = event.target;
      if (target instanceof Element && target.closest('.st-key-draft-workspace') &&
          (target.matches('textarea') || target.matches('input:not([role="combobox"]):not([type="search"]):not([type="radio"])'))) {
        state.dirty = true;
        const label = document.querySelector('.draft-save-status');
        if (label) {
          label.textContent = 'Есть несохранённые изменения';
          label.classList.remove('is-clean');
        }
      }
    }, true);
    document.addEventListener('click', (event) => {
      const target = event.target;
      // A download can be clicked before the text area's blur reaches Python.
      // A tab click has the same race. Flush the field first; Python then
      // offers the explicit discard action on the next tab click.
      if (state.dirty && !state.serverDirty && target instanceof Element &&
          target.closest('.st-key-workspace-tabs [role="tab"][aria-selected="false"]') &&
          document.querySelector('.st-key-draft-workspace')) {
        event.preventDefault();
        event.stopImmediatePropagation();
        document.activeElement?.blur();
        const label = document.querySelector('.draft-save-status');
        if (label) label.textContent = 'Есть несохранённые правки. Сохраните записку или повторите переход для подтверждения';
      }
      // Prevent that race from exporting the preceding saved revision.
      if (state.dirty && target instanceof Element &&
          target.closest('.st-key-draft-workspace') &&
          target.closest('[data-testid="stDownloadButton"]')) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const label = document.querySelector('.draft-save-status');
        if (label) label.textContent = 'Сначала сохраните правки, затем скачайте документ';
      }
    }, true);
  }
  const state = window.__skbDraftGuard;
  state.serverDirty = serverDirty;
  if (state.token !== resetToken) state.dirty = serverDirty;
  else if (serverDirty) state.dirty = true;
  state.token = resetToken;
})();
