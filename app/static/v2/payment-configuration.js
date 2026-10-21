document.querySelector('#vendor-default-search')?.addEventListener('input', event => {
  const query = event.target.value.trim().toLocaleLowerCase();
  document.querySelectorAll('[data-vendor-default]').forEach(row => {row.hidden = !row.dataset.vendorDefault.includes(query);});
});
// A failed connection leaves the same form/request key available for a safe retry.
document.querySelectorAll('form[action="/v2/payment-methods/configuration"]').forEach(form => {
  form.addEventListener('submit', () => {form.querySelectorAll('button').forEach(button => {button.disabled = true;});});
});
