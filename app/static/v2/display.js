(() => {
  const root = document.querySelector('[data-display-player]'); if (!root) return;
  const slides = [document.querySelector('[data-slide-a]'), document.querySelector('[data-slide-b]')];
  const fallback = document.querySelector('[data-fallback]'); const status = document.querySelector('[data-status]');
  let playlist = null; let index = 0; let active = 0; let timer = null; let etag = ''; let generation = 0;
  const cacheKey = `erupted-display-playlist:${root.dataset.displayName}`;
  const preload = (url) => new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = url; });
  async function showCurrent() {
    const currentGeneration = ++generation; clearTimeout(timer);
    if (!playlist?.items?.length) { fallback.hidden = false; status.textContent = 'Waiting for assigned content'; return; }
    const item = playlist.items[index % playlist.items.length];
    try {
      const loaded = await preload(item.media_url); if (currentGeneration !== generation) return;
      const next = active === 0 ? 1 : 0; slides[next].src = loaded.src; slides[next].alt = '';
      requestAnimationFrame(() => { slides[next].classList.add('is-visible'); slides[active].classList.remove('is-visible'); active = next; fallback.hidden = true; status.textContent = ''; });
      const following = playlist.items[(index + 1) % playlist.items.length]; if (following && following.media_url !== item.media_url) preload(following.media_url).catch(() => {});
      if (!item.permanent) timer = setTimeout(() => { index = (index + 1) % playlist.items.length; showCurrent(); }, item.duration_seconds * 1000);
    } catch { status.textContent = 'Content temporarily unavailable'; timer = setTimeout(showCurrent, 15000); }
  }
  async function refresh() {
    try {
      const response = await fetch('/display/api/playlist', { headers: etag ? { 'If-None-Match': etag } : {}, credentials: 'same-origin' });
      if (response.status === 401 || response.status === 403) { window.location.reload(); return; }
      if (response.status !== 304) {
        if (!response.ok) throw new Error('playlist'); const next = await response.json(); etag = response.headers.get('ETag') || '';
        if (!playlist || next.playlist_version !== playlist.playlist_version) { playlist = next; index = 0; localStorage.setItem(cacheKey, JSON.stringify(next)); showCurrent(); }
      }
    } catch { status.textContent = playlist?.items?.length ? 'Offline · showing saved rotation' : 'Connecting…'; }
    setTimeout(refresh, 300000);
  }
  try { const saved = JSON.parse(localStorage.getItem(cacheKey)); if (saved?.items) { playlist = saved; showCurrent(); } } catch { localStorage.removeItem(cacheKey); }
  refresh();
})();
