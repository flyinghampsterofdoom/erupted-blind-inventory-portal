(() => {
  const page = document.querySelector('[data-coverage-page]');
  if (!page) return;
  page.addEventListener('click', (event) => {
    const button = event.target.closest('[data-check-group]');
    if (!button) return;
    const container = page.querySelector(`[data-check-container="${button.dataset.checkGroup}"]`);
    if (!container) return;
    const requested = button.dataset.checkValues;
    const values = new Set(requested === 'all' || requested === 'none' ? [] : requested.split(','));
    container.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.checked = requested === 'all' || (requested !== 'none' && values.has(input.value));
    });
  });
})();
