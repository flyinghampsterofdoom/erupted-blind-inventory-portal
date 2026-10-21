(function (root) {
  'use strict';

  const DRAFT_VERSION = 1;

  function draftKey(vendorId, version) {
    return `ordering-par-draft:${Number(vendorId)}:v${Number(version || DRAFT_VERSION)}`;
  }

  function normalizeNullableInteger(value) {
    if (value === null) return null;
    if (!Number.isInteger(value) || value < 0) throw new Error('Par values must be non-negative whole numbers or null.');
    return value;
  }

  function normalizeChange(change) {
    if (!change || typeof change !== 'object') throw new Error('Invalid par draft row.');
    if (!Number.isInteger(change.store_id) || change.store_id <= 0) throw new Error('Invalid store in par draft.');
    if (typeof change.sku !== 'string' || !change.sku.trim()) throw new Error('Invalid SKU in par draft.');
    return {
      store_id: change.store_id,
      sku: change.sku.trim(),
      manual_level: normalizeNullableInteger(change.manual_level),
      manual_par: normalizeNullableInteger(change.manual_par),
    };
  }

  function decodeDraft(raw, vendorId, version) {
    if (!raw) return null;
    let draft;
    try {
      draft = JSON.parse(raw);
    } catch (_error) {
      return null;
    }
    const expectedVersion = Number(version || DRAFT_VERSION);
    if (
      !draft ||
      draft.version !== expectedVersion ||
      draft.vendor_id !== Number(vendorId) ||
      !Array.isArray(draft.changes)
    ) {
      return null;
    }
    try {
      return {
        version: expectedVersion,
        vendor_id: Number(vendorId),
        changes: draft.changes.map(normalizeChange),
      };
    } catch (_error) {
      return null;
    }
  }

  function persistDraft(storage, key, draft) {
    try {
      storage.setItem(key, JSON.stringify(draft));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function settleDraft(storage, key, saveSucceeded) {
    if (!saveSucceeded) return false;
    try {
      storage.removeItem(key);
      return true;
    } catch (_error) {
      return false;
    }
  }

  root.OrderingParEditorDraft = {
    DRAFT_VERSION,
    draftKey,
    decodeDraft,
    normalizeChange,
    persistDraft,
    settleDraft,
  };

  if (!root.document) return;
  const form = root.document.getElementById('ordering-par-editor');
  if (!form) return;

  const vendorId = Number(form.dataset.vendorId);
  const version = Number(form.dataset.draftVersion || DRAFT_VERSION);
  const key = draftKey(vendorId, version);
  const status = root.document.getElementById('par-save-state');
  const inputs = Array.from(form.querySelectorAll('.par-editor-input'));
  const saveButtons = Array.from(form.querySelectorAll('[data-par-save-button]'));
  const rows = new Map();
  const baselines = new Map();
  const dirtyRows = new Map();
  let unmatchedDraftChanges = [];

  function valueFromText(raw) {
    const text = String(raw).trim();
    if (text === '') return null;
    const value = Number(text);
    return normalizeNullableInteger(value);
  }

  function valuesEqual(left, right) {
    return left === right;
  }

  function setState(kind, message) {
    status.className = `par-save-state is-${kind}`;
    status.textContent = message;
  }

  inputs.forEach((input) => {
    const rowKey = input.dataset.rowKey;
    if (!rows.has(rowKey)) rows.set(rowKey, {});
    rows.get(rowKey)[input.dataset.field] = input;
  });

  rows.forEach((row, rowKey) => {
    baselines.set(rowKey, {
      manual_level: valueFromText(row.manual_level.dataset.savedValue),
      manual_par: valueFromText(row.manual_par.dataset.savedValue),
    });
  });

  function currentChange(rowKey) {
    const row = rows.get(rowKey);
    return {
      store_id: Number(row.manual_level.dataset.storeId),
      sku: row.manual_level.dataset.sku,
      manual_level: valueFromText(row.manual_level.value),
      manual_par: valueFromText(row.manual_par.value),
    };
  }

  function recomputeRow(rowKey) {
    const current = currentChange(rowKey);
    const baseline = baselines.get(rowKey);
    if (
      valuesEqual(current.manual_level, baseline.manual_level) &&
      valuesEqual(current.manual_par, baseline.manual_par)
    ) {
      dirtyRows.delete(rowKey);
    } else {
      dirtyRows.set(rowKey, current);
    }
  }

  function draftPayload() {
    return {
      version,
      vendor_id: vendorId,
      changes: Array.from(dirtyRows.values()).concat(unmatchedDraftChanges),
    };
  }

  function preserveCurrentDraft() {
    if (!dirtyRows.size && !unmatchedDraftChanges.length) {
      return settleDraft(root.localStorage, key, true);
    }
    return persistDraft(root.localStorage, key, draftPayload());
  }

  inputs.forEach((input) => {
    input.addEventListener('input', () => {
      recomputeRow(input.dataset.rowKey);
      preserveCurrentDraft();
      if (dirtyRows.size) {
        setState('unsaved', `Unsaved changes (${dirtyRows.size} row${dirtyRows.size === 1 ? '' : 's'})`);
      } else if (unmatchedDraftChanges.length) {
        setState('recovered', `${unmatchedDraftChanges.length} recovered draft row(s) are not in the current grid and remain preserved.`);
      } else {
        setState('saved', 'Saved');
      }
    });
  });

  let restoredRows = 0;
  let storedDraft = null;
  try {
    storedDraft = decodeDraft(root.localStorage.getItem(key), vendorId, version);
  } catch (_error) {
    storedDraft = null;
  }
  if (storedDraft) {
    storedDraft.changes.forEach((change) => {
      const rowKey = `${change.store_id}|${change.sku}`;
      const row = rows.get(rowKey);
      if (!row) {
        unmatchedDraftChanges.push(change);
        return;
      }
      row.manual_level.value = change.manual_level === null ? '' : String(change.manual_level);
      row.manual_par.value = change.manual_par === null ? '' : String(change.manual_par);
      recomputeRow(rowKey);
      restoredRows += 1;
    });
    if (dirtyRows.size || unmatchedDraftChanges.length) {
      preserveCurrentDraft();
      setState(
        'recovered',
        `Unsaved changes recovered from this browser (${restoredRows} restored, ${unmatchedDraftChanges.length} preserved off-grid).`,
      );
    } else {
      settleDraft(root.localStorage, key, true);
    }
  }

  root.addEventListener('beforeunload', (event) => {
    if (!dirtyRows.size && !unmatchedDraftChanges.length) return;
    event.preventDefault();
    event.returnValue = '';
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    if (!dirtyRows.size) {
      if (unmatchedDraftChanges.length) {
        setState('recovered', `${unmatchedDraftChanges.length} recovered draft row(s) are not in the current grid and remain preserved.`);
      } else {
        setState('saved', 'Saved');
      }
      return;
    }

    const sentRows = new Map(dirtyRows);
    const changes = Array.from(sentRows.values());
    preserveCurrentDraft();
    saveButtons.forEach((button) => { button.disabled = true; });
    setState('saving', `Saving ${changes.length} row${changes.length === 1 ? '' : 's'}…`);

    try {
      const response = await root.fetch(form.dataset.saveUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': form.elements.csrf_token.value,
        },
        body: JSON.stringify({vendor_id: vendorId, changes}),
      });
      let result = null;
      try {
        result = await response.json();
      } catch (_error) {
        result = null;
      }
      if (!response.ok || !result || result.ok !== true) {
        const message = result && result.error && result.error.message
          ? result.error.message
          : (result && result.detail ? result.detail : `Request failed (${response.status}).`);
        throw new Error(message);
      }

      sentRows.forEach((sent, rowKey) => {
        baselines.set(rowKey, {
          manual_level: sent.manual_level,
          manual_par: sent.manual_par,
        });
        recomputeRow(rowKey);
      });
      settleDraft(root.localStorage, key, true);
      preserveCurrentDraft();
      if (dirtyRows.size || unmatchedDraftChanges.length) {
        setState('unsaved', `Saved ${changes.length} row(s); other recovered or newer changes remain unsaved.`);
      } else {
        setState('saved', `Saved ${changes.length} row(s).`);
      }
    } catch (error) {
      preserveCurrentDraft();
      setState('failed', `Save failed — your changes are still preserved locally. ${error.message}`);
    } finally {
      saveButtons.forEach((button) => { button.disabled = false; });
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
