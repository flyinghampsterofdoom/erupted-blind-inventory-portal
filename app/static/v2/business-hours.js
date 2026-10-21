(() => {
  const form = document.querySelector('[data-business-hours-form]');
  if (!form) return;
  function update() {
    const closed = form.querySelector('[name="mode"]:checked')?.value === 'closed';
    form.querySelectorAll('[data-special-times] input').forEach(input => { input.disabled = closed; input.required = !closed; });
    const all = form.querySelector('[name="scope"]:checked')?.value === 'all';
    form.querySelectorAll('[data-store-selection] input').forEach(input => { input.disabled = all; });
  }
  form.addEventListener('change', update);
  update();
})();
