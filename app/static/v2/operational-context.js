/* Keep unfinished form values visible when a server rejects stale store scope. */
(() => {
  document.querySelectorAll('form input[name="working_context"]').forEach(input => {
    const form = input.form;
    // Blind Inventory owns its own draft/revision controller.
    if (!form || form.dataset.blindCountForm !== undefined || form.hasAttribute('data-count-form')) return;
    if (!form.action.includes('/store/') || form.method.toLowerCase() !== 'post') return;
    let dirty = false;
    form.addEventListener('input', () => { dirty = true; });
    window.addEventListener('beforeunload', event => {
      if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
    form.addEventListener('submit', async event => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      const button = event.submitter;
      const action = button?.getAttribute('formaction') || form.action;
      const data = new FormData(form);
      if (button?.name) data.set(button.name, button.value);
      let feedback = form.querySelector('[data-context-feedback]');
      if (!feedback) {
        feedback = document.createElement('p'); feedback.dataset.contextFeedback = '';
        feedback.setAttribute('role', 'alert'); form.append(feedback);
      }
      try {
        const response = await fetch(action, {method: 'POST', body: data, credentials: 'same-origin'});
        if (response.status === 409) {
          feedback.textContent = 'Your working assignment changed. These entries have not been submitted and remain on this page. Saved work stays at its original store. ';
          const link = document.createElement('a'); link.href = window.location.pathname;
          link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Open current store tasks in a new tab';
          // Use the task entry route, never an obsolete record-specific URL.
          link.href = action.includes('/daily-chore-sheet/') ? '/store/daily-chore-sheet' : '/store/home';
          feedback.append(link); return;
        }
        if (!response.ok || !response.redirected || new URL(response.url).pathname === '/login') {
          feedback.textContent = 'Could not submit. Your entries remain on this page; check your session and retry.'; return;
        }
        dirty = false; window.location.assign(response.url);
      } catch (_) { feedback.textContent = 'Connection failed. Your entries remain on this page.'; }
    });
  });
})();
