(function () {
  'use strict';

  var root = document.querySelector('[data-display-player]');
  if (!root) return;

  var slides = [document.querySelector('[data-slide-a]'), document.querySelector('[data-slide-b]')];
  var fallback = document.querySelector('[data-fallback]');
  var status = document.querySelector('[data-status]');
  var playlist = null;
  var index = 0;
  var active = 0;
  var timer = null;
  var etag = '';
  var generation = 0;
  var cacheKey = 'erupted-display-playlist:' + root.getAttribute('data-display-name');

  function setFallback(visible) {
    fallback.hidden = !visible;
    fallback.style.display = visible ? 'grid' : 'none';
  }

  function preload(url, onload, onerror) {
    var image = new Image();
    image.onload = function () { onload(image); };
    image.onerror = onerror;
    image.src = url;
  }

  function showCurrent() {
    var currentGeneration = ++generation;
    clearTimeout(timer);
    if (!playlist || !playlist.items || !playlist.items.length) {
      setFallback(true);
      status.textContent = 'Waiting for assigned content';
      return;
    }

    var item = playlist.items[index % playlist.items.length];
    preload(item.media_url, function (loaded) {
      if (currentGeneration !== generation) return;
      var next = active === 0 ? 1 : 0;
      slides[next].src = loaded.src;
      slides[next].alt = '';
      slides[next].className = 'display-slide is-visible';
      slides[active].className = 'display-slide';
      active = next;
      setFallback(false);
      status.textContent = '';

      var following = playlist.items[(index + 1) % playlist.items.length];
      if (following && following.media_url !== item.media_url) {
        preload(following.media_url, function () {}, function () {});
      }
      if (!item.permanent) {
        timer = setTimeout(function () {
          index = (index + 1) % playlist.items.length;
          showCurrent();
        }, item.duration_seconds * 1000);
      }
    }, function () {
      status.textContent = 'Content temporarily unavailable';
      timer = setTimeout(showCurrent, 15000);
    });
  }

  function handleRefreshFailure() {
    status.textContent = playlist && playlist.items && playlist.items.length
      ? 'Offline · showing saved rotation'
      : 'Connecting…';
  }

  function refresh() {
    var request = new XMLHttpRequest();
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      setTimeout(refresh, 300000);
    }

    request.open('GET', '/display/api/playlist', true);
    if (etag) request.setRequestHeader('If-None-Match', etag);
    request.onreadystatechange = function () {
      if (request.readyState !== 4) return;
      if (request.status === 401 || request.status === 403) {
        window.location.reload();
        finish();
        return;
      }
      if (request.status === 304) {
        finish();
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        handleRefreshFailure();
        finish();
        return;
      }
      try {
        var next = JSON.parse(request.responseText);
        etag = request.getResponseHeader('ETag') || '';
        if (!playlist || next.playlist_version !== playlist.playlist_version) {
          playlist = next;
          index = 0;
          try { window.localStorage.setItem(cacheKey, JSON.stringify(next)); } catch (storageError) {}
          showCurrent();
        }
      } catch (parseError) {
        handleRefreshFailure();
      }
      finish();
    };
    request.onerror = function () {
      handleRefreshFailure();
      finish();
    };
    request.send();
  }

  try {
    var saved = JSON.parse(window.localStorage.getItem(cacheKey));
    if (saved && saved.items) {
      playlist = saved;
      showCurrent();
    }
  } catch (storageError) {
    try { window.localStorage.removeItem(cacheKey); } catch (removeError) {}
  }
  refresh();
}());
