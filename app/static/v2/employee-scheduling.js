(() => {
  const form = document.querySelector('.compact-scheduling__form');
  if (!form) return;
  const shifts = form.elements.shifts_per_week;
  const date = form.querySelector('[data-allocation-date]');
  const updateDate = () => {
    const changed = shifts.dataset.newProfile !== 'true' && shifts.value !== shifts.dataset.currentAllocation;
    date.hidden = !changed;
    date.querySelector('input').required = changed;
  };
  shifts.addEventListener('input', updateDate);
  updateDate();
  document.querySelectorAll('[form="employee-settings"]').forEach(field => {
    field.addEventListener('input', () => form.dispatchEvent(new Event('input', {bubbles:true})));
    field.addEventListener('change', () => form.dispatchEvent(new Event('change', {bubbles:true})));
  });
})();
