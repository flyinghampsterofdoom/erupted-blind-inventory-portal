(() => {
  const shell = document.querySelector('[data-v2-shell]');
  const openButton = document.querySelector('[data-drawer-open]');
  const closeButtons = document.querySelectorAll('[data-drawer-close]');

  const setDrawer = (open) => {
    if (!shell) return;
    shell.classList.toggle('is-drawer-open', open);
    openButton?.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('v2-no-scroll', open);
  };

  openButton?.addEventListener('click', () => setDrawer(true));
  closeButtons.forEach((button) => button.addEventListener('click', () => setDrawer(false)));

  const navigationSections = Array.from(document.querySelectorAll('[data-nav-section]'));
  const navigationStorageKey = 'erupted-v2-navigation-sections';
  let savedNavigationState = {};
  try {
    savedNavigationState = JSON.parse(window.localStorage.getItem(navigationStorageKey) || '{}');
  } catch (_error) {
    savedNavigationState = {};
  }

  const setNavigationSection = (section, expanded, persist = true) => {
    const toggle = section.querySelector('[data-nav-section-toggle]');
    const children = section.querySelector('[data-nav-section-children]');
    if (!toggle || !children) return;
    toggle.setAttribute('aria-expanded', String(expanded));
    children.hidden = !expanded;
    section.classList.toggle('is-expanded', expanded);
    if (!persist) return;
    savedNavigationState[section.dataset.navKey] = expanded;
    try {
      window.localStorage.setItem(navigationStorageKey, JSON.stringify(savedNavigationState));
    } catch (_error) {
      // Navigation remains usable when storage is unavailable.
    }
  };

  navigationSections.forEach((section) => {
    const active = section.classList.contains('is-active');
    const saved = savedNavigationState[section.dataset.navKey];
    const initial = active || (typeof saved === 'boolean' ? saved : section.querySelector('[data-nav-section-toggle]')?.getAttribute('aria-expanded') === 'true');
    setNavigationSection(section, initial, false);
    section.querySelector('[data-nav-section-toggle]')?.addEventListener('click', () => {
      const expanded = section.querySelector('[data-nav-section-toggle]')?.getAttribute('aria-expanded') === 'true';
      setNavigationSection(section, !expanded);
    });
  });

  document.querySelectorAll('.v2-nav a').forEach((link) => {
    link.addEventListener('click', () => {
      if (window.matchMedia('(max-width: 760px)').matches) setDrawer(false);
    });
  });

  const scopeForm = document.querySelector('[data-scope-form]');
  const scopeTrigger = scopeForm?.querySelector('[data-scope-trigger]');
  const scopeMenu = scopeForm?.querySelector('[data-scope-menu]');
  const allStores = scopeForm?.querySelector('[data-all-stores]');
  const storeOptions = Array.from(scopeForm?.querySelectorAll('[data-store-option]') || []);

  const setScopeMenu = (open) => {
    if (!scopeMenu || !scopeTrigger) return;
    scopeMenu.hidden = !open;
    scopeTrigger.setAttribute('aria-expanded', String(open));
  };

  scopeTrigger?.addEventListener('click', () => setScopeMenu(!scopeMenu || scopeMenu.hidden));
  allStores?.addEventListener('change', () => {
    if (allStores.checked) storeOptions.forEach((option) => { option.checked = false; });
  });
  storeOptions.forEach((option) => option.addEventListener('change', () => {
    if (option.checked && allStores) allStores.checked = false;
    if (!storeOptions.some((candidate) => candidate.checked) && allStores) allStores.checked = true;
  }));

  document.addEventListener('click', (event) => {
    if (scopeForm && !scopeForm.contains(event.target)) setScopeMenu(false);
    const dialogButton = event.target.closest('[data-dialog-open]');
    if (dialogButton) {
      const dialog = document.getElementById(dialogButton.dataset.dialogOpen);
      if (dialog) {
        dialog._v2Opener = dialogButton;
        dialog.showModal();
      }
    }
    const dialogClose = event.target.closest('[data-dialog-close]');
    dialogClose?.closest('dialog')?.close('cancel');
  });

  document.querySelectorAll('dialog').forEach((dialog) => {
    dialog.addEventListener('close', () => dialog._v2Opener?.focus());
    dialog.addEventListener('keydown', (event) => {
      const confirmButton = dialog.querySelector('[data-dialog-confirm]');
      if (event.key !== 'Enter' || !confirmButton) return;
      if (event.target.closest('button, a, input, select, textarea')) return;
      event.preventDefault();
      confirmButton.click();
    });
  });

  document.addEventListener('keydown', (event) => {
    const openDialog = document.querySelector('dialog[open]');
    if (openDialog && event.key === 'Escape') {
      event.preventDefault();
      openDialog.close('cancel');
      return;
    }
    if (event.key === 'Escape') {
      setDrawer(false);
      setScopeMenu(false);
    }
  });

  const dirtyForms = document.querySelectorAll('[data-dirty-warning]');
  let hasDirtyForm = false;
  dirtyForms.forEach((form) => {
    form.addEventListener('input', () => { hasDirtyForm = true; });
    form.addEventListener('change', () => { hasDirtyForm = true; });
    form.addEventListener('submit', () => { hasDirtyForm = false; });
  });
  window.addEventListener('beforeunload', (event) => {
    if (!hasDirtyForm) return;
    event.preventDefault();
    event.returnValue = '';
  });

  const errorSummary = document.querySelector('[data-error-summary]');
  errorSummary?.focus();

  const historyAll = document.querySelector('[data-history-all]');
  const historyStores = Array.from(document.querySelectorAll('[data-history-store]'));
  historyAll?.addEventListener('change', () => {
    if (historyAll.checked) historyStores.forEach((option) => { option.checked = false; });
  });
  historyStores.forEach((option) => option.addEventListener('change', () => {
    if (option.checked && historyAll) historyAll.checked = false;
    if (!historyStores.some((candidate) => candidate.checked) && historyAll) historyAll.checked = true;
  }));

})();
