(() => {
  'use strict';
  const el = document.getElementById('purchasing-attention-indicator');
  if (!el || location.pathname === '/management/purchasing/attention') return;
  fetch('/management/purchasing/attention/summary', {credentials:'same-origin',cache:'no-store'})
    .then(r => {if (!r.ok) throw Error();return r.json();})
    .then(v => {
      if (v.state === 'unavailable') {el.textContent='Needs Attention · Open to check';return;}
      const label=v.sku_count ? `${v.sku_count} SKUs need attention` : 'No issues in last check';
      el.textContent=`${label} · ${v.state === 'stale' ? 'Stale' : 'Cached check'}`;
      el.title=`Observed ${v.observed_at}. Open Needs Attention for current evidence.`;
    }).catch(() => {el.textContent='Needs Attention · Check unavailable';});
})();
