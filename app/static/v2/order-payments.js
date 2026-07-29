(() => {
  const accountTypes = new Set(['WIRE', 'CREDIT_CARD', 'DEBIT_CARD']);
  document.querySelectorAll('[data-payment-method-form]').forEach((form) => {
    const category = form.querySelector('[data-payment-category]');
    if (!category) return;
    const refresh = () => {
      form.querySelectorAll('[data-payment-field]').forEach((field) => {
        const kind = field.dataset.paymentField;
        const visible = kind === 'terms' ? category.value === 'TERMS' : accountTypes.has(category.value);
        field.hidden = !visible;
        field.querySelectorAll('input, select, textarea').forEach((input) => { input.disabled = !visible; });
      });
    };
    category.addEventListener('change', refresh);
    refresh();
  });
  document.querySelectorAll('[data-adjustment-form]').forEach((form) => {
    const direction = form.querySelector('[data-adjustment-direction]');
    const type = form.querySelector('[data-adjustment-type]');
    if (!direction || !type) return;
    const refresh = () => {
      let firstVisible = null;
      Array.from(type.options).forEach((option) => {
        option.hidden = option.dataset.direction !== direction.value;
        option.disabled = option.hidden;
        if (!option.hidden && !firstVisible) firstVisible = option;
      });
      if (type.selectedOptions[0]?.disabled && firstVisible) type.value = firstVisible.value;
    };
    direction.addEventListener('change', refresh);
    refresh();
  });
})();
