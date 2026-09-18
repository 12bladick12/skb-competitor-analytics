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
          (target.matches('textarea') || target.matches('input:not([role="combobox"]):not([type="search"])'))) {
        state.dirty = true;
        const label = document.querySelector('.draft-save-status');
        if (label) {
          label.textContent = 'Есть несохранённые изменения';
          label.classList.remove('is-clean');
        }
      }
    }, true);
  }
  const state = window.__skbDraftGuard;
  if (state.token !== resetToken) state.dirty = serverDirty;
  else if (serverDirty) state.dirty = true;
  state.token = resetToken;
})();
