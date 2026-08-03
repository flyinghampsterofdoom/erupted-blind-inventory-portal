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
  document.querySelectorAll('[data-financial-assignment-form]').forEach((form) => {
    const orders = Array.from(form.querySelectorAll('[data-assignment-order]'));
    const vendor = form.querySelector('[data-financial-vendor-select]');
    const count = form.querySelector('[data-selected-order-count]');
    const original = form.querySelector('[data-original-vendor-summary]');
    const method = form.querySelector('[data-payment-method-summary]');
    const status = form.querySelector('[data-assignment-status-summary]');
    const settlement = form.querySelector('[data-consequence-settlement]');
    const review = form.querySelector('[data-review-assignment]');
    if (!vendor || !review) return;
    const summary = (selected, key, empty, multiple) => {
      const values = [...new Set(selected.map((row) => row.dataset[key]).filter(Boolean))];
      return values.length === 0 ? empty : values.length === 1 ? values[0] : multiple;
    };
    const refresh = () => {
      const selected = orders.filter((order) => order.checked);
      const selectedOption = vendor.selectedOptions[0];
      const vendorName = selectedOption?.dataset.vendorName || '';
      if (count) count.textContent = `${selected.length} selected`;
      if (original) original.textContent = summary(selected, 'originalVendor', 'Select an order', 'Multiple original vendors');
      if (method) method.textContent = summary(selected, 'paymentMethod', 'Select an order', 'Multiple methods');
      if (status) status.textContent = summary(selected, 'status', 'Select an order', 'Multiple statuses');
      if (settlement) settlement.textContent = vendorName || 'Choose a financial vendor';
      const currentIds = [...new Set(selected.map((row) => row.dataset.financialVendorId))];
      const unchanged = selected.length > 0 && currentIds.length === 1 && currentIds[0] === vendor.value;
      review.disabled = selected.length === 0 || !vendor.value || unchanged;
      review.textContent = unchanged ? 'Choose a Different Financial Vendor' : 'Review Changes';
    };
    orders.forEach((order) => order.addEventListener('change', refresh));
    vendor.addEventListener('change', refresh);
    refresh();
  });
  document.querySelectorAll('[data-bulk-assignment-form]').forEach((form) => {
    const orders = Array.from(document.querySelectorAll(`[data-bulk-order][form="${form.id}"]`));
    const count = form.closest('.v2-queue-bulk')?.querySelector('[data-bulk-selected-count]');
    const apply = form.querySelector('[data-apply-selected]');
    const refresh = () => {
      const selected = orders.filter((order) => order.checked).length;
      if (count) count.textContent = `${selected} selected`;
      if (apply) {
        apply.disabled = selected === 0;
        apply.textContent = selected === 0
          ? 'Apply to Selected'
          : `Apply to ${selected} Selected Order${selected === 1 ? '' : 's'}`;
      }
    };
    orders.forEach((order) => order.addEventListener('change', refresh));
    refresh();
  });
})();
