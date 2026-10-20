(() => {
  // Coalesce overlapping checks only. Explicit checks before a save stay fresh.
  let pending = null;
  window.eruptedSessionStatus = () => {
    if (!pending) {
      pending = fetch('/session-status', {method:'GET',credentials:'same-origin',cache:'no-store',
        headers:{'X-Requested-With':'autosave'}}).finally(() => {pending=null;});
    }
    return pending.then(response => response.clone());
  };
})();
