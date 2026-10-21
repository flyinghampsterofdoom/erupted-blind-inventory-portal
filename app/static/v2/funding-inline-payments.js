document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-inline-payment]').forEach((form) => {
    const checkbox = form.querySelector('[data-paid-in-full]');
    const amount = form.querySelector('input[name="amount"]');
    if (!checkbox || !amount) return;
    let enteredAmount = amount.value;
    const sync = () => {
      if (checkbox.checked) {
        enteredAmount = amount.value;
        amount.value = form.dataset.remaining;
        amount.readOnly = true;
      } else {
        amount.readOnly = false;
        amount.value = enteredAmount;
      }
    };
    checkbox.addEventListener('change', sync);
    sync();
  });
});
