(() => {
  const form = document.getElementById('catalog-add');
  if (!form) return;
  const query = document.getElementById('catalog-query'), results = document.getElementById('catalog-results');
  const identity = document.getElementById('catalog-variation'), cost = document.getElementById('catalog-cost');
  const selected = document.getElementById('catalog-selected'), add = document.getElementById('catalog-add-button');
  let sequence = 0, timer;
  query.addEventListener('input', () => {
    const current = ++sequence;
    clearTimeout(timer); identity.value = ''; add.disabled = true; cost.value = '';
    selected.textContent = 'Select a product from the results.'; results.replaceChildren();
    if (!query.value.trim()) return;
    timer = setTimeout(async () => {
      try {
        const response = await fetch(`${form.dataset.searchUrl}?q=${encodeURIComponent(query.value)}`);
        if (!response.ok || response.redirected) throw new Error();
        const payload = await response.json();
        if (current !== sequence) return;
        results.replaceChildren();
        for (const product of payload.products) {
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = `${product.name}${product.sku ? ' · ' + product.sku : ''}`;
          button.addEventListener('click', () => {
            identity.value = product.variation_id; cost.value = product.unit_cost ?? '';
            selected.textContent = product.name; add.disabled = false; results.replaceChildren(); cost.focus();
          });
          results.append(button, document.createElement('br'));
        }
        if (!payload.products.length) results.textContent = 'No matching products.';
      } catch (_) { if (current === sequence) results.textContent = 'Search unavailable. Try again; no product selected.'; }
    }, 180);
  });
})();
