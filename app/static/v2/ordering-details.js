/* Read-only, lazy detail retrieval; never loads the full result dataset. */
document.querySelectorAll('details.ordering-explanation').forEach(details => {
  details.addEventListener('toggle', async () => {
    const target = details.querySelector('[data-recommendation-detail]');
    if (!details.open || !target || target.dataset.loaded || target.dataset.loading) return;
    target.dataset.loading = '1';
    target.textContent = 'Loading calculation details…';
    try {
      const response = await fetch(target.dataset.recommendationDetail, {credentials: 'same-origin', redirect: 'error'});
      if (!response.ok) throw new Error('Request failed');
      target.innerHTML = await response.text();
      target.dataset.loaded = '1';
    } catch (_) {
      target.textContent = 'Details unavailable. Close and reopen to retry; your purchasing data has not changed.';
    } finally { delete target.dataset.loading; }
  });
});
