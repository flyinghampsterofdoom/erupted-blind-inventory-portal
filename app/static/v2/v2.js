(() => {
  const shell = document.querySelector('[data-v2-shell]');
  const openButton = document.querySelector('[data-drawer-open]');
  const closeButtons = document.querySelectorAll('[data-drawer-close]');

  const sidebar = document.getElementById('v2-navigation');
  const workspace = shell?.querySelector('.v2-workspace');
  const mobile = window.matchMedia(shell?.dataset.v2Primary === 'true' ? '(max-width: 999px)' : '(max-width: 760px)');
  let drawerOpen = false;
  const setDrawer = (open, restoreFocus = true) => {
    if (!shell || !sidebar) return;
    const wasOpen = drawerOpen;
    drawerOpen = Boolean(open && mobile.matches);
    shell.classList.toggle('is-drawer-open', drawerOpen);
    openButton?.setAttribute('aria-expanded', String(drawerOpen));
    document.body.classList.toggle('v2-no-scroll', drawerOpen);
    sidebar.inert = mobile.matches && !drawerOpen;
    if (workspace) workspace.inert = drawerOpen;
    if (mobile.matches) {
      sidebar.setAttribute('aria-hidden', String(!drawerOpen));
      if (drawerOpen) { sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true'); }
      else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
    } else { sidebar.removeAttribute('aria-hidden'); sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
    if (drawerOpen) sidebar.querySelector('[data-drawer-close]')?.focus();
    else if (wasOpen && restoreFocus) openButton?.focus();
  };
  openButton?.addEventListener('click', () => setDrawer(true));
  closeButtons.forEach((button) => button.addEventListener('click', () => setDrawer(false)));
  mobile.addEventListener('change', () => setDrawer(false, false));
  setDrawer(false, false);
  document.addEventListener('keydown', event => {
    if (!drawerOpen || event.key !== 'Tab') return;
    const targets = [...sidebar.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled)')]
      .filter(node => node.getClientRects().length && !node.closest('[hidden]'));
    const first = targets[0], last = targets[targets.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });

  const sidebarCollapse = document.querySelector('[data-sidebar-collapse]');
  const sidebarStorageKey = 'erupted-v2-sidebar-collapsed';
  const setSidebarCollapsed = (collapsed, persist = true) => {
    if (!shell) return;
    shell.classList.toggle('is-sidebar-collapsed', collapsed);
    sidebarCollapse?.setAttribute('aria-expanded', String(!collapsed));
    sidebarCollapse?.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
    sidebarCollapse?.setAttribute('title', collapsed ? 'Expand navigation' : 'Collapse navigation');
    if (sidebarCollapse) sidebarCollapse.textContent = collapsed ? '»' : '«';
    if (persist) {
      try { window.localStorage.setItem(sidebarStorageKey, String(collapsed)); } catch (_error) { /* State remains usable for this page. */ }
    }
  };
  let sidebarInitiallyCollapsed = false;
  try { sidebarInitiallyCollapsed = window.localStorage.getItem(sidebarStorageKey) === 'true'; } catch (_error) { /* Use expanded default. */ }
  setSidebarCollapsed(sidebarInitiallyCollapsed, false);
  sidebarCollapse?.addEventListener('click', () => setSidebarCollapsed(!shell?.classList.contains('is-sidebar-collapsed')));

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
      if (mobile.matches) setDrawer(false);
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
      if (openDialog.matches('[data-employee-action-dialog]')) return;
      event.preventDefault();
      openDialog.close('cancel');
      return;
    }
    if (event.key === 'Escape') {
      setDrawer(false);
      setScopeMenu(false);
    }
  });

  // Reveal invalid controls inside collapsed employee sections before browser focus.
  document.querySelector('.employee-editor')?.addEventListener('invalid', (event) => {
    let section = event.target.closest('details');
    while (section) {
      section.open = true;
      section = section.parentElement?.closest('details');
    }
  }, true);

  const dirtyForms = document.querySelectorAll('[data-dirty-warning]');
  const changedForms = new Set();
  dirtyForms.forEach((form) => {
    form.addEventListener('input', () => { changedForms.add(form); });
    form.addEventListener('change', () => { changedForms.add(form); });
    form.addEventListener('submit', () => { changedForms.delete(form); });
  });
  window.addEventListener('beforeunload', (event) => {
    if (!changedForms.size) return;
    event.preventDefault();
    event.returnValue = '';
  });

  const errorSummary = document.querySelector('[data-error-summary]');
  errorSummary?.focus();

  const squareData = document.querySelector('[data-square-data]');
  if (squareData?.dataset.squareState === 'updating') {
    let squarePollInFlight = false;
    const poll = async () => {
      if (document.hidden || squarePollInFlight) return;
      squarePollInFlight = true;
      try {
        const response = await window.fetch('/v2/square-data/status', {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
        });
        if (!response.ok) return;
        const status = await response.json();
        if (status.state !== 'updating') {
          window.clearInterval(pollSquareData);
          window.location.reload();
        }
      } catch (_error) {
        // The persisted state remains visible and a later page load retries.
      } finally {squarePollInFlight = false;}
    };
    const pollSquareData = window.setInterval(poll, 5000);
    document.addEventListener('visibilitychange', () => {if (!document.hidden) void poll();});
  }

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
