(() => {
  const form = document.querySelector('[data-supplier-recovery]');
  if (!form) return;
  const all = form.querySelector('[data-select-all]');
  const choices = [...form.querySelectorAll('input[name="selection"]')];
  all.addEventListener('change', () => {
    for (const choice of choices) choice.checked = all.checked;
  });
  for (const choice of choices) choice.addEventListener('change', () => {
    all.checked = choices.every(input => input.checked);
    all.indeterminate = choices.some(input => input.checked) && !all.checked;
  });
})();
